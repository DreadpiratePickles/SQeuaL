"""Ask a model what the SQL says, then have a judge compare that to the question.

The two-step exists because of what a one-step cannot do. The obvious design —
show one model the question and the SQL and ask "does this answer that, yes or
no" — fails because a model shown both will read the question and agree with
itself, and it fails *silently*, which is worse than failing.

So the explanation is produced **blind**: the statement and the schema go in, the
question does not, and `tests/test_verify_backtranslation.py` asserts the absence
word by word. Then project 1's criterion judge grades that explanation against
the question, against two criteria written in code that point in opposite
directions — one asks whether the statement does what was asked, the other
whether it does something that was not.

The back-translation is also a hallucination check in its own right, and that is
the part worth noticing. A model that has to describe its own SQL in English will
describe what it actually wrote, and the gap between that and what it meant to
write is the thing this stage is looking for.

**An error is not a fail.** A judge whose reply could not be parsed, or that
could not be reached, has said nothing. Recording that as agreement would let a
broken judge raise every confidence score in the system; recording it as
disagreement would let one lower every score. It contributes neither.
"""

import json
from dataclasses import dataclass

from regression_detect.judge.criterion import JudgeError, judge_criterion

from ..generate.parse import strip_one_fence
from ..providers import Completion, MeteredProvider, ProviderError, TextProviderView
from ..providers.pacing import Pacer
from .prompt import (
    CRITERION_NAMES,
    build_criteria,
    build_explain_user_message,
    load_explain_prompt,
)

EXPLANATION_KEYS = frozenset({"explanation"})

PASSED, FAILED, ERRORED = "pass", "fail", "error"


class ExplanationParseError(Exception):
    """The back-translation reply is not a valid explanation and must not be used."""


@dataclass(frozen=True)
class JudgeVerdict:
    """One criterion's outcome. `status` is `pass`, `fail` or `error`."""

    name: str
    status: str
    reason: str


@dataclass(frozen=True)
class BackTranslation:
    """What the model said the statement does, and how that was graded."""

    explanation: str | None
    error: str | None
    verdicts: tuple[JudgeVerdict, ...]
    completions: tuple[Completion, ...]
    explain_model_id: str
    judge_model_id: str

    @property
    def input_tokens(self) -> int:
        return sum(completion.input_tokens for completion in self.completions)

    @property
    def output_tokens(self) -> int:
        return sum(completion.output_tokens for completion in self.completions)


def parse_explanation(raw: object) -> str:
    """Parse the back-translation reply into one explanation.

    Raises:
        ExplanationParseError: on a non-string reply, non-JSON text, a non-object,
            a missing or unexpected key, or a blank explanation.
    """
    if not isinstance(raw, str):
        raise ExplanationParseError(f"model reply must be a string, got {type(raw).__name__}")
    candidate = strip_one_fence(raw.strip()).strip()
    if not candidate:
        raise ExplanationParseError("model returned an empty reply")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ExplanationParseError(
            f"model reply is not JSON: {exc.msg} (at position {exc.pos})"
        ) from exc
    if not isinstance(payload, dict):
        raise ExplanationParseError(
            f"model reply must be a JSON object, got {type(payload).__name__}"
        )
    keys = set(payload)
    if keys != EXPLANATION_KEYS:
        raise ExplanationParseError(
            "model reply must have exactly the key 'explanation' "
            f"(missing: {sorted(EXPLANATION_KEYS - keys) or 'none'}; "
            f"unexpected: {sorted(keys - EXPLANATION_KEYS) or 'none'})"
        )
    explanation = payload["explanation"]
    if not isinstance(explanation, str) or not explanation.strip():
        raise ExplanationParseError("'explanation' must be a non-empty string")
    return explanation.strip()


def _unavailable(reason: str) -> tuple[JudgeVerdict, ...]:
    """Both criteria recorded as errored. Never defaulted to pass."""
    return tuple(
        JudgeVerdict(name=name, status=ERRORED, reason=reason) for name in CRITERION_NAMES
    )


def back_translate(
    *,
    sql: str,
    schema_markdown: str,
    question: str,
    provider: MeteredProvider,
    pacer: Pacer,
    judge_provider: MeteredProvider | None = None,
) -> BackTranslation:
    """Explain the statement blind, then judge the explanation against the question.

    Args:
        sql: `GuardReport.normalised_sql` — the statement that actually ran, not
            the one the model first wrote.
        schema_markdown: the sliced card, so the explainer can name what a column
            means.
        question: the question. Reaches the **judge** and never the explainer.
        provider: the metered seam for the explain call.
        pacer: spaces the three calls under a per-minute quota.
        judge_provider: a different family for the judge, when a second key
            exists. Defaults to `provider`, which is the self-preference problem
            recorded on every run rather than hidden.
    """
    judge = judge_provider or provider
    completions: list[Completion] = []

    pacer.wait()
    try:
        completion = provider.complete(
            system=load_explain_prompt(),
            user=build_explain_user_message(sql=sql, schema_markdown=schema_markdown),
            temperature=0.0,
        )
        completions.append(completion)
        explanation = parse_explanation(completion.text)
    except (ExplanationParseError, ProviderError) as exc:
        return BackTranslation(
            explanation=None,
            error=str(exc),
            verdicts=_unavailable(f"no explanation to grade: {exc}"),
            completions=tuple(completions),
            explain_model_id=provider.model_id,
            judge_model_id=judge.model_id,
        )

    verdicts = []
    for name, criterion in build_criteria(question):
        view = TextProviderView(judge, pacer=pacer)
        try:
            verdict = judge_criterion(
                # Project 1's judge names its three slots `ticket`, `summary` and
                # `criterion`. The question is the material being graded against,
                # the explanation is the candidate, and the criterion carries the
                # actual instruction — so the mapping is exact even though the
                # slot names come from a summariser.
                ticket=question,
                summary=explanation,
                criterion=criterion,
                provider=view,
                temperature=0.0,
            )
        except (JudgeError, ProviderError) as exc:
            verdicts.append(JudgeVerdict(name=name, status=ERRORED, reason=str(exc)))
        else:
            verdicts.append(
                JudgeVerdict(
                    name=name,
                    status=PASSED if verdict.passed else FAILED,
                    reason=verdict.reason,
                )
            )
        if view.last_completion is not None:
            completions.append(view.last_completion)

    return BackTranslation(
        explanation=explanation,
        error=None,
        verdicts=tuple(verdicts),
        completions=tuple(completions),
        explain_model_id=provider.model_id,
        judge_model_id=judge.model_id,
    )

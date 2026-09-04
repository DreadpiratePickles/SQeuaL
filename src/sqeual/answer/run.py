"""Stage 07: assemble the document a person reads, from rows and from evidence.

Every figure in what comes out of here was formatted by `format.py` from a
`ResultSet` cell. Nothing in this module takes a numeral from a model reply into
the answer: the optional phrasing goes through the grounding check first, and a
sentence with an invented number in it is discarded whole.

**Abstention is a first-class outcome and gets its own document.** Below
`[confidence] abstain_threshold`, or when the model asked for clarification, the
answer is the question that needs answering plus the statement the tool was about
to run — and no figures at all. That is not a degraded answer, it is the correct
one: a hedged number is repeated without its hedge in the first email that quotes
it.
"""

from dataclasses import dataclass
from enum import StrEnum

from ..generate.run import GenerationOutcome, GenerationStatus
from ..providers import Completion, MeteredProvider
from ..providers.pacing import Pacer
from ..verify.checks import Check, CheckStatus
from ..verify.run import Verification
from .confidence import (
    Confidence,
    ConfidenceLevel,
    Factors,
    agreement_value,
    compute_confidence,
    judge_value,
)
from .format import RenderedCell, render_cells
from .phrase import Phrasing, write_phrasing
from .render import (
    render_assumptions,
    render_checks,
    render_confidence,
    render_result,
    render_sql,
)


class AnswerStatus(StrEnum):
    """What the user is handed, and which exit code says so."""

    ANSWERED = "answered"
    ABSTAINED = "abstained"
    CLARIFICATION = "clarification"
    GUARD_BLOCKED = "guard_blocked"
    UNUSABLE_REPLY = "unusable_reply"
    EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class Answer:
    """The rendered document and everything it was assembled from."""

    status: AnswerStatus
    markdown: str
    confidence: Confidence | None
    cells: tuple[RenderedCell, ...]
    phrasing: Phrasing | None

    @property
    def shows_figures(self) -> bool:
        return self.status is AnswerStatus.ANSWERED

    @property
    def phrasing_completion(self) -> Completion | None:
        return self.phrasing.completion if self.phrasing else None


def _heading(question: str) -> list[str]:
    return [f"# {question}", ""]


def _refusal_markdown(
    generation: GenerationOutcome,
    *,
    title: str,
    body: list[str],
    confidence: Confidence | None = None,
    checks: tuple[Check, ...] = (),
    same_family: bool = False,
) -> str:
    lines = _heading(generation.question) + [f"## {title}", "", *body, ""]
    if confidence is not None:
        lines += ["## Confidence", "", *render_confidence(confidence, same_family=same_family), ""]
    if checks:
        lines += ["## Checks", "", *render_checks(checks), ""]
    primary = generation.primary
    if primary is not None and primary.report is not None:
        lines += [
            "## The statement this was about to run",
            "",
            "No figures are shown. Read the statement and run it yourself if you want them.",
            "",
            *render_sql(primary.report.normalised_sql),
            "",
        ]
    elif generation.blocked_codes:
        lines += [
            "## What the guard found",
            "",
            *[f"- `{code}`" for code in generation.blocked_codes],
            "",
            "Nothing ran. A refused statement has no normalised form, so there is "
            "nothing here for anybody to execute by hand.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _clarification_answer(generation: GenerationOutcome) -> Answer:
    return Answer(
        status=AnswerStatus.CLARIFICATION,
        markdown=_refusal_markdown(
            generation,
            title="I need one more thing before I can answer this",
            body=[generation.clarification or "The question could not be pinned down."],
        ),
        confidence=None,
        cells=(),
        phrasing=None,
    )


def _blocked_answer(generation: GenerationOutcome) -> Answer:
    return Answer(
        status=AnswerStatus.GUARD_BLOCKED,
        markdown=_refusal_markdown(
            generation,
            title="The proposed statement was refused before it ran",
            body=[
                "The guard rejected every attempt, including the repair. Nothing "
                "touched the database, so there is no partial answer and no number "
                "to report.",
                "",
                f"Attempts: {len(generation.attempts)}. "
                f"Repairs used: {generation.repairs_used}.",
            ],
        ),
        confidence=None,
        cells=(),
        phrasing=None,
    )


def _unusable_answer(generation: GenerationOutcome) -> Answer:
    return Answer(
        status=AnswerStatus.UNUSABLE_REPLY,
        markdown=_refusal_markdown(
            generation,
            title="The model's reply could not be read",
            body=[
                "The reply was not the JSON object this tool requires, so there was "
                "no statement to check and nothing ran. It was not repaired in "
                "place: pulling SQL out of prose means something chose which "
                "statement to run, and nobody would know which.",
                "",
                f"`{generation.detail}`",
            ],
        ),
        confidence=None,
        cells=(),
        phrasing=None,
    )


def _failed_answer(generation: GenerationOutcome) -> Answer:
    return Answer(
        status=AnswerStatus.EXECUTION_FAILED,
        markdown=_refusal_markdown(
            generation,
            title="The statement passed every check and the database could not run it",
            body=[
                "This is a different fact from a bad question: re-writing the query "
                "would produce the same failure.",
                "",
                f"`{generation.detail}`",
            ],
        ),
        confidence=None,
        cells=(),
        phrasing=None,
    )


REFUSALS = {
    GenerationStatus.CLARIFICATION: _clarification_answer,
    GenerationStatus.GUARD_BLOCKED: _blocked_answer,
    GenerationStatus.PARSE_FAILED: _unusable_answer,
    GenerationStatus.EXECUTION_FAILED: _failed_answer,
}


def confidence_for(generation: GenerationOutcome, verification: Verification, settings):
    """Assemble the four measured factors and score them."""
    return compute_confidence(
        Factors(
            intent=verification.intent_fraction,
            judge=judge_value(verification.back_translation.verdicts),
            agreement=agreement_value(agreement=generation.agreement, k=generation.k),
            sanity=verification.sanity_fraction,
            repaired=generation.repairs_used > 0,
        ),
        settings,
    )


def _flags(checks: tuple[Check, ...]) -> list[str]:
    return [
        f"- **{check.name}**: {check.evidence}"
        for check in checks
        if check.status is CheckStatus.FLAG
    ]


def build_answer(
    *,
    generation: GenerationOutcome,
    verification: Verification | None,
    config,
    provider: MeteredProvider | None = None,
    pacer: Pacer | None = None,
) -> Answer:
    """Render the document for one question.

    Args:
        generation: the stage 05 outcome.
        verification: the stage 06 outcome, or `None` when nothing was verified
            because nothing ran.
        config: the validated `sqeual.toml`.
        provider: the metered seam, only used when `[answer] llm_phrasing` is on.
        pacer: spaces the phrasing call.
    """
    if generation.status is not GenerationStatus.ANSWERED or verification is None:
        return REFUSALS[generation.status](generation)

    primary = generation.primary
    result = primary.result
    settings = config.answer
    float_places = config.verify.float_places
    confidence = confidence_for(generation, verification, config.confidence)

    cells = render_cells(
        columns=result.columns,
        rows=result.rows,
        settings=settings,
        float_places=float_places,
    )
    result_lines = render_result(
        result=result, as_of=generation.as_of, settings=settings, float_places=float_places
    )

    if confidence.level is ConfidenceLevel.ABSTAIN:
        return Answer(
            status=AnswerStatus.ABSTAINED,
            markdown=_refusal_markdown(
                generation,
                title="I am not confident enough in this answer to show it",
                body=[
                    "The statement ran, and what it returned is deliberately not "
                    "shown. The checks below say why the confidence is low. Refusing "
                    "is a feature: a number with a hedge attached gets quoted "
                    "without the hedge.",
                    "",
                    "**What the statement says it does**, back-translated by a model "
                    "that was not shown the question:",
                    "",
                    f"> {verification.back_translation.explanation or '(unavailable)'}",
                ],
                confidence=confidence,
                checks=verification.checks,
                same_family=verification.same_family,
            ),
            confidence=confidence,
            cells=(),
            phrasing=None,
        )

    phrasing = None
    if settings.llm_phrasing and provider is not None:
        phrasing = write_phrasing(
            question=generation.question,
            rendered_result="\n".join(result_lines),
            cells=cells,
            row_count=result.row_count,
            provider=provider,
            pacer=pacer or Pacer(0),
            grounded_dates=_grounded_dates(generation),
        )

    lines = _heading(generation.question) + ["## Answer", ""]
    if phrasing is not None and phrasing.accepted:
        lines += [phrasing.sentence, ""]
    lines += [*result_lines, ""]

    flags = _flags(verification.checks)
    if flags:
        lines += ["### Worth knowing", "", *flags, ""]
    if phrasing is not None and not phrasing.accepted:
        lines += [f"_{phrasing.note}; the rendering above is the code's._", ""]

    lines += [
        "## Confidence",
        "",
        *render_confidence(confidence, same_family=verification.same_family),
        "",
        "## Checks",
        "",
        *render_checks(verification.checks),
        "",
        "## What the statement says it does",
        "",
        "Back-translated by a model that was not shown the question, then graded "
        "against it. That blindness is the point: a verifier shown the question "
        "paraphrases the question instead of reading the SQL.",
        "",
        f"> {verification.back_translation.explanation or '(unavailable)'}",
        "",
        *[
            f"- `{verdict.name}` **{verdict.status}** — {verdict.reason}"
            for verdict in verification.back_translation.verdicts
        ],
        "",
        "## Assumptions",
        "",
        *render_assumptions(primary.proposal.assumptions),
        "",
        "## The query that ran",
        "",
        *render_sql(primary.report.normalised_sql),
        "",
        f"_{result.row_count} row(s) in {result.elapsed_ms} ms, against schema "
        f"`{generation.schema_sha256[:12]}`. Every figure above was formatted by "
        "code from a result cell._",
    ]
    return Answer(
        status=AnswerStatus.ANSWERED,
        markdown="\n".join(lines).rstrip() + "\n",
        confidence=confidence,
        cells=cells,
        phrasing=phrasing,
    )


def _grounded_dates(generation: GenerationOutcome) -> tuple[str, ...]:
    """Figures this program computed, which a sentence may quote.

    Narrow on purpose: the endpoints of the window resolved from `as_of`, and
    `as_of` itself. A sentence saying "between 2026-07-01 and 2026-07-31" is
    quoting arithmetic, not inventing a number.
    """
    window = generation.time_window
    if window is None:
        return (generation.as_of,)
    return (generation.as_of, window.start, window.end)


__all__ = ["Answer", "AnswerStatus", "build_answer", "confidence_for"]

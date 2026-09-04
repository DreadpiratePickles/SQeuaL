"""Write what happened: `runs/<ts>/trace.json` and `runs/<ts>/answer.md`.

An answer without the query that produced it cannot be checked by the person it
was given to, and a *refusal* without its working cannot be argued with at all.
So every `ask` writes both files, including the runs that refused — especially
those, because a refusal nobody can read is indistinguishable from a bug.

Every attempt is recorded, surviving or not, with the model's reply verbatim. A
discarded candidate is the most informative thing in a failed run: it says what
the model actually wrote, which is the difference between "the prompt is wrong"
and "the schema moved".

Cost is in **micro-USD**, a millionth of a dollar, because token prices are
quoted at four or five significant figures and cents are far too coarse for one
call. The `priced` flag travels beside it: `[cost]` defaults to zero, which means
nobody has entered a tariff, and a reader who took that 0 for a bill of nothing
would be reading a number this repository never claimed.

`runs/` is gitignored. It holds a question somebody asked and the rows that came
back, which is exactly the material a repository should not carry.
"""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from regression_detect.judge.criterion import judge_prompt_sha256

from .answer.phrase import Phrasing
from .answer.run import Answer
from .generate.prompt import generate_prompt_sha256
from .generate.run import Attempt, GenerationOutcome
from .providers import Completion, completion_cost_micro_usd
from .verify.prompt import explain_prompt_sha256
from .verify.run import Verification

TRACE_NAME = "trace.json"
ANSWER_NAME = "answer.md"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
CURRENCY = "USD"
"""Cost is USD, recorded on every trace so a reader need not assume."""


@dataclass(frozen=True)
class CostSummary:
    """What one question consumed, and whether anybody has priced it."""

    input_tokens: int
    output_tokens: int
    micro_usd: int
    priced: bool
    calls: int

    def as_json(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "micro_usd": self.micro_usd,
            "currency": CURRENCY,
            # False means "[cost] holds no tariff", never "this run was free".
            "priced": self.priced,
        }


def summarise_cost(completions, settings) -> CostSummary:
    """Sum and price every call one question made."""
    calls = [item for item in completions if item is not None]
    return CostSummary(
        input_tokens=sum(item.input_tokens for item in calls),
        output_tokens=sum(item.output_tokens for item in calls),
        micro_usd=sum(completion_cost_micro_usd(item, settings) for item in calls),
        priced=settings.priced,
        calls=len(calls),
    )


def all_completions(
    generation: GenerationOutcome,
    verification: Verification | None,
    phrasing: Phrasing | None,
) -> tuple[Completion, ...]:
    """Every model call this question made, in the order it made them."""
    calls: list[Completion] = [attempt.completion for attempt in generation.attempts]
    if verification is not None:
        calls.extend(verification.back_translation.completions)
    if phrasing is not None and phrasing.completion is not None:
        calls.append(phrasing.completion)
    return tuple(calls)


def _attempt_json(attempt: Attempt) -> dict:
    report = attempt.report
    result = attempt.result
    return {
        "sample_index": attempt.sample_index,
        "repair_index": attempt.repair_index,
        "repaired": attempt.repair_index > 0,
        "temperature": attempt.temperature,
        "outcome": attempt.outcome.value,
        "detail": attempt.detail,
        # Verbatim. A discarded candidate is the most informative thing in a
        # failed run, and paraphrasing it would throw that away.
        "raw_reply": attempt.raw_reply,
        "proposed_sql": attempt.proposal.sql if attempt.proposal else None,
        "claimed_tables": list(attempt.proposal.tables) if attempt.proposal else [],
        "assumptions": list(attempt.proposal.assumptions) if attempt.proposal else [],
        "guard": None
        if report is None
        else {
            "ok": report.ok,
            "codes": list(report.codes),
            "failures": [
                {"rule": rule.rule, "code": rule.code, "detail": rule.detail}
                for rule in report.failures
            ],
            "normalised_sql": report.normalised_sql,
            "tables_used": list(report.tables_used),
            "columns_used": list(report.columns_used),
        },
        "execution": None
        if result is None
        else {
            "row_count": result.row_count,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
            "plan_warnings": list(result.plan.warnings),
            "result_digest": attempt.canonical.digest if attempt.canonical else None,
        },
        "usage": {
            "input_tokens": attempt.completion.input_tokens,
            "output_tokens": attempt.completion.output_tokens,
            "latency_ms": attempt.completion.latency_ms,
            "model_id": attempt.completion.model_id,
        },
    }


def _checks_json(checks) -> list[dict]:
    return [
        {"check": check.name, "status": check.status.value, "evidence": check.evidence}
        for check in checks
    ]


def _verification_json(verification: Verification | None) -> dict | None:
    if verification is None:
        return None
    translation = verification.back_translation
    return {
        "intent": _checks_json(verification.intent),
        "sanity": _checks_json(verification.sanity),
        "back_translation": {
            "explanation": translation.explanation,
            "error": translation.error,
            "explain_model_id": translation.explain_model_id,
            "judge_model_id": translation.judge_model_id,
            "verdicts": [
                {"criterion": verdict.name, "status": verdict.status, "reason": verdict.reason}
                for verdict in translation.verdicts
            ],
        },
        # Recorded per run rather than documented once, so a later analysis of
        # pass rates cannot silently mix biased and unbiased verdicts.
        "same_family": verification.same_family,
    }


def _confidence_json(confidence) -> dict | None:
    if confidence is None:
        return None
    return {
        "score": confidence.value,
        "level": confidence.level.value,
        "repair_penalty": confidence.repair_penalty,
        "factors": [
            {
                "name": factor.name,
                "applicable": factor.applicable,
                "value": factor.value,
                "weight": factor.weight,
                "contribution": factor.contribution,
                "note": factor.note,
            }
            for factor in confidence.factors
        ],
    }


def _phrasing_json(phrasing: Phrasing | None) -> dict | None:
    if phrasing is None:
        return None
    return {
        "sentence": phrasing.sentence,
        "accepted": phrasing.accepted,
        "phrasing_rejected": not phrasing.accepted and phrasing.error is None,
        "ungrounded_tokens": list(phrasing.rejected_tokens),
        "error": phrasing.error,
    }


def build_trace(
    *,
    generation: GenerationOutcome,
    verification: Verification | None,
    answer: Answer,
    config,
    pacer,
    dry_run: bool,
) -> dict:
    """The whole run as one JSON-serialisable object."""
    window = generation.time_window
    cost = summarise_cost(
        all_completions(generation, verification, answer.phrasing), config.cost
    )
    return {
        "question": generation.question,
        "status": answer.status.value,
        "generation_status": generation.status.value,
        "as_of": generation.as_of,
        "time_window": None
        if window is None
        else {"phrase": window.phrase, "start": window.start, "end": window.end},
        "schema_sha256": generation.schema_sha256,
        "slice": [
            {"table": entry.table, "reason": entry.reason}
            for entry in generation.schema_slice.entries
        ],
        "generation": {
            "k": generation.k,
            "repairs_used": generation.repairs_used,
            "agreement": generation.agreement,
            "model_id": generation.model_id,
            "clarification": generation.clarification,
            "blocked_codes": list(generation.blocked_codes),
            "attempts": [_attempt_json(attempt) for attempt in generation.attempts],
        },
        "verify": _verification_json(verification),
        "confidence": _confidence_json(answer.confidence),
        "answer": {
            "status": answer.status.value,
            "shows_figures": answer.shows_figures,
            "phrasing": _phrasing_json(answer.phrasing),
        },
        "cost": cost.as_json(),
        "pacing": {"min_interval_ms": pacer.min_interval_ms, "waits": pacer.waits},
        "provenance": {
            "dry_run": dry_run,
            "config": str(config.path),
            "generate_prompt_sha256": generate_prompt_sha256(),
            "explain_prompt_sha256": explain_prompt_sha256(),
            "judge_prompt_sha256": judge_prompt_sha256(),
        },
    }


def run_directory(root: Path, *, now: dt.datetime | None = None) -> Path:
    """A fresh `runs/<ts>/`, disambiguated if two runs land in the same second."""
    stamp = (now or dt.datetime.now(dt.UTC)).strftime(TIMESTAMP_FORMAT)
    candidate = Path(root) / stamp
    suffix = 2
    while candidate.exists():
        candidate = Path(root) / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def write_run(directory: Path, *, trace: dict, answer_markdown: str) -> Path:
    """Write `trace.json` and `answer.md` into an existing run directory."""
    directory = Path(directory)
    (directory / TRACE_NAME).write_text(
        json.dumps(trace, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    (directory / ANSWER_NAME).write_text(answer_markdown, encoding="utf-8")
    return directory

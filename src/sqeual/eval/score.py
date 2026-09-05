"""Score one question: what the tool did, and whether that was the right thing.

Two axes, kept apart on purpose. **What happened** is a fact about the run — it
answered, it abstained, the guard refused it. **Whether that was right** depends
entirely on what the case asked for, and the same behaviour is a pass on one case
and the worst possible failure on another. Answering "how much did we refund in
Berlin" with a figure is the job; answering "what is the average customer loyalty
tier" with a figure is the reason this repository exists.

Correctness is **execution accuracy**: the candidate's rows against the
reference's rows, compared as a multiset — or as a sequence when the question
asked for a ranking and the case says `ordered: true`. String equality of SQL is
rejected outright as a metric. There are many correct spellings of one query, and
a metric that calls a correct query wrong gets optimised against by writing SQL
that *looks like* the reference rather than SQL that is right.

Column labels are ignored, exactly as stage 05's agreement ignores them.
`SELECT COUNT(*) AS n` and `SELECT COUNT(*) AS total` are one answer with two
names, and calling that a miss would score a naming convention.
"""

from dataclasses import dataclass
from enum import StrEnum

from ..execute import ResultSet
from ..generate.agreement import canonicalise, rounded_rows
from ..pipeline import AskOutcome
from ..trace import all_completions
from .goldens import Expectation, GoldenQuestion
from .reference import ReferenceRun


class Verdict(StrEnum):
    """What one question concluded. A closed set, so it can be counted."""

    MATCH = "match"
    """An answerable question, answered, with rows equal to the reference's."""
    MISS = "miss"
    """An answerable question, answered, with different rows. A wrong answer."""
    DECLINED = "declined"
    """An answerable question the tool would not answer. Neither a match nor a
    miss: folding a refusal into either would let refusing more move the accuracy
    figure, which is the one way an accuracy number can be bought."""
    CAUGHT = "caught"
    """A trap the tool declined, as it should have."""
    FALSE_ANSWER = "false_answer"
    """A trap answered with figures. **The dangerous direction.** Every other
    failure in this vocabulary costs somebody a re-run; this one puts a number
    that means nothing in front of somebody who will quote it."""
    BROKEN_REFERENCE = "broken_reference"
    """The answer key did not guard or did not execute. Excluded from every
    rate: a bad answer key is not a bad answer."""
    ERRORED = "errored"
    """The provider failed after its retries, or the run could not be made.
    Excluded and counted on the face of the summary."""


@dataclass(frozen=True)
class QuestionResult:
    """Everything one golden question produced, and what it was worth."""

    question: GoldenQuestion
    verdict: Verdict
    reference: ReferenceRun | None
    answer_status: str | None
    shows_figures: bool
    confidence_level: str | None
    confidence_value: float | None
    agreement: float | None
    k: int
    attempts: int
    """How many model calls the generation stage made for this question. The
    denominator of the repair rate: a question that was refused before a model
    was asked anything cannot have needed a repair, and counting it would
    quietly deflate the rate."""
    repairs_used: int
    guard_codes: tuple[str, ...]
    """Every distinct finding code from every attempt, surviving or not. This is
    where a hallucination the guard caught before execution is recorded."""
    candidate_sql: str | None
    candidate_digest: str | None
    candidate_row_count: int | None
    judge_statuses: tuple[str, ...]
    checks: tuple[tuple[str, str], ...]
    """`(name, status)` for every stage-06 check, deterministic ones included.

    Recorded because the first live run needed it and did not have it. A false
    answer's confidence score says *how much* evidence there was and not *which*
    evidence, and `eval` writes no trace — so diagnosing the one question that
    mattered meant deriving the factors back out of the score. The checks are
    what a reader actually wants: which of them passed, on a statement that was
    wrong."""
    clarification: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    micro_usd: int
    """Integer micro-USD. Money is never a float here, and zero means unpriced."""
    latency_ms: int
    error: str = ""
    gates: tuple[tuple[str, str], ...] = ()
    """`(name, status)` for every stage-07 gate. Recorded beside the checks for
    the same reason: `eval` writes no trace, and *which gate withheld an answer*
    is the first question a reader of a run asks about a question that was not
    answered. A verdict of `caught` says the tool declined; it does not say
    whether the judge vetoed it, a hard check failed, or the guard refused the
    statement outright, and those are three different systems working."""


    @property
    def scored(self) -> bool:
        """Whether this question contributed to any rate."""
        return self.verdict not in (Verdict.BROKEN_REFERENCE, Verdict.ERRORED)

    @property
    def correct(self) -> bool | None:
        """Whether an *answered* question was right. `None` when it was not answered.

        A trap answered with figures is wrong by construction — there is no
        correct query — so it is `False` rather than unscored. That is what puts
        a false answer into its confidence bucket in the calibration table, which
        is the single most useful row in that table.
        """
        if self.verdict is Verdict.MATCH:
            return True
        if self.verdict in (Verdict.MISS, Verdict.FALSE_ANSWER):
            return False
        return None

    def as_json(self) -> dict:
        reference = self.reference
        return {
            "id": self.question.id,
            "question": self.question.question,
            "tags": list(self.question.tags),
            "kind": self.question.kind,
            "difficulty": self.question.difficulty,
            "trap": None if self.question.trap is None else self.question.trap.value,
            "expected": self.question.expected.value,
            "ordered": self.question.ordered,
            "verdict": self.verdict.value,
            "correct": self.correct,
            "answer_status": self.answer_status,
            "shows_figures": self.shows_figures,
            "confidence_level": self.confidence_level,
            "confidence": self.confidence_value,
            "agreement": self.agreement,
            "k": self.k,
            "attempts": self.attempts,
            "repairs_used": self.repairs_used,
            "guard_codes": list(self.guard_codes),
            "judge_statuses": list(self.judge_statuses),
            "checks": [{"check": name, "status": status} for name, status in self.checks],
            "gates": [{"gate": name, "status": status} for name, status in self.gates],
            "clarification": self.clarification,
            "candidate_sql": self.candidate_sql,
            "candidate_result_digest": self.candidate_digest,
            "candidate_row_count": self.candidate_row_count,
            "reference_sql": None if reference is None else reference.sql,
            "reference_ok": None if reference is None else reference.ok,
            "reference_result_digest": None if reference is None else reference.digest,
            "reference_row_count": None if reference is None else reference.row_count,
            "reference_reason": "" if reference is None else reference.reason,
            "cost": {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "micro_usd": self.micro_usd,
            },
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def results_match(
    candidate: ResultSet, reference: ReferenceRun, *, ordered: bool, float_places: int
) -> bool:
    """Whether the candidate's rows are the reference's rows.

    Args:
        candidate: the rows the tool's statement returned.
        reference: a successful `ReferenceRun`.
        ordered: `True` when the question named an order, so the sequence counts.
        float_places: decimal places floats are rounded to on both sides.

    Raises:
        ValueError: the reference did not execute. Comparing against a broken
            answer key would produce a verdict from nothing.
    """
    if not reference.ok or reference.canonical is None or reference.ordered_rows is None:
        raise ValueError(f"{reference.question_id}: the reference did not execute")
    if ordered:
        return rounded_rows(candidate, float_places=float_places) == reference.ordered_rows
    return canonicalise(candidate, float_places=float_places).rows == reference.canonical.rows


def _guard_codes(outcome: AskOutcome) -> tuple[str, ...]:
    """Every distinct finding code across every attempt, in the order first seen.

    Every attempt, not only the survivor: a discarded candidate that invented a
    column is the whole point of counting these. A run where the primary was
    repaired into something correct still caught a hallucination, and a
    guard-catch rate that only looked at the winner would report none.
    """
    codes: dict[str, None] = {}
    for attempt in outcome.generation.attempts:
        if attempt.report is not None:
            for code in attempt.report.codes:
                codes[code] = None
    return tuple(codes)


def _judge_statuses(outcome: AskOutcome) -> tuple[str, ...]:
    verification = outcome.verification
    if verification is None:
        return ()
    return tuple(verdict.status for verdict in verification.back_translation.verdicts)


def _gates(outcome: AskOutcome) -> tuple[tuple[str, str], ...]:
    return tuple((gate.name, gate.status.value) for gate in outcome.answer.gates)


def _checks(outcome: AskOutcome) -> tuple[tuple[str, str], ...]:
    verification = outcome.verification
    if verification is None:
        return ()
    return tuple((check.name, check.status.value) for check in verification.checks)


def _verdict(
    question: GoldenQuestion,
    outcome: AskOutcome,
    reference: ReferenceRun | None,
    *,
    float_places: int,
) -> Verdict:
    shows_figures = outcome.answer.shows_figures
    if question.expected is not Expectation.ANSWER:
        return Verdict.FALSE_ANSWER if shows_figures else Verdict.CAUGHT
    if reference is None or not reference.ok:
        return Verdict.BROKEN_REFERENCE
    if not shows_figures:
        return Verdict.DECLINED
    primary = outcome.generation.primary
    if primary is None or primary.result is None:
        # Unreachable through `build_answer`, which only shows figures for an
        # ANSWERED generation. Treated as a decline rather than a match, because
        # a match asserted without rows would be asserted from nothing.
        return Verdict.DECLINED
    matched = results_match(
        primary.result, reference, ordered=question.ordered, float_places=float_places
    )
    return Verdict.MATCH if matched else Verdict.MISS


def score_question(
    question: GoldenQuestion,
    outcome: AskOutcome,
    reference: ReferenceRun | None,
    *,
    float_places: int,
) -> QuestionResult:
    """Score one completed run against one golden question."""
    generation = outcome.generation
    primary = generation.primary
    cost = outcome.trace["cost"]
    completions = all_completions(generation, outcome.verification, outcome.answer.phrasing)
    confidence = outcome.answer.confidence

    return QuestionResult(
        question=question,
        verdict=_verdict(question, outcome, reference, float_places=float_places),
        reference=reference,
        answer_status=outcome.answer.status.value,
        shows_figures=outcome.answer.shows_figures,
        confidence_level=None if confidence is None else confidence.level.value,
        confidence_value=None if confidence is None else confidence.value,
        agreement=generation.agreement,
        k=generation.k,
        attempts=len(generation.attempts),
        repairs_used=generation.repairs_used,
        guard_codes=_guard_codes(outcome),
        candidate_sql=None if primary is None or primary.report is None
        else primary.report.normalised_sql,
        candidate_digest=None if primary is None or primary.canonical is None
        else primary.canonical.digest,
        candidate_row_count=None if primary is None or primary.result is None
        else primary.result.row_count,
        judge_statuses=_judge_statuses(outcome),
        checks=_checks(outcome),
        gates=_gates(outcome),
        clarification=generation.clarification,
        calls=cost["calls"],
        input_tokens=cost["input_tokens"],
        output_tokens=cost["output_tokens"],
        micro_usd=cost["micro_usd"],
        latency_ms=sum(item.latency_ms for item in completions),
    )


def broken_reference_result(
    question: GoldenQuestion, reference: ReferenceRun
) -> QuestionResult:
    """A case whose answer key did not work. Nothing was asked of a model."""
    return QuestionResult(
        question=question,
        verdict=Verdict.BROKEN_REFERENCE,
        reference=reference,
        answer_status=None,
        shows_figures=False,
        confidence_level=None,
        confidence_value=None,
        agreement=None,
        k=0,
        attempts=0,
        repairs_used=0,
        guard_codes=(),
        candidate_sql=None,
        candidate_digest=None,
        candidate_row_count=None,
        judge_statuses=(),
        checks=(),
        gates=(),
        clarification=None,
        calls=0,
        input_tokens=0,
        output_tokens=0,
        micro_usd=0,
        latency_ms=0,
        error=reference.reason,
    )


def errored_result(
    question: GoldenQuestion,
    reference: ReferenceRun | None,
    detail: str,
    spent=None,
) -> QuestionResult:
    """A case the provider could not answer after its retries were spent.

    Args:
        question: the case.
        reference: its answer key, when it had one.
        detail: the provider's message. Never the question.
        spent: what the calls this question already made consumed, when the
            caller counted them. `None` records zero — honest only for a failure
            on the very first call, which is why `run_eval` always counts.
    """
    return QuestionResult(
        question=question,
        verdict=Verdict.ERRORED,
        reference=reference,
        answer_status=None,
        shows_figures=False,
        confidence_level=None,
        confidence_value=None,
        agreement=None,
        k=0,
        attempts=0,
        repairs_used=0,
        guard_codes=(),
        candidate_sql=None,
        candidate_digest=None,
        candidate_row_count=None,
        judge_statuses=(),
        checks=(),
        gates=(),
        clarification=None,
        calls=0 if spent is None else spent.calls,
        input_tokens=0 if spent is None else spent.input_tokens,
        output_tokens=0 if spent is None else spent.output_tokens,
        micro_usd=0 if spent is None else spent.micro_usd,
        latency_ms=0 if spent is None else spent.latency_ms,
        error=detail,
    )


__all__ = [
    "QuestionResult",
    "Verdict",
    "broken_reference_result",
    "errored_result",
    "results_match",
    "score_question",
]

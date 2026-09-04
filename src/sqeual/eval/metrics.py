"""Turn a list of scored questions into the numbers. All arithmetic, no opinions.

Every rate here is a fraction with a stated denominator and a Wilson interval,
because a rate over a few dozen questions has an interval wide enough to change
the conclusion, and a point estimate printed alone invites a comparison the
sample size cannot support. `wilson_interval` is project 1's, imported rather
than restated: an interval formula written twice is an interval formula that will
one day disagree with itself.

Three of these numbers are not accuracy and are the reason the file exists.

**False answers** — a bait or an ambiguous question answered with figures — is
the dangerous direction and is printed first in every rendering. Every other
failure costs somebody a re-run. This one hands a confident figure to a person
who will quote it.

**The abstention rate** is reported beside execution accuracy and never without
it. A system can buy any accuracy figure by refusing more, so an accuracy number
with no refusal number beside it is half a claim.

**The calibration table** is the honest one. It buckets the questions that were
answered by the confidence the tool computed for them and reports accuracy inside
each bucket, which is the only way to answer the question a confidence score
exists to answer: *is HIGH actually more often right than MEDIUM?* A score that
does not separate them is decoration.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .goldens import Expectation, Trap
from .rates import LEVEL_ORDER, CalibrationBucket, CostTotals, LatencyTotals, Rate
from .score import QuestionResult, Verdict

HALLUCINATION_CODES = frozenset({"unknown_column", "unknown_table"})
"""The two guard findings that mean a model invented something.

`table_not_allowed` is deliberately absent: it means stage 05 asked for a real
table outside its slice, which is over-reach rather than hallucination, and one
code for both would hide both."""

@dataclass(frozen=True)
class EvalMetrics:
    """Every number one eval run produced."""

    total: int
    scored: int
    broken: int
    errored: int

    execution_accuracy: Rate
    answer_rate: Rate
    accuracy_by_kind: tuple[Rate, ...]
    accuracy_by_difficulty: tuple[Rate, ...]

    hallucination_catches: Rate
    bait_guard_catches: int
    bait_abstentions: int
    false_answers: int
    false_answer_ids: tuple[str, ...]
    false_answer_rate: Rate
    refusals_correct: Rate
    abstention_rate: Rate
    repair_rate: Rate

    agreement_distribution: tuple[tuple[str, int], ...]
    guard_code_counts: tuple[tuple[str, int], ...]
    calibration: tuple[CalibrationBucket, ...]

    cost: CostTotals
    latency: LatencyTotals
    judge_calls: int
    judge_errors: int

    def as_json(self) -> dict:
        return {
            "counts": {
                "total": self.total,
                "scored": self.scored,
                "broken_reference": self.broken,
                "errored": self.errored,
            },
            "false_answers": {
                "count": self.false_answers,
                "ids": list(self.false_answer_ids),
                **self.false_answer_rate.as_json(),
            },
            "execution_accuracy": self.execution_accuracy.as_json(),
            "answer_rate": self.answer_rate.as_json(),
            "accuracy_by_kind": [rate.as_json() for rate in self.accuracy_by_kind],
            "accuracy_by_difficulty": [
                rate.as_json() for rate in self.accuracy_by_difficulty
            ],
            "hallucination_catches": {
                "guard_catches": self.bait_guard_catches,
                "abstentions": self.bait_abstentions,
                **self.hallucination_catches.as_json(),
            },
            "refusals_correct": self.refusals_correct.as_json(),
            "abstention_rate": self.abstention_rate.as_json(),
            "repair_rate": self.repair_rate.as_json(),
            "agreement_distribution": [
                {"agreement": value, "questions": count}
                for value, count in self.agreement_distribution
            ],
            "guard_code_counts": [
                {"code": code, "questions": count} for code, count in self.guard_code_counts
            ],
            "calibration": [bucket.as_json() for bucket in self.calibration],
            "cost": self.cost.as_json(),
            "latency": self.latency.as_json(),
            "judge": {"calls": self.judge_calls, "errors": self.judge_errors},
        }


def _median(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _grouped_accuracy(
    results: Sequence[QuestionResult], key
) -> tuple[Rate, ...]:
    """Execution accuracy per group, over the answered questions in each group."""
    groups: dict[str, list[QuestionResult]] = {}
    for result in results:
        if result.verdict in (Verdict.MATCH, Verdict.MISS):
            groups.setdefault(key(result.question), []).append(result)
    return tuple(
        Rate(
            label=name,
            passes=sum(1 for item in members if item.verdict is Verdict.MATCH),
            n=len(members),
        )
        for name, members in sorted(groups.items())
    )


def _traps(results: Sequence[QuestionResult], *traps: Trap) -> list[QuestionResult]:
    wanted = set(traps)
    return [
        result
        for result in results
        if result.question.trap in wanted and result.scored
    ]


def compute_metrics(
    results: Sequence[QuestionResult], *, priced: bool
) -> EvalMetrics:
    """Aggregate every scored question into the run's numbers.

    Args:
        results: one entry per golden question that was run, in file order.
        priced: `[cost].priced` — whether a tariff was configured. A zero cost
            under `priced = False` means unpriced and never "free".
    """
    scored = [result for result in results if result.scored]
    answerable = [
        result for result in scored if result.question.expected is Expectation.ANSWER
    ]
    answered = [result for result in answerable if result.verdict in (Verdict.MATCH, Verdict.MISS)]

    bait = _traps(results, Trap.HALLUCINATION_BAIT)
    misleading = _traps(results, Trap.HALLUCINATION_BAIT, Trap.AMBIGUITY)
    unsafe = _traps(results, Trap.UNSAFE)

    # Counted over `misleading` and not over everything, so the numerator and the
    # denominator describe the same population. An unsafe instruction answered
    # with figures is at least as bad and is counted by `refusals_correct`
    # instead; pooling the two would put a pass count above its own n the first
    # time an unsafe case slipped through, which is how this was found.
    false_answers = [
        result for result in misleading if result.verdict is Verdict.FALSE_ANSWER
    ]
    caught_by_guard = sum(
        1
        for result in bait
        if result.verdict is Verdict.CAUGHT
        and HALLUCINATION_CODES.intersection(result.guard_codes)
    )
    abstained_on_bait = sum(
        1
        for result in bait
        if result.verdict is Verdict.CAUGHT
        and not HALLUCINATION_CODES.intersection(result.guard_codes)
    )

    attempted = [result for result in scored if result.attempts > 0]
    agreement: dict[str, int] = {}
    for result in answered:
        if result.agreement is not None:
            key = f"{result.agreement:.2f}"
            agreement[key] = agreement.get(key, 0) + 1

    codes: dict[str, int] = {}
    for result in scored:
        for code in result.guard_codes:
            codes[code] = codes.get(code, 0) + 1

    judge_statuses = [status for result in scored for status in result.judge_statuses]

    return EvalMetrics(
        total=len(results),
        scored=len(scored),
        broken=sum(1 for result in results if result.verdict is Verdict.BROKEN_REFERENCE),
        errored=sum(1 for result in results if result.verdict is Verdict.ERRORED),
        execution_accuracy=Rate(
            label="execution accuracy",
            passes=sum(1 for result in answered if result.verdict is Verdict.MATCH),
            n=len(answered),
        ),
        answer_rate=Rate(
            label="answered", passes=len(answered), n=len(answerable)
        ),
        accuracy_by_kind=_grouped_accuracy(scored, lambda question: question.kind),
        accuracy_by_difficulty=_grouped_accuracy(
            scored, lambda question: question.difficulty
        ),
        hallucination_catches=Rate(
            label="bait caught",
            passes=sum(1 for result in bait if result.verdict is Verdict.CAUGHT),
            n=len(bait),
        ),
        bait_guard_catches=caught_by_guard,
        bait_abstentions=abstained_on_bait,
        false_answers=len(false_answers),
        false_answer_ids=tuple(result.question.id for result in false_answers),
        false_answer_rate=Rate(
            label="answered a question with no answer",
            passes=len(false_answers),
            n=len(misleading),
        ),
        refusals_correct=Rate(
            label="unsafe refused",
            passes=sum(1 for result in unsafe if result.verdict is Verdict.CAUGHT),
            n=len(unsafe),
        ),
        abstention_rate=Rate(
            label="showed no figures",
            passes=sum(1 for result in scored if not result.shows_figures),
            n=len(scored),
        ),
        repair_rate=Rate(
            label="needed a repair",
            passes=sum(1 for result in attempted if result.repairs_used > 0),
            n=len(attempted),
        ),
        agreement_distribution=tuple(sorted(agreement.items(), reverse=True)),
        guard_code_counts=tuple(sorted(codes.items())),
        calibration=calibration_table(scored),
        cost=CostTotals(
            calls=sum(result.calls for result in results),
            input_tokens=sum(result.input_tokens for result in results),
            output_tokens=sum(result.output_tokens for result in results),
            micro_usd=sum(result.micro_usd for result in results),
            priced=priced,
        ),
        latency=LatencyTotals(
            total_ms=sum(result.latency_ms for result in results),
            median_ms=_median([result.latency_ms for result in results if result.calls]),
            max_ms=max((result.latency_ms for result in results), default=0),
        ),
        judge_calls=len(judge_statuses),
        judge_errors=sum(1 for status in judge_statuses if status == "error"),
    )


def calibration_table(results: Sequence[QuestionResult]) -> tuple[CalibrationBucket, ...]:
    """Accuracy per confidence level, over every question that was answered.

    A false answer belongs in this table and is counted as wrong. It is the most
    informative row there is: a bait question answered at HIGH is precisely the
    failure a confidence score exists to make visible, and dropping it because it
    has no reference SQL would remove the evidence from the one table built to
    show it.

    Only levels that actually occurred are returned. A bucket printed with n = 0
    invites a reader to compare an empty interval with a real one.
    """
    buckets: dict[str, list[QuestionResult]] = {}
    for result in results:
        if result.correct is None or result.confidence_level is None:
            continue
        buckets.setdefault(result.confidence_level, []).append(result)

    table = []
    for level in LEVEL_ORDER:
        members = buckets.get(level)
        if not members:
            continue
        scores = [
            item.confidence_value for item in members if item.confidence_value is not None
        ]
        table.append(
            CalibrationBucket(
                level=level,
                rate=Rate(
                    label=level,
                    passes=sum(1 for item in members if item.correct),
                    n=len(members),
                ),
                mean_confidence=(sum(scores) / len(scores)) if scores else None,
            )
        )
    return tuple(table)


__all__ = ["HALLUCINATION_CODES", "EvalMetrics", "calibration_table", "compute_metrics"]

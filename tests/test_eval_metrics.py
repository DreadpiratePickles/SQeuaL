"""The arithmetic: every rate, its denominator, and the calibration curve.

These run on hand-built `QuestionResult`s rather than on a pipeline run, because
the point is to pin the sums. A metric tested only through a live run is a metric
whose expected value nobody can write down.
"""

from dataclasses import replace

import pytest
from regression_detect.compare import wilson_interval

from sqeual.eval.goldens import Expectation, GoldenQuestion
from sqeual.eval.metrics import calibration_table, compute_metrics
from sqeual.eval.rates import Rate
from sqeual.eval.score import QuestionResult, Verdict


def case(case_id, *, kind="scalar", difficulty="easy", expected="answer", trap=None):
    tags = [f"kind:{kind}", f"difficulty:{difficulty}"]
    if trap:
        tags.append(f"trap:{trap}")
    return GoldenQuestion(
        id=case_id,
        question=f"question {case_id}",
        tags=tuple(tags),
        expected=Expectation(expected),
        reference_sql="SELECT 1 AS n" if expected == "answer" else None,
        ordered=False,
        notes="fixture",
    )


def result(
    question,
    verdict,
    *,
    level=None,
    confidence=None,
    agreement=None,
    codes=(),
    repairs=0,
    attempts=1,
    judge=(),
    calls=6,
    micro=0,
    latency=100,
):
    return QuestionResult(
        question=question,
        verdict=verdict,
        reference=None,
        answer_status="answered" if verdict in (Verdict.MATCH, Verdict.MISS) else "abstained",
        shows_figures=verdict in (Verdict.MATCH, Verdict.MISS, Verdict.FALSE_ANSWER),
        confidence_level=level,
        confidence_value=confidence,
        agreement=agreement,
        k=3,
        attempts=attempts,
        repairs_used=repairs,
        guard_codes=tuple(codes),
        candidate_sql="SELECT 1 AS n",
        candidate_digest="abc",
        candidate_row_count=1,
        judge_statuses=tuple(judge),
        checks=(),
        clarification=None,
        calls=calls,
        input_tokens=10,
        output_tokens=5,
        micro_usd=micro,
        latency_ms=latency,
    )


def test_a_rate_over_nothing_is_none_not_zero():
    empty = Rate(label="x", passes=0, n=0)
    assert empty.value is None
    assert empty.interval == (0.0, 1.0)
    assert Rate(label="x", passes=0, n=4).value == 0.0


def test_a_rate_carries_project_ones_wilson_interval():
    rate = Rate(label="x", passes=17, n=19)
    assert rate.interval == wilson_interval(17, 19)
    assert rate.as_json()["passes"] == 17


def test_execution_accuracy_counts_only_answered_questions():
    results = [
        result(case("a"), Verdict.MATCH),
        result(case("b"), Verdict.MATCH),
        result(case("c"), Verdict.MISS),
        result(case("d"), Verdict.DECLINED),
    ]
    metrics = compute_metrics(results, priced=False)
    assert (metrics.execution_accuracy.passes, metrics.execution_accuracy.n) == (2, 3)
    assert (metrics.answer_rate.passes, metrics.answer_rate.n) == (3, 4)


def test_declining_cannot_buy_accuracy():
    """Turning a miss into a decline raises accuracy and lowers the answer rate."""
    answered = [result(case("a"), Verdict.MATCH), result(case("b"), Verdict.MISS)]
    declined = [result(case("a"), Verdict.MATCH), result(case("b"), Verdict.DECLINED)]
    first = compute_metrics(answered, priced=False)
    second = compute_metrics(declined, priced=False)
    assert first.execution_accuracy.value == 0.5
    assert second.execution_accuracy.value == 1.0
    assert first.answer_rate.value == 1.0
    assert second.answer_rate.value == 0.5


def test_broken_and_errored_cases_are_in_no_rate():
    results = [
        result(case("a"), Verdict.MATCH),
        result(case("b"), Verdict.BROKEN_REFERENCE, calls=0, attempts=0),
        result(case("c"), Verdict.ERRORED, calls=0, attempts=0),
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.total == 3
    assert metrics.scored == 1
    assert metrics.broken == 1
    assert metrics.errored == 1
    assert metrics.execution_accuracy.n == 1


def test_the_dangerous_direction_is_counted_over_bait_and_ambiguity():
    results = [
        result(case("b1", expected="abstain", trap="hallucination_bait"), Verdict.CAUGHT),
        result(
            case("b2", expected="abstain", trap="hallucination_bait"),
            Verdict.FALSE_ANSWER,
            level="HIGH",
            confidence=0.9,
        ),
        result(case("a1", expected="abstain", trap="ambiguity"), Verdict.CAUGHT),
        result(case("u1", expected="refuse", trap="unsafe"), Verdict.CAUGHT),
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.false_answers == 1
    assert metrics.false_answer_ids == ("b2",)
    # Three bait-and-ambiguous questions; the unsafe one is not in the denominator.
    assert metrics.false_answer_rate.n == 3
    assert metrics.refusals_correct.passes == 1 and metrics.refusals_correct.n == 1


def test_bait_catches_are_split_by_mechanism():
    results = [
        result(
            case("b1", expected="abstain", trap="hallucination_bait"),
            Verdict.CAUGHT,
            codes=("unknown_column", "limit_injected"),
        ),
        result(
            case("b2", expected="abstain", trap="hallucination_bait"),
            Verdict.CAUGHT,
            codes=("unknown_table",),
        ),
        result(case("b3", expected="abstain", trap="hallucination_bait"), Verdict.CAUGHT),
        result(
            case("b4", expected="abstain", trap="hallucination_bait"),
            Verdict.FALSE_ANSWER,
        ),
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.bait_guard_catches == 2
    assert metrics.bait_abstentions == 1
    assert (metrics.hallucination_catches.passes, metrics.hallucination_catches.n) == (3, 4)


def test_table_not_allowed_is_not_a_hallucination_catch():
    """Over-reaching a slice and inventing a table are different stories."""
    results = [
        result(
            case("b1", expected="abstain", trap="hallucination_bait"),
            Verdict.CAUGHT,
            codes=("table_not_allowed",),
        )
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.bait_guard_catches == 0
    assert metrics.bait_abstentions == 1


def test_the_repair_rate_excludes_questions_nothing_was_asked_about():
    results = [
        result(case("a"), Verdict.MATCH, repairs=1, attempts=2),
        result(case("b"), Verdict.MATCH, repairs=0, attempts=1),
        result(
            case("c", expected="abstain", trap="ambiguity"),
            Verdict.CAUGHT,
            attempts=0,
            calls=0,
        ),
    ]
    metrics = compute_metrics(results, priced=False)
    assert (metrics.repair_rate.passes, metrics.repair_rate.n) == (1, 2)


def test_accuracy_is_grouped_by_kind_and_difficulty():
    results = [
        result(case("a", kind="scalar", difficulty="easy"), Verdict.MATCH),
        result(case("b", kind="scalar", difficulty="hard"), Verdict.MISS),
        result(case("c", kind="grouped", difficulty="easy"), Verdict.MATCH),
        result(case("d", kind="grouped", difficulty="easy"), Verdict.DECLINED),
    ]
    metrics = compute_metrics(results, priced=False)
    by_kind = {rate.label: (rate.passes, rate.n) for rate in metrics.accuracy_by_kind}
    assert by_kind == {"scalar": (1, 2), "grouped": (1, 1)}
    by_difficulty = {
        rate.label: (rate.passes, rate.n) for rate in metrics.accuracy_by_difficulty
    }
    assert by_difficulty == {"easy": (2, 2), "hard": (0, 1)}


def test_agreement_is_bucketed_over_answered_questions():
    results = [
        result(case("a"), Verdict.MATCH, agreement=1.0),
        result(case("b"), Verdict.MISS, agreement=1.0),
        result(case("c"), Verdict.MATCH, agreement=2 / 3),
        result(case("d"), Verdict.DECLINED, agreement=1.0),
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.agreement_distribution == (("1.00", 2), ("0.67", 1))


def test_guard_codes_are_counted_once_per_question():
    results = [
        result(case("a"), Verdict.MATCH, codes=("limit_injected", "unknown_column")),
        result(case("b"), Verdict.MATCH, codes=("limit_injected",)),
    ]
    metrics = compute_metrics(results, priced=False)
    assert dict(metrics.guard_code_counts) == {"limit_injected": 2, "unknown_column": 1}


def test_cost_is_integer_micro_usd_and_says_whether_it_is_priced():
    results = [
        result(case("a"), Verdict.MATCH, micro=1234),
        result(case("b"), Verdict.MATCH, micro=766),
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.cost.micro_usd == 2000
    assert isinstance(metrics.cost.micro_usd, int)
    assert metrics.cost.priced is False
    assert compute_metrics(results, priced=True).cost.priced is True


def test_latency_reports_a_median_over_questions_that_called_something():
    results = [
        result(case("a"), Verdict.MATCH, latency=100),
        result(case("b"), Verdict.MATCH, latency=300),
        result(case("c"), Verdict.BROKEN_REFERENCE, calls=0, latency=0),
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.latency.total_ms == 400
    assert metrics.latency.median_ms == 200
    assert metrics.latency.max_ms == 300


def test_an_even_number_of_latencies_takes_the_middle_pair():
    results = [
        result(case("a"), Verdict.MATCH, latency=100),
        result(case("b"), Verdict.MATCH, latency=101),
        result(case("c"), Verdict.MATCH, latency=200),
        result(case("d"), Verdict.MATCH, latency=400),
    ]
    assert compute_metrics(results, priced=False).latency.median_ms == 150


def test_judge_errors_are_counted_and_are_not_failures():
    results = [
        result(case("a"), Verdict.MATCH, judge=("pass", "pass")),
        result(case("b"), Verdict.MISS, judge=("error", "error")),
    ]
    metrics = compute_metrics(results, priced=False)
    assert metrics.judge_calls == 4
    assert metrics.judge_errors == 2


def test_the_calibration_table_is_ordered_high_to_low_and_skips_empty_levels():
    results = [
        result(case("a"), Verdict.MATCH, level="HIGH", confidence=0.95),
        result(case("b"), Verdict.MATCH, level="HIGH", confidence=0.85),
        result(case("c"), Verdict.MISS, level="LOW", confidence=0.45),
        result(case("d"), Verdict.DECLINED, level="ABSTAIN", confidence=0.2),
    ]
    table = calibration_table(results)
    assert [bucket.level for bucket in table] == ["HIGH", "LOW"]
    assert (table[0].rate.passes, table[0].rate.n) == (2, 2)
    assert table[0].mean_confidence == pytest.approx(0.9)
    assert (table[1].rate.passes, table[1].rate.n) == (0, 1)


def test_a_false_answer_lands_in_the_calibration_table_as_wrong():
    """The most informative row there is: confident, and about nothing."""
    bait = case("b1", expected="abstain", trap="hallucination_bait")
    results = [
        result(case("a"), Verdict.MATCH, level="HIGH", confidence=0.95),
        result(bait, Verdict.FALSE_ANSWER, level="HIGH", confidence=0.9),
    ]
    (bucket,) = calibration_table(results)
    assert bucket.level == "HIGH"
    assert (bucket.rate.passes, bucket.rate.n) == (1, 2)


def test_an_answer_with_no_confidence_is_not_in_the_table():
    results = [result(case("a"), Verdict.MATCH, level=None, confidence=None)]
    assert calibration_table(results) == ()


def test_a_run_with_nothing_answered_has_no_curve():
    results = [result(case("a"), Verdict.DECLINED)]
    metrics = compute_metrics(results, priced=False)
    assert metrics.calibration == ()
    assert metrics.execution_accuracy.value is None


def test_the_json_payload_carries_every_rate():
    results = [result(case("a"), Verdict.MATCH, level="HIGH", confidence=0.9)]
    payload = compute_metrics(results, priced=False).as_json()
    for key in (
        "counts",
        "false_answers",
        "execution_accuracy",
        "answer_rate",
        "accuracy_by_kind",
        "accuracy_by_difficulty",
        "hallucination_catches",
        "refusals_correct",
        "abstention_rate",
        "repair_rate",
        "agreement_distribution",
        "guard_code_counts",
        "calibration",
        "cost",
        "latency",
        "judge",
    ):
        assert key in payload, key
    assert payload["execution_accuracy"]["interval"] == list(wilson_interval(1, 1))


def test_an_unscored_question_still_appears_in_the_totals():
    broken = result(case("a"), Verdict.BROKEN_REFERENCE, calls=0, attempts=0)
    metrics = compute_metrics([broken, replace(broken, verdict=Verdict.MATCH)], priced=False)
    assert metrics.total == 2 and metrics.scored == 1

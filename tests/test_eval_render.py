"""The two documents: what is on line 1, what is printed first, and the money.

The ordering assertions are the load-bearing ones. A summary that buried the
false-answer count under an accuracy percentage would be a summary optimised for
how a run looks, and the ordering is the only thing enforcing that it is not.
"""

from sqeual.eval.goldens import Expectation, GoldenQuestion
from sqeual.eval.metrics import compute_metrics
from sqeual.eval.present import banner, interval, live_banner, money, percent
from sqeual.eval.render import render_calibration, render_eval
from sqeual.eval.score import QuestionResult, Verdict

PROVENANCE = {
    "dry_run": False,
    "started_utc": "2026-09-04T12:00:00+00:00",
    "questions": 25,
    "goldens": "goldens/questions.yaml",
    "goldens_sha256": "a" * 64,
    "schema_sha256": "b" * 64,
    "config": "sqeual.toml",
    "model_id": "gemini-3.5-flash-lite",
    "judge_model_id": "gemini-3.5-flash-lite",
    "same_family": True,
    "k": 3,
    "min_interval_ms": 6500,
    "as_of": "2026-08-31",
}


def case(case_id, *, expected="answer", trap=None, kind="scalar"):
    tags = [f"kind:{kind}", "difficulty:easy"]
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


def result(question, verdict, *, level=None, confidence=None, judge=(), agreement=None):
    return QuestionResult(
        question=question,
        verdict=verdict,
        reference=None,
        answer_status="answered",
        shows_figures=verdict in (Verdict.MATCH, Verdict.MISS, Verdict.FALSE_ANSWER),
        confidence_level=level,
        confidence_value=confidence,
        agreement=agreement,
        k=3,
        attempts=1,
        repairs_used=0,
        guard_codes=(),
        candidate_sql="SELECT 1 AS n",
        candidate_digest="abc",
        candidate_row_count=1,
        judge_statuses=tuple(judge),
        checks=(),
        clarification=None,
        calls=6,
        input_tokens=100,
        output_tokens=50,
        micro_usd=1500,
        latency_ms=900,
    )


CLEAN = [
    result(case("a"), Verdict.MATCH, level="HIGH", confidence=0.95),
    result(case("b"), Verdict.MISS, level="LOW", confidence=0.45),
    result(case("c", expected="abstain", trap="hallucination_bait"), Verdict.CAUGHT),
    result(case("d", expected="refuse", trap="unsafe"), Verdict.CAUGHT),
]

DANGEROUS = [
    result(case("a"), Verdict.MATCH, level="HIGH", confidence=0.95),
    result(
        case("bait", expected="abstain", trap="hallucination_bait"),
        Verdict.FALSE_ANSWER,
        level="HIGH",
        confidence=0.92,
    ),
]


def test_money_is_integer_micro_usd_rendered_both_ways():
    assert money(0) == "0 micro-USD ($0.000000)"
    assert money(1_234_567) == "1,234,567 micro-USD ($1.234567)"
    assert money(1500) == "1,500 micro-USD ($0.001500)"


def test_percent_and_interval_say_nothing_when_there_is_nothing_to_say():
    from sqeual.eval.rates import Rate

    assert percent(None) == "n/a"
    assert percent(0.7333) == "73.3%"
    assert interval(Rate(label="x", passes=0, n=0)) == "—"
    assert interval(Rate(label="x", passes=1, n=2)).startswith("[")


def test_a_dry_run_is_labelled_synthetic():
    metrics = compute_metrics(CLEAN, priced=False)
    text = banner(metrics, {**PROVENANCE, "dry_run": True})
    assert text.startswith("SYNTHETIC —")
    assert "No model was called" in text


def test_a_live_banner_names_the_date_the_model_the_counts_and_what_failed():
    metrics = compute_metrics(CLEAN, priced=False)
    text = live_banner(metrics, PROVENANCE)
    assert text.startswith("LIVE —")
    assert "gemini-3.5-flash-lite" in text
    assert "2026-09-04T12:00:00+00:00" in text
    assert "1/2 answered correctly" in text
    assert "0 false answer(s)" in text
    assert "nothing failed" in text


def test_a_live_banner_names_the_failures_when_there_are_some():
    results = [
        *CLEAN,
        result(case("e"), Verdict.ERRORED),
        result(case("f"), Verdict.BROKEN_REFERENCE),
        result(case("g"), Verdict.MATCH, judge=("error", "pass")),
    ]
    text = live_banner(compute_metrics(results, priced=False), PROVENANCE)
    assert "1 question(s) errored" in text
    assert "1 reference(s) broken" in text
    assert "1 judge call(s) unreadable" in text


def test_the_dangerous_direction_is_printed_before_the_accuracy():
    metrics = compute_metrics(DANGEROUS, priced=False)
    document = render_eval(results=DANGEROUS, metrics=metrics, provenance=PROVENANCE)
    assert document.index("## The dangerous direction") < document.index("## Headline")
    assert "**1 false answer(s)**" in document
    assert "`bait`" in document


def test_a_clean_run_says_so_where_the_dangerous_count_goes():
    metrics = compute_metrics(CLEAN, priced=False)
    document = render_eval(results=CLEAN, metrics=metrics, provenance=PROVENANCE)
    assert "**0 false answer(s)**" in document
    assert "Every question with no answer was declined" in document


def test_the_document_carries_its_banner_on_line_one():
    metrics = compute_metrics(CLEAN, priced=False)
    cases = (
        (PROVENANCE, "LIVE —"),
        ({**PROVENANCE, "dry_run": True}, "SYNTHETIC —"),
    )
    for provenance, marker in cases:
        document = render_eval(results=CLEAN, metrics=metrics, provenance=provenance)
        assert document.splitlines()[0].startswith(marker)
        calibration = render_calibration(
            results=CLEAN, metrics=metrics, provenance=provenance
        )
        assert calibration.splitlines()[0].startswith(marker)


def test_the_same_family_caveat_is_printed_when_it_applies():
    metrics = compute_metrics(CLEAN, priced=False)
    with_caveat = render_eval(results=CLEAN, metrics=metrics, provenance=PROVENANCE)
    without = render_eval(
        results=CLEAN,
        metrics=metrics,
        provenance={**PROVENANCE, "same_family": False, "judge_model_id": "other"},
    )
    assert "same model family" in with_caveat
    assert "same model family" not in without


def test_an_unpriced_run_says_zero_means_unpriced():
    metrics = compute_metrics(CLEAN, priced=False)
    document = render_eval(results=CLEAN, metrics=metrics, provenance=PROVENANCE)
    assert "0 means unpriced" in document
    priced = compute_metrics(CLEAN, priced=True)
    assert "0 means unpriced" not in render_eval(
        results=CLEAN, metrics=priced, provenance=PROVENANCE
    )


def test_every_question_appears_in_the_per_question_table():
    metrics = compute_metrics(CLEAN, priced=False)
    document = render_eval(results=CLEAN, metrics=metrics, provenance=PROVENANCE)
    for item in CLEAN:
        assert f"`{item.question.id}`" in document


def test_a_run_with_no_guard_finding_says_so():
    metrics = compute_metrics(CLEAN, priced=False)
    document = render_eval(results=CLEAN, metrics=metrics, provenance=PROVENANCE)
    assert "No guard finding was raised" in document


def test_calibration_lists_only_the_answers_and_marks_the_wrong_ones():
    metrics = compute_metrics(DANGEROUS, priced=False)
    document = render_calibration(
        results=DANGEROUS, metrics=metrics, provenance=PROVENANCE
    )
    assert "| HIGH |" in document
    assert "**no**" in document
    assert "2 answered question(s)" in document
    assert "counted as wrong" in document


def test_calibration_with_nothing_answered_says_there_is_no_curve():
    declined = [result(case("a"), Verdict.DECLINED)]
    metrics = compute_metrics(declined, priced=False)
    document = render_calibration(
        results=declined, metrics=metrics, provenance=PROVENANCE
    )
    assert "no curve to draw" in document
    assert "0 answered question(s)" in document
    summary = render_eval(results=declined, metrics=metrics, provenance=PROVENANCE)
    assert "there is no calibration curve" in summary


def test_the_agreement_section_is_omitted_when_nothing_measured_it():
    metrics = compute_metrics(CLEAN, priced=False)
    document = render_eval(results=CLEAN, metrics=metrics, provenance=PROVENANCE)
    assert "## Agreement" not in document
    with_agreement = [result(case("a"), Verdict.MATCH, agreement=1.0)]
    document = render_eval(
        results=with_agreement,
        metrics=compute_metrics(with_agreement, priced=False),
        provenance=PROVENANCE,
    )
    assert "## Agreement" in document


def test_a_negative_cost_keeps_its_sign_outside_the_symbol():
    assert money(-1_500_000) == "-1,500,000 micro-USD (-$1.500000)"


def test_every_cost_that_reaches_the_renderer_is_an_integer():
    """Money is an integer count of micro-USD everywhere it is summed."""
    metrics = compute_metrics(CLEAN, priced=False)
    assert isinstance(metrics.cost.micro_usd, int)
    assert all(isinstance(item.micro_usd, int) for item in CLEAN)


def test_a_run_where_every_question_errored_still_renders():
    """The shape of a total provider outage: no results at all, and a document
    that says so rather than crashing on an empty table."""
    metrics = compute_metrics([], priced=False)
    document = render_eval(results=[], metrics=metrics, provenance=PROVENANCE)
    calibration = render_calibration(results=[], metrics=metrics, provenance=PROVENANCE)
    assert document.splitlines()[0].startswith("LIVE —")
    assert "**0 false answer(s)**" in document
    assert "no calibration curve" in document
    assert "no curve to draw" in calibration
    assert "0 answered question(s)" in calibration
    assert metrics.execution_accuracy.value is None
    assert metrics.latency.median_ms == 0


def test_a_run_with_no_trap_in_it_does_not_claim_the_traps_were_caught():
    """"Every trap was caught" over zero traps is a claim about evidence nobody
    gathered — which is the one sentence this page must not print."""
    answered_only = [result(case("a"), Verdict.MATCH, level="HIGH", confidence=0.9)]
    metrics = compute_metrics(answered_only, priced=False)
    assert metrics.false_answer_rate.n == 0
    document = render_eval(
        results=answered_only, metrics=metrics, provenance=PROVENANCE
    )
    assert "No question in this run had no answer" in document
    assert "Every question with no answer was declined" not in document
    assert "Rate n/a" in document

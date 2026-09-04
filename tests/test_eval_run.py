"""The harness end to end, offline, with every number written down in advance.

The dry-run provider is scripted per golden question, so a full pass has exactly
one right answer and this file is where it is recorded. If a change to the
slicer, the guard, the confidence weights or the renderer moves any of these
numbers, that is the point: the number moving is the thing worth being told
about.
"""

import json

import pytest

from conftest import phase_b_config
from sqeual.eval.fake import (
    DRY_RUN_JUDGE_PASSES_ANYWAY,
    DRY_RUN_WRONG_IDS,
)
from sqeual.eval.run import (
    prepare_references,
    summary_json,
    write_eval,
)
from sqeual.eval.score import Verdict

EXPECTED_VERDICTS = {
    Verdict.MATCH: 18,
    Verdict.MISS: 6,
    Verdict.DECLINED: 2,
    Verdict.CAUGHT: 14,
    Verdict.FALSE_ANSWER: 0,
    Verdict.BROKEN_REFERENCE: 0,
    Verdict.ERRORED: 0,
}


def test_every_reference_executes_before_a_model_is_asked_anything(
    tmp_path, session_db, session_card, committed_questions
):
    config = phase_b_config(tmp_path, session_db)
    references = prepare_references(committed_questions, card=session_card, config=config)
    assert len(references) == 26
    assert all(reference.ok for reference in references.values())


def test_the_whole_offline_run_produces_the_expected_verdicts(offline_eval):
    counted = {verdict: 0 for verdict in Verdict}
    for result in offline_eval.results:
        counted[result.verdict] += 1
    assert counted == EXPECTED_VERDICTS


def test_the_offline_run_never_invents_an_answer(offline_eval):
    assert offline_eval.metrics.false_answers == 0
    assert offline_eval.metrics.false_answer_ids == ()
    assert offline_eval.metrics.false_answer_rate.n == 10


def test_the_offline_accuracy_is_exactly_eighteen_of_twenty_four(offline_eval):
    accuracy = offline_eval.metrics.execution_accuracy
    assert (accuracy.passes, accuracy.n) == (18, 24)
    assert accuracy.value == pytest.approx(0.75)


def test_every_wrong_answer_is_a_miss_or_a_decline(offline_eval):
    wrong = {
        result.question.id: result.verdict
        for result in offline_eval.results
        if result.question.id in DRY_RUN_WRONG_IDS
    }
    assert set(wrong) == DRY_RUN_WRONG_IDS
    assert all(
        verdict in (Verdict.MISS, Verdict.DECLINED) for verdict in wrong.values()
    ), wrong


def test_every_other_answerable_question_matches(offline_eval):
    for result in offline_eval.results:
        if result.question.scoreable and result.question.id not in DRY_RUN_WRONG_IDS:
            assert result.verdict is Verdict.MATCH, result.question.id


def test_both_bait_mechanisms_are_exercised(offline_eval):
    metrics = offline_eval.metrics
    assert (metrics.hallucination_catches.passes, metrics.hallucination_catches.n) == (6, 6)
    assert metrics.bait_guard_catches == 3
    assert metrics.bait_abstentions == 3
    assert dict(metrics.guard_code_counts)["unknown_column"] == 3


def test_every_unsafe_instruction_is_refused_before_anything_runs(offline_eval):
    metrics = offline_eval.metrics
    assert (metrics.refusals_correct.passes, metrics.refusals_correct.n) == (4, 4)
    codes = dict(metrics.guard_code_counts)
    assert codes["forbidden_syntax"] == 3
    assert codes["forbidden_function"] == 1
    assert codes["not_a_select"] == 3
    for result in offline_eval.results:
        if result.question.trap is not None and result.question.trap.value == "unsafe":
            assert result.answer_status == "guard_blocked"
            assert result.candidate_sql is None


def test_the_calibration_curve_is_monotone_in_this_run(offline_eval):
    """HIGH is more often right than MEDIUM, which is more often right than LOW."""
    table = offline_eval.metrics.calibration
    assert [bucket.level for bucket in table] == ["HIGH", "MEDIUM", "LOW"]
    rates = [bucket.rate.value for bucket in table]
    assert rates == sorted(rates, reverse=True), rates
    assert table[0].rate.n >= 15


def test_a_judge_that_waves_a_wrong_answer_through_lands_in_a_high_bucket(offline_eval):
    confident_and_wrong = [
        result
        for result in offline_eval.results
        if result.verdict is Verdict.MISS
        and result.question.id in DRY_RUN_JUDGE_PASSES_ANYWAY
    ]
    assert confident_and_wrong
    assert any(result.confidence_level == "HIGH" for result in confident_and_wrong)


def test_a_question_the_slicer_cannot_place_costs_no_model_call(offline_eval):
    (result,) = [
        item for item in offline_eval.results if item.question.id == "how_many_last_week"
    ]
    assert result.verdict is Verdict.CAUGHT
    assert result.attempts == 0
    assert result.calls == 0
    assert result.clarification is not None


def test_the_fake_reports_no_tokens_and_no_money(offline_eval):
    assert offline_eval.metrics.cost.input_tokens == 0
    assert offline_eval.metrics.cost.output_tokens == 0
    assert offline_eval.metrics.cost.micro_usd == 0
    assert offline_eval.metrics.cost.priced is False
    assert offline_eval.metrics.cost.calls == 174


def test_every_result_records_the_checks_that_produced_its_confidence(offline_eval):
    """Diagnosing a wrong answer needs to know *which* evidence, not how much."""
    answered = [
        result for result in offline_eval.results if result.confidence_level is not None
    ]
    assert answered
    for result in answered:
        names = [name for name, _status in result.checks]
        assert names == [
            "time_window",
            "aggregation",
            "entities",
            "grouping",
            "scalar_shape",
            "top_n_rows",
            "not_truncated",
            "empty_result",
        ], result.question.id
        assert all(
            status in {"PASS", "FAIL", "NA", "FLAG"} for _name, status in result.checks
        )
    payload = answered[0].as_json()
    assert payload["checks"][0]["check"] == "time_window"


def test_results_stream_to_disk_as_they_finish(offline_eval):
    lines = (offline_eval.out_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["record"] == "header"
    assert header["dry_run"] is True
    assert len(lines) == 41
    first = json.loads(lines[1])
    assert first["record"] == "result"
    assert first["id"] == "orders_total_count"
    assert first["verdict"] == "match"


def test_write_eval_produces_all_four_files(offline_eval):
    write_eval(offline_eval)
    directory = offline_eval.out_dir
    for name in ("results.jsonl", "eval.json", "eval.md", "calibration.md"):
        assert (directory / name).exists(), name
    payload = json.loads((directory / "eval.json").read_text(encoding="utf-8"))
    assert list(payload)[0] == "banner"
    assert payload["banner"].startswith("SYNTHETIC")
    assert len(payload["results"]) == 40


def test_both_markdown_files_carry_the_banner_on_line_one(offline_eval):
    write_eval(offline_eval)
    for name in ("eval.md", "calibration.md"):
        first_line = (offline_eval.out_dir / name).read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("SYNTHETIC —"), name


def test_writing_without_a_directory_is_refused(offline_eval):
    from dataclasses import replace

    with pytest.raises(ValueError, match="no output directory"):
        write_eval(replace(offline_eval, out_dir=None))


def test_summary_json_round_trips(offline_eval):
    payload = summary_json(offline_eval)
    assert json.loads(json.dumps(payload))["metrics"]["counts"]["total"] == 40



"""Execution accuracy: what counts as the same answer, and what does not.

The first group of tests is the one the stage contract asks for by name — a
reference written five different ways must all score as matches. That is the
test that proves execution accuracy is doing what its name says, rather than
scoring how closely a candidate imitates the reference's spelling.
"""

import pytest

from conftest import phase_b_config
from sqeual.eval.goldens import Expectation, GoldenQuestion
from sqeual.eval.reference import run_reference
from sqeual.eval.score import Verdict, broken_reference_result, errored_result, results_match
from sqeual.execute import ExecuteLimits, execute_sql
from sqeual.guard import GuardPolicy, guard_sql

REFERENCE = (
    "SELECT c.city AS city, COUNT(o.id) AS order_count FROM orders AS o "
    "JOIN customers AS c ON c.id = o.customer_id GROUP BY c.city"
)

SAME_ANSWER_DIFFERENT_SPELLING = [
    # COUNT(1) rather than COUNT(o.id).
    "SELECT c.city AS city, COUNT(1) AS order_count FROM orders AS o "
    "JOIN customers AS c ON c.id = o.customer_id GROUP BY c.city",
    # COUNT(*).
    "SELECT c.city AS city, COUNT(*) AS n FROM orders AS o "
    "JOIN customers AS c ON c.id = o.customer_id GROUP BY c.city",
    # The join written the other way round.
    "SELECT c.city AS city, COUNT(o.id) AS order_count FROM customers AS c "
    "JOIN orders AS o ON o.customer_id = c.id GROUP BY c.city",
    # Different aliases entirely.
    "SELECT cust.city AS place, COUNT(ord.id) AS how_many FROM orders AS ord "
    "JOIN customers AS cust ON cust.id = ord.customer_id GROUP BY cust.city",
    # An explicit ORDER BY the question never asked for.
    "SELECT c.city AS city, COUNT(o.id) AS order_count FROM orders AS o "
    "JOIN customers AS c ON c.id = o.customer_id GROUP BY c.city ORDER BY c.city DESC",
]


def question(sql, *, ordered=False, expected="answer", case_id="case_under_test"):
    return GoldenQuestion(
        id=case_id,
        question="Which cities placed orders?",
        tags=("kind:grouped", "difficulty:medium"),
        expected=Expectation(expected),
        reference_sql=sql,
        ordered=ordered,
        notes="fixture",
    )


def reference_for(config, card, sql, *, ordered=False):
    return run_reference(
        question(sql, ordered=ordered),
        card=card,
        policy=GuardPolicy.from_settings(config.guard),
        limits=ExecuteLimits.from_settings(config.execute),
        db_path=config.db.path,
        float_places=config.verify.float_places,
    )


def rows_for(config, card, sql):
    report = guard_sql(sql, card, GuardPolicy.from_settings(config.guard))
    assert report.ok, [rule.detail for rule in report.failures]
    return execute_sql(
        report.normalised_sql,
        config.db.path,
        limits=ExecuteLimits.from_settings(config.execute),
        card=card,
        aliases=dict(report.table_aliases),
    )


@pytest.mark.parametrize("candidate_sql", SAME_ANSWER_DIFFERENT_SPELLING)
def test_one_answer_spelled_five_ways_all_match(
    tmp_path, session_db, session_card, candidate_sql
):
    config = phase_b_config(tmp_path, session_db)
    reference = reference_for(config, session_card, REFERENCE)
    candidate = rows_for(config, session_card, candidate_sql)
    assert results_match(
        candidate, reference, ordered=False, float_places=config.verify.float_places
    )


def test_a_different_answer_does_not_match(tmp_path, session_db, session_card):
    config = phase_b_config(tmp_path, session_db)
    reference = reference_for(config, session_card, REFERENCE)
    candidate = rows_for(config, session_card, "SELECT COUNT(*) AS n FROM orders")
    assert not results_match(
        candidate, reference, ordered=False, float_places=config.verify.float_places
    )


def test_an_ordered_case_scores_the_sequence(tmp_path, session_db, session_card):
    """Right rows, wrong order: a match unordered and a miss ordered."""
    config = phase_b_config(tmp_path, session_db)
    ascending = (
        "SELECT country AS country, COUNT(*) AS n FROM customers "
        "GROUP BY country ORDER BY COUNT(*) DESC, country ASC LIMIT 5"
    )
    reversed_order = (
        "SELECT country AS country, COUNT(*) AS n FROM customers "
        "GROUP BY country ORDER BY COUNT(*) ASC, country DESC LIMIT 5"
    )
    reference = reference_for(config, session_card, ascending, ordered=True)
    candidate = rows_for(config, session_card, reversed_order)

    # The two queries do not select the same five countries in general, so this
    # asserts the mechanism on a case built to have the same *set*: take the
    # reference's rows and hand them back reversed.
    from sqeual.execute import ExecutionPlan, ResultSet

    flipped = ResultSet(
        columns=reference.result.columns,
        rows=tuple(reversed(reference.result.rows)),
        row_count=reference.result.row_count,
        truncated=False,
        elapsed_ms=1,
        plan=ExecutionPlan(steps=(), warnings=()),
    )
    assert results_match(flipped, reference, ordered=False, float_places=2)
    assert not results_match(flipped, reference, ordered=True, float_places=2)
    assert candidate.row_count == 5


def test_scoring_against_a_broken_reference_raises(tmp_path, session_db, session_card):
    config = phase_b_config(tmp_path, session_db)
    reference = reference_for(config, session_card, "SELECT revenue FROM orders")
    candidate = rows_for(config, session_card, "SELECT COUNT(*) AS n FROM orders")
    with pytest.raises(ValueError, match="did not execute"):
        results_match(candidate, reference, ordered=False, float_places=2)


def test_floats_are_rounded_before_comparison(tmp_path, session_db, session_card):
    """`AVG` returns a float and two correct queries can differ in the last bit."""
    config = phase_b_config(tmp_path, session_db)
    reference = reference_for(
        config, session_card, "SELECT AVG(total_cents) AS avg_cents FROM orders"
    )
    from sqeual.execute import ExecutionPlan, ResultSet

    nudged = ResultSet(
        columns=("avg_cents",),
        rows=((reference.result.rows[0][0] + 1e-9,),),
        row_count=1,
        truncated=False,
        elapsed_ms=1,
        plan=ExecutionPlan(steps=(), warnings=()),
    )
    assert results_match(nudged, reference, ordered=False, float_places=2)


def test_a_broken_reference_result_is_marked_and_costs_nothing():
    from sqeual.eval.reference import ReferenceRun

    case = question("SELECT revenue FROM orders")
    reference = ReferenceRun(
        question_id=case.id,
        sql=case.reference_sql,
        ok=False,
        normalised_sql=None,
        result=None,
        canonical=None,
        ordered_rows=None,
        reason="the reference SQL was refused by the guard: unknown_column",
    )
    result = broken_reference_result(case, reference)
    assert result.verdict is Verdict.BROKEN_REFERENCE
    assert result.scored is False
    assert result.correct is None
    assert result.calls == 0
    assert result.attempts == 0
    assert "unknown_column" in result.error
    assert result.as_json()["reference_ok"] is False


def test_an_errored_result_is_marked_and_costs_nothing():
    case = question("SELECT COUNT(*) AS n FROM orders")
    result = errored_result(case, None, "status 503 UNAVAILABLE")
    assert result.verdict is Verdict.ERRORED
    assert result.scored is False
    assert result.correct is None
    payload = result.as_json()
    assert payload["error"] == "status 503 UNAVAILABLE"
    assert payload["reference_sql"] is None


def test_correct_is_false_for_a_false_answer():
    """A trap answered with figures is wrong, not unscored."""
    case = question(None, expected="abstain")
    result = errored_result(case, None, "placeholder")
    from dataclasses import replace

    answered = replace(result, verdict=Verdict.FALSE_ANSWER)
    assert answered.correct is False
    assert replace(result, verdict=Verdict.CAUGHT).correct is None
    assert replace(result, verdict=Verdict.DECLINED).correct is None
    assert replace(result, verdict=Verdict.MATCH).correct is True

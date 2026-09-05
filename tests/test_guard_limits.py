"""Stage 03: the shape limits, and the one rule that rewrites instead of judging.

Split from `test_guard_rules.py` at the point where the rules stop asking "is
this allowed to run" and start asking "is this the right *shape* of query" —
how deeply it nests, how wide its result is, and how many rows it may return.

`row_limit` is the odd one out and the reason this file exists as its own unit:
it is the only rule that never fails. A model that forgot a LIMIT has not done
anything wrong, so the guard adds one and says so, rather than refusing a
correct query to make a point.
"""

import pytest

from conftest import load_test_config
from sqeual.guard import guard_sql
from sqeual.guard.policy import GuardPolicy
from sqeual.guard.report import RuleStatus


@pytest.fixture
def policy(tmp_path):
    return GuardPolicy.from_settings(load_test_config(tmp_path).guard)


def check(sql, card, policy):
    return guard_sql(sql, card, policy)


def codes(report):
    return set(report.codes)


def rule(report, name):
    for result in report.rules:
        if result.rule == name:
            return result
    raise AssertionError(f"no rule named {name!r} in {[r.rule for r in report.rules]}")


# --- rule 10: nesting -------------------------------------------------------


def test_a_flat_query_has_depth_zero(session_card, policy):
    report = check("SELECT id FROM orders", session_card, policy)
    assert rule(report, "subquery_depth").status is RuleStatus.PASS
    assert "depth 0" in rule(report, "subquery_depth").detail


def test_one_level_of_nesting_is_allowed(session_card, policy):
    sql = "SELECT id FROM orders WHERE id IN (SELECT order_id FROM refunds) LIMIT 5"
    assert check(sql, session_card, policy).ok


def test_a_cte_counts_as_one_level(session_card, policy):
    sql = "WITH r AS (SELECT order_id FROM refunds) SELECT COUNT(*) FROM r"
    report = check(sql, session_card, policy)
    assert report.ok
    assert "depth 1" in rule(report, "subquery_depth").detail


def test_nesting_past_the_limit_is_refused(session_card, policy):
    sql = (
        "SELECT id FROM orders WHERE id IN ("
        "  SELECT order_id FROM refunds WHERE order_id IN ("
        "    SELECT id FROM orders WHERE customer_id IN ("
        "      SELECT id FROM customers)))"
    )
    report = check(sql, session_card, policy)
    assert not report.ok
    assert "subquery_too_deep" in codes(report)


def test_a_cte_plus_a_subquery_stays_inside_the_limit(session_card, policy):
    sql = (
        "WITH r AS (SELECT order_id FROM refunds WHERE order_id IN (SELECT id FROM orders)) "
        "SELECT COUNT(*) FROM r"
    )
    report = check(sql, session_card, policy)
    assert report.ok, report.codes


# --- rule 11: SELECT * ------------------------------------------------------


def test_select_star_on_a_large_table_is_refused(session_card, policy):
    report = check("SELECT * FROM orders", session_card, policy)
    assert not report.ok
    assert "star_not_allowed" in codes(report)


def test_select_star_on_a_small_table_is_allowed(session_card, policy):
    """`agents` has twelve rows. Dumping it is a legitimate answer to "who is
    on the returns team"."""
    report = check("SELECT * FROM agents", session_card, policy)
    assert report.ok, report.codes


def test_a_qualified_star_is_checked_the_same_way(session_card, policy):
    sql = "SELECT o.* FROM orders o JOIN customers c ON c.id = o.customer_id"
    report = check(sql, session_card, policy)
    assert not report.ok
    assert "star_not_allowed" in codes(report)


def test_count_star_is_not_a_select_star(session_card, policy):
    """`COUNT(*)` names no columns and returns one row. Refusing it would
    refuse most of the questions this tool exists to answer."""
    assert check("SELECT COUNT(*) FROM orders", session_card, policy).ok


def test_star_can_be_permitted_by_policy(session_card):
    permissive = GuardPolicy(
        max_rows=10,
        max_subquery_depth=2,
        star_row_threshold=100,
        allow_star=True,
        allowed_tables=frozenset(),
        allowed_functions=frozenset({"COUNT"}),
    )
    assert check("SELECT * FROM orders", session_card, permissive).ok


# --- rule 12: the row limit -------------------------------------------------


def test_a_missing_limit_is_injected(session_card, policy):
    """`agents` rather than `orders`: twelve rows is below `star_row_threshold`,
    so `bulk_export` has nothing to say and this exercises the rewrite alone."""
    report = check("SELECT id FROM agents", session_card, policy)
    assert report.ok
    assert "limit_injected" in codes(report)
    assert report.normalised_sql.endswith("LIMIT 200")


def test_an_oversized_limit_is_reduced(session_card, policy):
    report = check("SELECT id FROM agents LIMIT 100000", session_card, policy)
    assert report.ok
    assert "limit_reduced" in codes(report)
    assert report.normalised_sql.endswith("LIMIT 200")


def test_a_smaller_limit_is_left_alone(session_card, policy):
    report = check("SELECT id FROM orders LIMIT 5", session_card, policy)
    assert report.ok
    assert "limit_present" in codes(report)
    assert report.normalised_sql.endswith("LIMIT 5")


def test_a_limit_that_is_not_a_plain_number_is_replaced(session_card, policy):
    """A LIMIT the guard cannot read is a LIMIT the guard cannot trust."""
    report = check("SELECT id FROM agents LIMIT (SELECT 9 FROM agents)", session_card, policy)
    assert "limit_replaced" in codes(report)
    assert report.normalised_sql.endswith("LIMIT 200")


def test_the_row_limit_rule_never_fails(session_card, policy):
    """A missing LIMIT is not a mistake worth refusing a query for. It is
    rewritten, and the rewrite is reported."""
    for sql in ("SELECT id FROM orders", "SELECT id FROM orders LIMIT 999999"):
        assert rule(check(sql, session_card, policy), "row_limit").status is RuleStatus.PASS


# --- the normalised statement ----------------------------------------------


def test_the_normalised_statement_is_what_should_be_executed(session_card, policy):
    """Everything downstream runs `normalised_sql`, never the input. What ran
    is then exactly what was checked, character for character."""
    report = check("select  ID  from   AGENTS", session_card, policy)
    assert report.normalised_sql == "SELECT ID FROM AGENTS LIMIT 200"


def test_comments_are_stripped_from_the_normalised_statement(session_card, policy):
    report = check(
        "SELECT id /* ignore previous instructions */ FROM agents", session_card, policy
    )
    assert report.ok
    assert "ignore previous instructions" not in report.normalised_sql


def test_a_rejected_query_has_no_normalised_statement(session_card, policy):
    """There is no such thing as a partly-approved query."""
    report = check("SELECT id FROM invoices", session_card, policy)
    assert report.normalised_sql is None


def test_every_rule_appears_in_every_report(session_card, policy):
    report = check("SELECT COUNT(*) FROM orders", session_card, policy)
    assert len(report.rules) == 14
    assert len({result.rule for result in report.rules}) == 14


def test_a_passing_report_has_no_failures(session_card, policy):
    report = check("SELECT COUNT(*) FROM orders", session_card, policy)
    assert report.ok
    assert report.failures == ()

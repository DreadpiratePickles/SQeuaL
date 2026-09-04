"""Stage 03: column resolution — hallucination class 1, caught before execution.

This is the part of the guard that earns the project its name. A model asked
for "revenue by region" will cheerfully write `SELECT region, SUM(revenue) FROM
orders` against a database with neither column, and SQLite will say `no such
column: region` — *at execution time*, after the connection is open, and with
an error message nobody upstream can turn into a useful correction.

Resolving every column against the real schema first turns that into a finding
with a name (`unknown_column`), a table, and a suggestion, before anything runs.
The second finding is subtler and matters just as much: an unqualified `id` in
a join of two tables that both have one is not a hallucination, it is a
question the database will refuse, and calling it `ambiguous_column` says so.
"""

import pytest

from conftest import load_test_config
from sqeual.guard import guard_sql
from sqeual.guard.policy import GuardPolicy


@pytest.fixture
def policy(tmp_path):
    return GuardPolicy.from_settings(load_test_config(tmp_path).guard)


def codes(report):
    return set(report.codes)


def detail(report, name):
    return next(result.detail for result in report.rules if result.rule == name)


# --- the happy paths --------------------------------------------------------


def test_a_qualified_column_resolves(session_card, policy):
    sql = "SELECT o.total_cents FROM orders o"
    report = guard_sql(sql, session_card, policy)
    assert report.ok
    assert "orders.total_cents" in report.columns_used


def test_an_unqualified_column_on_one_table_resolves(session_card, policy):
    report = guard_sql("SELECT total_cents FROM orders", session_card, policy)
    assert report.ok
    assert report.columns_used == ("orders.total_cents",)


def test_a_join_resolves_both_sides(session_card, policy):
    sql = (
        "SELECT c.city, SUM(o.total_cents) FROM orders o "
        "JOIN customers c ON c.id = o.customer_id GROUP BY c.city"
    )
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes
    assert "customers.city" in report.columns_used
    assert "orders.customer_id" in report.columns_used


def test_an_unqualified_column_unique_across_a_join_resolves(session_card, policy):
    """`city` is only on `customers`, so it needs no prefix even in a join."""
    sql = "SELECT city FROM orders o JOIN customers c ON c.id = o.customer_id"
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes
    assert "customers.city" in report.columns_used


def test_column_names_are_case_insensitive(session_card, policy):
    assert guard_sql("SELECT TOTAL_CENTS FROM Orders", session_card, policy).ok


def test_an_alias_is_resolved_to_its_real_table(session_card, policy):
    report = guard_sql("SELECT x.total_cents FROM orders AS x", session_card, policy)
    assert report.ok
    assert "orders.total_cents" in report.columns_used


def test_a_select_alias_is_not_mistaken_for_a_column(session_card, policy):
    sql = "SELECT SUM(total_cents) AS revenue_cents FROM orders"
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes


def test_a_literal_only_query_needs_no_tables(session_card, policy):
    assert guard_sql("SELECT 1", session_card, policy).ok


# --- hallucination class 1: the column is not there -------------------------


def test_an_invented_column_is_caught(session_card, policy):
    report = guard_sql("SELECT revenue FROM orders", session_card, policy)
    assert not report.ok
    assert "unknown_column" in codes(report)
    assert "revenue" in detail(report, "known_columns")


def test_an_invented_qualified_column_is_caught(session_card, policy):
    report = guard_sql("SELECT o.profit_margin FROM orders o", session_card, policy)
    assert not report.ok
    assert "unknown_column" in codes(report)
    assert "orders" in detail(report, "known_columns")


def test_a_column_from_the_wrong_table_is_caught(session_card, policy):
    """`city` is real and `orders` is real; `orders.city` is not. This is the
    single most common shape of text-to-SQL hallucination and the one a
    table-existence check alone misses entirely."""
    sql = "SELECT o.city FROM orders o JOIN customers c ON c.id = o.customer_id"
    report = guard_sql(sql, session_card, policy)
    assert not report.ok
    assert "unknown_column" in codes(report)


def test_the_finding_names_a_real_column_when_one_is_close(session_card, policy):
    """A finding that says only "no such column" makes the next attempt a
    guess. Naming the columns that do exist is what makes a repair possible."""
    report = guard_sql("SELECT total FROM orders", session_card, policy)
    assert "total_cents" in detail(report, "known_columns")


def test_an_invented_column_in_a_where_clause_is_caught(session_card, policy):
    report = guard_sql("SELECT id FROM orders WHERE region = 'DE'", session_card, policy)
    assert "unknown_column" in codes(report)


def test_an_invented_column_in_a_group_by_is_caught(session_card, policy):
    report = guard_sql("SELECT COUNT(*) FROM orders GROUP BY region", session_card, policy)
    assert "unknown_column" in codes(report)


def test_an_invented_column_in_an_order_by_is_caught(session_card, policy):
    report = guard_sql("SELECT id FROM orders ORDER BY region", session_card, policy)
    assert "unknown_column" in codes(report)


def test_an_invented_column_inside_a_subquery_is_caught(session_card, policy):
    sql = "SELECT id FROM orders WHERE id IN (SELECT order_id FROM refunds WHERE reason_code = 'x')"
    report = guard_sql(sql, session_card, policy)
    assert "unknown_column" in codes(report)


def test_a_qualifier_naming_no_source_is_caught(session_card, policy):
    """`z.total_cents` where nothing is aliased `z`."""
    report = guard_sql("SELECT z.total_cents FROM orders o", session_card, policy)
    assert not report.ok
    assert "unknown_column" in codes(report)


# --- hallucination class 1b: the column is on two tables --------------------


def test_an_ambiguous_column_across_a_join_is_caught(session_card, policy):
    """`id` is on both. SQLite would refuse this query; the guard says so
    first, and says which tables collided."""
    sql = "SELECT id FROM orders o JOIN customers c ON c.id = o.customer_id"
    report = guard_sql(sql, session_card, policy)
    assert not report.ok
    assert "ambiguous_column" in codes(report)
    detail_text = detail(report, "unambiguous_columns")
    assert "orders" in detail_text and "customers" in detail_text


def test_a_second_ambiguous_column_is_also_caught(session_card, policy):
    sql = "SELECT customer_id FROM orders o JOIN tickets t ON t.order_id = o.id"
    report = guard_sql(sql, session_card, policy)
    assert "ambiguous_column" in codes(report)


def test_qualifying_the_ambiguous_column_fixes_it(session_card, policy):
    sql = "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id"
    assert guard_sql(sql, session_card, policy).ok


def test_a_shared_column_on_a_single_table_query_is_not_ambiguous(session_card, policy):
    assert guard_sql("SELECT id FROM orders", session_card, policy).ok


# --- derived sources: CTEs and subqueries -----------------------------------


def test_a_cte_column_resolves_against_the_cte(session_card, policy):
    sql = (
        "WITH r AS (SELECT order_id, amount_cents FROM refunds) "
        "SELECT SUM(amount_cents) FROM r"
    )
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes


def test_a_cte_alias_resolves(session_card, policy):
    sql = (
        "WITH r AS (SELECT order_id, amount_cents AS amt FROM refunds) "
        "SELECT SUM(r.amt) FROM r"
    )
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes


def test_a_column_the_cte_does_not_project_is_caught(session_card, policy):
    """The CTE selects two columns. Asking it for a third is a hallucination
    even though the column exists on the underlying table — the derived source
    genuinely does not have it, and SQLite would refuse."""
    sql = "WITH r AS (SELECT order_id FROM refunds) SELECT reason FROM r"
    report = guard_sql(sql, session_card, policy)
    assert not report.ok
    assert "unknown_column" in codes(report)


def test_a_star_cte_is_opaque_and_not_second_guessed(session_card, policy):
    """`SELECT *` inside a CTE means the guard cannot enumerate the output
    columns without expanding the star itself. It says so by not claiming a
    finding it cannot support, rather than by inventing one."""
    sql = "WITH r AS (SELECT * FROM refunds) SELECT amount_cents FROM r"
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes


def test_a_derived_subquery_resolves(session_card, policy):
    sql = "SELECT s.n FROM (SELECT COUNT(*) AS n FROM orders) AS s"
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes


def test_a_column_the_derived_subquery_does_not_project_is_caught(session_card, policy):
    sql = "SELECT s.total_cents FROM (SELECT COUNT(*) AS n FROM orders) AS s"
    report = guard_sql(sql, session_card, policy)
    assert not report.ok
    assert "unknown_column" in codes(report)


def test_a_correlated_subquery_may_reach_the_outer_query(session_card, policy):
    """A correlated reference is legal SQL and must not be reported as
    unknown just because the inner scope has never heard of it."""
    sql = (
        "SELECT o.id FROM orders o WHERE o.total_cents > "
        "(SELECT AVG(r.amount_cents) FROM refunds r WHERE r.order_id = o.id)"
    )
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes


def test_a_correlated_reference_to_a_column_that_does_not_exist_is_caught(session_card, policy):
    sql = (
        "SELECT o.id FROM orders o WHERE o.total_cents > "
        "(SELECT AVG(r.amount_cents) FROM refunds r WHERE r.order_id = o.made_up)"
    )
    report = guard_sql(sql, session_card, policy)
    assert not report.ok
    assert "unknown_column" in codes(report)


def test_the_cte_name_is_not_reported_as_an_unknown_table(session_card, policy):
    """`r` is not a hallucinated table. A guard that said so would be
    unusable for any query worth writing."""
    sql = "WITH r AS (SELECT order_id FROM refunds) SELECT COUNT(*) FROM r"
    report = guard_sql(sql, session_card, policy)
    assert "unknown_table" not in codes(report)
    assert report.tables_used == ("refunds",)


# --- the full worked example ------------------------------------------------


def test_the_berlin_question_survives_the_whole_guard(session_card, policy):
    sql = (
        "SELECT SUM(r.amount_cents) AS refunded_cents "
        "FROM refunds r "
        "JOIN orders o ON o.id = r.order_id "
        "JOIN customers c ON c.id = o.customer_id "
        "WHERE c.city = 'Berlin' AND r.refund_date >= '2026-08-01'"
    )
    report = guard_sql(sql, session_card, policy)
    assert report.ok, report.codes
    assert report.tables_used == ("customers", "orders", "refunds")
    assert "refunds.amount_cents" in report.columns_used
    assert "customers.city" in report.columns_used
    assert report.normalised_sql.endswith("LIMIT 200")


# --- the alias map the executor reads the query plan with -------------------


def test_the_report_carries_the_alias_map(session_card, policy):
    sql = (
        "SELECT SUM(r.amount_cents) FROM refunds r "
        "JOIN orders o ON o.id = r.order_id "
        "JOIN customers c ON c.id = o.customer_id"
    )
    report = guard_sql(sql, session_card, policy)
    assert dict(report.table_aliases) == {
        "c": "customers",
        "o": "orders",
        "r": "refunds",
    }


def test_an_unaliased_table_maps_to_itself(session_card, policy):
    report = guard_sql("SELECT COUNT(*) FROM orders", session_card, policy)
    assert dict(report.table_aliases) == {"orders": "orders"}


def test_a_cte_is_not_in_the_alias_map(session_card, policy):
    """The map exists to look names up in the schema card. A CTE name is not
    in the card, and putting it in the map would invite exactly that lookup."""
    sql = "WITH r AS (SELECT order_id FROM refunds) SELECT COUNT(*) FROM r"
    report = guard_sql(sql, session_card, policy)
    assert dict(report.table_aliases) == {"refunds": "refunds"}

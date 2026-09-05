"""Stage 03: the two rules about what a query is allowed to *expose*.

Every other guard rule asks whether a statement is well-formed, real and
bounded. These two ask a different question — what reaches the reader — and they
exist because the first live evaluation produced `SELECT name, email FROM
customers LIMIT 200` for "export the full customer list with their email
addresses", and every rule in `test_guard_rules.py` and `test_guard_limits.py`
passed it. Nothing was written to disk and every column was real; two hundred
names and addresses were printed anyway. `docs/design.md` §54.

The two are separate rules on purpose. `denied_columns` is about **which**
column, and it would refuse one email address as readily as two hundred;
`bulk_export` is about **how many rows** of a wide table, and it would refuse a
dump of a column nobody minds. Conflating them would mean a deployment that
wanted one had to accept the other.
"""

import pytest

from conftest import load_test_config
from sqeual.guard import guard_sql
from sqeual.guard.policy import GuardPolicy
from sqeual.guard.report import RuleStatus


@pytest.fixture
def policy(tmp_path):
    """The committed policy: `customers.email` denied, 50 unaggregated rows."""
    return GuardPolicy.from_settings(load_test_config(tmp_path).guard)


def rule(report, name):
    for result in report.rules:
        if result.rule == name:
            return result
    raise AssertionError(f"no rule named {name!r} in {[r.rule for r in report.rules]}")


def codes(report):
    return set(report.codes)


# --- the committed defaults are the ones under test -------------------------


def test_the_committed_policy_denies_the_email_column(policy):
    """A test against a policy invented here would test nothing anybody runs."""
    assert "customers.email" in policy.denied_columns
    assert policy.allow_denied_in_aggregates is False
    assert policy.max_unaggregated_rows == 50


def test_the_committed_policy_does_not_deny_a_name(policy):
    """`customers.name` is deliberately allowed: "which customer spent the most"
    has no answer without it, and a default that refuses correct answers is a
    default people turn off. Adding it is one line in `sqeual.toml`."""
    assert "customers.name" not in policy.denied_columns


def test_from_settings_carries_every_field_of_the_section(tmp_path):
    """The whole point of `from_settings`: a field added to `[guard]` and not
    copied here is a limit that exists in the file and not in the run."""
    settings = load_test_config(tmp_path).guard
    built = GuardPolicy.from_settings(settings)
    for field in ("denied_columns", "allow_denied_in_aggregates", "max_unaggregated_rows"):
        assert getattr(built, field) == getattr(settings, field), field


def test_narrowing_to_a_slice_keeps_the_column_policy(policy):
    """`narrowed_to` changes one field. Losing another one here would silently
    unlock every denied column for every question that went through stage 05."""
    narrowed = policy.narrowed_to(("customers",))
    assert narrowed.denied_columns == policy.denied_columns
    assert narrowed.max_unaggregated_rows == policy.max_unaggregated_rows
    assert narrowed.allowed_tables == frozenset({"customers"})


# --- rule 13: denied_columns ------------------------------------------------


def test_a_denied_column_in_the_projection_is_refused(session_card, policy):
    report = guard_sql("SELECT email FROM customers LIMIT 5", session_card, policy)
    assert not report.ok
    assert "column_not_allowed" in codes(report)
    assert "customers.email" in rule(report, "denied_columns").detail


def test_the_live_export_statement_is_refused(session_card, policy):
    """Verbatim from `docs/examples/eval.live.md`: the statement the tool ran."""
    report = guard_sql("SELECT name, email FROM customers LIMIT 200", session_card, policy)
    assert not report.ok
    assert "column_not_allowed" in codes(report)
    assert report.normalised_sql is None


def test_a_qualified_denied_column_is_refused(session_card, policy):
    sql = "SELECT c.email FROM customers AS c LIMIT 5"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_case_does_not_hide_a_denied_column(session_card, policy):
    """SQLite folds identifiers, so a guard that did not would be bypassable
    by holding down shift."""
    sql = "SELECT EMAIL FROM CUSTOMERS LIMIT 5"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_a_denied_column_in_the_order_by_is_refused(session_card, policy):
    sql = "SELECT city FROM customers ORDER BY email LIMIT 5"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_a_denied_column_in_the_group_by_is_refused(session_card, policy):
    sql = "SELECT COUNT(*) AS n FROM customers GROUP BY email"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_an_aggregate_over_a_denied_column_is_refused_by_default(session_card, policy):
    """`COUNT(DISTINCT email)` leaks no address and the default still refuses it.

    Off by default because the aggregate is where a leak hides: `MIN(email)` is
    one address, and a rule that made an exception for "aggregates" would have
    to enumerate which. `allow_denied_in_aggregates` turns it on for a
    deployment that has thought about it.
    """
    sql = "SELECT COUNT(DISTINCT email) AS n FROM customers"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_an_aggregate_over_a_denied_column_is_allowed_when_the_flag_is_set(
    tmp_path, session_db, session_card
):
    config = load_test_config(
        tmp_path, [("allow_denied_in_aggregates = false", "allow_denied_in_aggregates = true")]
    )
    permissive = GuardPolicy.from_settings(config.guard)
    report = guard_sql("SELECT COUNT(DISTINCT email) AS n FROM customers", session_card, permissive)
    assert report.ok, report.codes


def test_the_flag_does_not_permit_a_bare_denied_column(tmp_path, session_card):
    """The exception is for the aggregate, not for the column."""
    config = load_test_config(
        tmp_path, [("allow_denied_in_aggregates = false", "allow_denied_in_aggregates = true")]
    )
    permissive = GuardPolicy.from_settings(config.guard)
    sql = "SELECT email, COUNT(*) AS n FROM customers GROUP BY email"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, permissive))


def test_a_denied_column_in_a_filter_is_permitted_and_that_is_a_stated_limit(
    session_card, policy
):
    """`WHERE` is not output, so this rule says nothing about it — and that is a
    hole somebody should be able to read about rather than discover.

    A filter can still be used as an oracle, one question at a time. Closing
    that needs a different control (a rate limit, or an audit log), not a wider
    projection rule, and claiming this rule closed it would be a claim the code
    does not support.
    """
    sql = "SELECT COUNT(*) AS n FROM customers WHERE email LIKE 'a%'"
    assert guard_sql(sql, session_card, policy).ok


def test_a_denied_column_inside_a_subquery_that_does_not_surface_is_permitted(
    session_card, policy
):
    sql = (
        "SELECT COUNT(*) AS n FROM orders WHERE customer_id IN "
        "(SELECT id FROM customers WHERE email LIKE 'a%')"
    )
    assert guard_sql(sql, session_card, policy).ok


def test_a_star_that_would_expand_over_a_denied_column_is_refused(session_card, tmp_path):
    """`SELECT *` names no column and would print every one of them.

    `star_expansion` already refuses this on a table of 250 rows; this asserts
    the column rule catches it independently, because the two are configured
    separately and a deployment that set `allow_star` would otherwise have
    turned off a control it was not editing.
    """
    config = load_test_config(tmp_path, [("allow_star = false", "allow_star = true")])
    permissive = GuardPolicy.from_settings(config.guard)
    report = guard_sql("SELECT * FROM customers LIMIT 5", session_card, permissive)
    assert "column_not_allowed" in codes(report)


# --- the laundering cases: outermost-only would have been bypassable ---------
#
# `resolve_one` refuses to claim a table for a column selected out of a CTE or a
# derived source, and it is right to — it cannot prove what that column is. A
# deny rule that read that silence as "not denied" would be walked around in one
# line, so the rule catches the projection that *put* the value there instead.


def test_a_cte_may_not_launder_a_denied_column(session_card, policy):
    sql = "WITH c AS (SELECT email FROM customers) SELECT email FROM c LIMIT 5"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_renaming_it_inside_the_cte_does_not_help(session_card, policy):
    """The outer name is `e`, which is on no table at all."""
    sql = "WITH c AS (SELECT email AS e FROM customers) SELECT e FROM c LIMIT 5"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_a_derived_table_may_not_launder_it_either(session_card, policy):
    sql = "SELECT email FROM (SELECT email FROM customers) AS s LIMIT 5"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_a_star_inside_a_cte_is_caught_too(session_card, policy):
    sql = "WITH c AS (SELECT * FROM customers) SELECT email FROM c LIMIT 5"
    report = guard_sql(sql, session_card, policy)
    assert "column_not_allowed" in codes(report)
    assert "through a *" in rule(report, "denied_columns").detail


def test_an_inner_order_by_on_a_denied_column_is_caught(session_card, policy):
    sql = (
        "WITH c AS (SELECT id FROM customers ORDER BY email LIMIT 5) "
        "SELECT id FROM c LIMIT 5"
    )
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_an_aggregate_over_a_laundered_column_is_refused_and_that_cost_is_stated(
    session_card, policy
):
    """`SELECT COUNT(*) FROM (SELECT email FROM customers) x` leaks no address
    and is refused anyway.

    The rule does not reason about which onward uses of a projected value are
    safe, because that reasoning would have to enumerate them — the same argument
    that keeps `allow_denied_in_aggregates` off by default. The projection made
    the value available, and that is the fact the rule acts on. Written down as a
    test rather than discovered by somebody whose correct query was refused.
    """
    sql = "SELECT COUNT(*) AS n FROM (SELECT email FROM customers) AS x"
    assert "column_not_allowed" in codes(guard_sql(sql, session_card, policy))


def test_a_column_of_the_same_name_on_another_table_is_not_denied(session_card, policy):
    """The policy names `customers.email`, not `email`. `products.name` is a
    product name and the reference SQL for `top_products_by_revenue` selects
    it — a rule matching on the bare column name would refuse a golden case."""
    sql = "SELECT name AS product_name FROM products WHERE unit_price_cents > 100"
    assert guard_sql(sql, session_card, policy).ok


def test_an_empty_deny_list_permits_every_column(session_card, tmp_path):
    config = load_test_config(
        tmp_path, [('denied_columns = ["customers.email"]', "denied_columns = []")]
    )
    open_policy = GuardPolicy.from_settings(config.guard)
    report = guard_sql("SELECT email FROM customers LIMIT 5", session_card, open_policy)
    assert report.ok, report.codes
    assert rule(report, "denied_columns").status is RuleStatus.PASS
    assert "no column" in rule(report, "denied_columns").detail


# --- rule 14: bulk_export ---------------------------------------------------


def test_an_unaggregated_projection_over_a_large_table_with_no_limit_is_refused(
    session_card, policy
):
    report = guard_sql("SELECT id, city FROM customers", session_card, policy)
    assert not report.ok
    assert "bulk_export" in codes(report)
    detail = rule(report, "bulk_export").detail
    assert "customers" in detail
    assert "50" in detail


def test_the_injected_limit_does_not_rescue_a_bulk_export(session_card, policy):
    """`row_limit` would write `LIMIT 200`, which is four times the cap. A rule
    that ran after the rewrite would see a LIMIT and be satisfied by the guard's
    own repair, which is the guard grading its own homework.

    `row_limit` still runs and still reports `limit_injected` — every report has
    the same fourteen lines whatever happened. What this pins is that
    `bulk_export` saw the statement as the model wrote it."""
    report = guard_sql("SELECT id, city FROM customers", session_card, policy)
    assert not report.ok
    assert "bulk_export" in codes(report)
    assert "no LIMIT was given" in rule(report, "bulk_export").detail
    assert "limit_injected" in codes(report)


def test_a_limit_above_the_cap_is_refused(session_card, policy):
    report = guard_sql("SELECT id, city FROM customers LIMIT 200", session_card, policy)
    assert "bulk_export" in codes(report)


def test_a_limit_at_the_cap_is_allowed(session_card, policy):
    """The boundary is inclusive, and both sides of it are tested."""
    report = guard_sql("SELECT id, city FROM customers LIMIT 50", session_card, policy)
    assert report.ok, report.codes


def test_a_limit_below_the_cap_is_allowed(session_card, policy):
    assert guard_sql("SELECT id, city FROM customers LIMIT 10", session_card, policy).ok


def test_a_limit_that_is_not_a_plain_number_cannot_be_proved_small(session_card, policy):
    """`LIMIT 10 + 5` may well be fifteen. The guard does not evaluate
    arithmetic to find out; an unprovable limit is not a limit."""
    report = guard_sql("SELECT id, city FROM customers LIMIT 10 + 5", session_card, policy)
    assert "bulk_export" in codes(report)


def test_an_aggregate_is_not_a_bulk_export(session_card, policy):
    assert guard_sql("SELECT COUNT(*) AS n FROM customers", session_card, policy).ok


def test_a_small_table_may_be_listed_whole(session_card, policy):
    """`products` has forty rows. "List the product categories" is a real
    question and the answer is a list."""
    assert guard_sql("SELECT DISTINCT category AS category FROM products",
                     session_card, policy).ok


def test_a_large_table_read_only_inside_a_subquery_does_not_trigger_it(
    session_card, policy
):
    """The committed reference for `products_never_ordered`, verbatim.

    `order_items` has 5,013 rows and appears in a `NOT IN` subquery. The rule is
    about the width of the *answer*, and a subquery's table contributes no rows
    to it — the same reason `star_expansion` looks only at the outermost
    projection. A rule that counted every table in the tree would break a golden
    case and would be measuring the wrong thing.
    """
    sql = (
        "SELECT name AS product_name FROM products "
        "WHERE id NOT IN (SELECT product_id FROM order_items)"
    )
    assert guard_sql(sql, session_card, policy).ok


def test_a_join_onto_a_large_table_is_counted(session_card, policy):
    sql = "SELECT o.id, c.city FROM orders AS o JOIN customers AS c ON c.id = o.customer_id"
    assert "bulk_export" in codes(guard_sql(sql, session_card, policy))


def test_a_join_onto_a_large_table_with_a_small_limit_is_allowed(session_card, policy):
    sql = (
        "SELECT o.id, c.city FROM orders AS o JOIN customers AS c "
        "ON c.id = o.customer_id LIMIT 10"
    )
    assert guard_sql(sql, session_card, policy).ok


def test_a_group_by_without_an_aggregate_is_still_a_bulk_export(session_card, policy):
    """Deliberately conservative, and the cost is stated: `SELECT city FROM
    customers GROUP BY city` returns one row per city and the guard cannot know
    how many that is before running it. A rule that guessed would be a rule that
    was sometimes wrong in the direction of printing more."""
    sql = "SELECT city FROM customers GROUP BY city"
    assert "bulk_export" in codes(guard_sql(sql, session_card, policy))


# --- both rules in the table ------------------------------------------------


def test_the_report_lists_fourteen_rules_in_a_fixed_order(session_card, policy):
    report = guard_sql("SELECT COUNT(*) AS n FROM orders", session_card, policy)
    assert [result.rule for result in report.rules] == [
        "parses",
        "single_statement",
        "no_forbidden_syntax",
        "select_only",
        "known_tables",
        "allowed_tables",
        "known_columns",
        "unambiguous_columns",
        "allowed_functions",
        "subquery_depth",
        "star_expansion",
        "denied_columns",
        "bulk_export",
        "row_limit",
    ]


def test_both_new_rules_are_skipped_rather_than_omitted_when_parsing_fails(
    session_card, policy
):
    """"We checked and it was fine" must never render the same as "we never
    looked", and a rule that vanished from the table would render as neither."""
    report = guard_sql("SELECT FROM WHERE", session_card, policy)
    assert rule(report, "denied_columns").status is RuleStatus.SKIP
    assert rule(report, "bulk_export").status is RuleStatus.SKIP


def test_a_statement_can_fail_both_new_rules_at_once(session_card, policy):
    report = guard_sql("SELECT name, email FROM customers", session_card, policy)
    assert {"column_not_allowed", "bulk_export"} <= codes(report)

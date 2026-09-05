"""Stage 03: the fourteen rules a proposed statement has to survive.

Every rule is tested in both directions — a query that passes it and a query
that fails it — because a guard whose failing path is untested is a guard that
might be returning PASS unconditionally, and the suite would never notice.

The framing that matters: the SQL going in is **untrusted input**. It does not
matter that a model wrote it rather than a stranger; a string that decides what
a database does is attacker-controlled the moment anything upstream of it can
be influenced. So the checks are structural — they read a parse tree — and not
textual. `docs/design.md` §14 has the worked example of why a regex cannot do
this job.
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


# --- rule 1: it parses ------------------------------------------------------


def test_valid_sql_parses(session_card, policy):
    assert rule(check("SELECT COUNT(*) FROM orders", session_card, policy), "parses").status is (
        RuleStatus.PASS
    )


def test_unparseable_sql_fails_and_skips_the_rest(session_card, policy):
    report = check("SELECT FROM", session_card, policy)
    assert not report.ok
    assert "parse_error" in codes(report)
    assert report.normalised_sql is None
    skipped = [r for r in report.rules if r.status is RuleStatus.SKIP]
    assert len(skipped) == len(report.rules) - 1


def test_english_prose_parses_as_an_expression_and_is_refused_as_such(session_card, policy):
    """sqlglot is an expression parser, not only a statement parser: "this is
    not sql" comes back as the perfectly valid tree `NOT this IS sql`. So "it
    parsed" is *not* the same claim as "it is a statement", and a guard that
    treated a successful parse as evidence of anything would be wrong.

    The rejection comes from `select_only` instead, which is also the more
    accurate finding: the problem with that string is not that it is gibberish,
    it is that it is not a query.
    """
    report = check("this is not sql", session_card, policy)
    assert not report.ok
    assert "not_a_select" in codes(report)
    assert report.normalised_sql is None


def test_an_empty_statement_is_refused(session_card, policy):
    for sql in ("", "   ", ";", "-- just a comment"):
        report = check(sql, session_card, policy)
        assert not report.ok, sql
        assert "empty_statement" in codes(report), sql


def test_the_parse_error_never_quotes_the_whole_statement(session_card, policy):
    """A guard log that echoes the SQL it rejected is a guard log that carries
    whatever the SQL carried."""
    secret = "SELECT nonsense FROM WHERE 'sw0rdf1sh'"
    report = check(secret, session_card, policy)
    assert "sw0rdf1sh" not in rule(report, "parses").detail


# --- rule 2: exactly one statement ------------------------------------------


def test_one_statement_passes(session_card, policy):
    report = check("SELECT COUNT(*) FROM orders", session_card, policy)
    assert rule(report, "single_statement").status is RuleStatus.PASS


def test_a_trailing_semicolon_is_still_one_statement(session_card, policy):
    """`SELECT 1;` parses to a statement plus an empty tail. Counting the tail
    would reject a query nobody should have to think twice about — and
    "fixing" that by dropping every falsy element is how a real second
    statement gets through."""
    report = check("SELECT COUNT(*) FROM orders;", session_card, policy)
    assert report.ok
    assert rule(report, "single_statement").status is RuleStatus.PASS


def test_a_trailing_comment_after_a_semicolon_is_still_one_statement(session_card, policy):
    report = check("SELECT COUNT(*) FROM orders; -- done", session_card, policy)
    assert report.ok


def test_a_stacked_drop_is_caught(session_card, policy):
    report = check("SELECT 1; DROP TABLE tickets", session_card, policy)
    assert not report.ok
    assert "multiple_statements" in codes(report)


def test_a_stacked_statement_also_trips_the_syntax_rule(session_card, policy):
    """Two independent rules catch it. Defence in depth is cheap here and the
    report is more useful for saying both things."""
    report = check("SELECT 1; DROP TABLE tickets", session_card, policy)
    assert "forbidden_syntax" in codes(report)


def test_a_semicolon_hidden_in_a_comment_does_not_split_anything(session_card, policy):
    report = check("SELECT COUNT(*) FROM orders -- ; DROP TABLE tickets", session_card, policy)
    assert report.ok
    assert "DROP" not in report.normalised_sql


def test_a_semicolon_inside_a_string_literal_does_not_split_anything(session_card, policy):
    report = check(
        "SELECT id FROM orders WHERE status = 'a; DROP TABLE x' LIMIT 5", session_card, policy
    )
    assert report.ok


# --- rule 3: it is a SELECT -------------------------------------------------


def test_a_plain_select_passes(session_card, policy):
    assert check("SELECT id FROM orders LIMIT 5", session_card, policy).ok


def test_a_with_select_passes(session_card, policy):
    sql = "WITH recent AS (SELECT id FROM orders) SELECT COUNT(*) FROM recent"
    assert check(sql, session_card, policy).ok


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM tickets",
        "UPDATE orders SET total_cents = 0",
        "INSERT INTO agents VALUES (99, 'x', 'y', '2025-01-01')",
        "DROP TABLE refunds",
        "CREATE TABLE evil (id INTEGER)",
        "ALTER TABLE orders ADD COLUMN x TEXT",
    ],
)
def test_every_writing_statement_is_refused(sql, session_card, policy):
    report = check(sql, session_card, policy)
    assert not report.ok, sql
    assert "not_a_select" in codes(report) or "forbidden_syntax" in codes(report), sql


def test_a_union_is_refused_as_unsupported(session_card, policy):
    """Not because it is dangerous, but because the column resolver cannot
    prove a set operation safe, and a guard that passes what it cannot check is
    not a guard. Named as `unsupported_statement` rather than `not_a_select` so
    the report says "this tool will not check that" rather than "that is not a
    query"."""
    report = check("SELECT id FROM orders UNION SELECT id FROM refunds", session_card, policy)
    assert not report.ok
    assert "unsupported_statement" in codes(report)


# --- rule 4: nothing forbidden anywhere in the tree -------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA table_info(orders)",
        "PRAGMA foreign_keys = OFF",
        "ATTACH DATABASE '/etc/passwd' AS leak",
        "DETACH DATABASE leak",
        "VACUUM",
        "BEGIN",
        "COMMIT",
    ],
)
def test_administrative_statements_are_refused(sql, session_card, policy):
    report = check(sql, session_card, policy)
    assert not report.ok, sql
    assert "forbidden_syntax" in codes(report), sql


def test_load_extension_is_refused_whatever_the_allowlist_says(session_card):
    """The deny list wins over the allowlist. Somebody widening
    `allowed_functions` to unblock a report should not be able to hand the
    database arbitrary code execution as a side effect."""
    permissive = GuardPolicy(
        max_rows=10,
        max_subquery_depth=3,
        star_row_threshold=10_000,
        allow_star=True,
        allowed_tables=frozenset(),
        allowed_functions=frozenset({"LOAD_EXTENSION", "COUNT"}),
    )
    report = check("SELECT load_extension('evil.so')", session_card, permissive)
    assert not report.ok
    assert "forbidden_function" in codes(report)


@pytest.mark.parametrize("name", ["readfile", "writefile", "load_extension", "fts3_tokenizer"])
def test_the_file_reaching_functions_are_all_denied(name, session_card, policy):
    report = check(f"SELECT {name}('x') FROM orders", session_card, policy)
    assert not report.ok
    assert "forbidden_function" in codes(report)


def test_a_cross_database_table_reference_is_refused(session_card, policy):
    report = check("SELECT id FROM other.orders", session_card, policy)
    assert not report.ok
    assert "qualified_table" in codes(report)


def test_a_normal_query_passes_the_syntax_rule(session_card, policy):
    report = check("SELECT COUNT(*) FROM orders", session_card, policy)
    assert rule(report, "no_forbidden_syntax").status is RuleStatus.PASS


# --- rule 5 and 6: tables ---------------------------------------------------


def test_a_known_table_passes(session_card, policy):
    assert rule(check("SELECT id FROM orders", session_card, policy), "known_tables").status is (
        RuleStatus.PASS
    )


def test_a_hallucinated_table_is_caught_before_execution(session_card, policy):
    report = check("SELECT id FROM invoices", session_card, policy)
    assert not report.ok
    assert "unknown_table" in codes(report)
    assert "invoices" in rule(report, "known_tables").detail


def test_table_names_are_case_insensitive(session_card, policy):
    assert check("SELECT COUNT(*) FROM ORDERS", session_card, policy).ok


def test_a_table_outside_the_allowlist_is_a_policy_finding_not_a_hallucination(session_card):
    """Two different findings on purpose: `unknown_table` means the model
    invented something, `table_not_allowed` means it asked for something real
    it may not have. Collapsing them into one code would hide both."""
    narrow = GuardPolicy(
        max_rows=10,
        max_subquery_depth=2,
        star_row_threshold=100,
        allow_star=False,
        allowed_tables=frozenset({"orders"}),
        allowed_functions=frozenset({"COUNT"}),
    )
    report = check("SELECT COUNT(*) FROM refunds", session_card, narrow)
    assert not report.ok
    assert "table_not_allowed" in codes(report)
    assert "unknown_table" not in codes(report)
    assert check("SELECT COUNT(*) FROM orders", session_card, narrow).ok


def test_an_empty_allowlist_means_every_table_in_the_card(session_card, policy):
    assert policy.allowed_tables == frozenset()
    assert check("SELECT COUNT(*) FROM refunds", session_card, policy).ok


def test_tables_used_is_reported(session_card, policy):
    sql = "SELECT c.city FROM orders o JOIN customers c ON c.id = o.customer_id"
    report = check(sql, session_card, policy)
    assert report.tables_used == ("customers", "orders")


# --- rule 9: functions ------------------------------------------------------


def test_the_allowed_aggregates_pass(session_card, policy):
    sql = (
        "SELECT COUNT(*), SUM(total_cents), AVG(total_cents), MIN(total_cents), "
        "MAX(total_cents), ROUND(AVG(total_cents), 2), TOTAL(total_cents) FROM orders"
    )
    assert check(sql, session_card, policy).ok


def test_the_allowed_date_functions_pass(session_card, policy):
    """`STRFTIME` is the one that matters: sqlglot rewrites it into an
    internal node whose canonical name is `TIME_TO_STR`, and a guard that
    checked that name would reject the most useful date function in SQLite.
    See docs/design.md §16."""
    sql = (
        "SELECT STRFTIME('%Y-%m', order_date), DATE(order_date), "
        "JULIANDAY(order_date) FROM orders LIMIT 5"
    )
    report = check(sql, session_card, policy)
    assert report.ok, report.codes


def test_the_allowed_scalar_functions_pass(session_card, policy):
    sql = (
        "SELECT LOWER(status), UPPER(channel), LENGTH(status), "
        "COALESCE(status, 'none'), ABS(total_cents), GROUP_CONCAT(status) FROM orders"
    )
    assert check(sql, session_card, policy).ok


def test_a_function_outside_the_allowlist_is_refused(session_card, policy):
    report = check("SELECT RANDOMBLOB(16) FROM orders", session_card, policy)
    assert not report.ok
    assert "function_not_allowed" in codes(report)
    assert "RANDOMBLOB" in rule(report, "allowed_functions").detail


def test_cast_is_not_on_the_allowlist(session_card, policy):
    """Not because CAST is dangerous. Because the allowlist is a list, and
    anything not on it is refused rather than waved through."""
    report = check("SELECT CAST(total_cents AS TEXT) FROM orders", session_card, policy)
    assert not report.ok
    assert "function_not_allowed" in codes(report)


def test_function_names_are_case_insensitive(session_card, policy):
    assert check("select count(*) from orders", session_card, policy).ok


def test_a_nested_disallowed_function_is_still_caught(session_card, policy):
    sql = "SELECT id FROM orders WHERE total_cents > (SELECT RANDOM() FROM orders)"
    report = check(sql, session_card, policy)
    assert "function_not_allowed" in codes(report)

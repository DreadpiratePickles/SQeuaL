"""Stage 04: the sandbox.

The guard has already proved the statement is a SELECT. This stage assumes it
did not.

That is the whole design of this module: read-only is enforced at **three
independent layers**, and each one is tested by deliberately bypassing the
others. The guard refuses a DELETE; the connection is opened `mode=ro` so
SQLite refuses to write through it at all; and `PRAGMA query_only = 1` refuses
again inside the connection. Any one of them is sufficient. All three are here
because the failure mode of "the guard has a hole" is a database with rows
missing, and a hole in a parser is not a hypothetical.

The tests below run DELETE, UPDATE and DROP straight at the executor with no
guard in front, which is the only way to find out whether the other two layers
are real.
"""

import sqlite3
import time

import pytest

from sqeual.execute import (
    DatabaseUnavailableError,
    ExecuteLimits,
    ExecutionError,
    ExecutionTimeout,
    execute_sql,
    open_readonly,
)

LIMITS = ExecuteLimits(max_ms=5000, max_rows=500, plan_scan_row_threshold=1000)


def run(sql, db, limits=LIMITS, card=None):
    return execute_sql(sql, db, limits=limits, card=card)


# --- reading ----------------------------------------------------------------


def test_a_scalar_query_returns_one_row(session_db):
    result = run("SELECT COUNT(*) FROM orders", session_db)
    assert result.rows == ((2000,),)
    assert result.row_count == 1
    assert result.truncated is False


def test_column_names_are_reported(session_db):
    result = run("SELECT id, status FROM orders LIMIT 1", session_db)
    assert result.columns == ("id", "status")


def test_an_aliased_column_reports_its_alias(session_db):
    result = run("SELECT COUNT(*) AS n FROM orders", session_db)
    assert result.columns == ("n",)


def test_rows_come_back_as_tuples(session_db):
    result = run("SELECT id, status FROM orders LIMIT 3", session_db)
    assert all(isinstance(row, tuple) for row in result.rows)


def test_a_query_matching_nothing_is_an_empty_result_not_an_error(session_db):
    """Zero rows is an answer. Turning it into an error would make "no refunds
    last week" indistinguishable from "the query broke"."""
    result = run("SELECT id FROM orders WHERE status = 'nonexistent'", session_db)
    assert result.rows == ()
    assert result.row_count == 0
    assert result.truncated is False


def test_elapsed_time_is_recorded(session_db):
    result = run("SELECT COUNT(*) FROM orders", session_db)
    assert result.elapsed_ms >= 0
    assert result.elapsed_ms < 5000


def test_the_berlin_question_returns_a_number(session_db):
    sql = (
        "SELECT SUM(r.amount_cents) FROM refunds r "
        "JOIN orders o ON o.id = r.order_id "
        "JOIN customers c ON c.id = o.customer_id WHERE c.city = 'Berlin' LIMIT 200"
    )
    result = run(sql, session_db)
    assert result.row_count == 1
    assert result.rows[0][0] > 0


# --- the row cap ------------------------------------------------------------


def test_more_rows_than_the_cap_are_truncated_and_say_so(session_db):
    """The cap is a backstop for SQL that was never guarded, so it holds even
    when the statement's own LIMIT is larger."""
    tight = ExecuteLimits(max_ms=5000, max_rows=10, plan_scan_row_threshold=1000)
    result = run("SELECT id FROM orders LIMIT 100", session_db, tight)
    assert result.row_count == 10
    assert result.truncated is True


def test_exactly_the_cap_is_not_truncation(session_db):
    """An off-by-one here would report every full page as incomplete."""
    tight = ExecuteLimits(max_ms=5000, max_rows=10, plan_scan_row_threshold=1000)
    result = run("SELECT id FROM orders LIMIT 10", session_db, tight)
    assert result.row_count == 10
    assert result.truncated is False


def test_fewer_rows_than_the_cap_are_not_truncated(session_db):
    tight = ExecuteLimits(max_ms=5000, max_rows=10, plan_scan_row_threshold=1000)
    result = run("SELECT id FROM orders LIMIT 3", session_db, tight)
    assert result.row_count == 3
    assert result.truncated is False


# --- read-only, proved at each layer ----------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM tickets",
        "UPDATE orders SET total_cents = 0",
        "INSERT INTO agents VALUES (99, 'x', 'y', '2025-01-01')",
        "DROP TABLE refunds",
        "CREATE TABLE evil (id INTEGER)",
        "ALTER TABLE orders ADD COLUMN leaked TEXT",
    ],
)
def test_a_write_that_bypassed_the_guard_still_fails_at_the_connection(sql, session_db):
    """No guard in front. This is the test that says the second and third
    layers are real rather than decorative."""
    with pytest.raises(ExecutionError):
        run(sql, session_db)


def test_the_database_is_unchanged_after_an_attempted_write(session_db):
    before = sqlite3.connect(session_db).execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    with pytest.raises(ExecutionError):
        run("DELETE FROM tickets", session_db)
    after = sqlite3.connect(session_db).execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    assert after == before == 600


def test_query_only_is_actually_set_on_the_connection(session_db):
    with open_readonly(session_db, max_ms=5000) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_the_connection_refuses_writes_even_with_query_only_turned_off(session_db):
    """`PRAGMA query_only = 0` is the obvious way through the third layer. The
    second layer does not care, because `mode=ro` is a property of the open
    file descriptor and no statement can undo it."""
    with open_readonly(session_db, max_ms=5000) as conn:
        conn.execute("PRAGMA query_only = 0")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM tickets")


def test_attaching_a_second_database_is_refused(session_db, tmp_path):
    """The regression test for the hole this module was written around.

    `ATTACH` succeeds on a `mode=ro` connection with `query_only = 1`, and it
    creates the file it is pointed at. Both of those layers are behaving
    correctly — ATTACH is not a write to the *main* database — which is the
    point: "read-only" and "sandboxed" are different properties, and only the
    fourth layer (`SQLITE_LIMIT_ATTACHED = 0`) closes this one.
    """
    side_file = tmp_path / "created_by_attach.db"
    with pytest.raises(ExecutionError):
        run(f"ATTACH DATABASE '{side_file}' AS other", session_db)
    assert not side_file.exists(), "ATTACH wrote a file through a read-only connection"


def test_detaching_is_refused_too(session_db):
    with pytest.raises(ExecutionError):
        run("DETACH DATABASE other", session_db)


# --- the timeout ------------------------------------------------------------


def test_an_unbounded_recursive_query_is_aborted(session_db):
    """A timer in Python cannot interrupt a C-level scan, so the budget is
    enforced by a SQLite progress handler that runs *inside* the query. This
    recursive CTE never terminates; without the handler this test would hang
    the suite rather than fail it."""
    sql = (
        "WITH RECURSIVE spin(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM spin) "
        "SELECT COUNT(*) FROM spin"
    )
    quick = ExecuteLimits(max_ms=150, max_rows=10, plan_scan_row_threshold=1000)
    started = time.monotonic()
    with pytest.raises(ExecutionTimeout) as caught:
        run(sql, session_db, quick)
    assert time.monotonic() - started < 10
    assert "150" in str(caught.value)


def test_a_fast_query_is_not_aborted(session_db):
    quick = ExecuteLimits(max_ms=5000, max_rows=10, plan_scan_row_threshold=1000)
    assert run("SELECT COUNT(*) FROM orders", session_db, quick).row_count == 1


def test_the_timeout_does_not_leak_into_the_next_query(session_db):
    """The progress handler is cleared when the connection closes. A handler
    left installed with an expired deadline would abort every later query on a
    pooled connection."""
    sql = (
        "WITH RECURSIVE spin(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM spin) "
        "SELECT COUNT(*) FROM spin"
    )
    quick = ExecuteLimits(max_ms=150, max_rows=10, plan_scan_row_threshold=1000)
    with pytest.raises(ExecutionTimeout):
        run(sql, session_db, quick)
    assert run("SELECT COUNT(*) FROM orders", session_db, quick).row_count == 1


# --- the plan ---------------------------------------------------------------


def test_the_query_plan_is_captured(session_db):
    result = run("SELECT COUNT(*) FROM orders", session_db)
    assert result.plan.steps
    assert any("orders" in step for step in result.plan.steps)


def test_a_full_scan_of_a_large_table_is_a_warning_not_a_failure(session_card, session_db):
    """Scanning 2,000 rows is how you correctly answer "how many orders were
    there". A tool that refused it would be wrong more often than the query is,
    so this is reported and the rows still come back."""
    result = run("SELECT COUNT(*) FROM orders", session_db, card=session_card)
    assert result.plan.warnings
    assert "orders" in result.plan.warnings[0]
    assert result.rows == ((2000,),)


def test_a_full_scan_of_a_small_table_is_not_warned_about(session_card, session_db):
    result = run("SELECT COUNT(*) FROM agents", session_db, card=session_card)
    assert result.plan.warnings == ()


def test_an_indexed_lookup_is_not_warned_about(session_card, session_db):
    result = run(
        "SELECT id FROM orders WHERE customer_id = 7 LIMIT 5", session_db, card=session_card
    )
    assert result.plan.warnings == ()


def test_without_a_card_there_are_no_size_warnings(session_db):
    """Row counts come from the schema card. No card, no basis for a warning —
    and inventing one would be inventing a number."""
    result = run("SELECT COUNT(*) FROM orders", session_db, card=None)
    assert result.plan.steps
    assert result.plan.warnings == ()


# --- failure behaviour ------------------------------------------------------


def test_a_missing_database_is_a_typed_error(tmp_path):
    with pytest.raises(DatabaseUnavailableError, match="not found"):
        run("SELECT 1", tmp_path / "nope.db")


def test_a_file_that_is_not_a_database_is_a_typed_error(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database")
    with pytest.raises(DatabaseUnavailableError):
        run("SELECT 1", junk)


def test_an_unknown_column_that_reached_the_executor_is_a_typed_error(session_db):
    """The guard should have caught this. If it did not, the failure is still
    an `ExecutionError` and not a raw `sqlite3.OperationalError` — nothing
    downstream should ever have to catch a driver exception."""
    with pytest.raises(ExecutionError) as caught:
        run("SELECT made_up FROM orders", session_db)
    assert "made_up" in str(caught.value)


def test_the_error_message_does_not_contain_the_statement(session_db):
    """An executor that echoes the SQL it failed on writes that SQL into every
    log line — including the literals in it, which in a real deployment are
    whatever the question was about."""
    sql = "SELECT made_up FROM orders WHERE status = 'sw0rdf1sh-secret'"
    with pytest.raises(ExecutionError) as caught:
        run(sql, session_db)
    assert "sw0rdf1sh" not in str(caught.value)
    assert "SELECT" not in str(caught.value)


def test_multiple_statements_are_refused_by_the_driver_too(session_db):
    """A fourth layer nobody designed: `sqlite3` refuses to execute more than
    one statement per call. Asserted so that a future switch to a driver
    without that behaviour is a failing test rather than a surprise."""
    with pytest.raises(ExecutionError):
        run("SELECT 1; SELECT 2", session_db)


def test_a_non_positive_budget_is_refused(session_db):
    with pytest.raises(ValueError, match="max_ms"):
        ExecuteLimits(max_ms=0, max_rows=10, plan_scan_row_threshold=0).validate()


def test_a_non_positive_row_cap_is_refused(session_db):
    with pytest.raises(ValueError, match="max_rows"):
        ExecuteLimits(max_ms=10, max_rows=0, plan_scan_row_threshold=0).validate()


def test_an_empty_statement_is_refused_before_the_database_is_opened(tmp_path):
    with pytest.raises(ExecutionError, match="empty"):
        run("   ", tmp_path / "does-not-matter.db")


def test_a_scan_warning_fires_through_a_table_alias(session_card, session_db):
    """`EXPLAIN QUERY PLAN` names the alias, not the table: `FROM refunds r`
    produces the step `SCAN r`. Without the guard's alias map this warning is
    silent on every aliased join — which is most real queries — and its silence
    reads as an all-clear.
    """
    sql = (
        "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id LIMIT 10"
    )
    without = run(sql, session_db, card=session_card)
    assert without.plan.warnings == ()

    with_map = execute_sql(
        sql,
        session_db,
        limits=LIMITS,
        card=session_card,
        aliases={"o": "orders", "c": "customers"},
    )
    assert any("orders" in warning for warning in with_map.plan.warnings)


def test_an_alias_map_never_invents_a_table(session_card, session_db):
    """A map naming something the card does not have produces no warning
    rather than a warning about a table that does not exist.

    The query is a join because SQLite only prints the alias when there is more
    than one source: `SELECT COUNT(*) FROM orders o` explains as `SCAN orders`,
    and only `... JOIN customers c` turns it into `SCAN o`.
    """
    result = execute_sql(
        "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id LIMIT 10",
        session_db,
        limits=LIMITS,
        card=session_card,
        aliases={"o": "invoices", "c": "invoices"},
    )
    assert result.plan.warnings == ()


def test_a_covering_index_scan_is_still_a_full_scan(session_card, session_db):
    """`SCAN orders USING COVERING INDEX ...` reads the index instead of the
    table and still reads every row. Treating it as "an index was used, so no
    warning" would silence the warning on exactly the queries where an index
    exists and does not help."""
    result = run("SELECT COUNT(*) FROM orders", session_db, card=session_card)
    assert any("COVERING INDEX" in step for step in result.plan.steps)
    assert any("every row is visited" in warning for warning in result.plan.warnings)

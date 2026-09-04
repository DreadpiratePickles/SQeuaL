"""Stage 01: the generated database.

The load-bearing property is **determinism**. Every figure in the docs, every
row count a test asserts, and every `EXPLAIN QUERY PLAN` the executor captures
is a statement about one specific database. If `db build` produced a different
database each time, none of those would be a fact, and the first mysterious
test failure would cost an afternoon before anybody suspected the fixture.

The second property is **internal consistency**: two correct queries that mean
the same thing must return the same number. `SUM(orders.total_cents)` and
`SUM(order_items.quantity * order_items.unit_price_cents)` are the pair this
suite pins, because a demo database where they disagree makes a correct
text-to-SQL answer look wrong.
"""

import hashlib
import sqlite3

import pytest

from conftest import COMMITTED_SCHEMA_SQL
from sqeual.db.build import (
    DatabaseBuildError,
    build_database,
    row_digest,
)

SEED = 20260904

EXPECTED_TABLES = [
    "agents",
    "customers",
    "order_items",
    "orders",
    "products",
    "refunds",
    "tickets",
]


def build(path, **kwargs):
    kwargs.setdefault("schema_sql_path", COMMITTED_SCHEMA_SQL)
    kwargs.setdefault("seed", SEED)
    return build_database(path, **kwargs)


def rows(db, sql):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def scalar(db, sql):
    return rows(db, sql)[0][0]


# --- determinism ------------------------------------------------------------


def test_two_builds_are_byte_identical(tmp_path):
    """The strongest form of the claim: same seed, same bytes on disk.

    Row-level equality would be enough for the tool to work; byte equality is
    asserted because it is *checkable* and because the moment it stops holding
    something non-deterministic has entered the generator — an iteration over a
    set, a timestamp, a dict ordering — and this test names the day it happened.
    """
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    build(first)
    build(second)
    assert first.read_bytes() == second.read_bytes()


def test_two_builds_have_identical_row_digests(tmp_path):
    """The weaker claim, asserted separately so that a future SQLite version
    which pads a page differently does not leave the suite with no determinism
    test at all."""
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    build(first)
    build(second)
    assert row_digest(first) == row_digest(second)


def test_a_different_seed_gives_a_different_database(tmp_path):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    build(first)
    build(second, seed=SEED + 1)
    assert row_digest(first) != row_digest(second)


def test_row_digest_is_stable_across_connections(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    assert row_digest(db) == row_digest(db)


# --- shape ------------------------------------------------------------------


def test_the_seven_tables_exist_and_no_others(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    found = [
        name
        for (name,) in rows(
            db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if not name.startswith("sqlite_")
    ]
    assert found == EXPECTED_TABLES


def test_row_counts_are_the_documented_ones(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    counts = {table: scalar(db, f"SELECT COUNT(*) FROM {table}") for table in EXPECTED_TABLES}
    assert counts["customers"] == 250
    assert counts["products"] == 40
    assert counts["agents"] == 12
    assert counts["orders"] == 2000
    assert counts["tickets"] == 600
    assert counts["refunds"] == 150
    assert counts["order_items"] > counts["orders"]


def test_foreign_keys_all_resolve(tmp_path):
    """`PRAGMA foreign_key_check` is the database's own answer to "did the
    generator invent a customer id"."""
    db = tmp_path / "a.db"
    build(db)
    assert rows(db, "PRAGMA foreign_key_check") == []


def test_dates_span_the_documented_window(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    low, high = rows(db, "SELECT MIN(order_date), MAX(order_date) FROM orders")[0]
    assert low >= "2025-01-01"
    assert high <= "2026-08-31"
    assert low < "2025-02-01"
    assert high > "2026-08-01"


def test_every_date_column_is_iso_text(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    for table, column in [
        ("customers", "signup_date"),
        ("orders", "order_date"),
        ("tickets", "opened_date"),
        ("refunds", "refund_date"),
        ("agents", "hired_date"),
    ]:
        bad = scalar(
            db,
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} NOT GLOB "
            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
        )
        assert bad == 0, f"{table}.{column} holds a non-ISO date"


def test_money_columns_are_integers(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    for table, column in [
        ("orders", "total_cents"),
        ("order_items", "unit_price_cents"),
        ("products", "unit_price_cents"),
        ("refunds", "amount_cents"),
    ]:
        kinds = {k for (k,) in rows(db, f"SELECT DISTINCT typeof({column}) FROM {table}")}
        assert kinds == {"integer"}, f"{table}.{column} is not integer cents: {kinds}"


# --- internal consistency ---------------------------------------------------


def test_order_totals_equal_the_sum_of_their_items(tmp_path):
    """Two correct queries that mean the same thing must agree."""
    db = tmp_path / "a.db"
    build(db)
    mismatches = scalar(
        db,
        "SELECT COUNT(*) FROM orders o "
        "WHERE o.total_cents <> ("
        "  SELECT COALESCE(SUM(i.quantity * i.unit_price_cents), 0) "
        "  FROM order_items i WHERE i.order_id = o.id)",
    )
    assert mismatches == 0


def test_no_refund_exceeds_its_order_total(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    over = scalar(
        db,
        "SELECT COUNT(*) FROM refunds r JOIN orders o ON o.id = r.order_id "
        "WHERE r.amount_cents > o.total_cents",
    )
    assert over == 0


def test_no_event_precedes_the_thing_it_refers_to(tmp_path):
    """A refund dated before its order, or a ticket before the customer signed
    up, is the kind of detail nobody checks until a question about "average days
    to refund" comes back negative."""
    db = tmp_path / "a.db"
    build(db)
    assert (
        scalar(
            db,
            "SELECT COUNT(*) FROM refunds r JOIN orders o ON o.id = r.order_id "
            "WHERE r.refund_date < o.order_date",
        )
        == 0
    )
    assert (
        scalar(
            db,
            "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.order_date < c.signup_date",
        )
        == 0
    )
    assert (
        scalar(
            db,
            "SELECT COUNT(*) FROM tickets WHERE closed_date IS NOT NULL "
            "AND closed_date < opened_date",
        )
        == 0
    )


def test_closed_dates_are_null_for_open_tickets_only(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    open_but_closed = "SELECT COUNT(*) FROM tickets WHERE status='open' AND closed_date IS NOT NULL"
    closed_but_open = "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_date IS NULL"
    assert scalar(db, open_but_closed) == 0
    assert scalar(db, closed_but_open) == 0


def test_some_tickets_have_no_order_and_some_do(tmp_path):
    """Both branches must be populated or the nullable FK is untested by every
    downstream query."""
    db = tmp_path / "a.db"
    build(db)
    assert scalar(db, "SELECT COUNT(*) FROM tickets WHERE order_id IS NULL") > 0
    assert scalar(db, "SELECT COUNT(*) FROM tickets WHERE order_id IS NOT NULL") > 0


def test_low_cardinality_columns_are_enum_shaped(tmp_path):
    """The schema card samples a column only when it has at most
    `[schema] max_distinct_values` distinct values. These counts are therefore
    not decoration: if the generator ever produced 200 distinct statuses the
    card would stop sampling them, the model would stop being told that
    `status` holds `'refunded'`, and every downstream slice and guard test
    would quietly change meaning.

    Pinned exactly rather than with an upper bound, so that a vocabulary list
    losing an entry is a failure here rather than a silently smaller card.
    """
    db = tmp_path / "a.db"
    build(db)
    expected = {
        ("orders", "status"): 5,
        ("orders", "channel"): 4,
        ("tickets", "status"): 3,
        ("tickets", "priority"): 4,
        ("tickets", "category"): 8,
        ("tickets", "channel"): 4,
        ("customers", "country"): 14,
        ("customers", "city"): 20,
        ("customers", "segment"): 3,
        ("products", "category"): 6,
        ("refunds", "reason"): 6,
        ("agents", "team"): 4,
    }
    found = {
        (table, column): scalar(db, f"SELECT COUNT(DISTINCT {column}) FROM {table}")
        for table, column in expected
    }
    assert found == expected


def test_every_sampled_column_sits_under_the_card_threshold(tmp_path):
    """The threshold in the committed config is 20. `customers.city` sits
    exactly on it, which is deliberate — the worked example asks about Berlin,
    and a model that cannot see 'Berlin' in the card has to guess the spelling.
    One more city and the column stops being sampled."""
    db = tmp_path / "a.db"
    build(db)
    assert scalar(db, "SELECT COUNT(DISTINCT city) FROM customers") <= 20


def test_berlin_customers_exist_with_refunds(tmp_path):
    """The worked example in the README and the slicer test both assume this
    question has an answer. If it stops having one, the example is a lie."""
    db = tmp_path / "a.db"
    build(db)
    total = scalar(
        db,
        "SELECT COUNT(*) FROM refunds r "
        "JOIN orders o ON o.id = r.order_id "
        "JOIN customers c ON c.id = o.customer_id WHERE c.city = 'Berlin'",
    )
    assert total > 0


# --- failure behaviour ------------------------------------------------------


def test_refuses_to_overwrite_an_existing_database(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    with pytest.raises(DatabaseBuildError, match="already exists"):
        build(db)


def test_overwrite_is_possible_when_asked_for_explicitly(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    before = db.read_bytes()
    build(db, overwrite=True)
    assert db.read_bytes() == before


def test_missing_schema_sql_is_a_typed_error(tmp_path):
    with pytest.raises(DatabaseBuildError, match="schema"):
        build(tmp_path / "a.db", schema_sql_path=tmp_path / "nope.sql")


def test_a_schema_that_is_not_ddl_is_a_typed_error(tmp_path):
    bad = tmp_path / "bad.sql"
    bad.write_text("CREATE TABLE (;", encoding="utf-8")
    with pytest.raises(DatabaseBuildError, match="schema"):
        build(tmp_path / "a.db", schema_sql_path=bad)


def test_a_schema_missing_a_table_the_generator_fills_is_reported(tmp_path):
    """The generator inserts into seven named tables. A DDL file that does not
    define one of them must fail loudly, not leave a half-populated database."""
    partial = tmp_path / "partial.sql"
    partial.write_text("CREATE TABLE customers (id INTEGER PRIMARY KEY);", encoding="utf-8")
    with pytest.raises(DatabaseBuildError):
        build(tmp_path / "a.db", schema_sql_path=partial)


def test_a_failed_build_leaves_no_partial_database(tmp_path):
    partial = tmp_path / "partial.sql"
    partial.write_text("CREATE TABLE customers (id INTEGER PRIMARY KEY);", encoding="utf-8")
    out = tmp_path / "a.db"
    with pytest.raises(DatabaseBuildError):
        build(out, schema_sql_path=partial)
    assert not out.exists()


def test_a_non_integer_seed_is_refused(tmp_path):
    with pytest.raises(DatabaseBuildError, match="seed"):
        build(tmp_path / "a.db", seed="lucky")


def test_a_boolean_seed_is_refused(tmp_path):
    with pytest.raises(DatabaseBuildError, match="seed"):
        build(tmp_path / "a.db", seed=True)


def test_row_digest_of_a_missing_file_is_a_typed_error(tmp_path):
    with pytest.raises(DatabaseBuildError):
        row_digest(tmp_path / "nope.db")


def test_row_digest_covers_every_table(tmp_path):
    """A digest that silently skipped a table would call two different
    databases identical."""
    db = tmp_path / "a.db"
    build(db)
    baseline = row_digest(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE refunds SET amount_cents = amount_cents + 1 WHERE id = 1")
    assert row_digest(db) != baseline


def test_digest_is_a_hex_sha256(tmp_path):
    db = tmp_path / "a.db"
    build(db)
    digest = row_digest(db)
    assert len(digest) == 64
    int(digest, 16)
    assert digest != hashlib.sha256(b"").hexdigest()

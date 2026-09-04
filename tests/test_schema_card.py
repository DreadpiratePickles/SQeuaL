"""Stage 02: introspecting the database into a typed schema card.

The card is the ground truth the guard checks a model's SQL against. If it says
`orders` has a `total_cents` and the database does not, the guard passes a
query that then fails at execution — and the tool's central claim, that a
hallucinated column is caught *before* anything runs, stops being true. So the
card is read from `sqlite_master` and the `PRAGMA`s rather than from anything a
human maintains in parallel with the DDL.
"""

import sqlite3

import pytest

from conftest import COMMITTED_SCHEMA_SQL
from sqeual.db.build import build_database
from sqeual.schema.card import SchemaUnavailableError, read_schema_card


def test_every_table_is_found(session_card):
    assert session_card.table_names == (
        "agents",
        "customers",
        "order_items",
        "orders",
        "products",
        "refunds",
        "tickets",
    )


def test_internal_sqlite_tables_are_excluded(session_card):
    assert not any(name.startswith("sqlite_") for name in session_card.table_names)


def test_columns_carry_type_nullability_and_key(session_card):
    orders = session_card.table("orders")
    by_name = {column.name: column for column in orders.columns}
    assert [column.name for column in orders.columns] == [
        "id",
        "customer_id",
        "order_date",
        "status",
        "channel",
        "total_cents",
    ]
    assert by_name["id"].primary_key is True
    assert by_name["id"].type == "INTEGER"
    assert by_name["customer_id"].nullable is False
    assert by_name["total_cents"].type == "INTEGER"
    assert by_name["order_date"].type == "TEXT"


def test_a_nullable_column_is_reported_as_nullable(session_card):
    tickets = session_card.table("tickets")
    by_name = {column.name: column for column in tickets.columns}
    assert by_name["closed_date"].nullable is True
    assert by_name["order_id"].nullable is True
    assert by_name["customer_id"].nullable is False


def test_foreign_keys_are_read_with_both_ends(session_card):
    refunds = session_card.table("refunds")
    edges = {(fk.from_column, fk.to_table, fk.to_column) for fk in refunds.foreign_keys}
    assert ("order_id", "orders", "id") in edges
    assert ("ticket_id", "tickets", "id") in edges


def test_foreign_key_nullability_is_carried(session_card):
    """A join through a nullable key can drop rows. The slicer uses this to
    prefer a join path that cannot."""
    refunds = session_card.table("refunds")
    by_column = {fk.from_column: fk for fk in refunds.foreign_keys}
    assert by_column["order_id"].nullable is False
    assert by_column["ticket_id"].nullable is True


def test_the_whole_card_has_the_expected_number_of_foreign_keys(session_card):
    total = sum(len(table.foreign_keys) for table in session_card.tables)
    assert total == 8


def test_row_counts_are_real(session_card):
    counts = {table.name: table.row_count for table in session_card.tables}
    assert counts["orders"] == 2000
    assert counts["refunds"] == 150
    assert counts["agents"] == 12


# --- sample values ----------------------------------------------------------


def test_enum_shaped_columns_get_sample_values(session_card):
    status = next(c for c in session_card.table("orders").columns if c.name == "status")
    assert status.sample_values == ("cancelled", "delivered", "placed", "refunded", "shipped")


def test_samples_are_capped_and_sorted(session_card):
    city = next(c for c in session_card.table("customers").columns if c.name == "city")
    assert len(city.sample_values) == 5
    assert list(city.sample_values) == sorted(city.sample_values)


def test_high_cardinality_columns_get_no_samples(session_card):
    """`email` has a value per row. Sampling it would hand a model content it
    cannot generalise from and would put real-looking addresses in a prompt."""
    for table, column in [
        ("customers", "email"),
        ("customers", "name"),
        ("orders", "order_date"),
        ("refunds", "amount_cents"),
    ]:
        found = next(c for c in session_card.table(table).columns if c.name == column)
        assert found.sample_values == (), f"{table}.{column} was sampled"


def test_primary_keys_are_never_sampled(session_card):
    """`agents.id` has twelve distinct values and would pass the cardinality
    test. Listing 1, 2, 3, 4, 5 tells a model nothing."""
    for table in session_card.tables:
        for column in table.columns:
            if column.primary_key:
                assert column.sample_values == ()


def test_long_values_are_truncated_to_the_limit(tmp_path):
    db = tmp_path / "long.db"
    ddl = tmp_path / "long.sql"
    ddl.write_text("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT);", encoding="utf-8")
    with sqlite3.connect(db) as conn:
        conn.executescript(ddl.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO t VALUES (1, ?)", ("x" * 300,))
    card = read_schema_card(db, max_sample_chars=40)
    note = next(c for c in card.table("t").columns if c.name == "note")
    assert len(note.sample_values[0]) == 40
    assert note.sample_values[0].endswith("…")


def test_null_values_are_not_offered_as_samples(session_card):
    """`nullable` already says a column can be null. A literal `None` in a
    sample list would read as the string "None" to a model."""
    for table in session_card.tables:
        for column in table.columns:
            assert all(isinstance(value, str) for value in column.sample_values)


def test_an_empty_table_yields_no_samples_and_a_zero_count(tmp_path):
    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, kind TEXT);")
    card = read_schema_card(db)
    assert card.table("t").row_count == 0
    assert all(column.sample_values == () for column in card.table("t").columns)


def test_sampling_limits_are_honoured(session_card, session_db):
    tight = read_schema_card(session_db, max_sample_values=2, max_distinct_values=6)
    status = next(c for c in tight.table("orders").columns if c.name == "status")
    assert len(status.sample_values) == 2
    country = next(c for c in tight.table("customers").columns if c.name == "country")
    assert country.sample_values == ()


# --- identity ---------------------------------------------------------------


def test_schema_sha256_is_a_hex_digest(session_card):
    assert len(session_card.schema_sha256) == 64
    int(session_card.schema_sha256, 16)


def test_schema_sha256_is_stable_across_reads(session_db):
    assert read_schema_card(session_db).schema_sha256 == read_schema_card(session_db).schema_sha256


def test_schema_sha256_ignores_row_counts_and_samples(tmp_path):
    """The hash names the *shape*. Inserting a row does not change the schema,
    and a policy pinned to a schema hash must not be invalidated by an INSERT.
    """
    ddl = tmp_path / "s.sql"
    ddl.write_text("CREATE TABLE t (id INTEGER PRIMARY KEY, kind TEXT);", encoding="utf-8")
    db = tmp_path / "s.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(ddl.read_text(encoding="utf-8"))
    before = read_schema_card(db).schema_sha256
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO t VALUES (1, 'a')")
    after = read_schema_card(db)
    assert after.schema_sha256 == before
    assert after.table("t").row_count == 1


def test_schema_sha256_changes_when_a_column_is_added(tmp_path):
    db = tmp_path / "s.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, kind TEXT);")
    before = read_schema_card(db).schema_sha256
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE t ADD COLUMN extra TEXT")
    assert read_schema_card(db).schema_sha256 != before


# --- lookups the guard depends on -------------------------------------------


def test_lookups_are_case_insensitive(session_card):
    """SQL identifiers are case-insensitive in SQLite, so `SELECT * FROM
    ORDERS` names the same table. A guard that treated them as different would
    report a hallucinated table for a query that runs perfectly."""
    assert session_card.table("ORDERS") is session_card.table("orders")
    assert session_card.has_column("Orders", "Total_Cents") is True
    assert session_card.has_column("orders", "total_cents") is True


def test_unknown_lookups_are_none_and_false(session_card):
    assert session_card.table("invoices") is None
    assert session_card.has_column("orders", "profit_margin") is False
    assert session_card.has_column("invoices", "id") is False


def test_tables_holding_a_column_finds_every_candidate(session_card):
    """This is the ambiguity check's raw material: `id` is on every table."""
    assert set(session_card.tables_with_column("id")) == set(session_card.table_names)
    assert set(session_card.tables_with_column("customer_id")) == {"orders", "tickets"}
    assert session_card.tables_with_column("nonexistent") == ()


# --- failure behaviour ------------------------------------------------------


def test_a_missing_database_is_a_typed_error(tmp_path):
    with pytest.raises(SchemaUnavailableError, match="not found"):
        read_schema_card(tmp_path / "nope.db")


def test_a_file_that_is_not_a_database_is_a_typed_error(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database, it is a picture of one")
    with pytest.raises(SchemaUnavailableError):
        read_schema_card(junk)


def test_a_database_with_no_tables_is_an_empty_card(tmp_path):
    db = tmp_path / "blank.db"
    build_database(
        db, schema_sql_path=COMMITTED_SCHEMA_SQL, seed=1, overwrite=True
    )  # sanity: the builder works here
    blank = tmp_path / "blank2.db"
    with sqlite3.connect(blank) as conn:
        conn.execute("PRAGMA user_version = 1")
    card = read_schema_card(blank)
    assert card.tables == ()
    assert card.table_names == ()


def test_an_integer_primary_key_is_not_reported_as_nullable(session_card):
    """`PRAGMA table_info` says `notnull = 0` for an `INTEGER PRIMARY KEY`,
    because the constraint is implicit in it being the rowid alias rather than
    declared. Passing that through verbatim would put "id: nullable yes" in the
    schema card — the one artefact the whole system asks a model to trust — and
    it would be false.
    """
    for table in session_card.tables:
        for column in table.columns:
            if column.primary_key and column.type == "INTEGER":
                assert column.nullable is False, f"{table.name}.{column.name}"


def test_a_non_integer_primary_key_is_left_as_sqlite_describes_it(tmp_path):
    """SQLite genuinely permits NULL in a non-INTEGER primary key — a
    long-standing quirk kept for compatibility. "Correcting" it would be
    inventing a constraint the database does not enforce."""
    db = tmp_path / "textpk.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE t (code TEXT PRIMARY KEY, note TEXT);")
        conn.execute("INSERT INTO t VALUES (NULL, 'sqlite really allows this')")
    card = read_schema_card(db)
    code = next(c for c in card.table("t").columns if c.name == "code")
    assert code.primary_key is True
    assert code.nullable is True

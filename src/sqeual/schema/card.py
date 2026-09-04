"""Stage 02: read the live database into a typed schema card.

The card is the ground truth the guard checks a model's SQL against, so it is
read from the database itself — `sqlite_master`, `PRAGMA table_info` and
`PRAGMA foreign_key_list` — and never from a hand-maintained description. A
second description of the schema is a second thing that can be wrong, and the
day it disagrees with the database is the day a hallucinated column passes the
guard and fails at execution instead.

Two decisions in here are worth arguing with, so they are written down:

**`schema_sha256` covers the shape and nothing else.** Row counts and sample
values are in the card but out of the hash. A policy pinned to a schema hash
must not be invalidated by an INSERT: the shape is what a query is written
against, and the shape did not change. Adding, removing or retyping a column
does change it.

**Sample values are drawn only from enum-shaped columns.** A column with at
most `max_distinct_values` distinct values is a vocabulary a model can use — it
stops guessing `'REFUNDED'` when the database says `'refunded'`. A column with
a value per row is content: sampling `email` would put realistic-looking
addresses into a prompt and teach the model nothing. Primary keys are excluded
outright; a twelve-row table's `id` passes the cardinality test and listing
`1, 2, 3, 4, 5` is noise.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_SAMPLE_VALUES = 5
DEFAULT_MAX_DISTINCT_VALUES = 20
DEFAULT_MAX_SAMPLE_CHARS = 40

TRUNCATION_MARK = "…"


class SchemaError(Exception):
    """Base class for every failure in stage 02."""


class SchemaUnavailableError(SchemaError):
    """The database cannot be opened or introspected."""


@dataclass(frozen=True)
class Column:
    """One column, as the database describes it."""

    name: str
    type: str
    nullable: bool
    primary_key: bool
    sample_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForeignKey:
    """One declared relationship, with the nullability of the referring side.

    `nullable` is not decoration: a join through a nullable key drops rows that
    have no counterpart, so the slicer prefers a join path made of NOT NULL
    edges when it has a choice.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    nullable: bool


@dataclass(frozen=True)
class Table:
    """One table: its columns in declaration order, its edges, and its size."""

    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...]
    row_count: int

    def column(self, name: str) -> Column | None:
        """Look a column up case-insensitively, as SQLite resolves identifiers."""
        folded = name.strip().lower()
        for column in self.columns:
            if column.name.lower() == folded:
                return column
        return None


@dataclass(frozen=True)
class SchemaCard:
    """Everything the guard and the prompt need to know about a database."""

    tables: tuple[Table, ...]
    schema_sha256: str

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.name for table in self.tables)

    def table(self, name: str) -> Table | None:
        """Look a table up case-insensitively.

        SQLite identifiers are case-insensitive, so `SELECT * FROM ORDERS`
        names the same table as `orders`. A guard that treated them as
        different would report a hallucinated table for a query that runs.
        """
        folded = name.strip().lower()
        for table in self.tables:
            if table.name.lower() == folded:
                return table
        return None

    def has_column(self, table: str, column: str) -> bool:
        """Whether `table.column` exists. False for an unknown table."""
        found = self.table(table)
        return found is not None and found.column(column) is not None

    def tables_with_column(self, column: str) -> tuple[str, ...]:
        """Every table carrying a column of this name, in card order.

        The raw material of the ambiguity check: an unqualified `id` in a join
        of two tables that both have one is not a column reference, it is a
        question the database will refuse.
        """
        return tuple(table.name for table in self.tables if table.column(column) is not None)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    if not db_path.exists():
        raise SchemaUnavailableError(f"database not found: {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        # `sqlite3.connect` is lazy: a file that is not a database opens
        # cleanly and fails on the first read. Forcing a read here turns that
        # into one typed error at the boundary rather than a bare
        # `DatabaseError` from somewhere in the middle of introspection.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        raise SchemaUnavailableError(f"database could not be read: {db_path} ({exc})") from exc
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [name for (name,) in rows]


def _quote(identifier: str) -> str:
    """Quote an identifier for interpolation into a PRAGMA or a COUNT.

    A table name cannot be a bound parameter in SQLite, so these three
    statements are the only place in the package where an identifier is
    formatted into SQL. The names come from `sqlite_master` — the database's
    own record of what it contains, never from a question, a model or a config
    file — and doubling any embedded quote makes the result a well-formed
    quoted identifier whatever the name is.
    """
    return '"' + identifier.replace('"', '""') + '"'


def _sample_values(
    conn: sqlite3.Connection,
    table: str,
    column: Column,
    *,
    max_sample_values: int,
    max_distinct_values: int,
    max_sample_chars: int,
) -> tuple[str, ...]:
    if column.primary_key:
        return ()
    quoted_table, quoted_column = _quote(table), _quote(column.name)
    distinct = conn.execute(
        f"SELECT COUNT(DISTINCT {quoted_column}) FROM {quoted_table}"  # noqa: S608
    ).fetchone()[0]
    if not distinct or distinct > max_distinct_values:
        return ()
    rows = conn.execute(
        f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "  # noqa: S608
        f"WHERE {quoted_column} IS NOT NULL ORDER BY {quoted_column} LIMIT ?",
        (max_sample_values,),
    ).fetchall()
    values = []
    for (raw,) in rows:
        text = str(raw)
        if len(text) > max_sample_chars:
            text = text[: max_sample_chars - len(TRUNCATION_MARK)] + TRUNCATION_MARK
        values.append(text)
    return tuple(values)


def _is_rowid_alias(declared: str, primary_key: bool) -> bool:
    """Whether this column is SQLite's `INTEGER PRIMARY KEY` rowid alias.

    It matters because `PRAGMA table_info` reports `notnull = 0` for such a
    column even though it can never hold NULL — the rowid always has a value.
    Reporting that verbatim would put "id: nullable yes" in the schema card,
    which is the one artefact the whole system asks a model to trust, and it
    would be false.

    Other primary keys are left as `table_info` describes them. SQLite really
    does permit NULL in a non-INTEGER PRIMARY KEY — a long-standing quirk kept
    for compatibility — so "correcting" those would be inventing a constraint
    the database does not enforce.
    """
    return primary_key and declared.strip().upper() == "INTEGER"


def _columns(conn: sqlite3.Connection, table: str) -> tuple[Column, ...]:
    rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    columns = []
    for _cid, name, declared, notnull, _default, pk in rows:
        # A column declared with no type has an empty `type` in `table_info`.
        # SQLite calls that BLOB affinity; saying so is more useful to a model
        # than an empty cell.
        declared_type = (declared or "BLOB").upper()
        primary_key = bool(pk)
        columns.append(
            Column(
                name=name,
                type=declared_type,
                nullable=not notnull and not _is_rowid_alias(declared or "", primary_key),
                primary_key=primary_key,
            )
        )
    return tuple(columns)


def _foreign_keys(
    conn: sqlite3.Connection, table: str, columns: tuple[Column, ...]
) -> tuple[ForeignKey, ...]:
    rows = conn.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall()
    by_name = {column.name.lower(): column for column in columns}
    edges = []
    for row in rows:
        _id, _seq, target_table, from_column, to_column = row[0], row[1], row[2], row[3], row[4]
        source = by_name.get(str(from_column).lower())
        edges.append(
            ForeignKey(
                from_table=table,
                from_column=str(from_column),
                to_table=str(target_table),
                # A foreign key declared without an explicit target column
                # references the target's primary key, which `foreign_key_list`
                # reports as NULL. `id` is that column throughout this schema.
                to_column=str(to_column) if to_column is not None else "id",
                nullable=source.nullable if source is not None else True,
            )
        )
    return tuple(sorted(edges, key=lambda fk: (fk.from_column, fk.to_table)))


def _structure_digest(tables: tuple[Table, ...]) -> str:
    """Hash the shape: names, types, nullability, keys and edges.

    Deliberately excludes row counts and sample values — see the module
    docstring. Serialised as sorted JSON so that neither dictionary ordering
    nor a future change in field order can move the digest on its own.
    """
    payload = [
        {
            "table": table.name,
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                }
                for column in table.columns
            ],
            "foreign_keys": [
                {
                    "from": fk.from_column,
                    "to_table": fk.to_table,
                    "to_column": fk.to_column,
                    "nullable": fk.nullable,
                }
                for fk in table.foreign_keys
            ],
        }
        for table in tables
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_schema_card(
    db_path: Path,
    *,
    max_sample_values: int = DEFAULT_MAX_SAMPLE_VALUES,
    max_distinct_values: int = DEFAULT_MAX_DISTINCT_VALUES,
    max_sample_chars: int = DEFAULT_MAX_SAMPLE_CHARS,
) -> SchemaCard:
    """Introspect `db_path` into a `SchemaCard`.

    The database is opened read-only. Nothing in stage 02 writes.

    Raises:
        SchemaUnavailableError: the file is missing, is not a database, or
            could not be introspected.
    """
    conn = _connect(db_path)
    try:
        tables = []
        for name in _table_names(conn):
            columns = _columns(conn, name)
            sampled = tuple(
                Column(
                    name=column.name,
                    type=column.type,
                    nullable=column.nullable,
                    primary_key=column.primary_key,
                    sample_values=_sample_values(
                        conn,
                        name,
                        column,
                        max_sample_values=max_sample_values,
                        max_distinct_values=max_distinct_values,
                        max_sample_chars=max_sample_chars,
                    ),
                )
                for column in columns
            )
            row_count = conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[  # noqa: S608
                0
            ]
            tables.append(
                Table(
                    name=name,
                    columns=sampled,
                    foreign_keys=_foreign_keys(conn, name, columns),
                    row_count=int(row_count),
                )
            )
    except sqlite3.Error as exc:
        raise SchemaUnavailableError(
            f"database could not be introspected: {db_path} ({exc})"
        ) from exc
    finally:
        conn.close()

    frozen = tuple(tables)
    return SchemaCard(tables=frozen, schema_sha256=_structure_digest(frozen))

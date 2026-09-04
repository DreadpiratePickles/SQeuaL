"""Stage 01: build the database from the committed DDL and the seeded rows.

The DDL is read from `data/schema.sql` and executed verbatim; this module
issues no `CREATE` of its own. There is therefore exactly one description of
the shape of the database, it is committed, and a human can read it — which
matters more than usual here, because the guard decides whether a column is a
hallucination by comparing it against that shape.

The database itself is **not** committed. It is a build artefact, and a
deterministic generator plus a seed is a smaller, more reviewable and more
honest way to carry a 700 KB binary in Git than the binary. `data/*.db` is
gitignored for that reason.

The build is atomic: rows go into a temporary file beside the destination and
the file is renamed into place only after every insert and every integrity
check has passed. A half-populated database that looked fine to `SELECT
COUNT(*)` and failed on the one join a question needed would be worse than no
database at all.
"""

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

from .rows import Dataset, generate

REQUIRED_TABLES: tuple[str, ...] = (
    "agents",
    "customers",
    "order_items",
    "orders",
    "products",
    "refunds",
    "tickets",
)
"""Sorted, because the digest and every "which tables exist" check reads this
order and a set's iteration order is not a promise."""

INSERTS: dict[str, str] = {
    "customers": "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)",
    "products": "INSERT INTO products VALUES (?, ?, ?, ?)",
    "agents": "INSERT INTO agents VALUES (?, ?, ?, ?)",
    "orders": "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
    "order_items": "INSERT INTO order_items VALUES (?, ?, ?, ?, ?)",
    "tickets": "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "refunds": "INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?)",
}
"""Parameterised, never formatted. Nothing in a generated row reaches SQLite as
part of a statement string — the same rule that holds for a model's SQL holds
for the generator's own rows, and for the same reason."""

INSERT_ORDER: tuple[str, ...] = (
    "customers",
    "products",
    "agents",
    "orders",
    "order_items",
    "tickets",
    "refunds",
)
"""Parents before children, so `PRAGMA foreign_keys = ON` holds throughout the
build rather than only at the end."""


class DatabaseBuildError(Exception):
    """The database could not be built, or could not be read back."""


def _validate_seed(seed: object) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DatabaseBuildError(f"seed must be an integer, got {type(seed).__name__}")
    return seed


def _read_schema_sql(schema_sql_path: Path) -> str:
    try:
        text = Path(schema_sql_path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DatabaseBuildError(
            f"schema file not found: {schema_sql_path}. The committed data/schema.sql is the "
            "one definition of this database's shape; point at it rather than inventing DDL."
        ) from exc
    except OSError as exc:
        raise DatabaseBuildError(f"schema file could not be read: {schema_sql_path}") from exc
    if not text.strip():
        raise DatabaseBuildError(f"schema file is empty: {schema_sql_path}")
    return text


def _fill(conn: sqlite3.Connection, dataset: Dataset) -> None:
    for table in INSERT_ORDER:
        rows = getattr(dataset, table)
        try:
            conn.executemany(INSERTS[table], rows)
        except sqlite3.Error as exc:
            # The offending row is deliberately absent from the message. It is
            # generated rather than personal here, but the habit is the point:
            # an error that quotes its input is an error that leaks its input
            # the first time the input is somebody's.
            raise DatabaseBuildError(f"could not insert into {table}: {exc}") from exc


def _check_integrity(conn: sqlite3.Connection) -> None:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise DatabaseBuildError(
            f"the generated rows violate {len(violations)} foreign key constraint(s); "
            "the generator and data/schema.sql disagree about this database's shape"
        )


def build_database(
    out_path: Path,
    *,
    schema_sql_path: Path,
    seed: int,
    overwrite: bool = False,
) -> Path:
    """Create the database at `out_path` and return the path.

    Args:
        out_path: where the `.db` file goes. Its parent is created if missing.
        schema_sql_path: the committed DDL, executed verbatim.
        seed: the PRNG seed. The same seed gives byte-identical output.
        overwrite: replace an existing file. Off by default — rebuilding over a
            database somebody is querying is a destructive act and should be
            asked for.

    Raises:
        DatabaseBuildError: for a bad seed, unreadable or invalid DDL, a schema
            that does not define the tables the generator fills, an insert
            failure, or a foreign key violation. Nothing is left at `out_path`
            when the build fails.
    """
    seed = _validate_seed(seed)
    out_path = Path(out_path)
    if out_path.exists() and not overwrite:
        raise DatabaseBuildError(
            f"{out_path} already exists. Pass overwrite=True (or `--force` on the command line) "
            "to replace it."
        )

    schema_sql = _read_schema_sql(schema_sql_path)
    dataset = generate(seed)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".partial", dir=out_path.parent
    )
    os.close(handle)
    temp_path = Path(temp_name)
    # mkstemp creates an empty file, which sqlite3 is happy to adopt as a new
    # database. It is removed on every failure path below, so a failed build
    # leaves neither a partial database at the destination nor litter beside it.
    try:
        conn = sqlite3.connect(temp_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                conn.executescript(schema_sql)
            except sqlite3.Error as exc:
                raise DatabaseBuildError(f"schema could not be applied: {exc}") from exc
            _fill(conn, dataset)
            _check_integrity(conn)
            conn.commit()
        finally:
            conn.close()
        os.replace(temp_path, out_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return out_path


def row_digest(db_path: Path) -> str:
    """A SHA-256 over every row of every table, in a fixed order.

    The weaker sibling of byte-equality, and the one worth keeping: it says
    "these two databases hold the same data" without also asserting that two
    SQLite builds lay out their pages identically. Every table is read in
    `REQUIRED_TABLES` order and every row in primary-key order, so the digest
    depends on the data and on nothing else.

    Raises:
        DatabaseBuildError: if the file is missing or is not a readable database.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise DatabaseBuildError(f"database not found: {db_path}")
    digest = hashlib.sha256()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DatabaseBuildError(f"database could not be opened: {db_path} ({exc})") from exc
    try:
        for table in REQUIRED_TABLES:
            digest.update(f"\ntable:{table}\n".encode())
            try:
                cursor = conn.execute(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
            except sqlite3.Error as exc:
                raise DatabaseBuildError(f"{db_path}: could not read {table} ({exc})") from exc
            for row in cursor:
                digest.update(repr(row).encode("utf-8"))
    finally:
        conn.close()
    return digest.hexdigest()

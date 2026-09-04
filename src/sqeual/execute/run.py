"""Stage 04: run a guarded statement and return typed rows.

What comes back is a `ResultSet`, not a cursor and not a string. Phase C renders
the answer from these rows in code — that is what "the model never writes a
number" means in practice — so the rows have to arrive as data with their
provenance attached: how many there were, whether the cap cut them off, how
long it took, and what SQLite actually did to produce them.

`truncated` is the field that earns its keep. A caller that received 500 rows
and did not know whether there were 501 would report a sum that is wrong and
looks right. Silence about truncation is the same class of mistake as a failed
read that becomes an empty result.
"""

import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..schema.card import SchemaCard
from .connection import open_readonly
from .errors import ExecutionError, ExecutionTimeout

SCAN_PREFIX = "SCAN "
"""How SQLite's query planner announces that it will visit every row.

`SEARCH x USING INDEX ...` is the seek form and is not warned about. `SCAN x
USING COVERING INDEX ...` *is* warned about, and the distinction is worth
knowing: a covering-index scan reads the index instead of the table heap, which
is cheaper per row, and still reads every row. The warning is about the row
count, not about whether an index exists, which is why its wording says "every
row is visited" rather than the tempting and wrong "no index was used"."""


@dataclass(frozen=True)
class ExecuteLimits:
    """The limits the sandbox enforces whatever the statement says."""

    max_ms: int
    max_rows: int
    plan_scan_row_threshold: int

    @classmethod
    def from_settings(cls, settings) -> "ExecuteLimits":
        """Build limits from a validated `[execute]` section."""
        return cls(
            max_ms=settings.max_ms,
            max_rows=settings.max_rows,
            plan_scan_row_threshold=settings.plan_scan_row_threshold,
        )

    def validate(self) -> "ExecuteLimits":
        """Check the limits at the boundary.

        Raises:
            ValueError: if a limit is not a positive integer. A zero budget
                would abort every query and a zero row cap would return nothing
                from a query that worked, which is worse than an error.
        """
        for name, value, minimum in (
            ("max_ms", self.max_ms, 1),
            ("max_rows", self.max_rows, 1),
            ("plan_scan_row_threshold", self.plan_scan_row_threshold, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}, got {value!r}")
        return self


@dataclass(frozen=True)
class ExecutionPlan:
    """What SQLite said it was going to do, and what about that is worth saying.

    Warnings are warnings and never failures. A full scan of 2,000 rows is how
    you correctly answer "how many orders were there"; a tool that refused it
    would be wrong more often than the query is.
    """

    steps: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResultSet:
    """The rows, and everything a caller needs to know about how they got here."""

    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    row_count: int
    truncated: bool
    elapsed_ms: int
    plan: ExecutionPlan


def _scanned_table(step: str) -> str | None:
    """The name in a `SCAN ...` plan step, if it is one."""
    if not step.startswith(SCAN_PREFIX):
        return None
    parts = step[len(SCAN_PREFIX) :].split()
    return parts[0] if parts else None


def _plan_warnings(
    steps: tuple[str, ...],
    card: SchemaCard | None,
    threshold: int,
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Full scans of tables larger than `threshold`.

    Row counts come from the schema card. Without a card there is no basis for
    a warning, and inventing one would be inventing a number.

    `aliases` maps the name SQLite prints to the real table. It matters more
    than it looks: `EXPLAIN QUERY PLAN` names the *alias*, so a join written
    `FROM refunds r` produces `SCAN r`, and looking `r` up in the card finds
    nothing. Without the map this warning is silent on exactly the queries it
    is most useful for — and a warning that is silent when it should fire is
    worse than no warning, because its absence reads as an all-clear.
    `GuardReport.table_aliases` supplies it.
    """
    if card is None:
        return ()
    resolved_aliases = {name.lower(): real for name, real in (aliases or {}).items()}
    warnings = []
    for step in steps:
        name = _scanned_table(step)
        if name is None:
            continue
        table = card.table(resolved_aliases.get(name.lower(), name))
        if table is not None and table.row_count > threshold:
            warnings.append(
                f"full scan of {table.name} ({table.row_count} rows, above the "
                f"{threshold}-row threshold): every row is visited"
            )
    return tuple(warnings)


def _capture_plan(conn: sqlite3.Connection, sql: str) -> tuple[str, ...]:
    """`EXPLAIN QUERY PLAN` for a statement, or nothing if it will not explain.

    The statement is prefixed rather than parameterised because a statement
    cannot be a bound parameter. That is safe here for a reason worth naming:
    by the time this runs the guard has proved the string is exactly one
    SELECT with its comments stripped, and `sqlite3.Connection.execute` refuses
    more than one statement in any case. Nothing is being concatenated except a
    fixed keyword onto a statement that has already been parsed.

    A plan that cannot be produced is not a failure — the query itself is about
    to run and will report its own errors, with a better message.
    """
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    except sqlite3.Error:
        return ()
    return tuple(str(row[-1]) for row in rows)


def execute_sql(
    sql: str,
    db_path: Path,
    *,
    limits: ExecuteLimits,
    card: SchemaCard | None = None,
    aliases: Mapping[str, str] | None = None,
) -> ResultSet:
    """Run one guarded statement against a read-only connection.

    Args:
        sql: the statement to run. Callers pass `GuardReport.normalised_sql`,
            never a model's original string — what runs is then exactly what
            was checked.
        db_path: the database. Opened `mode=ro`; see `connection.py`.
        limits: the wall-clock budget and the row cap.
        card: used only to turn a full scan into a warning. Optional.
        aliases: visible name -> real table, from `GuardReport.table_aliases`.
            Without it a scan warning cannot fire on an aliased join.

    Raises:
        ExecutionError: the statement was refused or failed while running.
        ExecutionTimeout: the statement exceeded `limits.max_ms`.
        DatabaseUnavailableError: the database is missing or unreadable.
    """
    limits.validate()
    if not isinstance(sql, str) or not sql.strip():
        raise ExecutionError("the statement is empty")

    with open_readonly(db_path, max_ms=limits.max_ms) as conn:
        steps = _capture_plan(conn, sql)
        started = time.monotonic()
        try:
            cursor = conn.execute(sql)
            # One more than the cap, so that "there were exactly max_rows" and
            # "there were more and we stopped" are distinguishable. Fetching
            # everything and slicing would defeat the cap's whole purpose.
            fetched = cursor.fetchmany(limits.max_rows + 1)
        except sqlite3.Error as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # Two signals, because neither alone is sound. SQLite reports an
            # aborted statement as `OperationalError: interrupted`, which is
            # exact but is a message rather than a code. The elapsed check
            # catches a build that words it differently — at the cost of
            # mislabelling an unrelated failure that happened to arrive after
            # the budget, which is why the message check comes first.
            if "interrupted" in str(exc).lower() or elapsed_ms >= limits.max_ms:
                raise ExecutionTimeout(
                    f"the query exceeded its {limits.max_ms} ms budget and was aborted"
                ) from exc
            # The SQLite message is kept because it is the only thing that says
            # *which* column or table was the problem. The statement is not, on
            # purpose — see errors.py.
            raise ExecutionError(f"the query failed: {exc}") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        columns = tuple(description[0] for description in cursor.description or ())

    truncated = len(fetched) > limits.max_rows
    rows = tuple(tuple(row) for row in fetched[: limits.max_rows])
    return ResultSet(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        plan=ExecutionPlan(
            steps=steps,
            warnings=_plan_warnings(steps, card, limits.plan_scan_row_threshold, aliases),
        ),
    )

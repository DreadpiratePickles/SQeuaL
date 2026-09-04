"""`sqeual guard` and `sqeual run` — check a statement, and run a checked one.

`run` is the whole Phase A chain in one command: read the card, guard the SQL,
and execute **the statement the guard produced** — never the one that was
typed. That last part is the property everything else rests on, so it is one
line of code and it is worth pointing at:

    execute_sql(report.normalised_sql, ...)

There is no code path in this package that executes a caller's string.
"""

import argparse
from collections.abc import Callable, Sequence

from .cli_schema import load_card
from .execute import ExecuteLimits, ResultSet, execute_sql
from .guard import GuardPolicy, guard_sql, render_report

Echo = Callable[..., None]

MAX_CELL_CHARS = 32
"""Cells are truncated for display only. The `ResultSet` a caller gets back
holds the full values; this is a terminal, not the result."""


def add_query_commands(subparsers: argparse._SubParsersAction, common) -> None:
    guard = subparsers.add_parser(
        "guard", parents=[common], help="check a statement without running it"
    )
    guard.add_argument("--sql", required=True, help="the statement to check")

    run = subparsers.add_parser(
        "run", parents=[common], help="check a statement and run it read-only"
    )
    run.add_argument("--sql", required=True, help="the statement to check and run")
    run.add_argument(
        "--plan", action="store_true", help="also print EXPLAIN QUERY PLAN and any warnings"
    )
    run.add_argument(
        "--rules", action="store_true", help="also print the full guard rule table"
    )


def _cell(value: object) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def _render_rows(result: ResultSet, echo: Echo) -> None:
    if not result.columns:
        echo("  (no columns)")
        return
    table: Sequence[Sequence[str]] = [
        list(result.columns),
        *[[_cell(value) for value in row] for row in result.rows],
    ]
    widths = [max(len(row[index]) for row in table) for index in range(len(result.columns))]
    for position, row in enumerate(table):
        echo("  " + "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
        if position == 0:
            echo("  " + "  ".join("-" * width for width in widths))


def command_guard(args: argparse.Namespace, config, echo: Echo) -> bool:
    card = load_card(config)
    report = guard_sql(args.sql, card, GuardPolicy.from_settings(config.guard))
    echo(render_report(report))
    return report.ok


def command_run(args: argparse.Namespace, config, echo: Echo) -> bool | ResultSet:
    """Guard, then execute. Returns False when the guard refused.

    Nothing touches the database until the guard has passed, which is why a
    refused statement costs no connection and no query plan.
    """
    card = load_card(config)
    report = guard_sql(args.sql, card, GuardPolicy.from_settings(config.guard))
    if not report.ok or args.rules:
        echo(render_report(report))
    if not report.ok:
        return False

    result = execute_sql(
        report.normalised_sql,
        config.db.path,
        limits=ExecuteLimits.from_settings(config.execute),
        card=card,
        aliases=dict(report.table_aliases),
    )
    _render_rows(result, echo)
    noun = "row" if result.row_count == 1 else "rows"
    suffix = " (truncated at the row cap)" if result.truncated else ""
    echo(f"  {result.row_count} {noun} in {result.elapsed_ms} ms{suffix}")
    if args.plan:
        for step in result.plan.steps:
            echo(f"  plan: {step}")
    for warning in result.plan.warnings:
        echo(f"  warning: {warning}")
    return result

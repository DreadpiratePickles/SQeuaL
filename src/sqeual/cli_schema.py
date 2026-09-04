"""`sqeual schema show` and `sqeual schema slice`.

`show` prints the card. `slice` prints which tables a question would reach and
*why* — the reasons are the whole point of the command. A slicer nobody can
interrogate is a slicer nobody can fix, and the first thing anybody asks when a
question gets the wrong answer is "what did the model actually see".
"""

import argparse
from collections.abc import Callable

from .schema.card import read_schema_card
from .schema.render import render_card
from .schema.slice import slice_for_question

Echo = Callable[..., None]


def add_schema_commands(subparsers: argparse._SubParsersAction, common) -> None:
    parser = subparsers.add_parser("schema", help="read and slice the schema card")
    actions = parser.add_subparsers(dest="action", required=True)

    show = actions.add_parser("show", parents=[common], help="print the schema card")
    show.add_argument(
        "--tables",
        default=None,
        help="comma-separated subset to print, in that order. Default: all of them.",
    )

    sliced = actions.add_parser(
        "slice", parents=[common], help="which tables a question reaches, and why"
    )
    sliced.add_argument("--question", required=True, help="the question, in plain English")
    sliced.add_argument(
        "--max-tables",
        type=int,
        default=None,
        help="override [schema] max_tables for this run.",
    )
    sliced.add_argument(
        "--card",
        action="store_true",
        help="also print the rendered card the slice would send to a model.",
    )


def load_card(config):
    """The schema card for the configured database, at the configured limits."""
    return read_schema_card(
        config.db.path,
        max_sample_values=config.schema.max_sample_values,
        max_distinct_values=config.schema.max_distinct_values,
        max_sample_chars=config.schema.max_sample_chars,
    )


def command_schema_show(args: argparse.Namespace, config, echo: Echo) -> bool:
    card = load_card(config)
    names = None
    if args.tables:
        names = tuple(name.strip() for name in args.tables.split(",") if name.strip())
    echo(render_card(card, names))
    return True


def command_schema_slice(args: argparse.Namespace, config, echo: Echo) -> bool:
    """Returns whether the slice found anything.

    A slice that matched nothing is a *finding*, not a crash: the caller should
    fall back to the whole card and say why, and the exit code says which
    happened.
    """
    card = load_card(config)
    result = slice_for_question(
        args.question,
        card,
        max_tables=args.max_tables or config.schema.max_tables,
        synonyms=config.schema.synonyms,
    )
    echo(f"question: {result.question}")
    if not result.tables:
        echo("  no table matched. Send the whole card, or add a synonym to [schema.synonyms].")
        return False
    for index, entry in enumerate(result.entries, start=1):
        echo(f"  {index}. {entry.table:<14} {entry.reason}")
    if args.card:
        echo("")
        echo(render_card(card, result.tables))
    return True

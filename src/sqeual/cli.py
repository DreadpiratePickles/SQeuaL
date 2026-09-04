"""The command line: `db`, `schema`, `guard` and `run`.

The logic lives in the packages this imports; this file is the parser, the
dispatch table and the exit codes. `main()` returns a code rather than calling
`exit`, so the test suite exercises it directly without a subprocess.

The four exit codes are a fixed vocabulary and the distinction between 1 and 3
is the one that matters in a pipeline:

    0  the run completed and found nothing wrong
    1  the run completed and produced a finding — the guard refused the SQL,
       or a slice matched no table. Phase B feeds the finding back to a model.
    2  the run never started — bad configuration, missing database, bad usage.
       Nothing about the question would change this.
    3  the run started and the execution failed — a timeout or a SQLite error.
       The SQL was fine; rewriting it would not help.

Projects 1, 2 and 9 use 0/1/2 with the same meanings. The 3 is new here because
this tool has a stage that can fail *after* everything about the request was
found to be correct, and collapsing that into 1 would tell a repair loop to
rewrite a query that had nothing wrong with it.

**Phase A calls no model.** There is no `ask` command yet, and nothing here
reads an API key or opens a socket.
"""

import argparse
from collections.abc import Callable, Sequence

from .cli_db import add_db_commands, command_db_build
from .cli_query import add_query_commands, command_guard, command_run
from .cli_schema import add_schema_commands, command_schema_show, command_schema_slice
from .config_file import DEFAULT_CONFIG_PATH, ConfigFileError, load_config
from .db.build import DatabaseBuildError
from .execute import ExecuteError, ExecutionError, ExecutionTimeout
from .schema.card import SchemaError, SchemaUnavailableError

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_BAD_CONFIG = 2
EXIT_EXECUTION_FAILED = 3

Echo = Callable[..., None]

DESCRIPTION = (
    "Text-to-SQL with guardrails. Phase A: build the database, read its schema "
    "card, guard a statement, and run a guarded one read-only. No model is called."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqeual", description=DESCRIPTION)
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="path to sqeual.toml. Defaults to ./sqeual.toml.",
    )

    # The same option again on every leaf subcommand, so that both
    # `sqeual --config x guard --sql ...` and `sqeual guard --config x --sql ...`
    # work. The second is how people actually type it, and argparse does not
    # accept a top-level optional after the subcommand name.
    #
    # `SUPPRESS` is load-bearing: without it the subparser writes its own
    # default into the namespace and silently overwrites a `--config` given
    # before the subcommand (CPython bpo-9351). With it the key appears only
    # when the option was actually typed.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command")
    add_db_commands(subparsers, common)
    add_schema_commands(subparsers, common)
    add_query_commands(subparsers, common)
    return parser


def _dispatch(args: argparse.Namespace, config, echo: Echo) -> int:
    if args.command == "db":
        command_db_build(args, config, echo)
        return EXIT_OK
    if args.command == "schema":
        if args.action == "show":
            command_schema_show(args, config, echo)
            return EXIT_OK
        return EXIT_OK if command_schema_slice(args, config, echo) else EXIT_FINDING
    if args.command == "guard":
        return EXIT_OK if command_guard(args, config, echo) else EXIT_FINDING
    return EXIT_OK if command_run(args, config, echo) else EXIT_FINDING


def main(argv: Sequence[str] | None = None, echo: Echo = print) -> int:
    """Run one command and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        echo(parser.format_usage().strip())
        echo("say one of: db build, schema show, schema slice, guard, run")
        return EXIT_BAD_CONFIG

    try:
        config = load_config(args.config)
    except ConfigFileError as exc:
        echo(f"configuration error: {exc}")
        return EXIT_BAD_CONFIG

    try:
        return _dispatch(args, config, echo)
    except (DatabaseBuildError, SchemaUnavailableError) as exc:
        # The run never started: the database is missing, unreadable, or
        # already there when a build was asked for. Nothing about the question
        # would change any of those.
        echo(f"cannot start: {exc}")
        return EXIT_BAD_CONFIG
    except SchemaError as exc:
        echo(f"schema error: {exc}")
        return EXIT_BAD_CONFIG
    except (ExecutionTimeout, ExecutionError) as exc:
        echo(f"execution failed: {exc}")
        return EXIT_EXECUTION_FAILED
    except ExecuteError as exc:
        echo(f"cannot start: {exc}")
        return EXIT_BAD_CONFIG

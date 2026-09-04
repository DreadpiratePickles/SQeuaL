"""`sqeual db build` — generate the database from the committed DDL.

The only command in Phase A that writes anything, and the reason `--force`
exists: rebuilding over a database somebody is querying is a destructive act,
so it has to be asked for rather than assumed.
"""

import argparse
from collections.abc import Callable

from .db.build import build_database, row_digest
from .schema.card import read_schema_card

Echo = Callable[..., None]


def add_db_commands(subparsers: argparse._SubParsersAction, common) -> None:
    parser = subparsers.add_parser("db", help="build the database")
    actions = parser.add_subparsers(dest="action", required=True)

    build = actions.add_parser(
        "build", parents=[common], help="generate the database from data/schema.sql"
    )
    build.add_argument(
        "--out",
        default=None,
        help="where to write it. Defaults to [db] path in the configuration.",
    )
    build.add_argument(
        "--force",
        action="store_true",
        help="replace an existing database. Off by default: rebuilding over one "
        "somebody is querying is destructive.",
    )


def command_db_build(args: argparse.Namespace, config, echo: Echo) -> None:
    out_path = args.out or config.db.path
    build_database(
        out_path,
        schema_sql_path=config.db.schema_sql_path,
        seed=config.db.seed,
        overwrite=args.force,
    )
    echo(f"built {out_path} from {config.db.schema_sql_path.name}, seed {config.db.seed}")
    card = read_schema_card(
        out_path,
        max_sample_values=config.schema.max_sample_values,
        max_distinct_values=config.schema.max_distinct_values,
        max_sample_chars=config.schema.max_sample_chars,
    )
    for table in card.tables:
        echo(f"  {table.name:<14} {table.row_count:>6} rows")
    # Printed so that "did my rebuild produce the same database" is a question
    # anybody can answer by eye, without diffing two binaries.
    echo(f"  row digest {row_digest(out_path)}")
    echo(f"  schema     {card.schema_sha256}")

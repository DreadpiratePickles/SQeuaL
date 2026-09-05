"""Stage 03: run every rule against one proposed statement.

The order of the rules is load-bearing. Each one assumes the ones before it
held: there is no point resolving columns in a statement that did not parse, or
counting subquery depth in a `DROP TABLE`. A rule whose precondition failed is
reported as SKIP rather than omitted, so a report always has the same fourteen
lines and "we checked and it was fine" never renders the same as "we never
looked".

`normalised_sql` is produced only for a passing statement, and it is the string
callers execute. That is the strongest property this module has: what runs is
exactly what was checked, regenerated from the tree the checks read, with
comments stripped and the row limit already in it. The model's original string
is never executed.
"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..schema.card import SchemaCard
from .exposure import check_bulk_export, check_denied_columns
from .policy import GuardPolicy
from .report import GuardReport, RuleResult, RuleStatus
from .resolve import resolve_columns
from .rules import (
    apply_row_limit,
    check_allowed_functions,
    check_allowed_tables,
    check_known_columns,
    check_known_tables,
    check_no_forbidden_syntax,
    check_select_only,
    check_single_statement,
    check_star_expansion,
    check_subquery_depth,
    check_unambiguous_columns,
    referenced_tables,
    skipped,
)

DIALECT = "sqlite"

RULE_ORDER: tuple[str, ...] = (
    "parses",
    "single_statement",
    "no_forbidden_syntax",
    "select_only",
    "known_tables",
    "allowed_tables",
    "known_columns",
    "unambiguous_columns",
    "allowed_functions",
    "subquery_depth",
    "star_expansion",
    "denied_columns",
    "bulk_export",
    "row_limit",
)
"""Every report lists these fourteen, in this order, whatever happened.

The two exposure rules sit **before** `row_limit` because `bulk_export` reads
the LIMIT the model wrote, and `row_limit` is the rule that writes one. A
`bulk_export` satisfied by the guard's own injected LIMIT would be the guard
grading its own homework."""


def _substantive(statements: list[exp.Expression | None]) -> list[exp.Expression]:
    """Drop the empty tails sqlglot returns for trailing semicolons.

    `SELECT 1;` parses to a statement plus a `None`, and `SELECT 1; -- x` to a
    statement plus a bare `Semicolon`. Counting those would reject a query
    nobody should have to think twice about. The filter is deliberately narrow:
    dropping every falsy element, or every element the caller does not
    recognise, is how a real second statement gets through.
    """
    return [
        statement
        for statement in statements
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]


def _all_skipped(*, done: dict[str, RuleResult], why: str) -> list[RuleResult]:
    return [
        done[name] if name in done else skipped(name, why)
        for name in RULE_ORDER
    ]


def _rejected(done: dict[str, RuleResult], why: str) -> GuardReport:
    return GuardReport(
        rules=tuple(_all_skipped(done=done, why=why)),
        normalised_sql=None,
        tables_used=(),
        columns_used=(),
    )


def guard_sql(sql: str, card: SchemaCard, policy: GuardPolicy) -> GuardReport:
    """Check one proposed statement and return the full rule table.

    The statement is untrusted input. It is parsed rather than pattern-matched,
    every table and column is resolved against `card`, and the statement that
    comes back in `normalised_sql` — the only one a caller should execute — is
    regenerated from the tree that was checked.

    Never raises for a bad statement: a rejection is a report, not an
    exception, because the caller has to be able to show a human *why* and
    Phase B has to be able to feed the codes back to a model.
    """
    done: dict[str, RuleResult] = {}

    try:
        parsed = sqlglot.parse(sql, dialect=DIALECT)
    except SqlglotError as exc:
        # The message deliberately does not quote the statement. A guard log
        # that echoes what it rejected carries whatever that carried, and this
        # is the one path where the input is known to be malformed and may be
        # anything at all.
        first_line = str(exc).splitlines()[0] if str(exc).strip() else "unparseable"
        done["parses"] = RuleResult(
            "parses", RuleStatus.FAIL, f"could not be parsed as SQLite: {first_line}", "parse_error"
        )
        return _rejected(done, "the statement did not parse")

    statements = _substantive(parsed)
    if not statements:
        done["parses"] = RuleResult(
            "parses", RuleStatus.FAIL, "the statement is empty", "empty_statement"
        )
        return _rejected(done, "the statement is empty")

    done["parses"] = RuleResult("parses", RuleStatus.PASS, "parsed as SQLite")
    done["single_statement"] = check_single_statement(statements)
    done["no_forbidden_syntax"] = check_no_forbidden_syntax(statements)

    statement = statements[0]
    done["select_only"] = check_select_only(statement)

    if any(result.status is RuleStatus.FAIL for result in done.values()):
        # Column resolution and the rewrite are meaningless on a statement that
        # is not a single, self-contained SELECT. Stopping here also means the
        # report never carries a `normalised_sql` for something that was
        # refused, which is the property that stops one being executed.
        return _rejected(done, "the statement is not a single SELECT")

    tables = referenced_tables(statement)
    done["known_tables"] = check_known_tables(tables, card)
    done["allowed_tables"] = check_allowed_tables(tables, card, policy)

    resolution = resolve_columns(statement, card)
    done["known_columns"] = check_known_columns(resolution)
    done["unambiguous_columns"] = check_unambiguous_columns(resolution)
    done["allowed_functions"] = check_allowed_functions(statement, policy)
    done["subquery_depth"] = check_subquery_depth(statement, policy)
    done["star_expansion"] = check_star_expansion(statement, card, policy)
    done["denied_columns"] = check_denied_columns(statement, card, policy)
    done["bulk_export"] = check_bulk_export(statement, card, policy)

    limited, limit_result = apply_row_limit(statement, policy)
    done["row_limit"] = limit_result

    rules = tuple(done[name] for name in RULE_ORDER)
    failed = any(result.status is RuleStatus.FAIL for result in rules)

    table_names = tuple(
        sorted({found.name for table in tables if (found := card.table(table.name)) is not None})
    )
    aliases = {}
    for table in tables:
        found = card.table(table.name)
        if found is not None:
            aliases[table.alias_or_name or found.name] = found.name
    return GuardReport(
        rules=rules,
        # Comments are stripped: they are attacker-controlled text that would
        # otherwise ride along into the executed statement and into every log
        # that records it.
        normalised_sql=None if failed else limited.sql(dialect=DIALECT, comments=False),
        tables_used=table_names,
        columns_used=resolution.columns_used,
        table_aliases=tuple(sorted(aliases.items())),
    )

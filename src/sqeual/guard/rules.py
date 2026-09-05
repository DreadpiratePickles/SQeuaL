"""The individual checks, each producing one `RuleResult`.

Every function here takes a parsed statement and returns a verdict on one
question. They are separate rather than a single `validate()` because the
report is a table: a caller needs to know *which* rule objected, and a repair
loop needs the finding's code rather than a sentence.

Structural, never textual. Every check reads the parse tree — the node classes,
the resolved sources, the function nodes — and none of them looks at the
original string. `docs/design.md` §14 has the worked example of why: a
blocklist regex for `DROP` rejects `SELECT drop_reason FROM refunds` and passes
`SELECT 1;/**/DrOp TABLE x`, and both mistakes are the same mistake.
"""

from sqlglot import exp

from ..schema.card import SchemaCard
from .policy import DENIED_FUNCTIONS, GuardPolicy
from .report import RuleResult, RuleStatus
from .resolve import AMBIGUOUS_COLUMN, UNKNOWN_COLUMN, Resolution

FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.Merge,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Use,
    exp.Set,
    exp.Command,
)
"""Node types that may never appear anywhere in the tree.

`exp.Command` is the important one and the least obvious: it is sqlglot's "I do
not model this statement, here is the raw text" node, and `VACUUM`, `REINDEX`
and anything else the parser does not understand become one. Treating an
unmodelled statement as forbidden is the only safe default — the guard cannot
check what it cannot represent.
"""


def _pass(rule: str, detail: str, code: str | None = None) -> RuleResult:
    return RuleResult(rule=rule, status=RuleStatus.PASS, detail=detail, code=code)


def _fail(rule: str, detail: str, code: str) -> RuleResult:
    return RuleResult(rule=rule, status=RuleStatus.FAIL, detail=detail, code=code)


def skipped(rule: str, why: str) -> RuleResult:
    """A check that was not run, reported rather than omitted.

    "We checked and it was fine" and "we never looked" must never render the
    same way.
    """
    return RuleResult(rule=rule, status=RuleStatus.SKIP, detail=why)


# --- structure --------------------------------------------------------------


def check_single_statement(statements: list[exp.Expression]) -> RuleResult:
    count = len(statements)
    if count == 1:
        return _pass("single_statement", "one statement")
    return _fail(
        "single_statement",
        f"{count} statements were submitted; only the first would be checked",
        "multiple_statements",
    )


def check_no_forbidden_syntax(statements: list[exp.Expression]) -> RuleResult:
    """DDL, DML, PRAGMA, ATTACH, transactions, and the file-reaching functions.

    Runs over *every* submitted statement, not only the first, so that
    `SELECT 1; DROP TABLE x` reports what the second statement was as well as
    that there were two.
    """
    offences: list[str] = []
    for statement in statements:
        for node in statement.walk():
            if isinstance(node, FORBIDDEN_NODES):
                offences.append(type(node).__name__.upper())
            if isinstance(node, exp.Anonymous) and str(node.name).upper() in DENIED_FUNCTIONS:
                return _fail(
                    "no_forbidden_syntax",
                    f"{node.name} is denied outright, whatever allowed_functions says",
                    "forbidden_function",
                )
    if offences:
        unique = sorted(set(offences))
        return _fail(
            "no_forbidden_syntax",
            f"statement contains {', '.join(unique)}",
            "forbidden_syntax",
        )
    return _pass("no_forbidden_syntax", "no DDL, DML, PRAGMA, ATTACH or transaction control")


def check_select_only(statement: exp.Expression) -> RuleResult:
    if isinstance(statement, exp.Select):
        shape = "WITH ... SELECT" if statement.args.get("with") else "SELECT"
        return _pass("select_only", f"the statement is a {shape}")
    if isinstance(statement, exp.SetOperation):
        return _fail(
            "select_only",
            f"{type(statement).__name__.upper()} is not supported: the column resolver cannot "
            "prove a set operation safe, and an unchecked query is not a guarded one",
            "unsupported_statement",
        )
    return _fail(
        "select_only",
        f"the statement is a {type(statement).__name__.upper()}, not a SELECT",
        "not_a_select",
    )


# --- tables -----------------------------------------------------------------


def referenced_tables(statement: exp.Expression) -> tuple[exp.Table, ...]:
    """Every real table reference: the CTE names are filtered out.

    A CTE name is not a table, and reporting `r` as a hallucinated table would
    make the guard unusable for any query worth writing.
    """
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    return tuple(
        table
        for table in statement.find_all(exp.Table)
        if table.name.lower() not in cte_names and table.name
    )


def check_known_tables(tables: tuple[exp.Table, ...], card: SchemaCard) -> RuleResult:
    qualified = sorted({f"{t.db or t.catalog}.{t.name}" for t in tables if t.db or t.catalog})
    if qualified:
        return _fail(
            "known_tables",
            f"cross-database reference(s): {', '.join(qualified)}. This tool serves one database",
            "qualified_table",
        )
    unknown = sorted({t.name for t in tables if card.table(t.name) is None})
    if unknown:
        known = ", ".join(card.table_names)
        return _fail(
            "known_tables",
            f"no such table: {', '.join(unknown)}. This database has: {known}",
            "unknown_table",
        )
    return _pass("known_tables", f"{len({t.name.lower() for t in tables})} table(s), all real")


def check_allowed_tables(
    tables: tuple[exp.Table, ...], card: SchemaCard, policy: GuardPolicy
) -> RuleResult:
    if not policy.allowed_tables:
        return _pass("allowed_tables", "policy allows every table in this database")
    refused = sorted(
        {
            t.name
            for t in tables
            if card.table(t.name) is not None and not policy.permits_table(t.name)
        }
    )
    if refused:
        permitted = ", ".join(sorted(policy.allowed_tables))
        return _fail(
            "allowed_tables",
            f"policy does not permit: {', '.join(refused)}. Permitted: {permitted}",
            "table_not_allowed",
        )
    return _pass("allowed_tables", "every table is on the policy's allowlist")


# --- columns ----------------------------------------------------------------


def check_known_columns(resolution: Resolution) -> RuleResult:
    findings = resolution.findings_with(UNKNOWN_COLUMN)
    if findings:
        return _fail(
            "known_columns",
            "; ".join(finding.detail for finding in findings),
            UNKNOWN_COLUMN,
        )
    count = len(resolution.columns_used)
    return _pass("known_columns", f"{count} column reference(s), all real")


def check_unambiguous_columns(resolution: Resolution) -> RuleResult:
    findings = resolution.findings_with(AMBIGUOUS_COLUMN)
    if findings:
        return _fail(
            "unambiguous_columns",
            "; ".join(finding.detail for finding in findings),
            AMBIGUOUS_COLUMN,
        )
    return _pass("unambiguous_columns", "every unqualified column resolves to one source")


# --- functions --------------------------------------------------------------


def surface_name(node: exp.Func) -> str | None:
    """The function name as SQLite would see it, or `None` if it is not a call.

    This exists because `sql_name()` gives sqlglot's *canonical* name, not the
    dialect's. `STRFTIME('%Y', d)` parses to a `TimeToStr` whose canonical name
    is `TIME_TO_STR` and which wraps its argument in a synthetic
    `TsOrDsToTimestamp` the author never wrote. Checking canonical names would
    reject the most useful date function in SQLite and would police a node that
    does not exist in the query.

    So the node is rendered back to SQLite and the leading identifier taken.
    The rendering is sqlglot's own output for one node, never the caller's
    string, and the *decision* still rests on the tree. An `Anonymous` — an
    unknown function — is read from its `name` directly, because a hostile name
    might not render as a bare identifier and must not slip through as "not a
    call". A typed node that renders without a call, like that synthetic
    wrapper, invokes no SQLite function and is transparent.
    """
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    rendered = node.sql(dialect="sqlite")
    head, separator, _ = rendered.partition("(")
    name = head.strip()
    if not separator or not name.isidentifier():
        return None
    return name.upper()


def check_allowed_functions(statement: exp.Expression, policy: GuardPolicy) -> RuleResult:
    called: list[str] = []
    for node in statement.find_all(exp.Func):
        name = surface_name(node)
        if name is not None:
            called.append(name)
    refused = sorted({name for name in called if not policy.permits_function(name)})
    if refused:
        allowed = ", ".join(sorted(policy.allowed_functions))
        return _fail(
            "allowed_functions",
            f"not permitted: {', '.join(refused)}. Allowed: {allowed}",
            "function_not_allowed",
        )
    listed = ", ".join(sorted(set(called))) or "none"
    return _pass("allowed_functions", f"functions called: {listed}")


# --- shape limits -----------------------------------------------------------


def select_depth(select: exp.Select) -> int:
    """How many SELECTs enclose this one, counting a CTE's own body as nested."""
    depth = 0
    current = select.parent
    while current is not None:
        if isinstance(current, exp.Select):
            depth += 1
        current = current.parent
    return depth


def check_subquery_depth(statement: exp.Expression, policy: GuardPolicy) -> RuleResult:
    deepest = max((select_depth(select) for select in statement.find_all(exp.Select)), default=0)
    if deepest > policy.max_subquery_depth:
        return _fail(
            "subquery_depth",
            f"nested {deepest} level(s) deep; the limit is {policy.max_subquery_depth}",
            "subquery_too_deep",
        )
    return _pass("subquery_depth", f"nested to depth {deepest}, limit {policy.max_subquery_depth}")


def starred_tables(
    statement: exp.Select, card: SchemaCard, ctes: set[str]
) -> list[str]:
    """Tables the **outermost** SELECT's `*` would expand over.

    Only the outermost projection, on purpose. This rule is about the shape of
    the *answer* — `SELECT *` is what a model writes when it has not worked out
    which columns answer the question, and a row of twelve columns is a worse
    answer than a number. A `*` inside a CTE that is then aggregated produces
    nothing wide for anyone to read, and refusing it would refuse a perfectly
    good query for a reason that does not apply to it. If an inner star's
    columns really do reach the caller, the outer projection is a star too, and
    that is what this rule sees.

    A bare `*` expands over every source of the SELECT; a qualified `t.*` over
    one. Both are resolved to real tables so the row-count threshold has
    something to apply to.
    """
    sources: list[exp.Table] = []
    from_clause = statement.args.get("from_") or statement.args.get("from")
    if from_clause is not None and isinstance(from_clause.this, exp.Table):
        sources.append(from_clause.this)
    for join in statement.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            sources.append(join.this)
    real = [table for table in sources if table.name.lower() not in ctes]

    names: list[str] = []
    for projection in statement.selects:
        if isinstance(projection, exp.Star):
            names.extend(table.name for table in real)
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            qualifier = projection.table.lower()
            names.extend(
                table.name
                for table in real
                if (table.alias_or_name or table.name).lower() == qualifier
            )
    return names


def check_star_expansion(
    statement: exp.Select, card: SchemaCard, policy: GuardPolicy
) -> RuleResult:
    ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    starred = starred_tables(statement, card, ctes)
    if not starred:
        return _pass("star_expansion", "no SELECT *")
    if policy.allow_star:
        return _pass("star_expansion", "policy permits SELECT * at any size")
    too_big = sorted(
        {
            name
            for name in starred
            if (found := card.table(name)) is not None
            and found.row_count > policy.star_row_threshold
        }
    )
    if too_big:
        return _fail(
            "star_expansion",
            f"SELECT * over {', '.join(too_big)}, above the {policy.star_row_threshold}-row "
            "threshold. Name the columns that answer the question",
            "star_not_allowed",
        )
    small = ", ".join(sorted(set(starred)))
    return _pass("star_expansion", f"SELECT * over small table(s): {small}")


# --- the rewrite ------------------------------------------------------------


def apply_row_limit(statement: exp.Select, policy: GuardPolicy) -> tuple[exp.Select, RuleResult]:
    """Ensure the statement carries a LIMIT no larger than the policy's.

    A rewrite rather than a rejection. A model that forgets a LIMIT has not
    done anything wrong, and refusing the query teaches it nothing; adding the
    LIMIT bounds the damage and reports that it did. This rule never fails —
    it is the one place the guard changes a query instead of judging it.
    """
    limit = statement.args.get("limit")
    if limit is None:
        return statement.limit(policy.max_rows), _pass(
            "row_limit", f"no LIMIT was given; LIMIT {policy.max_rows} added", "limit_injected"
        )

    expression = limit.expression
    if not (isinstance(expression, exp.Literal) and expression.is_int):
        return statement.limit(policy.max_rows), _pass(
            "row_limit",
            f"the LIMIT was not a plain number, so it was replaced with {policy.max_rows}",
            "limit_replaced",
        )

    given = int(expression.name)
    if given > policy.max_rows:
        return statement.limit(policy.max_rows), _pass(
            "row_limit",
            f"LIMIT {given} reduced to {policy.max_rows}",
            "limit_reduced",
        )
    return statement, _pass(
        "row_limit", f"LIMIT {given} is within {policy.max_rows}", "limit_present"
    )

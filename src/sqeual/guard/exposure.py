"""Two rules about what a query is allowed to *expose*, rather than to do.

Every other rule in this package asks whether a statement is well-formed, real,
read-only and bounded. These two ask what reaches the reader, and they exist
because the first live evaluation produced

    SELECT name, email FROM customers LIMIT 200

for "export the full customer list with their email addresses", and every
existing rule passed it. `writefile` was never proposed, nothing was written to
disk, every column resolved against the real schema, and two hundred names and
addresses were printed. The tool did not export a file; it rendered the export.
`docs/design.md` §54.

They are two rules and not one because they refuse different things. **Which**
column is `denied_columns`, and it would refuse a single address as readily as
two hundred. **How many rows** of a wide table is `bulk_export`, and it would
refuse a dump of a column nobody minds. A deployment that wants one should not
have to accept the other.

They draw their boundaries differently, and the difference is not an
inconsistency.

`bulk_export` looks only at the **outermost** query — its projection and its own
FROM and JOIN sources — the same boundary `star_expansion` draws and for the same
reason. It is about the shape of the *answer*, and a table read inside a subquery
to compute a count contributes no rows for anybody to read. A rule that walked
the whole tree would refuse `SELECT name FROM products WHERE id NOT IN (SELECT
product_id FROM order_items)` — a committed golden case — for a reason that does
not apply to it.

`denied_columns` looks at **every** projection, ORDER BY and GROUP BY at every
level, because outermost-only is bypassable in one line:

    WITH c AS (SELECT email FROM customers) SELECT email FROM c

The outer `email` resolves to a CTE, `resolve_one` refuses to claim a table for
it, and a rule reading `None` as "not denied" prints two hundred addresses. The
resolver is right to say nothing — it cannot prove what a CTE column is — so the
rule has to catch the projection that *put* the value there. A denied column in
any select list is refused; a denied column in any `WHERE`, at any level, is not.
The cost of that is stated rather than hidden: `SELECT COUNT(*) FROM (SELECT
email FROM customers) x` leaks nothing and is refused anyway, for the same reason
`allow_denied_in_aggregates` is off by default — the projection made the value
available, and a rule that reasoned about which onward uses were safe would have
to enumerate them.
"""

from sqlglot import exp

from ..schema.card import SchemaCard
from .policy import GuardPolicy
from .report import RuleResult, RuleStatus
from .resolve import resolve_one
from .rules import starred_tables

COLUMN_NOT_ALLOWED = "column_not_allowed"
"""Sibling of `table_not_allowed`, and deliberately not `unknown_column`. One
means the model asked for something real it may not have; the other means it
invented something. One code for both would hide both."""
BULK_EXPORT = "bulk_export"

PROJECTION = "a projection"
ORDER_BY = "an ORDER BY"
GROUP_BY = "a GROUP BY"


def _pass(rule: str, detail: str) -> RuleResult:
    return RuleResult(rule=rule, status=RuleStatus.PASS, detail=detail)


def _fail(rule: str, detail: str, code: str) -> RuleResult:
    return RuleResult(rule=rule, status=RuleStatus.FAIL, detail=detail, code=code)


def _regions(statement: exp.Expression) -> tuple[tuple[str, exp.Expression], ...]:
    """The three places a column's *value* or *grouping* is made available, at
    every level of the statement.

    Every level, because a CTE or a derived table that projects a denied column
    hands it to whatever selects from it, and the resolver cannot prove what a
    column selected out of a CTE refers to. Catching the projection that put the
    value there is the check that cannot be walked around.

    A `WHERE` is deliberately absent, at every level too. It puts no value in
    front of anybody, and the stated limit — that a filter can still be used as
    an oracle one question at a time — is closed by a rate limit or an audit log,
    not by a wider projection rule. Claiming this rule closed it would be a claim
    the code does not support.
    """
    regions: list[tuple[str, exp.Expression]] = []
    for select in statement.find_all(exp.Select):
        regions += [(PROJECTION, projection) for projection in select.expressions]
        order = select.args.get("order")
        if order is not None:
            regions.append((ORDER_BY, order))
        group = select.args.get("group")
        if group is not None:
            regions.append((GROUP_BY, group))
    return tuple(regions)


def _inside_aggregate(column: exp.Column, region: exp.Expression) -> bool:
    """Whether this reference sits under an aggregate within its own region."""
    current = column.parent
    while current is not None:
        if isinstance(current, exp.AggFunc):
            return True
        if current is region:
            return False
        current = current.parent
    return False


def _starred_denials(
    statement: exp.Expression, card: SchemaCard, policy: GuardPolicy
) -> list[str]:
    """Denied columns a `*` would print, at every level of the statement.

    A star names no column and would print every one of them, so it cannot be
    checked by resolving a reference. `star_expansion` already refuses a star over
    a large table, and this is checked independently because the two are
    configured separately: a deployment that set `allow_star` would otherwise have
    switched off a control it was not editing.

    Every level, for the same reason `_regions` walks every level: `WITH c AS
    (SELECT * FROM customers) SELECT email FROM c` puts the column in the CTE's
    projection and names it in an outer query the resolver cannot follow.
    """
    ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    found: list[str] = []
    for select in statement.find_all(exp.Select):
        for name in starred_tables(select, ctes):
            table = card.table(name)
            if table is None:
                continue
            for column in policy.denied_on(table.name):
                entry = f"{table.name}.{column}"
                if entry not in found:
                    found.append(entry)
    return found


def check_denied_columns(
    statement: exp.Select, card: SchemaCard, policy: GuardPolicy
) -> RuleResult:
    """Refuse a statement that would put a denied column in front of a reader.

    Checked in every projection, ORDER BY and GROUP BY, at every level — see the
    module docstring for why outermost-only is bypassable in one line. An
    aggregate over a denied column is refused too unless
    `[guard] allow_denied_in_aggregates` is set: the aggregate is exactly where a
    leak hides, because `MIN(email)` is one address, and an exception for
    "aggregates" would have to enumerate which ones are safe.
    """
    if not policy.denied_columns:
        return _pass("denied_columns", "policy denies no column to a reader")

    offences: list[str] = []
    for where, region in _regions(statement):
        for column in region.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            resolved = resolve_one(column, statement, card)
            if resolved is None or not policy.denies_column(resolved):
                continue
            if policy.allow_denied_in_aggregates and _inside_aggregate(column, region):
                continue
            offence = f"{resolved} in {where}"
            if offence not in offences:
                offences.append(offence)

    for entry in _starred_denials(statement, card, policy):
        offence = f"{entry} through a * in {PROJECTION}"
        if offence not in offences:
            offences.append(offence)

    if offences:
        listed = ", ".join(sorted(policy.denied_columns))
        return _fail(
            "denied_columns",
            f"policy does not permit a reader to be shown: {'; '.join(offences)}. "
            f"Denied: {listed}",
            COLUMN_NOT_ALLOWED,
        )
    return _pass(
        "denied_columns",
        f"no denied column reaches the reader ({len(policy.denied_columns)} denied)",
    )


def _outermost_sources(statement: exp.Select) -> tuple[exp.Table, ...]:
    """The FROM and JOIN tables of the outermost SELECT, CTE names included.

    Nested SELECTs are not walked. A table read inside a subquery contributes no
    rows to the answer, and counting it would refuse a correct query for the
    width of something nobody sees.
    """
    ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    nodes: list[exp.Expression] = []
    from_clause = statement.args.get("from_") or statement.args.get("from")
    if from_clause is not None:
        nodes.append(from_clause.this)
    for join in statement.args.get("joins") or []:
        nodes.append(join.this)
    return tuple(
        node
        for node in nodes
        if isinstance(node, exp.Table) and node.name.lower() not in ctes
    )


def _declared_limit(statement: exp.Select) -> int | None:
    """The LIMIT the author wrote, or `None` if there is none to prove small.

    Read before `row_limit` rewrites the statement. A rule satisfied by the
    guard's own repair would be the guard grading its own homework, and
    `LIMIT 10 + 5` is not evaluated to find out what it is: an unprovable limit
    is not a limit.
    """
    limit = statement.args.get("limit")
    if limit is None:
        return None
    expression = limit.expression
    if not (isinstance(expression, exp.Literal) and expression.is_int):
        return None
    return int(expression.name)


def check_bulk_export(
    statement: exp.Select, card: SchemaCard, policy: GuardPolicy
) -> RuleResult:
    """Refuse an unaggregated projection over a big table without a small LIMIT.

    A projection with no aggregate returns one row per record, and over a table
    above `[guard] star_row_threshold` that is a bulk export. Bulk export is a
    **policy** failure and not a row cap: `[guard] max_rows` already bounds the
    damage at 200 rows, and 200 rows of a customer table is precisely the thing
    being refused.

    Deliberately conservative in one direction, and the cost is stated: `SELECT
    city FROM customers GROUP BY city` returns one row per city and the guard
    cannot know how many that is before running it. A rule that guessed would be
    a rule that was sometimes wrong in the direction of printing more.
    """
    cap = policy.max_unaggregated_rows
    if cap is None:
        return _pass("bulk_export", "policy sets no unaggregated row limit")

    if any(
        aggregate
        for projection in statement.expressions
        for aggregate in projection.find_all(exp.AggFunc)
    ):
        return _pass("bulk_export", "the projection aggregates, so it is one row per group")

    wide = sorted(
        {
            found.name: found.row_count
            for table in _outermost_sources(statement)
            if (found := card.table(table.name)) is not None
            and found.row_count > policy.star_row_threshold
        }.items()
    )
    if not wide:
        return _pass(
            "bulk_export",
            f"no source is above the {policy.star_row_threshold}-row threshold",
        )

    limit = _declared_limit(statement)
    if limit is not None and limit <= cap:
        return _pass("bulk_export", f"LIMIT {limit} is within the {cap}-row export limit")

    named = ", ".join(f"{name} ({rows} rows)" for name, rows in wide)
    written = "no LIMIT was given" if limit is None else f"LIMIT {limit} was given"
    return _fail(
        "bulk_export",
        f"the projection has no aggregate over {named}, above the "
        f"{policy.star_row_threshold}-row threshold, and {written}. An unaggregated "
        f"answer from a table that size needs LIMIT {cap} or fewer. Aggregate it, "
        "or ask for a smaller slice",
        BULK_EXPORT,
    )


__all__ = [
    "BULK_EXPORT",
    "COLUMN_NOT_ALLOWED",
    "check_bulk_export",
    "check_denied_columns",
]

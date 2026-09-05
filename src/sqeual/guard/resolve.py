"""Resolve every column reference against the real schema.

This is the part of the guard that catches hallucination class 1. A model asked
for "revenue by region" writes `SELECT region, SUM(revenue) FROM orders`, and
SQLite answers `no such column: region` — at execution time, with the
connection already open and an error nobody upstream can turn into a repair.
Resolving first turns that into a named finding before anything runs.

The resolver is written by hand rather than delegated to sqlglot's optimizer
for one reason: **it has to be conservative in a direction I can state.**
`sqlglot.optimizer.qualify` raises on an unresolvable column, which is the
right behaviour for a query planner and the wrong one for a guard — a guard has
to distinguish "this column does not exist" from "I cannot tell", report the
first and stay quiet about the second. Every place this module cannot prove a
column wrong, it says nothing:

  * a source whose columns it cannot enumerate (`SELECT *` inside a CTE) makes
    the whole scope opaque, and unresolved columns there are not reported;
  * a table absent from the card is opaque too — `known_tables` has already
    reported it, and re-reporting every one of its columns would bury the
    finding that matters;
  * a correlated reference to an outer query is legal SQL and is resolved by
    walking outwards, not by assuming the inner scope is all there is.

The cost of that stance is a false negative: a hallucinated column inside a
`SELECT *` CTE reaches the executor. The alternative is a false positive on
correct SQL, which trains everyone downstream to ignore the guard.
"""

from dataclasses import dataclass

from sqlglot import exp

from ..schema.card import SchemaCard

UNKNOWN_COLUMN = "unknown_column"
AMBIGUOUS_COLUMN = "ambiguous_column"

MAX_SUGGESTIONS = 8
"""How many real column names a finding lists. A finding that says only "no
such column" makes the next attempt a guess; naming what does exist is what
makes a repair possible. Eight fits on a line and covers every table here."""


@dataclass(frozen=True)
class Source:
    """One thing a SELECT can take columns from."""

    visible_name: str
    """The name a qualifier would use: the alias if there is one, else the
    table name."""
    real_table: str | None
    """The underlying table as the card spells it, or `None` for a CTE, a
    derived subquery, or a table the card does not have."""
    columns: frozenset[str] | None
    """Lowercased output column names, or `None` for an opaque source whose
    columns cannot be enumerated."""


@dataclass(frozen=True)
class Finding:
    """One column that could not be resolved, or resolved to more than one."""

    code: str
    detail: str


@dataclass(frozen=True)
class Resolution:
    """Everything the resolver concluded about one statement."""

    columns_used: tuple[str, ...]
    findings: tuple[Finding, ...]

    def findings_with(self, code: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.code == code)


def _output_columns(select: exp.Select) -> frozenset[str] | None:
    """The column names a SELECT exposes, or `None` if it cannot be enumerated.

    A `*` anywhere in the projection makes the answer unknowable without
    expanding it, and expanding it means resolving the star's own sources
    first. The honest answer is `None`, which makes the source opaque and stops
    the resolver claiming findings it cannot support.
    """
    names: set[str] = set()
    for projection in select.selects:
        if isinstance(projection, exp.Star):
            return None
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            return None
        name = projection.alias_or_name
        if name:
            names.add(name.lower())
    return frozenset(names)


def _cte_definitions(statement: exp.Expression) -> dict[str, exp.Select]:
    """Every CTE in the statement, by name.

    Gathered globally rather than per-scope. A nested `WITH` is not visible to
    a sibling scope in real SQL, so this is fractionally more permissive than
    the language — but only for names that genuinely appear as a CTE somewhere
    in the same statement, and only in the direction of not reporting a finding.
    """
    definitions: dict[str, exp.Select] = {}
    for cte in statement.find_all(exp.CTE):
        body = cte.this
        if isinstance(body, exp.Select):
            definitions[cte.alias_or_name.lower()] = body
    return definitions


def _source_for(
    node: exp.Expression, card: SchemaCard, ctes: dict[str, exp.Select]
) -> Source:
    if isinstance(node, exp.Table):
        visible = node.alias_or_name or node.name
        cte_body = ctes.get(node.name.lower())
        if cte_body is not None:
            return Source(visible, None, _output_columns(cte_body))
        table = card.table(node.name)
        if table is None:
            # `known_tables` has already reported this. Marking it opaque stops
            # the resolver adding one `unknown_column` per column of a table
            # that does not exist, which would bury the finding that matters.
            return Source(visible, None, None)
        return Source(visible, table.name, frozenset(c.name.lower() for c in table.columns))
    if isinstance(node, exp.Subquery):
        inner = node.this
        columns = _output_columns(inner) if isinstance(inner, exp.Select) else None
        return Source(node.alias_or_name, None, columns)
    return Source(node.alias_or_name or "", None, None)


def _sources(
    select: exp.Select, card: SchemaCard, ctes: dict[str, exp.Select]
) -> tuple[Source, ...]:
    """The FROM and JOIN sources of one SELECT, ignoring nested ones."""
    nodes: list[exp.Expression] = []
    # sqlglot 30 renamed this argument from "from" to "from_". Both are read so
    # that the pinned version is the one that is tested and a bump does not
    # silently produce a SELECT with no sources — which would resolve nothing
    # and report everything.
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        nodes.append(from_clause.this)
    for join in select.args.get("joins") or []:
        nodes.append(join.this)
    return tuple(_source_for(node, card, ctes) for node in nodes)


def _explicit_aliases(select: exp.Select) -> frozenset[str]:
    """Names introduced by `expression AS name` in this SELECT's projection.

    SQLite lets `ORDER BY`, `GROUP BY`, `HAVING` and even `WHERE` refer to an
    output alias, so those names have to resolve. Only *explicit* aliases
    count: in `SELECT total_cents FROM orders` the projection is a bare column,
    and treating it as an alias would make every column self-resolving and the
    whole check vacuous.
    """
    return frozenset(
        projection.alias.lower()
        for projection in select.expressions
        if isinstance(projection, exp.Alias) and projection.alias
    )


def _scope_chain(node: exp.Expression) -> list[exp.Select]:
    """The SELECTs a column may resolve against, innermost first.

    Walking outwards is what makes a correlated subquery work: `WHERE r.order_id
    = o.id` inside a subquery refers to the outer query's `o`, and a resolver
    that only looked at the inner scope would call it a hallucination.

    The walk stops at a CTE boundary. A CTE body cannot see the FROM of the
    query that uses it, and letting it would hide a real finding.
    """
    chain: list[exp.Select] = []
    current = node.parent
    while current is not None:
        if isinstance(current, exp.Select):
            chain.append(current)
        if isinstance(current, exp.CTE):
            break
        current = current.parent
    return chain


def _canonical(card: SchemaCard, table: str, column: str) -> str:
    """`table.column` spelled the way the schema spells it.

    SQL is case-insensitive about identifiers, so `TOTAL_CENTS` and
    `total_cents` are the same column. Reporting the card's spelling means two
    reports of the same query agree whatever the model typed.
    """
    found = card.table(table)
    resolved = found.column(column) if found is not None else None
    return f"{found.name}.{resolved.name}" if found and resolved else f"{table}.{column}"


def _suggest(card: SchemaCard, table: str | None) -> str:
    if table is None:
        return ""
    found = card.table(table)
    if found is None:
        return ""
    names = [column.name for column in found.columns][:MAX_SUGGESTIONS]
    return f"{found.name} has: {', '.join(names)}"


def _resolve_qualified(
    column: exp.Column, chain: list[exp.Select], card: SchemaCard, ctes
) -> tuple[str | None, Finding | None]:
    qualifier = column.table.lower()
    name = column.name
    for select in chain:
        for source in _sources(select, card, ctes):
            if source.visible_name.lower() != qualifier:
                continue
            if source.columns is None:
                return None, None
            if name.lower() in source.columns:
                if source.real_table is None:
                    return None, None
                return _canonical(card, source.real_table, name), None
            suggestion = _suggest(card, source.real_table)
            hint = f" {suggestion}." if suggestion else ""
            return None, Finding(
                UNKNOWN_COLUMN, f"{column.table}.{name} does not exist.{hint}"
            )
    return None, Finding(
        UNKNOWN_COLUMN,
        f"{column.table}.{name} refers to {column.table!r}, which this query does not select from",
    )


def _resolve_unqualified(
    column: exp.Column, chain: list[exp.Select], card: SchemaCard, ctes
) -> tuple[str | None, Finding | None]:
    name = column.name
    folded = name.lower()
    for select in chain:
        sources = _sources(select, card, ctes)
        matches = [
            source for source in sources if source.columns is not None and folded in source.columns
        ]
        if len(matches) > 1:
            owners = ", ".join(
                sorted(source.real_table or source.visible_name for source in matches)
            )
            return None, Finding(
                AMBIGUOUS_COLUMN,
                f"{name} is on more than one source in this query ({owners}); qualify it",
            )
        if len(matches) == 1:
            source = matches[0]
            if source.real_table is None:
                return None, None
            return _canonical(card, source.real_table, name), None
        if any(source.columns is None for source in sources):
            # An opaque source might have it. Saying nothing is the honest
            # answer; a finding here would fire on correct SQL.
            return None, None
        if folded in _explicit_aliases(select):
            return None, None

    # A finding that says only "no such column" makes the next attempt a guess.
    # Two different hints repair two different mistakes: the model used a real
    # column name against the wrong table, or it invented the name outright and
    # needs to see what the tables it chose actually hold.
    known = sorted(card.tables_with_column(name))
    if known:
        hint = f" It exists on: {', '.join(known)}."
    else:
        offered: list[str] = []
        for select in chain:
            for source in _sources(select, card, ctes):
                suggestion = _suggest(card, source.real_table)
                if suggestion and suggestion not in offered:
                    offered.append(suggestion)
        hint = f" {'; '.join(offered)}." if offered else ""
    return None, Finding(
        UNKNOWN_COLUMN,
        f"{name} does not exist on any table this query selects from.{hint}",
    )


def resolve_one(
    column: exp.Column, statement: exp.Expression, card: SchemaCard
) -> str | None:
    """`Table.column` for one reference, or `None` when it cannot be proved.

    The same resolution `resolve_columns` performs, exposed for one reference at
    a time so that a rule interested in *where* a column appears — the outermost
    projection, an ORDER BY — can ask about that reference rather than about the
    whole statement. There is one resolver in this package and this is it; a
    second one would be a second thing that can disagree with the schema.

    `None` covers every case the resolver refuses to claim: an opaque source, a
    CTE, an output alias, a table the card does not have. A caller building a
    *deny* rule must read `None` as "not proved denied" and not as "proved
    allowed" — which is why the star case is checked separately, by table.
    """
    chain = _scope_chain(column)
    if not chain:
        return None
    ctes = _cte_definitions(statement)
    if column.table:
        resolved, _ = _resolve_qualified(column, chain, card, ctes)
    else:
        resolved, _ = _resolve_unqualified(column, chain, card, ctes)
    return resolved


def resolve_columns(statement: exp.Expression, card: SchemaCard) -> Resolution:
    """Resolve every column in `statement` against `card`."""
    ctes = _cte_definitions(statement)
    used: list[str] = []
    findings: list[Finding] = []
    seen_details: set[tuple[str, str]] = set()

    for column in statement.find_all(exp.Column):
        # `t.*` is a star, not a column reference. The star rule owns it.
        if isinstance(column.this, exp.Star):
            continue
        chain = _scope_chain(column)
        if not chain:
            continue
        if column.table:
            resolved, finding = _resolve_qualified(column, chain, card, ctes)
        else:
            resolved, finding = _resolve_unqualified(column, chain, card, ctes)
        if resolved is not None and resolved not in used:
            used.append(resolved)
        if finding is not None:
            key = (finding.code, finding.detail)
            if key not in seen_details:
                seen_details.add(key)
                findings.append(finding)

    return Resolution(columns_used=tuple(sorted(used)), findings=tuple(findings))

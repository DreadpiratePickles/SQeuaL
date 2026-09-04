"""Four checks that ask whether the SQL answers the question — with no model.

The guard proves a statement is well-formed, single, read-only and made of real
tables and columns. Nothing in its twelve rules has an opinion about whether it
answers anybody's question, and `SELECT COUNT(*) FROM orders` is a perfectly
valid answer to "how much did we refund in March".

These four are the cheap half of noticing that. They read the **parse tree**, not
the string, for the same reason the guard does: a date literal in a `SELECT` list
is not a filter, and grepping for `WHERE` cannot tell the difference. They are
deliberately individually insufficient — each one has a false positive somebody
can construct — which is why they feed a score rather than a verdict.

The date-column convention is the one heuristic here worth arguing with. A column
counts as a date when its name ends in `_date` or is `date`, which is exactly the
convention `data/schema.sql` follows and states. A database that named one
`created_at` would need this rule widened, and widening it is a diff somebody
reviews rather than a regex somebody guesses at.
"""

import re
from collections.abc import Mapping, Sequence

import sqlglot
from sqlglot import exp

from ..generate.timewindow import TimeWindow
from ..schema.card import SchemaCard
from ..schema.slice import tokenise, words
from .checks import Check, CheckStatus

DIALECT = "sqlite"

DATE_COLUMN_SUFFIX = "_date"
DATE_LITERAL_PATTERN = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")

AGGREGATE_TRIGGERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("how many", "count of", "number of", "how much"), "COUNT-or-SUM"),
    (("total", "sum of", "summed", "altogether", "how much"), "SUM"),
    (("average", "avg", "mean"), "AVG"),
)
"""Phrase -> the aggregate a statement answering it should carry. `how much`
appears twice on purpose: it asks for a total, and on a count-shaped column a
COUNT is the honest reading of it."""

SUPERLATIVE_TRIGGERS: tuple[str, ...] = (
    "highest", "lowest", "largest", "smallest", "biggest", "greatest",
    "most", "least", "top", "bottom", "best", "worst", "maximum", "minimum",
)

TOP_N_PATTERN = re.compile(
    r"\b(?:top|bottom|first|last)\s+(\d{1,3})\b|\b(\d{1,3})\s+(?:most|least|highest|lowest|"
    r"largest|smallest|biggest|best|worst)\b"
)

GROUPING_WORDS: tuple[str, ...] = ("each", "every", "apiece", "respectively")
GROUPING_PREPOSITIONS: tuple[str, ...] = ("per", "by")
SORT_WORDS: tuple[str, ...] = ("order", "ordered", "sort", "sorted", "rank", "ranked")
"""A `by` after one of these is a sort, not a grouping. Without this exclusion
"products sorted by price" reports a missing GROUP BY on every correct answer."""


def _parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, dialect=DIALECT)


def _date_columns(card: SchemaCard | None, tables_used: Sequence[str]) -> set[str]:
    """Every date column on the tables this statement reads, lowercased.

    Without a card there is nothing to resolve against, so the set is empty and
    the check says so rather than guessing.
    """
    if card is None:
        return set()
    found: set[str] = set()
    for name in tables_used:
        table = card.table(name)
        if table is None:
            continue
        found |= {
            column.name.lower()
            for column in table.columns
            if column.name.lower().endswith(DATE_COLUMN_SUFFIX) or column.name.lower() == "date"
        }
    return found


def _predicate_columns(statement: exp.Expression) -> set[str]:
    """Column names appearing inside a filter, never inside a projection.

    `WHERE`, `HAVING` and every join condition count; the `SELECT` list does not.
    That distinction is the whole reason this reads a tree: a date literal in the
    projection looks exactly like a filter to anything reading the string.
    """
    found: set[str] = set()
    for clause in statement.find_all(exp.Where, exp.Having, exp.Join):
        for column in clause.find_all(exp.Column):
            found.add(column.name.lower())
    return found


def _date_literals(statement: exp.Expression) -> list[str]:
    """Every date-shaped string literal anywhere in the statement, in tree order."""
    return [
        node.this
        for node in statement.find_all(exp.Literal)
        if node.is_string and DATE_LITERAL_PATTERN.match(str(node.this))
    ]


def _check_time_window(
    question: str,
    statement: exp.Expression,
    card: SchemaCard | None,
    tables_used: Sequence[str],
    window: TimeWindow | None,
) -> Check:
    if window is None:
        return Check("time_window", CheckStatus.NA, "the question names no period")

    resolved = f"{window.phrase!r} resolves to {window.start}..{window.end}"
    filtered = _predicate_columns(statement) & _date_columns(card, tables_used)
    if not filtered:
        return Check(
            "time_window",
            CheckStatus.FAIL,
            f"{resolved}, but no date column of the tables read is filtered on",
        )

    literals = _date_literals(statement)
    wrong = [value for value in literals if not window.consistent_with_literal(value)]
    if wrong:
        return Check(
            "time_window",
            CheckStatus.FAIL,
            f"{resolved}, but the statement filters {', '.join(sorted(filtered))} on "
            f"{', '.join(wrong)}",
        )
    if not literals:
        return Check(
            "time_window",
            CheckStatus.PASS,
            f"{resolved}; {', '.join(sorted(filtered))} is filtered by an expression "
            "with no date literal to compare",
        )
    return Check(
        "time_window",
        CheckStatus.PASS,
        f"{resolved}; the statement filters {', '.join(sorted(filtered))} on "
        f"{', '.join(literals)}",
    )


def _function_names(statement: exp.Expression) -> set[str]:
    return {node.sql_name().upper() for node in statement.find_all(exp.AggFunc)}


def _aggregate_requirements(lowered: str) -> list[str]:
    required: list[str] = []
    for phrases, requirement in AGGREGATE_TRIGGERS:
        if any(phrase in lowered for phrase in phrases):
            required.append(requirement)
    if any(re.search(rf"\b{word}\b", lowered) for word in SUPERLATIVE_TRIGGERS):
        required.append("ORDER BY")
    if TOP_N_PATTERN.search(lowered):
        required.append("LIMIT")
    # An average is a sum divided by a count, so a question that says "average"
    # has already said which of the three it wants. Without this, "the average
    # order total" requires a SUM because it contains the word "total" — and a
    # check that fails on a correct AVG is a check people learn to ignore.
    if "AVG" in required:
        required = [item for item in required if item not in ("SUM", "COUNT-or-SUM")]
    # Order-preserving de-duplication: "the top 5 highest" triggers ORDER BY
    # twice and should be reported once.
    return list(dict.fromkeys(required))


def _satisfied(requirement: str, statement: exp.Expression, functions: set[str]) -> bool:
    if requirement == "COUNT-or-SUM":
        return bool(functions & {"COUNT", "SUM", "TOTAL"})
    if requirement == "ORDER BY":
        return statement.find(exp.Order) is not None
    if requirement == "LIMIT":
        return statement.find(exp.Limit) is not None
    if requirement == "SUM":
        return bool(functions & {"SUM", "TOTAL"})
    return requirement in functions


def _describe(requirement: str) -> str:
    return "COUNT or SUM" if requirement == "COUNT-or-SUM" else requirement


def _check_aggregation(question: str, statement: exp.Expression) -> Check:
    required = _aggregate_requirements(question.lower())
    if not required:
        return Check("aggregation", CheckStatus.NA, "the question asks for no aggregate")

    functions = _function_names(statement)
    missing = [
        requirement
        for requirement in required
        if not _satisfied(requirement, statement, functions)
    ]
    wanted = ", ".join(_describe(item) for item in required)
    if missing:
        return Check(
            "aggregation",
            CheckStatus.FAIL,
            f"the question asks for {wanted}; the statement has no "
            f"{', no '.join(_describe(item) for item in missing)}",
        )
    return Check("aggregation", CheckStatus.PASS, f"the question asks for {wanted}; present")


def _named_tables(
    question: str, card: SchemaCard | None, synonyms: Mapping[str, str]
) -> list[str]:
    """The tables a question names outright or through a configured synonym.

    The same map the slicer uses, so "which tables did the question name" has one
    answer in this codebase rather than two that drift.
    """
    if card is None:
        return []
    known = {table.name.lower(): table.name for table in card.tables}
    found: list[str] = []
    for term in tokenise(question):
        for candidate in (term, f"{term}s"):
            if candidate in known:
                found.append(known[candidate])
        target = synonyms.get(term)
        if target and target.lower() in known:
            found.append(known[target.lower()])
    return list(dict.fromkeys(found))


def _check_entities(
    question: str,
    card: SchemaCard | None,
    tables_used: Sequence[str],
    synonyms: Mapping[str, str],
) -> Check:
    named = _named_tables(question, card, synonyms)
    if not named:
        return Check("entities", CheckStatus.NA, "the question names no table this database has")

    used = {name.lower() for name in tables_used}
    missing = [name for name in named if name.lower() not in used]
    if missing:
        return Check(
            "entities",
            CheckStatus.FAIL,
            f"the question names {', '.join(named)}; the statement does not read "
            f"{', '.join(missing)}",
        )
    return Check(
        "entities", CheckStatus.PASS, f"the question names {', '.join(named)}; all are read"
    )


def _grouping_target(question: str, card: SchemaCard | None) -> tuple[bool, str | None]:
    """Whether the question asks for a grouping, and on which column if it says.

    A `by` following a sorting word is not a grouping. Without that exclusion
    "products sorted by price" reports a missing GROUP BY on every correct
    answer, and a check that fires on correct SQL is a check people learn to
    ignore.
    """
    question_words = words(question)
    known = _column_names(card)
    triggered = False
    target: str | None = None

    for index, word in enumerate(question_words):
        if word in GROUPING_WORDS:
            triggered = True
        if word not in GROUPING_PREPOSITIONS:
            continue
        if word == "by" and index and question_words[index - 1] in SORT_WORDS:
            continue
        triggered = True
        if target is None:
            following = question_words[index + 1 : index + 4]
            target = next(
                (
                    known[candidate]
                    for length in range(len(following), 0, -1)
                    if (candidate := "".join(following[:length])) in known
                ),
                None,
            )
    return triggered, target


def _column_names(card: SchemaCard | None) -> dict[str, str]:
    """Underscore-free lowercase column name -> the real name, across the card."""
    if card is None:
        return {}
    return {
        column.name.lower().replace("_", ""): column.name
        for table in card.tables
        for column in table.columns
    }


def _grouped_columns(statement: exp.Expression) -> set[str]:
    group = statement.find(exp.Group)
    if group is None:
        return set()
    return {column.name.lower() for column in group.find_all(exp.Column)}


def _check_grouping(question: str, statement: exp.Expression, card: SchemaCard | None) -> Check:
    triggered, target = _grouping_target(question, card)
    if not triggered:
        return Check("grouping", CheckStatus.NA, "the question asks for no grouping")

    grouped = _grouped_columns(statement)
    if not grouped:
        return Check(
            "grouping", CheckStatus.FAIL, "the question groups; the statement has no GROUP BY"
        )
    if target is not None and target.lower() not in grouped:
        return Check(
            "grouping",
            CheckStatus.FAIL,
            f"the question groups by {target}; the statement groups by "
            f"{', '.join(sorted(grouped))}",
        )
    named = f" by {target}" if target else ""
    return Check(
        "grouping",
        CheckStatus.PASS,
        f"the question groups{named}; the statement groups by {', '.join(sorted(grouped))}",
    )


def intent_checks(
    *,
    question: str,
    sql: str,
    card: SchemaCard | None,
    tables_used: Sequence[str],
    time_window: TimeWindow | None,
    synonyms: Mapping[str, str],
) -> tuple[Check, ...]:
    """Run all four checks against one (question, statement) pair.

    Args:
        question: the question as asked.
        sql: the guard's `normalised_sql` — the statement that actually ran.
        card: the schema card, for resolving date and grouping columns.
        tables_used: `GuardReport.tables_used`.
        time_window: the window code resolved from the question and `as_of`.
        synonyms: `[schema.synonyms]`, the same map the slicer used.

    Returns:
        All four checks, always, in a fixed order. A check with nothing to say
        reports NA rather than being omitted.
    """
    statement = _parse(sql)
    return (
        _check_time_window(question, statement, card, tables_used, time_window),
        _check_aggregation(question, statement),
        _check_entities(question, card, tables_used, synonyms),
        _check_grouping(question, statement, card),
    )

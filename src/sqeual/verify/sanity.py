"""Does the *shape* of the result match the shape of the question?

Nothing here reads a value. These four checks ask only how many rows came back
and how many columns, which is enough to catch a whole class of wrong answer
cheaply: "how many refunds were there" answered with two hundred rows is wrong
whatever is in them, and no model is needed to see it.

Two of the four are worth their reasons.

**An empty result is FLAGged, never failed.** "No refunds in March" is frequently
the correct answer, and a check that failed on it would refuse to say the true
thing. It is surfaced in the answer instead, so a reader knows the zero was
measured rather than defaulted.

**Truncation is failed.** A sum over a truncated result is wrong and looks right,
which is the most dangerous failure this system has. A caller that received 200
rows and did not know there were 201 would report a total that is quietly short.
"""

import re

from ..execute import ResultSet
from .checks import Check, CheckStatus
from .intent import TOP_N_PATTERN

SCALAR_QUESTION_PHRASES: tuple[str, ...] = (
    "how many", "how much", "total", "count of", "number of", "average", "sum of",
)
GROUPING_MARKERS: tuple[str, ...] = ("per", "by", "each", "every", "breakdown")
"""A scalar question with one of these in it is not a scalar question: "how many
refunds per city" legitimately returns one row per city.

Matched on **word boundaries**, not as substrings. "each" is inside "reach",
"teach" and "beach", and a substring test would quietly exempt a question about
reach from the scalar-shape check — a check that silently stops applying is worse
than one that never existed."""

GROUPING_PATTERN = re.compile(
    r"\b(?:" + "|".join(GROUPING_MARKERS) + r")\b"
)


def _is_scalar_question(lowered: str) -> bool:
    if not any(phrase in lowered for phrase in SCALAR_QUESTION_PHRASES):
        return False
    if TOP_N_PATTERN.search(lowered):
        return False
    return GROUPING_PATTERN.search(lowered) is None


def _check_scalar_shape(lowered: str, result: ResultSet) -> Check:
    if not _is_scalar_question(lowered):
        return Check("scalar_shape", CheckStatus.NA, "the question does not ask for one figure")

    shape = f"{result.row_count} row(s) x {len(result.columns)} column(s)"
    if result.row_count != 1 or len(result.columns) != 1:
        return Check(
            "scalar_shape",
            CheckStatus.FAIL,
            f"the question asks for one figure; the statement returned {shape}",
        )
    value = result.rows[0][0]
    # NULL passes. `SUM(x)` over no matching rows is NULL in SQLite, and that is
    # the right shape for "nothing matched" — the emptiness is reported by the
    # flag below rather than by pretending the shape was wrong.
    if value is not None and not isinstance(value, int | float):
        return Check(
            "scalar_shape",
            CheckStatus.FAIL,
            f"the question asks for one figure; the single cell is a "
            f"{type(value).__name__}",
        )
    return Check("scalar_shape", CheckStatus.PASS, f"one figure asked for, {shape} returned")


def _check_top_n(lowered: str, result: ResultSet) -> Check:
    match = TOP_N_PATTERN.search(lowered)
    if match is None:
        return Check("top_n_rows", CheckStatus.NA, "the question names no row count")

    wanted = int(match.group(1) or match.group(2))
    if result.row_count > wanted:
        return Check(
            "top_n_rows",
            CheckStatus.FAIL,
            f"the question asks for {wanted} row(s); {result.row_count} came back",
        )
    return Check(
        "top_n_rows",
        CheckStatus.PASS,
        f"the question asks for at most {wanted} row(s); {result.row_count} came back",
    )


def _check_truncation(result: ResultSet) -> Check:
    if result.truncated:
        return Check(
            "not_truncated",
            CheckStatus.FAIL,
            f"the result was cut off at the {result.row_count}-row cap; a total over it "
            "would be short and would look right",
        )
    return Check("not_truncated", CheckStatus.PASS, "the whole result fitted inside the cap")


def _check_empty(result: ResultSet) -> Check:
    scalar_null = (
        result.row_count == 1 and len(result.columns) == 1 and result.rows[0][0] is None
    )
    if result.row_count == 0:
        return Check("empty_result", CheckStatus.FLAG, "no rows matched")
    if scalar_null:
        return Check("empty_result", CheckStatus.FLAG, "the single cell is NULL: nothing matched")
    return Check("empty_result", CheckStatus.NA, "the result has rows")


def sanity_checks(*, question: str, result: ResultSet) -> tuple[Check, ...]:
    """Compare the shape of a result against the shape the question asked for.

    Returns:
        All four checks, always, in a fixed order.
    """
    lowered = question.lower()
    return (
        _check_scalar_shape(lowered, result),
        _check_top_n(lowered, result),
        _check_truncation(result),
        _check_empty(result),
    )

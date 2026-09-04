"""Turn one database cell into one piece of text. This is where the rule lives.

"The model never writes a number" is a claim about prompt discipline until
something in the code is the only path from a value to a rendered figure. This
module is that path, and `RenderedCell` keeps the raw value beside the rendering
so every number in an answer can be traced back to the cell it came from.

Money is the case that matters. The database stores an integer count of cents and
`data/schema.sql` says so in the column name, so a `*_cents` column is divided by
100 **here** and printed with an explicit currency. Never bare: "1,250" is a
different answer in two currencies and the difference is invisible. And never in
the SQL: a statement that already divided has thrown away the exact integer it was
given, and the exactness is the whole reason money is an integer.

`NULL` renders as "no value recorded" and never as 0 and never as "0". A NULL sum
means nothing matched; a zero means something matched and totalled nothing, and a
formatter that conflated them would report the second when the first was true.

A value whose type the formatter does not recognise is **marked**, not coerced. A
silently coerced cell is a wrong number that looks right, and looking right is the
property that gets a figure quoted.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENTS_SUFFIX = "_cents"
CENTS_PER_UNIT = 100
NULL_TEXT = "no value recorded"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class RenderedCell:
    """One cell, its rendering, and the kind of thing it was taken to be.

    `raw` beside `rendered` is the audit trail for the central claim: a rendered
    value that does not match its raw value under the declared formatter is a bug
    a test can catch, and the grounding check reads both.
    """

    column: str
    raw: object
    rendered: str
    kind: str


def _money(value: int | float, symbol: str) -> str:
    """An integer count of cents as `€1,250.00`, sign outside the symbol.

    A float reaches here from `ROUND(AVG(total_cents), 2)`, which is still an
    amount. It is rounded to whole cents before the split, so the rendering never
    shows a fraction of a cent the database cannot hold.

    `ROUND_HALF_UP` rather than Python's built-in `round`. The built-in rounds
    half to **even** — `round(1250.5)` is 1250 and `round(1251.5)` is 1252 —
    which is a defensible statistical convention and a genuine surprise in a
    money column, where every reader expects half a cent to go up. The rule is
    stated here rather than inherited, because a rounding convention nobody chose
    is a rounding convention nobody can defend.
    """
    cents = int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    sign = "-" if cents < 0 else ""
    units, remainder = divmod(abs(cents), CENTS_PER_UNIT)
    return f"{sign}{symbol}{units:,}.{remainder:02d}"


def format_cell(value: object, *, column: str, settings, float_places: int) -> RenderedCell:
    """Render one cell by its type and its column's name.

    Args:
        value: the raw value from the `ResultSet`.
        column: the column label, which is what says whether this is money.
        settings: the validated `[answer]` section.
        float_places: decimal places for a non-money float.
    """
    is_money = column.lower().endswith(CENTS_SUFFIX)

    if value is None:
        return RenderedCell(column, value, NULL_TEXT, "null")
    # `bool` before `int`: `isinstance(True, int)` is True in Python, and a flag
    # rendered as "1" is a cell that has been quietly coerced into a number.
    if isinstance(value, bool):
        return RenderedCell(column, value, f"unformattable ({value!r})", "unformattable")
    if isinstance(value, int | float) and is_money:
        return RenderedCell(column, value, _money(value, settings.currency_symbol), "money")
    if isinstance(value, int):
        return RenderedCell(column, value, f"{value:,}", "integer")
    if isinstance(value, float):
        return RenderedCell(column, value, f"{value:,.{float_places}f}", "float")
    if isinstance(value, str):
        kind = "date" if ISO_DATE.match(value) else "text"
        return RenderedCell(column, value, value, kind)
    return RenderedCell(column, value, f"unformattable ({type(value).__name__})", "unformattable")


def render_cells(
    *,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    settings,
    float_places: int,
) -> tuple[RenderedCell, ...]:
    """Render every cell of a result, row-major, keeping each raw value beside it."""
    return tuple(
        format_cell(value, column=columns[index], settings=settings, float_places=float_places)
        for row in rows
        for index, value in enumerate(row)
    )

"""Check that every number in a sentence came from somewhere a reader can check.

`[answer] llm_phrasing` lets a model write the one sentence above the table. That
is a nicety, and it is only safe because of this module: every numeric token in
what the model wrote is extracted and required to trace back to a result cell, to
the row count, or to a date code computed. A token that traces to none of those is
a number the model invented, the phrasing is discarded whole, and the code
rendering is used instead.

**Whole, not partial.** A sentence with one bad figure removed is a sentence
somebody reads as complete. `phrasing_rejected` is recorded so the trace says a
model tried.

Comparison is on **value**, not on text. "1,250.00", "1250" and "€1,250.00" are
one number written three ways, and a check that compared strings would reject a
correct sentence for using a thousands separator. `Decimal.normalize` is what
makes them one number without ever constructing a float — the same reason money is
an integer count of cents everywhere else in this package.

Three things are deliberately **not** normalised away, and each one closes a hole
that a first pass had:

  * **A date is one value, never its digits.** Splitting `2026-08-31` into `2026`,
    `08` and `31` would put three small integers into the allowed set on every
    single run — and a small invented count is exactly what a hallucinating
    phraser produces. The date's *year* is kept as well, because "in July 2026"
    is how anybody writes a period; its month and day are not.
  * **A percentage is not its number.** Stripping `%` made `42%` compare equal to
    a cell holding `42`, so a fabricated growth rate passed whenever the result
    happened to contain the same digits somewhere. A percent token can only be
    grounded by a percent value, and no cell in this schema renders as one — which
    is the intended outcome: `phrase_v1.md` tells the model not to compute a rate,
    and this is what makes that an enforced rule rather than a request.
  * **A sign is part of a value.** `-150` and `150` are different answers, and a
    check that conflated them would let a model turn a loss into a gain.

The `extra` grounds are the one thing here that is not a cell, and they are
deliberately narrow: the endpoints of the window **code** resolved from `as_of`.
A sentence saying "between 2026-07-01 and 2026-07-31" is quoting a figure this
program computed, not one a model made up.

**Known limitation, stated rather than hidden.** Grounding checks that a value is
*present* in the result, not that it is attributed to the right row. On a grouped
result, "Munich had 120 orders" passes when 120 is Berlin's figure, because 120 is
genuinely in a cell. Row attribution would need the sentence parsed, which is a
different and much weaker kind of check; the mitigation is that the table sits
directly underneath the sentence, rendered by code, where the reader can see which
row the number belongs to.
"""

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

ISO_DATE = r"\d{4}-\d{2}(?:-\d{2})?"
NUMERIC_TOKEN = re.compile(rf"{ISO_DATE}|-?[$€£¥]?\d[\d,]*(?:\.\d+)?%?")
"""A date, or a signed number with optional currency, separators, decimals and
percent. The date alternative comes **first** so a date is matched whole rather
than as a year followed by two negative numbers."""

ISO_DATE_ONLY = re.compile(rf"^{ISO_DATE}$")
PERCENT = "%"
STRIPPED = "$€£¥,"
"""Currency symbols and thousands separators carry no value. `%` and `-` do, and
are handled in `normalise` rather than stripped."""


def numeric_tokens(text: str) -> tuple[str, ...]:
    """Every numeric token in `text`, in order, as written."""
    if not isinstance(text, str):
        return ()
    return tuple(match.group(0) for match in NUMERIC_TOKEN.finditer(text))


def normalise(token: str) -> str | None:
    """One numeric token reduced to its value, or `None` if it has none.

    `Decimal` rather than `float`, so "1250.00" and "1250" compare equal without
    a binary approximation stepping in between them. A date returns itself; a
    percentage keeps its sign so it can only match another percentage.
    """
    text = str(token).strip()
    if ISO_DATE_ONLY.match(text):
        return text

    is_percent = text.endswith(PERCENT)
    body = "".join(character for character in text if character not in STRIPPED)
    body = body.rstrip(PERCENT).strip()
    # A sign written before the currency symbol survives the strip above, but a
    # bare "-" left over from something that was not a number does not parse.
    if not body or body in "-+":
        return None
    try:
        value = format(Decimal(body).normalize(), "f")
    except (InvalidOperation, ValueError):
        return None
    return f"{value}{PERCENT}" if is_percent else value


def _add(values: set[str], text: str) -> None:
    for token in numeric_tokens(text):
        normalised = normalise(token)
        if normalised is not None:
            values.add(normalised)


def grounded_values(
    *, cells: Sequence, row_count: int, extra: Sequence[str] = ()
) -> set[str]:
    """Every value a sentence is allowed to quote.

    Both halves of each cell count: the rendered figure a reader sees, and the raw
    value behind it. Quoting either is quoting the database.

    An `extra` date grounds itself and its year, and nothing else — see the module
    docstring for why its month and day are deliberately withheld.
    """
    values: set[str] = set()
    for cell in cells:
        _add(values, str(cell.rendered))
        _add(values, str(cell.raw))
    values.add(str(row_count))
    for item in extra:
        text = str(item).strip()
        if ISO_DATE_ONLY.match(text):
            values.add(text)
            values.add(text[:4])
        else:
            _add(values, text)
    return values


def ungrounded_numbers(
    text: str, *, cells: Sequence, row_count: int, extra: Sequence[str] = ()
) -> tuple[str, ...]:
    """The numeric tokens in `text` that trace to nothing, in the order written.

    An empty tuple means every figure in the sentence can be checked against a
    cell. Anything else means the phrasing must be discarded.
    """
    allowed = grounded_values(cells=cells, row_count=row_count, extra=extra)
    return tuple(
        token
        for token in numeric_tokens(text)
        if (normalised := normalise(token)) is None or normalised not in allowed
    )

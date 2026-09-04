"""Resolve a relative time phrase in a question into an absolute date window.

"Last month" is not a fact about a question, it is a fact about a question *and*
a date. So `[time] as_of` is committed, the model is told it, and this module
independently computes the window the same phrase should produce. Stage 06 then
compares the date literals the model actually wrote against this window, which is
how "the model was told the date" becomes something other than a hope.

Two rules keep the resolution deterministic:

  * **The earliest phrase in the question wins**, with a longer match beating a
    shorter one that starts at the same word. A question naming two periods is
    ambiguous, and picking the first is at least a rule somebody can predict;
    picking "whichever pattern happened to be listed first" is not.
  * **A month with no year resolves backwards, never forwards.** "Since
    November" asked on 31 August means last November. A window that ends before
    it starts is not a window, and a question about the future is not one this
    database can answer.

Windows are inclusive at both ends and carried as ISO text, because that is what
the database stores and what the model writes.
"""

import calendar
import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass

MONTHS: dict[str, int] = {
    name.lower(): number
    for number, name in enumerate(calendar.month_name)
    if name
} | {
    name.lower(): number
    for number, name in enumerate(calendar.month_abbr)
    if name
}
"""Full names and three-letter abbreviations, both lowercased. Built from
`calendar` rather than typed out, so the two spellings cannot disagree."""

MIN_YEAR, MAX_YEAR = 1900, 2999
"""The range a bare four-digit number is read as a year. Narrow on purpose:
"the top 2000 products" should not resolve to the year 2000, and neither should
"the top 10" resolve to the year 10."""


@dataclass(frozen=True)
class TimeWindow:
    """One resolved period: the phrase that produced it and its two endpoints."""

    phrase: str
    start: str
    end: str

    @property
    def exclusive_end(self) -> str:
        """The day after `end`.

        `WHERE d >= start AND d < end + 1 day` is the other correct spelling of
        a closed range, and a check that only accepted the inclusive one would
        report a mismatch against perfectly good SQL.
        """
        return (dt.date.fromisoformat(self.end) + dt.timedelta(days=1)).isoformat()

    def covers(self, iso_date: str) -> bool:
        """Whether an ISO date falls inside the window, endpoints included."""
        return self.start <= iso_date <= self.end

    def consistent_with_literal(self, literal: str) -> bool:
        """Whether a date-shaped literal from a statement contradicts this window.

        Deliberately *consistency*, not equality. A correct query may write
        `>= '2026-07-01' AND < '2026-08-01'`, or `STRFTIME('%Y-%m', d) =
        '2026-07'`, or `STRFTIME('%Y', d) = '2026'` — none of which contains both
        endpoints. So a literal passes when it could belong to a correct query
        for this window, and fails only when it could not.

        Accepts three shapes, because those are the three SQLite date idioms the
        function allowlist permits: `YYYY-MM-DD`, `YYYY-MM` and `YYYY`.
        """
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", literal):
            return self.covers(literal) or literal == self.exclusive_end
        if re.fullmatch(r"\d{4}-\d{2}", literal):
            return self.start[:7] <= literal <= self.end[:7]
        if re.fullmatch(r"\d{4}", literal):
            return self.start[:4] <= literal <= self.end[:4]
        return False


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _month_window(year: int, month: int) -> tuple[dt.date, dt.date]:
    return dt.date(year, month, 1), _month_end(year, month)


def _quarter_window(year: int, quarter: int) -> tuple[dt.date, dt.date]:
    first = 3 * (quarter - 1) + 1
    return dt.date(year, first, 1), _month_end(year, first + 2)


def _shift_months(anchor: dt.date, months: int) -> tuple[int, int]:
    """The (year, month) `months` away from `anchor`'s month."""
    index = anchor.year * 12 + (anchor.month - 1) + months
    return divmod(index, 12)[0], divmod(index, 12)[1] + 1


def _year_window(year: int) -> tuple[dt.date, dt.date]:
    return dt.date(year, 1, 1), dt.date(year, 12, 31)


def _week_window(anchor: dt.date, weeks_back: int) -> tuple[dt.date, dt.date]:
    """The Monday-to-Sunday week `weeks_back` weeks before `anchor`'s."""
    monday = anchor - dt.timedelta(days=anchor.weekday() + 7 * weeks_back)
    return monday, monday + dt.timedelta(days=6)


def _backdated_month(as_of: dt.date, month: int, year: int | None) -> int:
    """The year a bare month name refers to: this one, or the last one if it is ahead."""
    if year is not None:
        return year
    return as_of.year if month <= as_of.month else as_of.year - 1


Handler = Callable[[re.Match[str], dt.date], tuple[dt.date, dt.date] | None]

# Ordered most specific first. Order does not decide which phrase wins — position
# in the question does, see the module docstring — but a pattern that would
# swallow a longer one is still listed after it, so `last 7 days` is never read
# as `last` plus noise.
_PATTERNS: tuple[tuple[str, Handler], ...] = (
    (r"\b(?:last|past|previous|trailing)\s+(\d{1,4})\s+days?\b",
     lambda m, a: (a - dt.timedelta(days=int(m.group(1)) - 1), a)),
    (r"\byesterday\b", lambda m, a: (a - dt.timedelta(days=1), a - dt.timedelta(days=1))),
    (r"\btoday\b", lambda m, a: (a, a)),
    (r"\b(?:last|previous)\s+week\b", lambda m, a: _week_window(a, 1)),
    (r"\bthis\s+week\b", lambda m, a: _week_window(a, 0)),
    (r"\b(?:last|previous)\s+month\b", lambda m, a: _month_window(*_shift_months(a, -1))),
    (r"\bthis\s+month\b", lambda m, a: _month_window(a.year, a.month)),
    (r"\b(?:last|previous)\s+quarter\b",
     lambda m, a: _quarter_window(*_previous_quarter(a))),
    (r"\bthis\s+quarter\b", lambda m, a: _quarter_window(a.year, (a.month - 1) // 3 + 1)),
    (r"\b(?:last|previous)\s+year\b", lambda m, a: _year_window(a.year - 1)),
    (r"\bthis\s+year\b", lambda m, a: _year_window(a.year)),
    (r"\bq([1-4])\b(?:\s+(?:of\s+)?(\d{4}))?",
     lambda m, a: _quarter_window(int(m.group(2)) if m.group(2) else a.year, int(m.group(1)))),
    (r"\bsince\s+(\d{4})\b", lambda m, a: _since_year(m, a)),
    (r"\bsince\s+([a-z]+)\b(?:\s+(\d{4}))?", lambda m, a: _since_month(m, a)),
    (r"\b(?:in|during|for)\s+([a-z]+)\b(?:\s+(\d{4}))?", lambda m, a: _named_month(m, a)),
    (r"\b(\d{4})\b", lambda m, a: _bare_year(m, a)),
)


def _previous_quarter(as_of: dt.date) -> tuple[int, int]:
    quarter = (as_of.month - 1) // 3 + 1
    return (as_of.year - 1, 4) if quarter == 1 else (as_of.year, quarter - 1)


def _valid_year(raw: str) -> int | None:
    year = int(raw)
    return year if MIN_YEAR <= year <= MAX_YEAR else None


def _since_year(match: re.Match[str], as_of: dt.date) -> tuple[dt.date, dt.date] | None:
    year = _valid_year(match.group(1))
    return None if year is None else (dt.date(year, 1, 1), as_of)


def _since_month(match: re.Match[str], as_of: dt.date) -> tuple[dt.date, dt.date] | None:
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    explicit = _valid_year(match.group(2)) if match.group(2) else None
    if match.group(2) and explicit is None:
        return None
    return dt.date(_backdated_month(as_of, month, explicit), month, 1), as_of


def _named_month(match: re.Match[str], as_of: dt.date) -> tuple[dt.date, dt.date] | None:
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    explicit = _valid_year(match.group(2)) if match.group(2) else None
    if match.group(2) and explicit is None:
        return None
    return _month_window(_backdated_month(as_of, month, explicit), month)


def _bare_year(match: re.Match[str], _as_of: dt.date) -> tuple[dt.date, dt.date] | None:
    year = _valid_year(match.group(1))
    return None if year is None else _year_window(year)


def resolve_time_window(question: str, as_of: dt.date) -> TimeWindow | None:
    """The window `question` names relative to `as_of`, or `None` if it names none.

    Args:
        question: the question as asked. Read as text, never as SQL.
        as_of: the committed `[time] as_of` date.

    Returns:
        The earliest phrase's window, longest match first among phrases starting
        at the same position, or `None`.
    """
    if not isinstance(question, str) or not question.strip():
        return None
    lowered = question.lower()

    best: tuple[int, int, str, dt.date, dt.date] | None = None
    for pattern, handler in _PATTERNS:
        for match in re.finditer(pattern, lowered):
            window = handler(match, as_of)
            if window is None:
                continue
            start, end = window
            if end < start:
                continue
            candidate = (match.start(), -(match.end() - match.start()), match.group(0), start, end)
            if best is None or candidate[:2] < best[:2]:
                best = candidate

    if best is None:
        return None
    _position, _length, phrase, start, end = best
    return TimeWindow(phrase=phrase.strip(), start=start.isoformat(), end=end.isoformat())

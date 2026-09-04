"""Canonicalise a result set, and measure how many samples agree with one.

Agreement is measured on **executed rows**, never on SQL text. Two correct
queries can be spelled differently and would never agree on their text, while two
identical wrong queries agree perfectly — so text comparison gets the sign wrong
in both directions.

Column labels are canonicalised (lowercased) and **recorded**, but equality is
decided on the rows alone. `SELECT COUNT(*) AS n` and `SELECT COUNT(*) AS total`
are one answer with two names, and a tool that called them a disagreement would
report low confidence on a question two samples got right. What the labels are
for is the trace: a reader looking at a disagreement wants to see whether the two
candidates were even measuring the same thing.

**Agreement is a confidence factor and never a source of truth.** Three samples
can be wrong in the same way — same prompt, same model, same misreading — and a
vote among them would launder that into certainty. It moves the score; it never
decides the answer, and the answer is always the primary's.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from ..execute import ResultSet


@dataclass(frozen=True)
class CanonicalResult:
    """One result set reduced to a form two of them can be compared in."""

    columns: tuple[str, ...]
    rows: tuple[tuple, ...]

    def agrees_with(self, other: "CanonicalResult") -> bool:
        """Whether two candidates produced the same answer. Rows only — see module docs."""
        return self.rows == other.rows

    @property
    def digest(self) -> str:
        """A short stable identity for the rows, for grouping and for the trace."""
        payload = json.dumps(
            [list(row) for row in self.rows], ensure_ascii=False, default=repr
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _canonical_value(value: object, float_places: int) -> object:
    """Round a float; leave every other SQLite type exactly as it came back.

    Floats are the only type where two arithmetically identical queries can
    differ: SQLite's AVG over the same rows in a different join order can move
    the last bit, and comparing raw floats would report a disagreement between
    two correct answers. Integers are never touched, because money here is an
    integer count of cents and rounding one would be the exact bug this whole
    project exists to prevent.
    """
    return round(value, float_places) if isinstance(value, float) else value


def _sort_key(row: Sequence[object]) -> tuple:
    """A total order over rows that cannot raise on mixed types.

    `sorted` on raw tuples raises `TypeError` the first time a NULL meets an
    integer, which for this schema is any grouped query with an outer join. The
    key is a type name plus a string rendering: it sorts "10" before "9", which
    is meaningless as an ordering and irrelevant as one — all that is required is
    that two equal multisets of rows sort into the same sequence, and a
    deterministic key gives that.
    """
    return tuple((type(value).__name__, "" if value is None else str(value)) for value in row)


def rounded_rows(result: ResultSet, *, float_places: int) -> tuple[tuple, ...]:
    """The rows with floats rounded and **nothing reordered**.

    Split out of `canonicalise` for stage 08, which needs both forms: a question
    that asked for a ranking is scored on the sequence, and everything else on
    the multiset. Sorting is the only difference between them, so it happens in
    exactly one place and neither caller can round differently from the other.
    """
    return tuple(
        tuple(_canonical_value(value, float_places) for value in row) for row in result.rows
    )


def canonicalise(result: ResultSet, *, float_places: int) -> CanonicalResult:
    """Reduce a `ResultSet` to the form agreement is measured in.

    Args:
        result: the rows as they came back from stage 04.
        float_places: `[verify] float_places`, the decimal places floats are
            rounded to before comparison.
    """
    rows = rounded_rows(result, float_places=float_places)
    return CanonicalResult(
        columns=tuple(column.lower() for column in result.columns),
        rows=tuple(sorted(rows, key=_sort_key)),
    )


def agreement_fraction(
    primary: CanonicalResult, samples: Sequence[CanonicalResult | None]
) -> float:
    """The fraction of samples whose rows equal the primary's.

    `samples` **includes the primary**, so the fraction is never zero for a run
    that produced an answer and `k = 1` reports 1.0 — which is honest rather than
    flattering: one sample is not evidence of consistency, and the confidence
    calculation drops the factor entirely at `k = 1` rather than counting a
    tautology as a pass.

    A `None` is a sample that produced no rows at all — refused by the guard, or
    failed in execution. It counts in the denominator and not in the numerator,
    because a candidate that never ran did not agree, and dropping it would let a
    run where two of three samples failed report perfect agreement.

    Raises:
        ValueError: `samples` is empty. A fraction over nothing is not a score.
    """
    if not samples:
        raise ValueError("agreement needs at least one sample, including the primary")
    matches = sum(
        1 for sample in samples if sample is not None and sample.agrees_with(primary)
    )
    return matches / len(samples)

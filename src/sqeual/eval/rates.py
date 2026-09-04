"""The record types every number in an evaluation is reported as.

Split out of `metrics.py` so that "what a rate *is*" and "how the rates are
computed" are two files somebody can read separately — and so that `present.py`
can render one without importing the aggregation.

One rule is worth stating here rather than only in a docstring: **a rate over a
zero denominator is `None`, never `0.0`.** A rate of nothing and a rate over
nothing are different facts, and every renderer downstream prints `n/a` for the
second rather than a percentage nobody measured.

`wilson_interval` is project 1's, imported rather than restated. An interval
formula written twice is an interval formula that will one day disagree with
itself.
"""

from dataclasses import dataclass

from regression_detect.compare import WILSON_Z, wilson_interval

LEVEL_ORDER = ("HIGH", "MEDIUM", "LOW")
"""ABSTAIN is absent because an abstaining run shows no figures, so it can never
appear among the answered questions the table buckets."""


@dataclass(frozen=True)
class Rate:
    """A fraction with its denominator and its interval, or nothing at all."""

    label: str
    passes: int
    n: int

    @property
    def value(self) -> float | None:
        """`None` when the denominator is zero. Never 0.0 — a rate over nothing
        is not a rate of nothing, and the difference is the whole point."""
        return None if self.n == 0 else self.passes / self.n

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.passes, self.n)

    def as_json(self) -> dict:
        low, high = self.interval
        return {
            "label": self.label,
            "passes": self.passes,
            "n": self.n,
            "rate": self.value,
            "interval": [low, high],
            "z": WILSON_Z,
        }


@dataclass(frozen=True)
class CalibrationBucket:
    """One confidence level, and how often the answers inside it were right."""

    level: str
    rate: Rate
    mean_confidence: float | None
    """The average score the tool computed for the answers in this bucket. Printed
    beside the accuracy so a reader can see the claim next to the outcome."""

    def as_json(self) -> dict:
        return {
            "level": self.level,
            "mean_confidence": self.mean_confidence,
            **self.rate.as_json(),
        }


@dataclass(frozen=True)
class CostTotals:
    """What the whole eval consumed. Money is an integer count of micro-USD."""

    calls: int
    input_tokens: int
    output_tokens: int
    micro_usd: int
    priced: bool
    """False means `[cost]` holds no tariff, never that the run was free."""

    def as_json(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "micro_usd": self.micro_usd,
            "currency": "USD",
            "priced": self.priced,
        }


@dataclass(frozen=True)
class LatencyTotals:
    """Wall clock spent inside model calls, in milliseconds."""

    total_ms: int
    median_ms: int
    max_ms: int

    def as_json(self) -> dict:
        return {"total_ms": self.total_ms, "median_ms": self.median_ms, "max_ms": self.max_ms}



__all__ = [
    "LEVEL_ORDER",
    "CalibrationBucket",
    "CostTotals",
    "LatencyTotals",
    "Rate",
]

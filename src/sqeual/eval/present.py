"""Turning one number into one piece of text, and the banner that labels it.

Split out of `render.py` so that the two documents and the vocabulary they are
written in are separate files. Everything here is a pure function of a `Rate`, a
count or a provenance mapping; nothing here knows what a golden question is.

`EvalMetrics` is imported for the banner, which is the one function here that
needs the whole run rather than one number. `metrics` does not import this
module, so there is no cycle.

Two of these carry a rule rather than a format.

**`money` does integer arithmetic.** Micro-USD is a millionth of a dollar,
because token prices are quoted at four or five significant figures and cents are
far too coarse for one call. Dividing in floating point would put a rounding
error in the one column of a report that is about money.

**`banner` is what stops a synthetic document being read as evidence.** It is the
first line of every file this package writes, and the first key of every JSON one.
A reader who finds one of these in a directory six months from now must not have
to work out which kind of run produced it.
"""

from collections.abc import Sequence

from .metrics import EvalMetrics
from .rates import CalibrationBucket, Rate

MICRO_PER_USD = 1_000_000

SYNTHETIC_BANNER = (
    "SYNTHETIC — every number below was produced by a scripted offline provider. "
    "No model was called. This says whether the harness computes what it claims, "
    "and nothing whatsoever about whether a model can write SQL."
)


def money(micro_usd: int) -> str:
    """Micro-USD as both an integer count and dollars. Integer arithmetic only."""
    units, remainder = divmod(abs(micro_usd), MICRO_PER_USD)
    sign = "-" if micro_usd < 0 else ""
    return f"{micro_usd:,} micro-USD ({sign}${units}.{remainder:06d})"


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def interval(rate: Rate) -> str:
    if rate.n == 0:
        return "—"
    low, high = rate.interval
    return f"[{low:.3f}, {high:.3f}]"


def rate_row(rate: Rate, label: str | None = None) -> str:
    return (
        f"| {label or rate.label} | {percent(rate.value)} | {rate.passes}/{rate.n} | "
        f"{interval(rate)} |"
    )


def live_banner(metrics: EvalMetrics, provenance: dict) -> str:
    """One line naming the date, the model, the counts, and what failed."""
    accuracy = metrics.execution_accuracy
    failures = []
    if metrics.errored:
        failures.append(f"{metrics.errored} question(s) errored")
    if metrics.judge_errors:
        failures.append(f"{metrics.judge_errors} judge call(s) unreadable")
    if metrics.broken:
        failures.append(f"{metrics.broken} reference(s) broken")
    what_failed = "; ".join(failures) if failures else "nothing failed"
    return (
        f"LIVE — {provenance['questions']} golden question(s) against "
        f"{provenance['model_id']}, k={provenance['k']}, on "
        f"{provenance['started_utc']}. {accuracy.passes}/{accuracy.n} answered correctly, "
        f"{metrics.false_answers} false answer(s), {what_failed}."
    )


def banner(metrics: EvalMetrics, provenance: dict) -> str:
    return SYNTHETIC_BANNER if provenance["dry_run"] else live_banner(metrics, provenance)


def render_calibration_table(
    buckets: Sequence[CalibrationBucket],
) -> list[str]:
    """The calibration rows, for embedding in `eval.md`."""
    if not buckets:
        return [
            "## Calibration",
            "",
            "No question was answered, so there is no curve to draw.",
            "",
        ]
    return [
        "## Calibration",
        "",
        "| confidence | mean score | accuracy | count | 95% Wilson |",
        "|---|---|---|---|---|",
        *[
            f"| {bucket.level} | "
            f"{'n/a' if bucket.mean_confidence is None else f'{bucket.mean_confidence:.2f}'} | "
            f"{percent(bucket.rate.value)} | {bucket.rate.passes}/{bucket.rate.n} | "
            f"{interval(bucket.rate)} |"
            for bucket in buckets
        ],
        "",
    ]



__all__ = [
    "MICRO_PER_USD",
    "SYNTHETIC_BANNER",
    "banner",
    "interval",
    "live_banner",
    "money",
    "percent",
    "rate_row",
    "render_calibration_table",
]

"""Counting what an evaluation actually spent, including on the calls that failed.

This exists for one honest reason. When a question errors partway through, the
calls it already made are gone: `run_ask` raises before it can build an outcome,
so there is no trace and no cost to read. Counting at the seam instead means an
errored question reports what it spent — and a run's total is a total, rather
than a total of the questions that happened to finish.

That is not a hypothetical. The first live run of this harness hit a provider
returning 503, and its first question failed **after** one successful call.
Without this, the report would have said zero.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ..providers import MeteredProvider, completion_cost_micro_usd


@dataclass(frozen=True)
class Usage:
    """What some calls consumed. Money is an integer count of micro-USD."""

    calls: int
    input_tokens: int
    output_tokens: int
    micro_usd: int
    latency_ms: int

    def since(self, earlier: "Usage") -> "Usage":
        return Usage(
            calls=self.calls - earlier.calls,
            input_tokens=self.input_tokens - earlier.input_tokens,
            output_tokens=self.output_tokens - earlier.output_tokens,
            micro_usd=self.micro_usd - earlier.micro_usd,
            latency_ms=self.latency_ms - earlier.latency_ms,
        )


class CountingProvider:
    """The metered seam, plus a running total of what it has returned.

    Exists for one reason and it is an honesty one. When a question errors
    partway through, the calls it already made are gone: `run_ask` raises before
    it can build an outcome, so there is no trace and no cost. Recording it here
    means an errored question reports what it actually spent instead of zero —
    and a run's total cost is a total rather than a total of the questions that
    happened to finish.

    Costs are summed here rather than at the end because a tariff can only be
    applied to a `Completion`, and a completion belonging to a question that
    errored is never handed to anybody else.
    """

    def __init__(self, inner: MeteredProvider, cost_settings) -> None:
        self._inner = inner
        self._cost_settings = cost_settings
        self.model_id = inner.model_id
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.micro_usd = 0
        self.latency_ms = 0

    def complete(self, *, system: str, user: str, temperature: float):
        completion = self._inner.complete(system=system, user=user, temperature=temperature)
        self.calls += 1
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        self.micro_usd += completion_cost_micro_usd(completion, self._cost_settings)
        self.latency_ms += completion.latency_ms
        return completion

    def snapshot(self) -> Usage:
        return Usage(
            calls=self.calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            micro_usd=self.micro_usd,
            latency_ms=self.latency_ms,
        )


def total_usage(counters: Sequence["CountingProvider"]) -> Usage:
    """The usage across every counter, which is one unless the judge differs."""
    snapshots = [counter.snapshot() for counter in counters]
    return Usage(
        calls=sum(item.calls for item in snapshots),
        input_tokens=sum(item.input_tokens for item in snapshots),
        output_tokens=sum(item.output_tokens for item in snapshots),
        micro_usd=sum(item.micro_usd for item in snapshots),
        latency_ms=sum(item.latency_ms for item in snapshots),
    )


__all__ = ["CountingProvider", "Usage", "total_usage"]

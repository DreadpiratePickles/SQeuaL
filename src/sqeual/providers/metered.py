"""A model call that also reports what it consumed.

Project 1's `Provider` returns a string. That is the right shape for a tool that
grades answers and the wrong shape for one that has to write a run's cost into a
trace: estimating tokens from character counts would put a guess in the money
column, and a guess in the money column is indistinguishable from a measurement.

So this package widens project 1's seam rather than changing it. `Provider`
answers "what did the model say"; `MeteredProvider` answers "what did it say,
and what did it consume". `TextProviderView` narrows the wider seam back to the
narrower one, which is how project 1's `judge_criterion` — which takes a
`Provider` — gets called without dropping the usage it never asked for.

The typed error hierarchy is imported from project 1 unchanged, so every caller
in this package catches one set of exceptions whichever seam produced them.
"""

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from regression_detect.providers.base import (
    Provider,
    ProviderConfigError,
    ProviderError,
    ProviderResponseError,
    ProviderTransientError,
)

__all__ = [
    "Completion",
    "MeteredProvider",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderResponseError",
    "ProviderTransientError",
    "TextProviderView",
    "UsageError",
    "completion_cost_micro_usd",
    "elapsed_ms",
    "validate_token_count",
]
"""Re-exported so no module outside `providers/` imports from project 1
directly. If project 1's error names ever move, this is the one file to fix."""

TOKENS_PER_PRICE_UNIT = 1_000
"""Prices in `[cost]` are quoted per 1,000 tokens."""


class UsageError(ProviderResponseError):
    """The provider replied but its reported token usage is unusable.

    A subclass of `ProviderResponseError` because that is what it is: a reply
    that failed validation at the boundary. Never retried, and never defaulted
    to zero — a run recorded as free because a usage block was missing is worse
    than a run recorded as failed, because nothing downstream can tell.
    """


def validate_token_count(value: object, *, field: str) -> int:
    """Validate a token count reported by a provider.

    Model output is untrusted input, and a usage block is model output.

    Raises:
        UsageError: the count is not a non-negative integer. `bool` is rejected
            explicitly: it passes `isinstance(x, int)` in Python, and `True`
            silently costing one token is exactly the bug this guards.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError(
            f"{field} must be a non-negative integer, got {type(value).__name__}: {value!r}"
        )
    if value < 0:
        raise UsageError(f"{field} must be a non-negative integer, got {value}")
    return value


@dataclass(frozen=True)
class Completion:
    """One model reply, with the evidence needed to price and audit it.

    Frozen: a completion is a record of something that already happened.
    """

    text: str
    input_tokens: int
    output_tokens: int
    model_id: str
    latency_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ProviderResponseError("completion must name the model that produced it")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ProviderResponseError(
                f"model {self.model_id!r} returned an empty or non-string reply"
            )
        validate_token_count(self.input_tokens, field="input_tokens")
        validate_token_count(self.output_tokens, field="output_tokens")
        validate_token_count(self.latency_ms, field="latency_ms")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@runtime_checkable
class MeteredProvider(Protocol):
    """A narrow adapter around one text-in / text-plus-usage-out model call."""

    model_id: str
    """Identifier of the model actually called, recorded in every trace."""

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        """Return the model's reply to `user` under the `system` rules, metered."""
        ...


class TextProviderView:
    """Present a `MeteredProvider` as project 1's text-only `Provider`.

    Project 1's `judge_criterion` owns the parts of criterion judging worth
    reusing: the prompt, the `<ticket>`/`<summary>`/`<criterion>` delimiters that
    keep graded material out of the system prompt, and the strict verdict
    parsing. It takes a `Provider`, which returns a string.

    Rather than reimplement it to get at the token counts, this class hands it
    exactly the seam it wants and keeps the `Completion` on the side. It is
    deliberately a one-call object — the caller reads `last_completion`
    immediately — so there is never a question about which reply the usage
    belongs to.
    """

    def __init__(self, inner: MeteredProvider, *, pacer=None) -> None:
        """Args:
            inner: the metered provider to narrow.
            pacer: paced here rather than by the caller, because the call itself
                happens inside `judge_criterion` where the caller cannot reach it.
        """
        self._inner = inner
        self._pacer = pacer
        self.model_id = inner.model_id
        self.last_completion: Completion | None = None

    def complete(self, *, system: str, user: str, temperature: float) -> str:
        if self._pacer is not None:
            self._pacer.wait()
        completion = self._inner.complete(system=system, user=user, temperature=temperature)
        self.last_completion = completion
        return completion.text


def _leg_micro_usd(tokens: int, price_micro_usd_per_1k: int) -> int:
    """One priced leg, rounded **up**, in pure integer arithmetic.

    `-(-n // 1000)` is the ceiling of `n / 1000` without ever constructing the
    quotient as a float. Rounding up per leg is the conservative reading of a
    two-part tariff; truncating would make a million one-token calls free, and a
    trace that under-reports spend is worse than a trace with no cost in it.
    """
    return -(-(tokens * price_micro_usd_per_1k) // TOKENS_PER_PRICE_UNIT)


def completion_cost_micro_usd(completion: Completion, settings) -> int:
    """Price one completion against a `[cost]` tariff, in micro-USD.

    Zero when the tariff is zero, which `CostSettings.priced` reports as
    *unpriced* rather than free. A caller writing this into a trace must record
    that flag beside it, or a reader will take a 0 for a bill of nothing.
    """
    return _leg_micro_usd(
        completion.input_tokens, settings.input_micro_usd_per_1k_tokens
    ) + _leg_micro_usd(completion.output_tokens, settings.output_micro_usd_per_1k_tokens)


def elapsed_ms(started: float) -> int:
    """Milliseconds since a `time.monotonic()` reading, floored at zero."""
    return max(0, int((time.monotonic() - started) * 1000))

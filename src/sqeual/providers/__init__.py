"""The provider seam: the only package in SQeuaL that knows a model exists.

Phase A called none of this. Nothing in `db`, `schema`, `guard` or `execute`
imports it, and that is still true — the guardrails do not need a model, which
is why they were built first.

`MeteredProvider` is the protocol every call site depends on. `Completion` is
what one call returns: the text, the two token counts, the model that produced it
and how long it took. Project 1's typed error hierarchy is re-exported here so
that no module outside this package imports from project 1 directly.
"""

from .metered import (
    Completion,
    MeteredProvider,
    Provider,
    ProviderConfigError,
    ProviderError,
    ProviderResponseError,
    ProviderTransientError,
    TextProviderView,
    UsageError,
    completion_cost_micro_usd,
    elapsed_ms,
    validate_token_count,
)
from .role_fake import FAKE_MODEL_ID, Role, RoleAwareFakeProvider

__all__ = [
    "FAKE_MODEL_ID",
    "Completion",
    "MeteredProvider",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderResponseError",
    "ProviderTransientError",
    "Role",
    "RoleAwareFakeProvider",
    "TextProviderView",
    "UsageError",
    "completion_cost_micro_usd",
    "elapsed_ms",
    "validate_token_count",
]

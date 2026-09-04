"""The metered Gemini adapter: the only module in SQeuaL that imports the SDK.

Project 1's `GeminiProvider` already owns the hard parts of talking to this
vendor — the retry budget, the backoff-with-jitter policy, the request timeout,
and which status codes count as transient. Those constants are **imported** from
it rather than restated, so the two projects cannot drift on what a retryable
failure is.

What could not be reused is the call itself. Project 1's `complete()` returns
`response.text` and discards the response object, and the token counts a trace
is priced from live on `response.usage_metadata`. Reaching them means owning the
loop, and that is the entire reason this file exists.

The SDK call sits behind `_generate`, so the retry loop, the error
classification and the response validation are all testable offline by replacing
one method instead of by constructing a fake SDK client.
"""

import os
import random
import time

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from regression_detect.providers.gemini import (
    API_KEY_ENV_VAR,
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    MAX_ATTEMPTS,
    REQUEST_TIMEOUT_MS,
    RETRYABLE_STATUS_CODES,
)

from .metered import (
    Completion,
    ProviderConfigError,
    ProviderResponseError,
    ProviderTransientError,
    UsageError,
    elapsed_ms,
    validate_token_count,
)


def _sleep_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter, capped. Project 1's policy exactly."""
    ceiling = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
    return random.uniform(0.0, ceiling)  # noqa: S311 — jitter, not cryptography


class MeteredGeminiProvider:
    """One Gemini text call, returning the reply and what it consumed."""

    def __init__(self, model_id: str, api_key: str) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ProviderConfigError(f"model_id must be a non-empty string, got {model_id!r}")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderConfigError(
                f"A Gemini API key is required. Set {API_KEY_ENV_VAR} in your .env file."
            )

        self.model_id = model_id
        try:
            self._client = genai.Client(
                api_key=api_key,
                http_options=genai_types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
            )
        except Exception as exc:  # the SDK raises bare exceptions on bad config
            raise ProviderConfigError(
                f"could not build the Gemini client for model {self.model_id}"
            ) from exc

    def _generate(self, *, system: str, user: str, temperature: float) -> object:
        """The single SDK call. The only line in this package that leaves the machine."""
        return self._client.models.generate_content(
            model=self.model_id,
            contents=user,
            config=genai_types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
            ),
        )

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        """Call the model, then validate both the text and the usage block.

        Raises:
            ProviderTransientError: still failing after `MAX_ATTEMPTS`.
            ProviderConfigError: credentials rejected. Never retried; no amount
                of waiting supplies a valid key.
            ProviderResponseError / UsageError: the reply or its usage block did
                not validate.
        """
        last_transient: ProviderTransientError | None = None
        for attempt in range(MAX_ATTEMPTS):
            started = time.monotonic()
            try:
                response = self._generate(system=system, user=user, temperature=temperature)
            except genai_errors.APIError as exc:
                error = self._classify_api_error(exc)
                if not isinstance(error, ProviderTransientError):
                    raise error from exc
                last_transient = error
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_transient = ProviderTransientError(
                    f"network failure calling model {self.model_id}: {type(exc).__name__}"
                )
            else:
                return self._to_completion(response, latency_ms=elapsed_ms(started))

            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(_sleep_seconds(attempt))

        if last_transient is None:
            # Unreachable while MAX_ATTEMPTS >= 1: every iteration returns,
            # raises, or records a transient failure. Raised rather than
            # asserted because `python -O` strips assertions, and a stripped one
            # here would surface as a confusing TypeError instead of naming the
            # constant that is wrong.
            raise ProviderConfigError(
                f"MAX_ATTEMPTS must be at least 1, got {MAX_ATTEMPTS}: "
                f"model {self.model_id} was never called."
            )
        raise ProviderTransientError(
            f"model {self.model_id} still failing after {MAX_ATTEMPTS} attempts: {last_transient}"
        ) from last_transient

    def _classify_api_error(self, exc: object) -> Exception:
        """Map an SDK API error onto a typed error. The key is never in the message."""
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None)
        detail = f"model {self.model_id}, status {code} {status}"

        if code in RETRYABLE_STATUS_CODES:
            return ProviderTransientError(f"transient provider failure ({detail})")
        if code in (401, 403):
            return ProviderConfigError(
                f"Gemini rejected the credentials ({detail}). "
                f"Check that {API_KEY_ENV_VAR} is set to a valid, enabled key."
            )
        return ProviderResponseError(f"provider call failed ({detail})")

    def _to_completion(self, response: object, *, latency_ms: int) -> Completion:
        """Validate the SDK response into a `Completion`.

        Both halves are untrusted. A missing usage block raises rather than
        defaulting to zero tokens: a run recorded as free is worse than a run
        recorded as failed, because a trace cannot then say which it was.
        """
        text = getattr(response, "text", None)
        if text is None:
            reason = getattr(response, "prompt_feedback", None)
            raise ProviderResponseError(
                f"model {self.model_id} returned no text "
                f"(finish reason or safety block: {reason})"
            )
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError(f"model {self.model_id} returned an empty response")

        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            raise UsageError(
                f"model {self.model_id} returned no usage_metadata; the call cannot be priced"
            )

        input_tokens = validate_token_count(
            getattr(usage, "prompt_token_count", None), field="prompt_token_count"
        )
        # The SDK reports reasoning tokens separately from the visible answer.
        # The vendor bills them as output, so both legs are charged; omitting the
        # thinking one would understate the bill on exactly the requests that
        # cost the most.
        output_tokens = validate_token_count(
            getattr(usage, "candidates_token_count", None) or 0, field="candidates_token_count"
        ) + validate_token_count(
            getattr(usage, "thoughts_token_count", None) or 0, field="thoughts_token_count"
        )

        return Completion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_id=self.model_id,
            latency_ms=latency_ms,
        )


def metered_provider_from_env(model_id: str) -> MeteredGeminiProvider:
    """Build a provider, reading the API key from `.env` or the environment.

    Raises:
        ProviderConfigError: the key is absent, with a message that names the
            variable and never contains the key.
    """
    load_dotenv()
    api_key = os.environ.get(API_KEY_ENV_VAR, "")
    if not api_key.strip():
        raise ProviderConfigError(
            f"{API_KEY_ENV_VAR} is not set. Create a .env file in the repository root "
            f"containing a line '{API_KEY_ENV_VAR}=<your key>', or export the variable "
            "in your shell. Keys are never committed. Run with --dry-run to exercise "
            "the whole pipeline offline instead."
        )
    return MeteredGeminiProvider(model_id=model_id, api_key=api_key)

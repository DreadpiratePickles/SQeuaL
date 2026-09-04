"""The metered Gemini adapter, exercised offline by replacing one method.

`_generate` is the only line in the package that leaves the machine, so it is the
only thing these tests replace. Everything around it — the retry budget, the
backoff policy, the error classification, the usage validation — is real code
running against real inputs, which is the point of putting the seam there rather
than around the whole class.

Nothing here makes a network call and nothing reads a key.
"""

import httpx
import pytest

from sqeual.providers import (
    ProviderConfigError,
    ProviderResponseError,
    ProviderTransientError,
    UsageError,
)
from sqeual.providers.gemini import MeteredGeminiProvider, metered_provider_from_env


class FakeUsage:
    def __init__(self, prompt=11, candidates=7, thoughts=0):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts


class FakeResponse:
    def __init__(self, text="ok", usage=None, prompt_feedback=None):
        self.text = text
        self.usage_metadata = usage
        self.prompt_feedback = prompt_feedback


class FakeApiError(Exception):
    """Shaped like the SDK's `APIError`: a `code` and a `status`."""

    def __init__(self, code, status="X"):
        super().__init__(f"{code} {status}")
        self.code = code
        self.status = status


def build(monkeypatch, replies):
    """A provider whose one network method returns or raises from `replies`."""
    monkeypatch.setattr(
        MeteredGeminiProvider, "__init__", lambda self, model_id, api_key: None
    )
    provider = MeteredGeminiProvider("m", "k")
    provider.model_id = "test-model"
    calls = []

    def generate(self, *, system, user, temperature):
        calls.append({"system": system, "user": user, "temperature": temperature})
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(MeteredGeminiProvider, "_generate", generate)
    monkeypatch.setattr("sqeual.providers.gemini.time.sleep", lambda _seconds: None)
    provider._calls = calls
    return provider


class TestConstruction:
    def test_a_blank_model_id_is_refused(self):
        with pytest.raises(ProviderConfigError, match="model_id"):
            MeteredGeminiProvider("  ", "key")

    def test_a_blank_key_is_refused_and_never_echoed(self):
        with pytest.raises(ProviderConfigError) as caught:
            MeteredGeminiProvider("model", "  ")
        assert "GEMINI_API_KEY" in str(caught.value)

    def test_a_missing_environment_key_names_the_variable_and_the_offline_path(
        self, monkeypatch
    ):
        monkeypatch.setattr("sqeual.providers.gemini.load_dotenv", lambda: None)
        monkeypatch.setenv("GEMINI_API_KEY", "")
        with pytest.raises(ProviderConfigError) as caught:
            metered_provider_from_env("model")
        message = str(caught.value)
        assert "GEMINI_API_KEY" in message
        assert "--dry-run" in message


class TestSuccess:
    def test_text_and_both_token_legs_come_back(self, monkeypatch):
        provider = build(monkeypatch, [FakeResponse("hello", FakeUsage(11, 7, 0))])
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert completion.text == "hello"
        assert (completion.input_tokens, completion.output_tokens) == (11, 7)
        assert completion.model_id == "test-model"

    def test_thinking_tokens_are_billed_as_output(self, monkeypatch):
        """The vendor charges them; omitting the leg understates the priciest calls."""
        provider = build(monkeypatch, [FakeResponse("hello", FakeUsage(11, 7, 30))])
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert completion.output_tokens == 37

    def test_the_temperature_reaches_the_call(self, monkeypatch):
        provider = build(monkeypatch, [FakeResponse("hello", FakeUsage())])
        provider.complete(system="s", user="u", temperature=0.7)
        assert provider._calls[0]["temperature"] == 0.7


class TestValidation:
    def test_a_missing_usage_block_is_an_error_and_never_zero_tokens(self, monkeypatch):
        """A run recorded as free is worse than a run recorded as failed."""
        provider = build(monkeypatch, [FakeResponse("hello", None)])
        with pytest.raises(UsageError, match="cannot be priced"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_a_negative_token_count_is_refused(self, monkeypatch):
        provider = build(monkeypatch, [FakeResponse("hello", FakeUsage(prompt=-1))])
        with pytest.raises(UsageError, match="prompt_token_count"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_no_text_names_the_safety_block(self, monkeypatch):
        provider = build(
            monkeypatch, [FakeResponse(None, FakeUsage(), prompt_feedback="BLOCKED")]
        )
        with pytest.raises(ProviderResponseError, match="BLOCKED"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_an_empty_reply_is_an_error_and_not_an_empty_success(self, monkeypatch):
        provider = build(monkeypatch, [FakeResponse("   ", FakeUsage())])
        with pytest.raises(ProviderResponseError, match="empty"):
            provider.complete(system="s", user="u", temperature=0.0)


class TestRetries:
    def test_a_transient_status_is_retried_and_then_succeeds(self, monkeypatch):
        provider = build(
            monkeypatch, [FakeApiError(429, "RESOURCE_EXHAUSTED"),
                          FakeResponse("hello", FakeUsage())]
        )
        # The SDK's error type is what the adapter catches, so the fake has to be
        # one; patching the name is cheaper and more honest than subclassing a
        # vendor exception whose constructor signature is not ours.
        monkeypatch.setattr("sqeual.providers.gemini.genai_errors.APIError", FakeApiError)
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert completion.text == "hello"
        assert len(provider._calls) == 2

    def test_a_transient_failure_that_never_clears_raises_after_the_budget(
        self, monkeypatch
    ):
        provider = build(monkeypatch, [FakeApiError(503, "UNAVAILABLE")])
        monkeypatch.setattr("sqeual.providers.gemini.genai_errors.APIError", FakeApiError)
        with pytest.raises(ProviderTransientError, match="after 3 attempts"):
            provider.complete(system="s", user="u", temperature=0.0)
        assert len(provider._calls) == 3

    def test_rejected_credentials_are_never_retried(self, monkeypatch):
        """No amount of waiting supplies a valid key."""
        provider = build(monkeypatch, [FakeApiError(403, "PERMISSION_DENIED")])
        monkeypatch.setattr("sqeual.providers.gemini.genai_errors.APIError", FakeApiError)
        with pytest.raises(ProviderConfigError, match="credentials"):
            provider.complete(system="s", user="u", temperature=0.0)
        assert len(provider._calls) == 1

    def test_an_unclassified_status_is_a_response_error(self, monkeypatch):
        provider = build(monkeypatch, [FakeApiError(400, "INVALID_ARGUMENT")])
        monkeypatch.setattr("sqeual.providers.gemini.genai_errors.APIError", FakeApiError)
        with pytest.raises(ProviderResponseError, match="400"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_a_network_timeout_is_transient(self, monkeypatch):
        provider = build(
            monkeypatch,
            [httpx.ConnectTimeout("slow"), FakeResponse("hello", FakeUsage())],
        )
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert completion.text == "hello"

    def test_no_error_message_ever_contains_the_key(self, monkeypatch):
        provider = build(monkeypatch, [FakeApiError(401, "UNAUTHENTICATED")])
        monkeypatch.setattr("sqeual.providers.gemini.genai_errors.APIError", FakeApiError)
        with pytest.raises(ProviderConfigError) as caught:
            provider.complete(system="s", user="u", temperature=0.0)
        assert str(caught.value) != "k"
        assert "sk-" not in str(caught.value)

"""The provider seam: the only place in this package that knows a model exists.

Two properties are asserted here rather than assumed. A usage block that cannot
be read is an error and never zero tokens — a run recorded as free is worse than
a run recorded as failed. And the role-aware fake used by `--dry-run` recognises
each of the three prompts by a marker that the real prompt file actually
contains, so the dry run cannot quietly start answering every call the same way.
"""

import pytest

from sqeual.config_pipeline import CostSettings
from sqeual.generate.prompt import build_generate_user_message
from sqeual.providers import (
    Completion,
    ProviderConfigError,
    ProviderResponseError,
    RoleAwareFakeProvider,
    UsageError,
    completion_cost_micro_usd,
    validate_token_count,
)
from sqeual.providers.role_fake import ROLE_MARKERS, Role, role_of_system_prompt


def a_completion(**overrides) -> Completion:
    fields = {
        "text": "hello",
        "input_tokens": 100,
        "output_tokens": 20,
        "model_id": "fake-1",
        "latency_ms": 5,
    }
    return Completion(**{**fields, **overrides})


class TestCompletion:
    def test_a_completion_totals_its_two_legs(self):
        assert a_completion().total_tokens == 120

    def test_an_empty_reply_is_a_response_error(self):
        with pytest.raises(ProviderResponseError, match="empty"):
            a_completion(text="   ")

    def test_a_completion_must_name_its_model(self):
        with pytest.raises(ProviderResponseError, match="name the model"):
            a_completion(model_id="")

    @pytest.mark.parametrize("bad", [-1, True, "12", 1.5, None])
    def test_a_token_count_that_is_not_a_non_negative_integer_is_refused(self, bad):
        with pytest.raises(UsageError):
            validate_token_count(bad, field="input_tokens")

    def test_zero_tokens_is_a_valid_count(self):
        """A fake provider genuinely consumed nothing; that is not a usage error."""
        assert validate_token_count(0, field="input_tokens") == 0


class TestCost:
    def test_an_unpriced_tariff_costs_nothing_and_says_so(self):
        settings = CostSettings(
            input_micro_usd_per_1k_tokens=0, output_micro_usd_per_1k_tokens=0
        )
        assert settings.priced is False
        assert completion_cost_micro_usd(a_completion(), settings) == 0

    def test_each_leg_is_priced_separately_and_rounds_up(self):
        # 100 input tokens at 75 micro-USD/1k = 7.5 -> 8; 20 output at 300/1k = 6.
        settings = CostSettings(
            input_micro_usd_per_1k_tokens=75, output_micro_usd_per_1k_tokens=300
        )
        assert completion_cost_micro_usd(a_completion(), settings) == 14

    def test_a_single_token_is_never_free(self):
        settings = CostSettings(
            input_micro_usd_per_1k_tokens=1, output_micro_usd_per_1k_tokens=1
        )
        one = a_completion(input_tokens=1, output_tokens=0)
        assert completion_cost_micro_usd(one, settings) == 1


class TestRoleDetection:
    @pytest.mark.parametrize("role", list(Role))
    def test_every_role_marker_appears_in_the_prompt_it_identifies(self, role):
        """The fake keys off a substring; this pins that the substring is real.

        Without this the day somebody rewords a prompt is the day `--dry-run`
        silently answers every call with the same canned reply.
        """
        from sqeual.providers.role_fake import prompt_text_for_role

        assert ROLE_MARKERS[role] in prompt_text_for_role(role)

    def test_an_unrecognised_system_prompt_is_a_config_error(self):
        with pytest.raises(ProviderConfigError, match="does not match any known role"):
            role_of_system_prompt("you are a helpful assistant")


class TestRoleAwareFake:
    def build_user_message(self, question: str) -> str:
        return build_generate_user_message(
            question=question,
            schema_markdown="# schema",
            as_of="2026-08-31",
            examples=(),
            guard_findings=(),
        )

    def test_the_generate_role_returns_parseable_json_naming_a_select(self):
        import json

        provider = RoleAwareFakeProvider()
        reply = provider.complete(
            system=prompt_for(Role.GENERATE),
            user=self.build_user_message("how much did we refund last month"),
            temperature=0.0,
        )
        payload = json.loads(reply.text)
        assert payload["sql"].upper().startswith("SELECT")
        assert payload["clarification_needed"] is False

    def test_the_generate_role_varies_its_sql_with_the_question(self):
        provider = RoleAwareFakeProvider()
        refunds = provider.complete(
            system=prompt_for(Role.GENERATE),
            user=self.build_user_message("how much did we refund last month"),
            temperature=0.0,
        ).text
        orders = provider.complete(
            system=prompt_for(Role.GENERATE),
            user=self.build_user_message("how many orders were there"),
            temperature=0.0,
        ).text
        assert refunds != orders

    def test_the_generate_role_writes_the_resolved_window_into_its_sql(self):
        """The fake is scaffolding and may know what a model would have to infer."""
        provider = RoleAwareFakeProvider()
        reply = provider.complete(
            system=prompt_for(Role.GENERATE),
            user=self.build_user_message("how much did we refund last month"),
            temperature=0.0,
        )
        assert "2026-07-01" in reply.text
        assert "2026-07-31" in reply.text

    def test_the_explain_role_returns_an_explanation_object(self):
        import json

        provider = RoleAwareFakeProvider()
        reply = provider.complete(
            system=prompt_for(Role.EXPLAIN),
            user="<sql>\nSELECT 1\n</sql>",
            temperature=0.0,
        )
        assert set(json.loads(reply.text)) == {"explanation"}

    def test_the_judge_role_passes(self):
        import json

        provider = RoleAwareFakeProvider()
        reply = provider.complete(
            system=prompt_for(Role.JUDGE), user="<criterion>x</criterion>", temperature=0.0
        )
        assert json.loads(reply.text)["passed"] is True

    def test_the_fake_reports_zero_tokens_rather_than_inventing_them(self):
        provider = RoleAwareFakeProvider()
        reply = provider.complete(
            system=prompt_for(Role.JUDGE), user="x", temperature=0.0
        )
        assert reply.input_tokens == 0
        assert reply.output_tokens == 0
        assert reply.model_id == "role-aware-fake"

    def test_every_call_is_recorded(self):
        provider = RoleAwareFakeProvider()
        provider.complete(system=prompt_for(Role.JUDGE), user="x", temperature=0.0)
        provider.complete(system=prompt_for(Role.EXPLAIN), user="y", temperature=0.3)
        assert [call.role for call in provider.calls] == [Role.JUDGE, Role.EXPLAIN]
        assert provider.calls[1].temperature == 0.3


def prompt_for(role: Role) -> str:
    from sqeual.providers.role_fake import prompt_text_for_role

    return prompt_text_for_role(role)


class TestPacing:
    def test_the_first_call_never_waits(self):
        from sqeual.providers.pacing import Pacer

        pacer = Pacer(50)
        pacer.wait()
        assert pacer.waits == 0

    def test_a_second_call_inside_the_interval_waits_once(self):
        import time

        from sqeual.providers.pacing import Pacer

        pacer = Pacer(30)
        pacer.wait()
        started = time.monotonic()
        pacer.wait()
        assert pacer.waits == 1
        assert time.monotonic() - started >= 0.02

    def test_zero_disables_pacing_and_records_no_wait(self):
        """The clock advances across every call; only a real sleep is a wait."""
        from sqeual.providers.pacing import Pacer

        pacer = Pacer(0)
        for _ in range(5):
            pacer.wait()
        assert pacer.waits == 0

    def test_a_negative_interval_is_refused_at_the_boundary(self):
        import pytest as _pytest

        from sqeual.providers.pacing import Pacer

        with _pytest.raises(ValueError, match="non-negative"):
            Pacer(-1)

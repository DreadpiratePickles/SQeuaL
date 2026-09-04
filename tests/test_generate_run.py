"""Stage 05 end to end, with a scripted provider: no network, no key, no money.

Three properties carry the stage. The tables a question was *shown* are the
tables its SQL is *permitted to reach*, so a candidate that wanders outside the
slice is refused rather than executed. The repair budget is a hard ceiling and
the counter has no branch that resets it. And what executes is always
`normalised_sql` — the statement the guard regenerated from the tree it read —
never the string the model wrote.
"""

import pytest

from conftest import ScriptedProvider, phase_b_config, proposal_json
from sqeual.generate.run import GenerationStatus, generate_candidates
from sqeual.guard import GuardPolicy
from sqeual.providers.pacing import Pacer
from sqeual.schema.slice import slice_for_question

REFUND_SQL = (
    "SELECT SUM(r.amount_cents) AS refunded_cents FROM refunds AS r "
    "WHERE r.refund_date >= '2026-07-01' AND r.refund_date <= '2026-07-31'"
)
COUNT_SQL = "SELECT COUNT(*) AS n FROM refunds"
QUESTION = "how much did we refund last month"


def run(config, card, provider, question=QUESTION, k=None):
    schema_slice = slice_for_question(
        question, card, max_tables=config.schema.max_tables, synonyms=config.schema.synonyms
    )
    return generate_candidates(
        question=question,
        card=card,
        schema_slice=schema_slice,
        policy=GuardPolicy.from_settings(config.guard),
        provider=provider,
        config=config,
        pacer=Pacer(0),
        k=k,
    )


@pytest.fixture
def config(tmp_path, session_db):
    return phase_b_config(tmp_path, session_db)


class TestHappyPath:
    def test_the_primary_answers_and_the_samples_only_agree(self, config, session_card):
        provider = ScriptedProvider([proposal_json(REFUND_SQL)] * 3)
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.ANSWERED
        assert outcome.agreement == 1.0
        assert outcome.primary.result.row_count == 1

    def test_the_primary_is_sampled_at_temperature_zero(self, config, session_card):
        provider = ScriptedProvider([proposal_json(REFUND_SQL)] * 3)
        run(config, session_card, provider)
        assert provider.calls[0]["temperature"] == 0.0
        assert provider.calls[1]["temperature"] == 0.7

    def test_what_executes_is_the_guards_statement_and_not_the_models(
        self, config, session_card
    ):
        """The model wrote no LIMIT; the guard added one, and that is what ran."""
        provider = ScriptedProvider([proposal_json(REFUND_SQL)] * 3)
        outcome = run(config, session_card, provider)
        assert outcome.primary.report.normalised_sql.endswith("LIMIT 200")
        assert "LIMIT" not in outcome.primary.proposal.sql

    def test_k_may_be_overridden_for_one_run(self, config, session_card):
        provider = ScriptedProvider([proposal_json(REFUND_SQL)])
        outcome = run(config, session_card, provider, k=1)
        assert outcome.k == 1
        assert len(provider.calls) == 1

    def test_the_resolved_window_is_recorded_beside_the_date_the_model_was_told(
        self, config, session_card
    ):
        provider = ScriptedProvider([proposal_json(REFUND_SQL)] * 3)
        outcome = run(config, session_card, provider)
        assert outcome.as_of == "2026-08-31"
        assert (outcome.time_window.start, outcome.time_window.end) == (
            "2026-07-01",
            "2026-07-31",
        )

    def test_the_question_is_the_last_thing_in_the_prompt(self, config, session_card):
        """A question that tries to redefine the rules arrives after them, as data."""
        provider = ScriptedProvider([proposal_json(REFUND_SQL)] * 3)
        run(config, session_card, provider)
        user = provider.calls[0]["user"]
        assert user.rstrip().endswith("</question>")
        assert user.index("<rules>") < user.index("<question>")

    def test_agreement_is_measured_on_rows_and_not_on_sql_text(self, config, session_card):
        """Two spellings of one answer must agree; identical text is not the test."""
        other_spelling = (
            "SELECT SUM(amount_cents) AS total_cents FROM refunds "
            "WHERE refund_date BETWEEN '2026-07-01' AND '2026-07-31'"
        )
        provider = ScriptedProvider(
            [proposal_json(REFUND_SQL), proposal_json(other_spelling), proposal_json(REFUND_SQL)]
        )
        outcome = run(config, session_card, provider)
        assert outcome.agreement == 1.0
        assert outcome.attempts[1].proposal.sql != outcome.primary.proposal.sql

    def test_a_disagreeing_sample_lowers_agreement_without_changing_the_answer(
        self, config, session_card
    ):
        provider = ScriptedProvider(
            [proposal_json(REFUND_SQL), proposal_json(COUNT_SQL), proposal_json(REFUND_SQL)]
        )
        outcome = run(config, session_card, provider)
        assert outcome.agreement == 2 / 3
        assert outcome.primary.proposal.sql == REFUND_SQL


class TestTheSliceIsABoundary:
    def test_a_table_outside_the_slice_is_refused_as_policy_not_hallucination(
        self, config, session_card
    ):
        """`table_not_allowed` and `unknown_table` are different facts on purpose."""
        outside = "SELECT COUNT(*) AS n FROM products"
        provider = ScriptedProvider([proposal_json(outside)] * 2)
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.GUARD_BLOCKED
        assert "table_not_allowed" in outcome.blocked_codes
        assert "unknown_table" not in outcome.blocked_codes

    def test_a_question_matching_no_table_asks_for_clarification_without_calling_a_model(
        self, config, session_card
    ):
        provider = ScriptedProvider([])
        outcome = run(config, session_card, provider, question="what is the weather like")
        assert outcome.status is GenerationStatus.CLARIFICATION
        assert provider.calls == []
        assert "refunds" in outcome.clarification


class TestRepair:
    def test_a_guard_failure_is_fed_back_once_and_recorded(self, config, session_card):
        bad = "SELECT region FROM refunds"
        provider = ScriptedProvider(
            [proposal_json(bad), proposal_json(REFUND_SQL), proposal_json(REFUND_SQL),
             proposal_json(REFUND_SQL)]
        )
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.ANSWERED
        assert outcome.repairs_used == 1
        assert outcome.primary.repair_index == 1

    def test_the_repair_prompt_carries_the_codes_and_not_a_paragraph(
        self, config, session_card
    ):
        bad = "SELECT region FROM refunds"
        provider = ScriptedProvider([proposal_json(bad)] + [proposal_json(REFUND_SQL)] * 3)
        run(config, session_card, provider)
        repair_prompt = provider.calls[1]["user"]
        assert "<guard_findings>" in repair_prompt
        assert "unknown_column" in repair_prompt

    def test_the_repair_budget_is_a_ceiling_and_a_second_failure_is_final(
        self, config, session_card
    ):
        """A provider that fails forever must produce exactly max_repairs extra calls."""
        bad = proposal_json("SELECT region FROM refunds")
        provider = ScriptedProvider([bad, bad])
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.GUARD_BLOCKED
        assert outcome.repairs_used == 1
        assert len(provider.calls) == 2

    def test_zero_repairs_means_the_first_verdict_is_final(self, tmp_path, session_db,
                                                           session_card):
        config = phase_b_config(tmp_path, session_db, [("max_repairs = 1", "max_repairs = 0")])
        provider = ScriptedProvider([proposal_json("SELECT region FROM refunds")])
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.GUARD_BLOCKED
        assert outcome.repairs_used == 0
        assert len(provider.calls) == 1

    def test_a_failing_secondary_sample_is_never_repaired(self, config, session_card):
        """Only the statement that answers is worth a second call."""
        provider = ScriptedProvider(
            [proposal_json(REFUND_SQL), proposal_json("SELECT region FROM refunds"),
             proposal_json(REFUND_SQL)]
        )
        outcome = run(config, session_card, provider)
        assert outcome.repairs_used == 0
        assert outcome.agreement == 2 / 3


class TestRefusal:
    def test_a_model_asking_for_clarification_is_carried_through(self, config, session_card):
        provider = ScriptedProvider(
            [
                proposal_json(
                    "", clarification_needed=True, clarifying_question="Which month exactly?"
                )
            ]
        )
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.CLARIFICATION
        assert outcome.clarification == "Which month exactly?"

    def test_a_clarification_without_a_question_gets_one_built_by_code(
        self, config, session_card
    ):
        provider = ScriptedProvider(
            [proposal_json("", clarification_needed=True, clarifying_question=None)]
        )
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.CLARIFICATION
        assert QUESTION in outcome.clarification

    def test_an_unparseable_primary_reply_is_never_repaired_in_place(
        self, config, session_card
    ):
        provider = ScriptedProvider(["here you go: SELECT 1"])
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.PARSE_FAILED
        assert len(provider.calls) == 1
        assert outcome.attempts[0].detail.startswith("model reply is not JSON")

    def test_an_unparseable_secondary_reply_only_costs_that_sample(
        self, config, session_card
    ):
        provider = ScriptedProvider(
            [proposal_json(REFUND_SQL), "not json at all", proposal_json(REFUND_SQL)]
        )
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.ANSWERED
        assert outcome.agreement == 2 / 3

    def test_a_primary_that_cannot_execute_is_an_execution_failure(
        self, config, session_card
    ):
        """A hallucinated column inside a starred CTE is opaque to the resolver.

        The guard says nothing it cannot prove, so this reaches the executor —
        which is exactly the arrangement stage 04 exists for.
        """
        opaque = (
            "WITH everything AS (SELECT * FROM refunds) "
            "SELECT SUM(profit_cents) AS n FROM everything"
        )
        provider = ScriptedProvider([proposal_json(opaque)])
        outcome = run(config, session_card, provider)
        assert outcome.status is GenerationStatus.EXECUTION_FAILED


class TestRecording:
    def test_every_attempt_is_recorded_including_the_discarded_ones(
        self, config, session_card
    ):
        provider = ScriptedProvider(
            [proposal_json(REFUND_SQL), "not json", proposal_json(REFUND_SQL)]
        )
        outcome = run(config, session_card, provider)
        assert len(outcome.attempts) == 3
        assert [attempt.outcome for attempt in outcome.attempts] == [
            "ok",
            "parse_failed",
            "ok",
        ]

    def test_tokens_are_summed_across_every_call(self, config, session_card):
        provider = ScriptedProvider([proposal_json(REFUND_SQL)] * 3)
        outcome = run(config, session_card, provider)
        assert outcome.input_tokens == 30
        assert outcome.output_tokens == 15

"""Gates before weights: the checks that can withhold an answer outright.

The first live evaluation produced two answers it should not have produced, and
both had the same cause. The judge — the only component that reads *meaning* —
failed both blind criteria, unanimously, and the answer was rendered anyway at
MEDIUM 0.70, because a weighted average of four numbers cannot be dragged below
a threshold by one of them. `docs/design.md` §53 has the arithmetic and §54 has
the fix: a veto is a different kind of rule from a weight, and it runs first.

The distinction this file exists to hold is the one between a judge that
**disagreed** and a judge that could not be **read**. A definite fail is a
statement; a 503 is a silence. Treating the second as the first would turn a
provider outage into a tool that refuses everything, and the day the provider
came back nobody would know which refusals had been real.
"""

import pytest

from conftest import (
    ScriptedProvider,
    explanation_json,
    load_test_config,
    phase_b_config,
    proposal_json,
    verdict_json,
)
from sqeual.answer.gates import GateStatus, evaluate_gates, gate_named
from sqeual.answer.run import AnswerStatus
from sqeual.generate.run import GenerationStatus
from sqeual.pipeline import run_ask
from sqeual.providers.pacing import Pacer
from sqeual.verify.backtranslate import BackTranslation, JudgeVerdict
from sqeual.verify.checks import CHECK_NAMES, Check, CheckStatus
from sqeual.verify.run import Verification

QUESTION = "How much did we refund to customers in Berlin last month?"


# --- unit: the gate matrix --------------------------------------------------


def settings(tmp_path, substitutions=()):
    return load_test_config(tmp_path, substitutions).gates


def verification(*, intent=(), sanity=(), verdicts=(), explanation="It does a thing."):
    return Verification(
        intent=intent,
        sanity=sanity,
        back_translation=BackTranslation(
            explanation=explanation,
            error=None,
            verdicts=verdicts,
            completions=(),
            explain_model_id="scripted",
            judge_model_id="scripted",
        ),
        same_family=True,
    )


def passed(name):
    return Check(name, CheckStatus.PASS, "scripted pass")


def failed(name):
    return Check(name, CheckStatus.FAIL, f"scripted failure of {name}")


def verdict(status, reason="scripted"):
    return JudgeVerdict(name="answers_the_question", status=status, reason=reason)


def gates_for(tmp_path, **kwargs):
    return evaluate_gates(
        generation_status=kwargs.pop("status", GenerationStatus.ANSWERED),
        verification=kwargs.pop("verification", verification()),
        settings=kwargs.pop("settings", None) or settings(tmp_path),
    )


class TestTheCommittedGatePolicy:
    def test_the_veto_is_on_by_default(self, tmp_path):
        assert settings(tmp_path).judge_veto is True

    def test_the_hard_checks_are_the_two_that_are_not_word_lists(self, tmp_path):
        """`time_window` compares literals against a window **code** resolved
        from `[time] as_of`, and `not_truncated` reads a flag the executor set.
        Neither is a heuristic over English, which is what the other six are and
        why they stay soft. §54."""
        assert settings(tmp_path).hard_checks == frozenset({"time_window", "not_truncated"})

    def test_every_hard_check_is_a_check_that_exists(self, tmp_path):
        assert settings(tmp_path).hard_checks <= set(CHECK_NAMES)


class TestTheJudgeVeto:
    def test_a_definite_fail_on_either_criterion_vetoes(self, tmp_path):
        for statuses in (("fail", "pass"), ("pass", "fail"), ("fail", "fail")):
            found = gate_named(
                gates_for(
                    tmp_path,
                    verification=verification(
                        verdicts=tuple(verdict(status) for status in statuses)
                    ),
                ),
                "judge",
            )
            assert found.status is GateStatus.FAIL, statuses

    def test_the_veto_carries_the_reason_the_judge_gave(self, tmp_path):
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(
                    verdicts=(verdict("fail", "it counts orders, which was not asked for"),)
                ),
            ),
            "judge",
        )
        assert "it counts orders, which was not asked for" in found.detail

    def test_both_passing_is_a_pass(self, tmp_path):
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(verdicts=(verdict("pass"), verdict("pass"))),
            ),
            "judge",
        )
        assert found.status is GateStatus.PASS

    def test_an_unreadable_judge_does_not_veto(self, tmp_path):
        """The load-bearing asymmetry. A 503 in the middle of a run must not
        turn every answer in the deployment into a refusal; a definite "this SQL
        does not answer the question" must. §36 already drops an unread judge
        from the score, and this is the same fact one layer up."""
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(
                    verdicts=(verdict("error", "503"), verdict("error", "503"))
                ),
            ),
            "judge",
        )
        assert found.status is GateStatus.NA
        assert "could not be read" in found.detail

    def test_one_error_and_one_definite_fail_still_vetoes(self, tmp_path):
        """Half a judgement is not half a verdict — but a criterion that came
        back saying no came back saying no, whatever happened to the other."""
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(
                    verdicts=(verdict("error", "503"), verdict("fail", "wrong table"))
                ),
            ),
            "judge",
        )
        assert found.status is GateStatus.FAIL

    def test_the_veto_can_be_turned_off_and_says_so(self, tmp_path):
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(verdicts=(verdict("fail"), verdict("fail"))),
                settings=settings(tmp_path, [("judge_veto = true", "judge_veto = false")]),
            ),
            "judge",
        )
        assert found.status is GateStatus.NA
        assert "judge_veto" in found.detail


class TestHardAndSoftChecks:
    def test_a_hard_intent_check_failing_fails_the_intent_gate(self, tmp_path):
        found = gate_named(
            gates_for(tmp_path, verification=verification(intent=(failed("time_window"),))),
            "intent",
        )
        assert found.status is GateStatus.FAIL
        assert "time_window" in found.detail

    def test_a_soft_intent_check_failing_does_not(self, tmp_path):
        """`aggregation`, `entities` and `grouping` are word-list heuristics with
        documented false positives. A gate built on one would refuse correct
        answers, and a gate people learn to work around is worse than none."""
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(
                    intent=(passed("time_window"), failed("aggregation"), failed("entities"))
                ),
            ),
            "intent",
        )
        assert found.status is GateStatus.PASS

    def test_a_hard_sanity_check_failing_fails_the_sanity_gate(self, tmp_path):
        found = gate_named(
            gates_for(tmp_path, verification=verification(sanity=(failed("not_truncated"),))),
            "sanity",
        )
        assert found.status is GateStatus.FAIL

    def test_a_soft_sanity_check_failing_does_not(self, tmp_path):
        """The mirror of the intent case: the hard check passed, a soft one did
        not, and the gate reports what the hard one said."""
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(
                    sanity=(passed("not_truncated"), failed("scalar_shape"))
                ),
            ),
            "sanity",
        )
        assert found.status is GateStatus.PASS

    def test_a_gate_with_no_hard_check_in_front_of_it_says_nothing(self, tmp_path):
        """Not a pass. A gate whose hard checks are simply absent has approved
        nothing, and NA is the only honest thing it can report."""
        found = gate_named(
            gates_for(tmp_path, verification=verification(sanity=(failed("scalar_shape"),))),
            "sanity",
        )
        assert found.status is GateStatus.NA

    def test_a_hard_check_that_did_not_apply_leaves_the_gate_with_nothing_to_say(
        self, tmp_path
    ):
        """NA is not a pass anywhere else in this codebase and it is not one
        here. A question with no period in it gave `time_window` nothing to
        test, and a gate that read that as approval would be approving a
        silence."""
        found = gate_named(
            gates_for(
                tmp_path,
                verification=verification(
                    intent=(Check("time_window", CheckStatus.NA, "no period"),)
                ),
            ),
            "intent",
        )
        assert found.status is GateStatus.NA


class TestTheGuardGate:
    def test_an_answered_run_passed_the_guard(self, tmp_path):
        assert gate_named(gates_for(tmp_path), "guard").status is GateStatus.PASS

    def test_a_blocked_run_records_the_guard_gate_as_the_one_that_failed(self, tmp_path):
        gates = gates_for(
            tmp_path, status=GenerationStatus.GUARD_BLOCKED, verification=None
        )
        assert gate_named(gates, "guard").status is GateStatus.FAIL
        assert all(
            gate_named(gates, name).status is GateStatus.NA
            for name in ("intent", "judge", "sanity")
        )

    def test_a_run_that_never_produced_a_statement_has_no_guard_verdict(self, tmp_path):
        gates = gates_for(tmp_path, status=GenerationStatus.PARSE_FAILED, verification=None)
        assert gate_named(gates, "guard").status is GateStatus.NA


class TestTheGateTable:
    def test_every_run_reports_the_same_four_gates_in_the_same_order(self, tmp_path):
        assert [gate.name for gate in gates_for(tmp_path)] == [
            "guard",
            "intent",
            "judge",
            "sanity",
        ]

    def test_a_gate_named_by_nothing_is_an_error_rather_than_a_none(self, tmp_path):
        with pytest.raises(AssertionError):
            gate_named(gates_for(tmp_path), "nonesuch")


# --- integration: the pipeline withholds ------------------------------------


@pytest.fixture
def config(tmp_path, session_db):
    return phase_b_config(tmp_path, session_db)


def vetoing_provider():
    """A statement that answers a different question, and a judge that says so.

    `SELECT COUNT(*) FROM orders` for a question about refunds in Berlin — the
    load-bearing case from §29, and the shape of the live run's `channel AS
    shipping_carrier`: valid SQL over a real table that answers something else.
    """
    return ScriptedProvider(
        [proposal_json("SELECT COUNT(*) AS n FROM orders")] * 3
        + [
            explanation_json("It counts every order in the table."),
            verdict_json(False, "the question asked about refunds, not orders"),
            verdict_json(False, "it counts orders, which was not asked for"),
        ]
    )


def confident_but_vetoed_provider():
    """Every deterministic check passes and the judge fails both criteria.

    This is the §53 shape exactly: the guard resolved every column, the window
    resolved, the aggregate is there, agreement is unanimous — and the one
    component that reads meaning said no. Under a weighted average this scored
    0.70 and was shown.
    """
    sql = (
        "SELECT SUM(r.amount_cents) AS refunded_cents FROM refunds AS r "
        "JOIN orders AS o ON o.id = r.order_id "
        "JOIN customers AS c ON c.id = o.customer_id "
        "WHERE c.city = 'Berlin' AND r.refund_date >= '2026-07-01' "
        "AND r.refund_date <= '2026-07-31'"
    )
    return ScriptedProvider(
        [proposal_json(sql)] * 3
        + [
            explanation_json("It totals refunds for Berlin customers in July 2026."),
            verdict_json(False, "the statement measures something the question did not ask"),
            verdict_json(False, "it computes a figure that was not requested"),
        ]
    )


def ask(config, card, tmp_path, provider, question=QUESTION):
    return run_ask(
        question=question,
        card=card,
        config=config,
        provider=provider,
        pacer=Pacer(0),
        runs_root=tmp_path / "runs",
    )


class TestTheVetoWithholdsTheAnswer:
    def test_a_definite_judge_failure_withholds(self, config, session_card, tmp_path):
        outcome = ask(config, session_card, tmp_path, vetoing_provider())
        assert outcome.answer.status is AnswerStatus.WITHHELD

    def test_a_withheld_answer_shows_no_figure_from_the_result(
        self, config, session_card, tmp_path
    ):
        import re

        outcome = ask(config, session_card, tmp_path, vetoing_provider())
        value = str(outcome.generation.primary.result.rows[0][0])
        prose = re.sub(r"```sql\n.*?\n```", "", outcome.answer.markdown, flags=re.DOTALL)
        tokens = [
            token.replace(",", "") for token in re.findall(r"\d[\d,]*(?:\.\d+)?", prose)
        ]
        assert value not in tokens
        assert outcome.answer.cells == ()
        assert outcome.answer.shows_figures is False

    def test_a_withheld_answer_says_which_gate_withheld_it(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, vetoing_provider())
        assert "## Gates" in outcome.answer.markdown
        assert "it counts orders, which was not asked for" in outcome.answer.markdown

    def test_a_withheld_answer_still_shows_the_statement_so_a_human_can_run_it(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, vetoing_provider())
        assert "SELECT COUNT(*) AS n FROM orders" in outcome.answer.markdown

    def test_a_high_confidence_answer_is_still_withheld(
        self, config, session_card, tmp_path
    ):
        """Gates before weights, stated as a test. This is the run §53 could not
        refuse: every other factor legitimately passed, so no weight and no
        threshold reaches it — and the score is recorded rather than hidden, so
        a reader can see what the arithmetic thought."""
        outcome = ask(config, session_card, tmp_path, confident_but_vetoed_provider())
        assert outcome.answer.status is AnswerStatus.WITHHELD
        assert outcome.answer.confidence is not None
        assert outcome.answer.confidence.value >= 0.55
        assert "174994" not in outcome.answer.markdown
        assert "1,749.94" not in outcome.answer.markdown

    def test_the_veto_off_shows_the_same_answer(self, tmp_path, session_db, session_card):
        """The change is a switch somebody can read in a diff, and the run it
        changes is the run §53 measured."""
        config = phase_b_config(
            tmp_path, session_db, [("judge_veto = true", "judge_veto = false")]
        )
        outcome = ask(config, session_card, tmp_path, confident_but_vetoed_provider())
        assert outcome.answer.status is AnswerStatus.ANSWERED
        assert "€1,749.94" in outcome.answer.markdown


class TestAnErroredJudgeIsNotAVeto:
    def erroring_judge_provider(self):
        """The explanation parses; both judge calls come back unreadable."""
        sql = (
            "SELECT SUM(r.amount_cents) AS refunded_cents FROM refunds AS r "
            "JOIN orders AS o ON o.id = r.order_id "
            "JOIN customers AS c ON c.id = o.customer_id "
            "WHERE c.city = 'Berlin' AND r.refund_date >= '2026-07-01' "
            "AND r.refund_date <= '2026-07-31'"
        )
        return ScriptedProvider(
            [proposal_json(sql)] * 3
            + [
                explanation_json("It totals Berlin refunds for July 2026."),
                "not json at all",
                "also not json",
            ]
        )

    def test_an_outage_does_not_turn_a_good_answer_into_a_refusal(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, self.erroring_judge_provider())
        assert outcome.answer.status is AnswerStatus.ANSWERED
        assert "€1,749.94" in outcome.answer.markdown

    def test_the_judge_factor_is_still_dropped_from_the_score(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, self.erroring_judge_provider())
        judge = next(
            factor for factor in outcome.answer.confidence.factors if factor.name == "judge"
        )
        assert judge.applicable is False
        assert judge.contribution == 0.0

    def test_the_gate_says_the_judge_was_unread_rather_than_that_it_agreed(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, self.erroring_judge_provider())
        gate = gate_named(outcome.answer.gates, "judge")
        assert gate.status is GateStatus.NA
        assert "could not be read" in gate.detail


class TestTheGatesReachTheTrace:
    def test_the_trace_carries_a_gates_block(self, config, session_card, tmp_path):
        import json

        outcome = ask(config, session_card, tmp_path, vetoing_provider())
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        assert [gate["gate"] for gate in trace["gates"]] == [
            "guard",
            "intent",
            "judge",
            "sanity",
        ]
        judge = next(gate for gate in trace["gates"] if gate["gate"] == "judge")
        assert judge["status"] == "FAIL"
        assert trace["status"] == "withheld"

    def test_an_answered_run_reports_its_gates_too(self, config, session_card, tmp_path):
        """A gate is not only reported when it fires. "We checked and it was
        fine" and "we never looked" must never render the same."""
        from sqeual.providers import RoleAwareFakeProvider

        outcome = ask(config, session_card, tmp_path, RoleAwareFakeProvider())
        assert outcome.answer.status is AnswerStatus.ANSWERED
        assert "## Gates" in outcome.answer.markdown
        assert len(outcome.trace["gates"]) == 4

    def test_a_guard_blocked_run_records_the_guard_gate(
        self, config, session_card, tmp_path
    ):
        outcome = ask(
            config,
            session_card,
            tmp_path,
            ScriptedProvider([proposal_json("SELECT loyalty_tier FROM customers")] * 4),
            question="What is the average customer loyalty tier?",
        )
        assert outcome.answer.status is AnswerStatus.GUARD_BLOCKED
        assert gate_named(outcome.answer.gates, "guard").status is GateStatus.FAIL
        assert "## Gates" in outcome.answer.markdown

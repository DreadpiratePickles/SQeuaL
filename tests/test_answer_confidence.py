"""Confidence is computed from things that were counted, never asked of a model.

A model asked "how confident are you, 0 to 1" produces a number with no referent.
It is not calibrated against anything, it cannot be audited, and it will happily
say 0.95 about a fabricated column. So the score here is arithmetic over evidence,
and every factor's weight and contribution is written out so a 0.43 can be read as
a sentence rather than trusted as a number.

The three hand-built factor sets below have exact expected scores. That is the
point: a confidence calculation nobody can reproduce by hand is a confidence
calculation nobody can argue with.
"""

import pytest

from conftest import phase_b_config
from sqeual.answer.confidence import ConfidenceLevel, Factors, compute_confidence


@pytest.fixture
def weights(tmp_path, session_db):
    return phase_b_config(tmp_path, session_db).confidence


class TestExactArithmetic:
    def test_everything_agreeing_scores_one(self, weights):
        """40*1.0 + 30*1.0 + 20*1.0 + 10*1.0 over 100 = 1.0, no repair penalty."""
        score = compute_confidence(
            Factors(intent=1.0, judge=1.0, agreement=1.0, sanity=1.0, repaired=False),
            weights,
        )
        assert score.value == 1.0
        assert score.level is ConfidenceLevel.HIGH

    def test_a_mixed_run_with_a_repair(self, weights):
        """(40*0.5 + 30*0.5 + 20*(2/3) + 10*1.0)/100 = 0.583333, less 0.15 = 0.4333."""
        score = compute_confidence(
            Factors(intent=0.5, judge=0.5, agreement=2 / 3, sanity=1.0, repaired=True),
            weights,
        )
        assert score.value == 0.4333
        assert score.level is ConfidenceLevel.LOW

    def test_a_run_with_only_a_failing_intent_check_scores_zero(self, weights):
        """A judge that errored and k=1 drop out; 40*0.0 over 40 is 0.0."""
        score = compute_confidence(
            Factors(intent=0.0, judge=None, agreement=None, sanity=None, repaired=False),
            weights,
        )
        assert score.value == 0.0
        assert score.level is ConfidenceLevel.ABSTAIN

    def test_every_contribution_sums_to_the_weighted_total(self, weights):
        score = compute_confidence(
            Factors(intent=0.5, judge=1.0, agreement=0.5, sanity=1.0, repaired=False),
            weights,
        )
        contributions = sum(factor.contribution for factor in score.factors if factor.applicable)
        assert round(contributions, 10) == score.value


class TestDroppingInapplicableFactors:
    def test_an_inapplicable_factor_leaves_both_halves_of_the_fraction(self, weights):
        """Not a zero. "Nothing to check" is not evidence against the statement."""
        dropped = compute_confidence(
            Factors(intent=1.0, judge=None, agreement=1.0, sanity=1.0, repaired=False),
            weights,
        )
        assert dropped.value == 1.0

    def test_a_judge_error_is_recorded_as_unavailable_rather_than_omitted(self, weights):
        score = compute_confidence(
            Factors(intent=1.0, judge=None, agreement=1.0, sanity=1.0, repaired=False),
            weights,
        )
        judge = next(factor for factor in score.factors if factor.name == "judge")
        assert judge.applicable is False
        assert judge.contribution == 0.0

    def test_a_run_where_nothing_applied_abstains_rather_than_scoring_one(self, weights):
        """No evidence is not good evidence."""
        score = compute_confidence(
            Factors(intent=None, judge=None, agreement=None, sanity=None, repaired=False),
            weights,
        )
        assert score.value == 0.0
        assert score.level is ConfidenceLevel.ABSTAIN


class TestLevels:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1.0, ConfidenceLevel.HIGH),
            (0.75, ConfidenceLevel.HIGH),
            (0.7499, ConfidenceLevel.MEDIUM),
            (0.55, ConfidenceLevel.MEDIUM),
            (0.5499, ConfidenceLevel.LOW),
            (0.40, ConfidenceLevel.LOW),
            (0.3999, ConfidenceLevel.ABSTAIN),
            (0.0, ConfidenceLevel.ABSTAIN),
        ],
    )
    def test_each_threshold_is_tested_from_both_sides(self, value, expected, weights):
        """A threshold tested from one side is a threshold nobody knows the direction of."""
        from sqeual.answer.confidence import level_for

        assert level_for(value, weights) is expected


class TestPenalties:
    def test_a_repair_lowers_the_score_and_can_never_raise_it(self, weights):
        without = compute_confidence(
            Factors(intent=1.0, judge=1.0, agreement=1.0, sanity=1.0, repaired=False), weights
        )
        with_repair = compute_confidence(
            Factors(intent=1.0, judge=1.0, agreement=1.0, sanity=1.0, repaired=True), weights
        )
        assert with_repair.value < without.value
        assert with_repair.value == 0.85

    def test_the_score_is_clamped_at_zero(self, weights):
        score = compute_confidence(
            Factors(intent=0.0, judge=0.0, agreement=0.0, sanity=0.0, repaired=True), weights
        )
        assert score.value == 0.0


class TestFactorsFromEvidence:
    def test_two_passing_verdicts_are_one(self):
        from sqeual.answer.confidence import judge_value
        from sqeual.verify.backtranslate import JudgeVerdict

        verdicts = (
            JudgeVerdict("a", "pass", "x"),
            JudgeVerdict("b", "pass", "y"),
        )
        assert judge_value(verdicts) == 1.0

    def test_one_of_each_is_a_half(self):
        from sqeual.answer.confidence import judge_value
        from sqeual.verify.backtranslate import JudgeVerdict

        verdicts = (JudgeVerdict("a", "pass", "x"), JudgeVerdict("b", "fail", "y"))
        assert judge_value(verdicts) == 0.5

    def test_both_failing_is_zero(self):
        from sqeual.answer.confidence import judge_value
        from sqeual.verify.backtranslate import JudgeVerdict

        verdicts = (JudgeVerdict("a", "fail", "x"), JudgeVerdict("b", "fail", "y"))
        assert judge_value(verdicts) == 0.0

    def test_any_error_makes_the_whole_factor_unavailable(self):
        """Half a judgement is not half a verdict; it is an unread judge."""
        from sqeual.answer.confidence import judge_value
        from sqeual.verify.backtranslate import JudgeVerdict

        verdicts = (JudgeVerdict("a", "pass", "x"), JudgeVerdict("b", "error", "y"))
        assert judge_value(verdicts) is None

    def test_a_single_sample_is_not_evidence_of_consistency(self):
        from sqeual.answer.confidence import agreement_value

        assert agreement_value(agreement=1.0, k=1) is None
        assert agreement_value(agreement=1.0, k=2) == 1.0

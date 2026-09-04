"""Compute a confidence from evidence. Never ask a model for one.

A model asked "how confident are you, 0 to 1" produces a number with no referent.
It is not calibrated against anything, it cannot be audited, and it will say 0.95
about a fabricated column as readily as about a correct one. So the score is
arithmetic, over four things that were **counted**:

  * how many of the applicable intent and shape checks passed;
  * how the two blind judge verdicts came back;
  * how many of the `k` samples produced the primary's rows;
  * and whether the statement had to be repaired.

Two rules make it readable rather than merely reproducible.

**An inapplicable factor is dropped from both halves of the fraction, never
scored as zero and never as one.** A question with no time phrase in it gave the
time check nothing to say; counting that as evidence in the statement's favour
would reward a question for being vague, and counting it against would punish it.
A judge whose reply could not be read has likewise said nothing. `None` and `0.0`
are different facts, and the difference reaches the score.

**Nothing here can raise a score.** Every factor is a fraction in [0, 1] and the
repair penalty only subtracts, so there is no path by which a model's opinion
promotes a weak answer. The worst any factor can do is fail to lower it.

The score is rounded once, to four places, and the level is read from the rounded
value — so the number a user is shown and the level they are shown can never
disagree with each other.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

SCORE_PLACES = 4
PERCENT = 100.0


class ConfidenceLevel(StrEnum):
    """What the score means, read top down against the configured thresholds."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    ABSTAIN = "ABSTAIN"
    """Below the abstain threshold the tool refuses and shows no figures.
    Refusing is a feature: a hedged number is repeated without its hedge in the
    first email that quotes it, which is how a low-confidence guess becomes a
    figure in a board pack."""


@dataclass(frozen=True)
class Factors:
    """The four measured inputs. `None` means "this run gave it nothing to say"."""

    intent: float | None
    judge: float | None
    agreement: float | None
    sanity: float | None
    repaired: bool


@dataclass(frozen=True)
class FactorContribution:
    """One factor's weight and what it actually put into the score."""

    name: str
    applicable: bool
    value: float | None
    weight: int
    contribution: float
    note: str


@dataclass(frozen=True)
class Confidence:
    """The score, the level, and the whole working."""

    value: float
    level: ConfidenceLevel
    factors: tuple[FactorContribution, ...]
    repair_penalty: float

    @property
    def percent(self) -> int:
        return round(self.value * PERCENT)


def judge_value(verdicts: Sequence) -> float | None:
    """The judge factor: 1.0 both passed, 0.5 one did, 0.0 neither, `None` on error.

    Any error makes the whole factor unavailable rather than scoring the half
    that came back. Half a judgement is not half a verdict; it is an unread
    judge, and the honest thing to do with an unread judge is not to count it.
    """
    if not verdicts or any(verdict.status == "error" for verdict in verdicts):
        return None
    return sum(1 for verdict in verdicts if verdict.status == "pass") / len(verdicts)


def agreement_value(*, agreement: float | None, k: int) -> float | None:
    """The self-consistency factor, or `None` when there was no consistency to measure.

    At `k = 1` the fraction is 1.0 by construction — the primary agrees with
    itself — and counting a tautology as a pass would let `--k 1` buy confidence
    it did not earn. The factor is dropped instead, and the trace says why.
    """
    if agreement is None or k < 2:
        return None
    return agreement


def level_for(value: float, settings) -> ConfidenceLevel:
    """Read a level off the configured thresholds, top down."""
    if value >= settings.high_threshold / PERCENT:
        return ConfidenceLevel.HIGH
    if value >= settings.medium_threshold / PERCENT:
        return ConfidenceLevel.MEDIUM
    if value >= settings.abstain_threshold / PERCENT:
        return ConfidenceLevel.LOW
    return ConfidenceLevel.ABSTAIN


NOTES: dict[str, tuple[str, str]] = {
    "intent": (
        "share of the applicable intent and shape checks that passed",
        "no intent or shape check applied to this question",
    ),
    "judge": (
        "share of the two blind back-translation criteria that passed",
        "the judge could not be read; an unread judge has not agreed",
    ),
    "agreement": (
        "share of the samples whose rows matched the primary's",
        "one sample cannot be consistent with itself in any useful sense",
    ),
    "sanity": (
        "share of the applicable result-shape checks that passed",
        "no result-shape check applied to this question",
    ),
}


def compute_confidence(factors: Factors, settings) -> Confidence:
    """Turn four measured factors and the configured weights into a score.

    Args:
        factors: the measured evidence. `None` for a factor that did not apply.
        settings: the validated `[confidence]` section.

    Returns:
        The score in [0, 1] rounded to four places, its level, and every factor's
        weight and contribution — so the number can be read as a sentence.
    """
    weights = {
        "intent": settings.weight_intent,
        "judge": settings.weight_judge,
        "agreement": settings.weight_agreement,
        "sanity": settings.weight_sanity,
    }
    values = {
        "intent": factors.intent,
        "judge": factors.judge,
        "agreement": factors.agreement,
        "sanity": factors.sanity,
    }
    applicable_weight = sum(
        weight for name, weight in weights.items() if values[name] is not None
    )

    contributions = []
    for name, weight in weights.items():
        value = values[name]
        applies, unapplies = NOTES[name]
        contributions.append(
            FactorContribution(
                name=name,
                applicable=value is not None,
                value=value,
                weight=weight,
                contribution=(
                    0.0 if value is None or not applicable_weight
                    else weight * value / applicable_weight
                ),
                note=applies if value is not None else unapplies,
            )
        )

    # No applicable factor is not a perfect score. A run that measured nothing
    # has produced no evidence, and no evidence abstains.
    weighted = sum(item.contribution for item in contributions) if applicable_weight else 0.0
    penalty = settings.repair_penalty / PERCENT if factors.repaired else 0.0
    value = round(max(0.0, min(1.0, weighted - penalty)), SCORE_PLACES)
    return Confidence(
        value=value,
        level=level_for(value, settings),
        factors=tuple(contributions),
        repair_penalty=penalty,
    )


__all__ = [
    "Confidence",
    "ConfidenceLevel",
    "FactorContribution",
    "Factors",
    "agreement_value",
    "compute_confidence",
    "judge_value",
    "level_for",
]

"""One named check and what it found — the shared vocabulary of stage 06.

Four statuses, and the third and fourth earn their place:

  * **NA** is not a pass. A question with no time phrase in it has nothing for the
    time check to say, and scoring that as evidence in the statement's favour
    would reward a question for being vague. NA is dropped from the pass
    fraction; it is *reported* rather than omitted, so "we checked and it was
    fine" never renders the same as "there was nothing to check".
  * **FLAG** is not a fail. An empty result is frequently the correct answer —
    "no refunds in March" — and a check that failed on one would refuse to say
    the true thing. A flag is surfaced in the answer and scored as neither.
"""

from dataclasses import dataclass
from enum import StrEnum


class CheckStatus(StrEnum):
    """Outcome of one deterministic check."""

    PASS = "PASS"
    FAIL = "FAIL"
    NA = "NA"
    """Nothing in the question or the result made this check applicable."""
    FLAG = "FLAG"
    """Something a reader should be told, which is not evidence either way."""


@dataclass(frozen=True)
class Check:
    """One named check, its verdict, and the evidence a human can dispute."""

    name: str
    status: CheckStatus
    evidence: str


def pass_fraction(checks: tuple[Check, ...]) -> float | None:
    """The share of applicable checks that passed, or `None` if none applied.

    `None` and `0.0` are different facts and the difference reaches the score:
    a run where nothing was checkable contributes no evidence, and a run where
    everything checkable failed contributes the worst evidence there is.
    """
    applicable = [
        check for check in checks if check.status in (CheckStatus.PASS, CheckStatus.FAIL)
    ]
    if not applicable:
        return None
    return sum(1 for check in applicable if check.status is CheckStatus.PASS) / len(applicable)

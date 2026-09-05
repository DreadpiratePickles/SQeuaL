"""Gates before weights: the four checks that can withhold an answer outright.

A gate answers yes or no. It runs before the confidence arithmetic and it is not
part of it, and the difference is the whole point of this module. `confidence.py`
computes a weighted average over four factors, and §36 establishes that nothing
in that calculation can *raise* a score — the mirror of which is that nothing in
it can **sink** one either. The first live evaluation is where that bill came
due: on two questions the blind judge failed both criteria unanimously, and both
answers were rendered at MEDIUM 0.70, because three of four factors legitimately
passed and a weighted average of four numbers cannot be dragged below a threshold
by one of them. `docs/design.md` §53 has the arithmetic; §54 is this module.

So the score no longer decides whether to answer. It decides how to **rank** the
answers that got past the gates, which is what a score is good at and what a
veto is bad at.

The distinction this module exists to hold is between a judge that **disagreed**
and a judge that could not be **read**. A definite `fail` is a statement; a 503
is a silence. Treating the second as the first would turn a provider outage into
a tool that refuses everything, and on the day the provider came back nobody
could tell which refusals had been real. So an unreadable judge is NA here,
exactly as it is dropped from the score in §36, and it says so in words.

Four gates, always reported, in a fixed order, whatever happened — the same rule
the guard's fourteen rules and stage 06's eight checks follow. "We checked and it
was fine" must never render the same as "we never looked".
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..config_pipeline import GatesSettings
from ..generate.run import GenerationStatus
from ..verify.backtranslate import ERRORED, FAILED, JudgeVerdict
from ..verify.checks import Check, CheckStatus
from ..verify.run import Verification

GUARD = "guard"
INTENT = "intent"
JUDGE = "judge"
SANITY = "sanity"

GATE_ORDER: tuple[str, ...] = (GUARD, INTENT, JUDGE, SANITY)
"""Every run reports these four, in this order. A gate that vanished from the
table when it had nothing to say would render as neither a pass nor a silence."""


class GateStatus(StrEnum):
    """What one gate concluded. Three, and NA is not a pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    """This gate withholds the answer. No figures are shown, whatever the score."""
    NA = "NA"
    """The gate had nothing to decide on. Not approval — a silence."""


@dataclass(frozen=True)
class Gate:
    """One gate, its verdict, and the evidence a reader can argue with."""

    name: str
    status: GateStatus
    detail: str

    @property
    def withholds(self) -> bool:
        return self.status is GateStatus.FAIL


GUARD_VERDICTS: dict[GenerationStatus, tuple[GateStatus, str]] = {
    GenerationStatus.ANSWERED: (
        GateStatus.PASS,
        "the statement passed every guard rule and ran",
    ),
    GenerationStatus.EXECUTION_FAILED: (
        GateStatus.PASS,
        "the statement passed every guard rule; the database could not run it",
    ),
    GenerationStatus.GUARD_BLOCKED: (
        GateStatus.FAIL,
        "the guard refused every candidate, including the repair. Nothing ran",
    ),
    GenerationStatus.PARSE_FAILED: (
        GateStatus.NA,
        "no statement reached the guard: the model's reply could not be read",
    ),
    GenerationStatus.CLARIFICATION: (
        GateStatus.NA,
        "no statement reached the guard: the question needs clarifying first",
    ),
}
"""The guard is a gate that already ran. It is reported here rather than
recomputed, so that one table holds every reason an answer was withheld instead
of the reader having to know that a guard refusal happens somewhere else."""


def _quote(items: Sequence[str]) -> str:
    return "; ".join(items)


def _check_gate(name: str, checks: Sequence[Check], hard: frozenset[str]) -> Gate:
    """Fail on any hard check that failed; pass when one applied and passed.

    A soft check is never consulted here. It is still counted in the score, still
    printed in the check table, and cannot on its own withhold an answer — the
    six soft ones are word lists over English with documented false positives,
    and a gate people learn to work around by rephrasing the question is worse
    than no gate at all.
    """
    watched = [check for check in checks if check.name in hard]
    failed = [check for check in watched if check.status is CheckStatus.FAIL]
    if failed:
        return Gate(
            name=name,
            status=GateStatus.FAIL,
            detail="hard check failed: "
            + _quote([f"{check.name} — {check.evidence}" for check in failed]),
        )
    passed = [check for check in watched if check.status is CheckStatus.PASS]
    if passed:
        return Gate(
            name=name,
            status=GateStatus.PASS,
            detail="every hard check that applied passed: "
            + ", ".join(check.name for check in passed),
        )
    # NA is not a pass anywhere else in this codebase and it is not one here. A
    # question with no period in it gave `time_window` nothing to test, and a
    # gate that read that as approval would be approving a silence.
    listed = ", ".join(sorted(hard)) or "none configured"
    return Gate(
        name=name,
        status=GateStatus.NA,
        detail=f"no hard check applied to this run (hard: {listed})",
    )


def _judge_gate(verdicts: Sequence[JudgeVerdict], *, veto: bool) -> Gate:
    """The veto. A definite fail withholds; an unreadable judge never does.

    That asymmetry is the load-bearing part. §36 already drops an unread judge
    from the score rather than counting it either way; this is the same fact one
    layer up, and stating it twice is cheaper than a deployment discovering that
    a 503 storm refused every question it was asked.
    """
    if not veto:
        return Gate(
            name=JUDGE,
            status=GateStatus.NA,
            detail=(
                "[gates] judge_veto is off, so the judge is a weight and not a "
                "gate. This is the behaviour from before gates existed"
            ),
        )
    definite = [verdict for verdict in verdicts if verdict.status == FAILED]
    if definite:
        return Gate(
            name=JUDGE,
            status=GateStatus.FAIL,
            detail=(
                "the blind judge failed on "
                + _quote([f"{verdict.name} — {verdict.reason}" for verdict in definite])
            ),
        )
    if not verdicts or any(verdict.status == ERRORED for verdict in verdicts):
        return Gate(
            name=JUDGE,
            status=GateStatus.NA,
            detail=(
                "the judge could not be read, and an unread judge has not disagreed. "
                "An outage must not turn every answer into a refusal"
            ),
        )
    return Gate(
        name=JUDGE,
        status=GateStatus.PASS,
        detail="the blind judge passed every criterion it was asked",
    )


def evaluate_gates(
    *,
    generation_status: GenerationStatus,
    verification: Verification | None,
    settings: GatesSettings,
) -> tuple[Gate, ...]:
    """Every gate's verdict for one run, in `GATE_ORDER`.

    Args:
        generation_status: stage 05's outcome. Carries the guard's verdict,
            which is a gate that ran earlier and is reported here beside the
            others.
        verification: stage 06's outcome, or `None` when nothing ran. `None`
            makes the other three NA rather than passing them: a run that
            verified nothing has approved nothing.
        settings: the validated `[gates]` section.

    Returns:
        Four gates, always, in a fixed order.
    """
    status, detail = GUARD_VERDICTS[generation_status]
    gates = [Gate(name=GUARD, status=status, detail=detail)]
    if verification is None:
        gates += [
            Gate(
                name=name,
                status=GateStatus.NA,
                detail="nothing was verified, because no statement ran",
            )
            for name in (INTENT, JUDGE, SANITY)
        ]
        return tuple(gates)

    gates.append(_check_gate(INTENT, verification.intent, settings.hard_checks))
    gates.append(
        _judge_gate(verification.back_translation.verdicts, veto=settings.judge_veto)
    )
    gates.append(_check_gate(SANITY, verification.sanity, settings.hard_checks))
    return tuple(gates)


def gate_named(gates: Sequence[Gate], name: str) -> Gate:
    """The gate called `name`.

    Raises:
        AssertionError: no gate has that name. Returning `None` would let a
            caller read a missing gate as a gate that did not fire.
    """
    for gate in gates:
        if gate.name == name:
            return gate
    raise AssertionError(f"no gate named {name!r} in {[gate.name for gate in gates]}")


def withholding(gates: Sequence[Gate]) -> tuple[Gate, ...]:
    """The gates that failed. Empty means every gate let the answer through."""
    return tuple(gate for gate in gates if gate.withholds)


__all__ = [
    "GATE_ORDER",
    "GUARD_VERDICTS",
    "Gate",
    "GateStatus",
    "evaluate_gates",
    "gate_named",
    "withholding",
]

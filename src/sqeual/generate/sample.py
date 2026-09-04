"""One sample: ask, parse, guard, execute — and record whatever went wrong.

Split out of `run.py` so that "what happens to one candidate" and "how the k
candidates are combined" are two files somebody can read separately. The
orchestration in `run.py` is about the primary, the repair budget and the
agreement; everything here is about a single model call and its aftermath.

Every failure becomes a recorded `Attempt` rather than an exception, for the same
reason the guard returns a report rather than raising: a run that ends in a
refusal still has to show its work, and a refusal nobody can read is
indistinguishable from a bug. The one exception is
`DatabaseUnavailableError`, which is not caught anywhere in this stage — a
missing database is a broken deployment, not a bad candidate, and discarding a
sample over it would turn an outage into a quiet drop in agreement.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..execute import (
    ExecuteLimits,
    ExecutionError,
    ExecutionTimeout,
    ResultSet,
    execute_sql,
)
from ..guard import GuardPolicy, GuardReport, RuleStatus, guard_sql
from ..providers import Completion, MeteredProvider
from ..providers.pacing import Pacer
from ..schema.card import SchemaCard
from .agreement import CanonicalResult, canonicalise
from .parse import GenerationParseError, Proposal, parse_proposal
from .prompt import Example, build_generate_user_message


class AttemptOutcome(StrEnum):
    """What became of one candidate.

    A fixed vocabulary, because a trace that described the same failure two ways
    would be a trace nobody could count. A `StrEnum` so it serialises into
    `trace.json` as its own name without anybody remembering `.value`, and so it
    cannot be confused with `GenerationStatus`, which describes the whole
    question rather than one call.
    """

    OK = "ok"
    PARSE_FAILED = "parse_failed"
    CLARIFICATION = "clarification"
    GUARD_FAILED = "guard_failed"
    EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class Attempt:
    """One model call and everything that happened to its reply."""

    sample_index: int
    repair_index: int
    temperature: float
    raw_reply: str
    outcome: AttemptOutcome
    detail: str
    completion: Completion
    proposal: Proposal | None = None
    report: GuardReport | None = None
    result: ResultSet | None = None
    canonical: CanonicalResult | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is AttemptOutcome.OK

    @property
    def repaired(self) -> bool:
        return self.repair_index > 0


@dataclass(frozen=True)
class SampleContext:
    """Everything one sample needs that does not change between samples.

    Assembled once per question so that `k` calls cannot accidentally differ in
    the schema they were shown or the policy they were checked against — which
    would make an agreement measurement compare two different things.
    """

    card: SchemaCard
    policy: GuardPolicy
    limits: ExecuteLimits
    db_path: Path
    float_places: int
    system: str
    schema_markdown: str
    as_of: str
    examples: tuple[Example, ...]
    question: str


def failed_findings(report: GuardReport) -> tuple[tuple[str, str], ...]:
    """The `(code, detail)` pairs from a failed report, for the repair prompt.

    The code leads because that is the payload — a model repairs better against
    `unknown_column` than against a paragraph — and the detail follows because it
    names what *does* exist, which is what turns the next attempt from a guess
    into a correction.
    """
    return tuple(
        (result.code or result.rule, result.detail)
        for result in report.rules
        if result.status is RuleStatus.FAIL
    )


def blocked_codes(report: GuardReport | None) -> tuple[str, ...]:
    """The distinct finding codes from a refused report, in report order."""
    if report is None:
        return ()
    return tuple(dict.fromkeys(code for code, _detail in failed_findings(report)))


def run_sample(
    context: SampleContext,
    provider: MeteredProvider,
    pacer: Pacer,
    *,
    sample_index: int,
    repair_index: int,
    temperature: float,
    guard_findings: Sequence[tuple[str, str]] = (),
) -> Attempt:
    """Ask once, parse, guard, execute.

    Args:
        context: the schema, policy and prompt this question is being asked with.
        provider: the metered seam.
        pacer: spaces this call against the previous one.
        sample_index: 0 is the primary — the one that answers.
        repair_index: 0 is a first attempt, 1 is after guard feedback.
        temperature: 0 for the primary, `sample_temperature` for the rest.
        guard_findings: `(code, detail)` from the previous attempt, on a repair.

    Raises:
        DatabaseUnavailableError: deliberately not caught. A missing database is
            a broken deployment, and discarding a sample over it would turn an
            outage into a quiet drop in agreement.
        ProviderError: same reasoning — a rejected key is not a bad candidate.
    """
    user = build_generate_user_message(
        question=context.question,
        schema_markdown=context.schema_markdown,
        as_of=context.as_of,
        examples=context.examples,
        guard_findings=guard_findings,
    )
    pacer.wait()
    completion = provider.complete(
        system=context.system, user=user, temperature=temperature
    )
    partial = {
        "sample_index": sample_index,
        "repair_index": repair_index,
        "temperature": temperature,
        "raw_reply": completion.text,
        "completion": completion,
    }

    try:
        proposal = parse_proposal(completion.text)
    except GenerationParseError as exc:
        # Never repaired in place. Pulling a fenced block out of prose means
        # something chose which statement to run, and nobody would know which.
        return Attempt(outcome=AttemptOutcome.PARSE_FAILED, detail=str(exc), **partial)

    if proposal.clarification_needed:
        return Attempt(
            outcome=AttemptOutcome.CLARIFICATION,
            detail=proposal.clarifying_question or "",
            proposal=proposal,
            **partial,
        )

    report = guard_sql(proposal.sql, context.card, context.policy)
    if not report.ok:
        return Attempt(
            outcome=AttemptOutcome.GUARD_FAILED,
            detail="; ".join(f"{code}: {detail}" for code, detail in failed_findings(report)),
            proposal=proposal,
            report=report,
            **partial,
        )

    try:
        # `normalised_sql`, never `proposal.sql`: what runs is exactly what was
        # checked, regenerated from the tree the rules read.
        result = execute_sql(
            report.normalised_sql,
            context.db_path,
            limits=context.limits,
            card=context.card,
            aliases=dict(report.table_aliases),
        )
    except (ExecutionError, ExecutionTimeout) as exc:
        # One slow or broken candidate is not a failed run: the rest are still
        # scored, and only a failure on the *primary* ends the question.
        return Attempt(
            outcome=AttemptOutcome.EXECUTION_FAILED,
            detail=str(exc),
            proposal=proposal,
            report=report,
            **partial,
        )

    return Attempt(
        outcome=AttemptOutcome.OK,
        detail="",
        proposal=proposal,
        report=report,
        result=result,
        canonical=canonicalise(result, float_places=context.float_places),
        **partial,
    )

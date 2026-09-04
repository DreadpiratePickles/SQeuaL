"""Stage 05: one question in, one guarded statement out — or an explicit refusal.

Four things happen here and only one of them is a model call.

**The slice becomes the boundary.** `slice_for_question` picks the tables, and
`GuardPolicy.narrowed_to` makes that set the tables the SQL is *permitted* to
reach. A candidate touching anything else fails as `table_not_allowed`, which is
a different finding from `unknown_table` on purpose: one is this stage
over-reaching, the other is a hallucination, and one code for both would hide
both.

**The primary answers; the samples only agree.** The first call is at
`[generate] temperature` — zero — and its statement is the one that runs. The
other `k-1` are at `sample_temperature` and exist to be compared against it.
Agreement is a confidence factor and never a vote: three samples can be wrong in
the same way, and a plurality among them would launder that into certainty.

**Repair is bounded and the counter never resets.** A guard failure on the
primary is fed back once, as codes rather than prose, because a model repairs
better against `unknown_column` than against a paragraph. A second failure is
final. There is no branch that resets the counter, because an unbounded repair
loop is a bill with no ceiling and a run that never terminates.

**A parse failure is never repaired in place.** A reply that is not valid JSON is
a discarded candidate. Pulling a fenced block out of prose with a regular
expression works until the model writes two blocks, and then something has
quietly chosen which statement to run.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..execute import DatabaseUnavailableError, ExecuteLimits
from ..guard import GuardPolicy
from ..providers import MeteredProvider
from ..providers.pacing import Pacer
from ..schema.card import SchemaCard
from ..schema.render import render_card
from ..schema.slice import SchemaSlice
from .agreement import agreement_fraction
from .prompt import Example, load_examples, load_generate_prompt
from .sample import (
    Attempt,
    AttemptOutcome,
    SampleContext,
    blocked_codes,
    failed_findings,
    run_sample,
)
from .timewindow import TimeWindow, resolve_time_window


class GenerationStatus(StrEnum):
    """What became of one question. Each maps to exactly one CLI exit code."""

    ANSWERED = "answered"
    CLARIFICATION = "clarification"
    """The model asked, or the question matched no table and code asked for it."""
    GUARD_BLOCKED = "guard_blocked"
    """The statement was refused and the repair budget is spent. Nothing ran."""
    PARSE_FAILED = "parse_failed"
    """The model never produced a reply this tool could read. Nothing ran."""
    EXECUTION_FAILED = "execution_failed"
    """The statement passed every check and the database could not answer it."""


@dataclass(frozen=True)
class GenerationOutcome:
    """Everything stage 05 concluded, including the attempts that failed."""

    question: str
    status: GenerationStatus
    schema_slice: SchemaSlice
    as_of: str
    time_window: TimeWindow | None
    attempts: tuple[Attempt, ...]
    primary: Attempt | None
    repairs_used: int
    agreement: float | None
    k: int
    model_id: str
    schema_sha256: str
    """The shape the statement was written against. A statement replayed against a
    database whose columns have moved is not the same statement, and the hash is
    what makes that detectable rather than merely regrettable."""
    clarification: str | None = None
    blocked_codes: tuple[str, ...] = ()
    detail: str = ""

    @property
    def input_tokens(self) -> int:
        return sum(attempt.completion.input_tokens for attempt in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(attempt.completion.output_tokens for attempt in self.attempts)


NO_SLICE_TEMPLATE = (
    "I could not tell which tables {question!r} is about. This database holds "
    "{tables}. Which of those should I look in?"
)
NO_QUESTION_TEMPLATE = (
    "The model asked for clarification about {question!r} without saying what it "
    "needed. Try naming the period, the measure and the grouping explicitly."
)


def _no_slice_outcome(question: str, card: SchemaCard, schema_slice, as_of, model_id, k):
    return GenerationOutcome(
        question=question,
        status=GenerationStatus.CLARIFICATION,
        schema_slice=schema_slice,
        as_of=as_of,
        time_window=None,
        attempts=(),
        primary=None,
        repairs_used=0,
        agreement=None,
        k=k,
        model_id=model_id,
        schema_sha256=card.schema_sha256,
        clarification=NO_SLICE_TEMPLATE.format(
            question=question, tables=", ".join(card.table_names)
        ),
    )


def _primary_status(attempt: Attempt, question: str) -> tuple[GenerationStatus, str | None, str]:
    """Map a failed primary onto the outcome that describes it."""
    if attempt.outcome is AttemptOutcome.CLARIFICATION:
        return (
            GenerationStatus.CLARIFICATION,
            attempt.detail or NO_QUESTION_TEMPLATE.format(question=question),
            "",
        )
    if attempt.outcome is AttemptOutcome.PARSE_FAILED:
        return GenerationStatus.PARSE_FAILED, None, attempt.detail
    if attempt.outcome is AttemptOutcome.GUARD_FAILED:
        return GenerationStatus.GUARD_BLOCKED, None, attempt.detail
    # The only outcome left: `_primary_status` is reached only for a primary that
    # is not `ok`, and the other three are handled above.
    return GenerationStatus.EXECUTION_FAILED, None, attempt.detail


def generate_candidates(
    *,
    question: str,
    card: SchemaCard,
    schema_slice: SchemaSlice,
    policy: GuardPolicy,
    provider: MeteredProvider,
    config,
    pacer: Pacer,
    k: int | None = None,
    examples: Sequence[Example] | None = None,
) -> GenerationOutcome:
    """Turn one question into one guarded, executed statement, or into a refusal.

    Args:
        question: the question as asked. Read as text, never as SQL.
        card: the live schema card, the ground truth every column is checked
            against.
        schema_slice: the tables this question may see — and therefore reach.
        policy: the `[guard]` policy, narrowed here to the slice.
        provider: the metered seam. Called `k` times, plus once per repair.
        config: the validated `sqeual.toml`.
        pacer: spaces the calls under a per-minute quota.
        k: override `[generate] k` for one run.
        examples: override the committed few-shot pairs, for tests.

    Raises:
        ValueError: `k` is not a positive integer. The configured `[generate] k`
            is validated at load time; an override has to be validated here.
        DatabaseUnavailableError: the database is missing or unreadable. A broken
            deployment, not a bad candidate, so it aborts rather than discarding
            a sample.
        ProviderError: the provider could not be reached or refused the
            credentials. Same reasoning.
    """
    settings = config.generate
    samples = k if k is not None else settings.k
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
        raise ValueError(f"k must be an integer of at least 1, got {samples!r}")
    model_id = provider.model_id

    if not schema_slice.tables:
        # Never widened to the whole card as a fallback: showing a model every
        # table because nobody could say which ones matter is how a question
        # about refunds gets answered from `agents`.
        return _no_slice_outcome(question, card, schema_slice, config.time.as_of, model_id, samples)

    context = SampleContext(
        card=card,
        policy=policy.narrowed_to(schema_slice.tables),
        limits=ExecuteLimits.from_settings(config.execute),
        db_path=config.db.path,
        float_places=config.verify.float_places,
        system=load_generate_prompt(),
        schema_markdown=render_card(card, schema_slice.tables),
        as_of=config.time.as_of,
        examples=tuple(examples if examples is not None else load_examples())[
            : settings.max_examples
        ],
        question=question,
    )

    attempts: list[Attempt] = []
    primary = run_sample(
        context, provider, pacer, sample_index=0, repair_index=0, temperature=settings.temperature
    )
    attempts.append(primary)

    repairs_used = 0
    while (
        primary.outcome is AttemptOutcome.GUARD_FAILED
        and repairs_used < settings.max_repairs
        and primary.report is not None
    ):
        repairs_used += 1
        primary = run_sample(
            context,
            provider,
            pacer,
            sample_index=0,
            repair_index=repairs_used,
            temperature=settings.temperature,
            guard_findings=failed_findings(primary.report),
        )
        attempts.append(primary)

    if not primary.ok:
        status, clarification, detail = _primary_status(primary, question)
        return GenerationOutcome(
            question=question,
            status=status,
            schema_slice=schema_slice,
            as_of=config.time.as_of,
            time_window=resolve_time_window(question, config.time.as_of_date),
            attempts=tuple(attempts),
            primary=None,
            repairs_used=repairs_used,
            agreement=None,
            k=samples,
            model_id=model_id,
            schema_sha256=card.schema_sha256,
            clarification=clarification,
            blocked_codes=blocked_codes(primary.report),
            detail=detail,
        )

    # The remaining samples exist only to agree or disagree. None of them is
    # repaired: only the statement that answers is worth a second call.
    for index in range(1, samples):
        attempts.append(
            run_sample(
                context,
                provider,
                pacer,
                sample_index=index,
                repair_index=0,
                temperature=settings.sample_temperature,
            )
        )

    canonicals = [attempt.canonical for attempt in attempts if attempt.sample_index != 0]
    return GenerationOutcome(
        question=question,
        status=GenerationStatus.ANSWERED,
        schema_slice=schema_slice,
        as_of=config.time.as_of,
        time_window=resolve_time_window(question, config.time.as_of_date),
        attempts=tuple(attempts),
        primary=primary,
        repairs_used=repairs_used,
        agreement=agreement_fraction(primary.canonical, [primary.canonical, *canonicals]),
        k=samples,
        model_id=model_id,
        schema_sha256=card.schema_sha256,
    )


__all__ = [
    "Attempt",
    "AttemptOutcome",
    "DatabaseUnavailableError",
    "GenerationOutcome",
    "GenerationStatus",
    "generate_candidates",
]
"""`Attempt` and `DatabaseUnavailableError` are re-exported so callers import
one module rather than three. `sample.py` owns the first; stage 04 owns the
second, and it appears here because it is the one failure this stage deliberately
does not catch."""

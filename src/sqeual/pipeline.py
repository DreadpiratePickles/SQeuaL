"""One question, end to end: slice, generate, verify, render, record.

This is the only module that knows the stages exist in an order. Each of them is
independently testable and none of them imports another's runner; this file is
where they meet, and it is deliberately thin — the interesting decisions all live
in the stage that owns them.

The one decision that lives here is what happens when a stage refuses. Stage 06
is **skipped entirely** when stage 05 produced no executed statement: verifying a
run that never ran would attach a confidence score to a refusal, and a refusal
with a number beside it is exactly the thing this tool exists not to hand back.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from .answer.run import Answer, AnswerStatus, build_answer
from .generate.run import GenerationOutcome, GenerationStatus, generate_candidates
from .guard import GuardPolicy
from .providers import FAKE_MODEL_ID, MeteredProvider
from .providers.pacing import Pacer
from .schema.card import SchemaCard
from .schema.slice import slice_for_question
from .trace import build_trace, run_directory, write_run
from .verify.run import Verification, verify_answer

DEFAULT_RUNS_ROOT = Path("runs")


@dataclass(frozen=True)
class AskOutcome:
    """Everything one `ask` produced, plus where it was written."""

    generation: GenerationOutcome
    verification: Verification | None
    answer: Answer
    trace: dict
    run_dir: Path | None

    @property
    def status(self) -> AnswerStatus:
        return self.answer.status

    @property
    def confidence_level(self) -> str | None:
        """`HIGH`, `MEDIUM`, `LOW`, `ABSTAIN`, or `None` when nothing ran."""
        return None if self.answer.confidence is None else self.answer.confidence.level.value


def run_ask(
    *,
    question: str,
    card: SchemaCard,
    config,
    provider: MeteredProvider,
    pacer: Pacer,
    judge_provider: MeteredProvider | None = None,
    k: int | None = None,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    now: dt.datetime | None = None,
    write: bool = True,
) -> AskOutcome:
    """Answer one question, or refuse it, and write the run either way.

    Args:
        question: the question as asked. Read as text, never as SQL.
        card: the live schema card.
        config: the validated `sqeual.toml`.
        provider: the metered seam for generation, back-translation and phrasing.
        pacer: spaces every call under a per-minute quota.
        judge_provider: a different family for the judge, when a second key
            exists. Defaults to `provider`, and the trace records which.
        k: override `[generate] k` for this run.
        runs_root: where `runs/<ts>/` goes.
        now: the run timestamp, injectable so a test does not race a clock.
        write: `False` skips the files, for tests that only want the outcome.

    Raises:
        SchemaError: the question or the slicing configuration cannot be used.
        DatabaseUnavailableError: the database is missing or unreadable.
        ProviderError: the provider could not be built or reached.
    """
    schema_slice = slice_for_question(
        question,
        card,
        max_tables=config.schema.max_tables,
        synonyms=config.schema.synonyms,
    )
    generation = generate_candidates(
        question=question,
        card=card,
        schema_slice=schema_slice,
        policy=GuardPolicy.from_settings(config.guard),
        provider=provider,
        config=config,
        pacer=pacer,
        k=k,
    )

    verification = None
    if generation.status is GenerationStatus.ANSWERED:
        verification = verify_answer(
            generation=generation,
            card=card,
            config=config,
            provider=provider,
            pacer=pacer,
            judge_provider=judge_provider,
        )

    answer = build_answer(
        generation=generation,
        verification=verification,
        config=config,
        provider=provider,
        pacer=pacer,
    )
    trace = build_trace(
        generation=generation,
        verification=verification,
        answer=answer,
        config=config,
        pacer=pacer,
        dry_run=getattr(provider, "model_id", "") == FAKE_MODEL_ID,
    )

    run_dir = None
    if write:
        run_dir = run_directory(runs_root, now=now)
        write_run(run_dir, trace=trace, answer_markdown=answer.markdown)

    return AskOutcome(
        generation=generation,
        verification=verification,
        answer=answer,
        trace=trace,
        run_dir=run_dir,
    )


__all__ = ["DEFAULT_RUNS_ROOT", "AskOutcome", "run_ask"]

"""Stage 08: run every golden question through the whole tool and score it.

The harness is deliberately thin. It executes the answer keys, drives stages 05
to 07 once per question, hands each outcome to `score.py`, and hands the whole
list to `metrics.py`. Every judgement lives in one of those two modules and every
number in the summary is arithmetic over things that were counted.

Four properties are worth naming here rather than only in the stage contract.

**One pacer for the whole run.** A per-minute quota does not reset between
questions, so the gap is enforced across the whole eval rather than inside each
question — which is the difference between forty paced bursts and one paced run.

**Nothing is written under `runs/<ts>/`.** `run_ask` is called with `write=False`
and this stage writes its own record instead. Forty questions writing forty run
directories beside their own results file would be the same evidence twice, and
the second copy is the one that goes stale.

**A provider failure ends one question, not the run.** It is recorded as
`errored`, excluded from every rate, and counted on the face of the summary. A
free tier that starts returning 503 halfway through should cost the questions it
hit and nothing else, and the summary should say how many that was.

**Results stream to disk as they finish.** A live run over forty questions at a
6.5-second pace takes a quarter of an hour, and a run interrupted at question
thirty should leave thirty results behind rather than nothing.
"""

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..execute import ExecuteLimits
from ..guard import GuardPolicy
from ..pipeline import run_ask
from ..providers import MeteredProvider, ProviderError
from ..providers.pacing import Pacer
from ..schema.card import SchemaCard
from .goldens import Expectation, GoldenQuestion, goldens_sha256
from .metrics import EvalMetrics, compute_metrics
from .present import banner
from .reference import ReferenceRun, run_reference
from .render import render_calibration, render_eval
from .score import (
    QuestionResult,
    broken_reference_result,
    errored_result,
    score_question,
)
from .usage import CountingProvider, total_usage

DEFAULT_GOLDENS = Path("goldens/questions.yaml")
DEFAULT_EVAL_ROOT = Path("runs/eval")
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

RESULTS_NAME = "results.jsonl"
SUMMARY_JSON_NAME = "eval.json"
SUMMARY_MD_NAME = "eval.md"
CALIBRATION_MD_NAME = "calibration.md"

@dataclass(frozen=True)
class EvalRun:
    """One whole evaluation: what happened, what it added up to, and where."""

    results: tuple[QuestionResult, ...]
    metrics: EvalMetrics
    provenance: dict
    out_dir: Path | None


def prepare_references(
    questions: Sequence[GoldenQuestion],
    *,
    card: SchemaCard,
    config,
) -> dict[str, ReferenceRun]:
    """Execute every answer key once, before a single model call is made.

    Up front on purpose: a broken reference should cost nothing, and discovering
    one halfway through a paid run means the money for the questions before it
    was spent measuring against an answer key nobody had checked.
    """
    policy = GuardPolicy.from_settings(config.guard)
    limits = ExecuteLimits.from_settings(config.execute)
    return {
        question.id: run_reference(
            question,
            card=card,
            policy=policy,
            limits=limits,
            db_path=config.db.path,
            float_places=config.verify.float_places,
        )
        for question in questions
        if question.expected is Expectation.ANSWER
    }


def eval_directory(root: Path, *, now: dt.datetime | None = None) -> Path:
    """A fresh `runs/eval/<ts>/`, disambiguated if two runs land in one second."""
    stamp = (now or dt.datetime.now(dt.UTC)).strftime(TIMESTAMP_FORMAT)
    candidate = Path(root) / stamp
    suffix = 2
    while candidate.exists():
        candidate = Path(root) / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def build_provenance(
    *,
    questions: Sequence[GoldenQuestion],
    goldens_path: Path,
    card: SchemaCard,
    config,
    provider: MeteredProvider,
    judge_model_id: str,
    k: int | None,
    pacer: Pacer,
    dry_run: bool,
    now: dt.datetime | None = None,
) -> dict:
    """Everything a reader needs to know which run produced which numbers.

    The goldens hash is the one that matters most: a rate is a statement about a
    set of questions, and two runs against different question files are not
    comparable however similar the numbers look.
    """
    return {
        "dry_run": dry_run,
        "started_utc": (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds"),
        "questions": len(questions),
        "goldens": str(goldens_path),
        "goldens_sha256": goldens_sha256(goldens_path),
        "schema_sha256": card.schema_sha256,
        "config": str(config.path),
        "model_id": provider.model_id,
        "judge_model_id": judge_model_id,
        "same_family": provider.model_id == judge_model_id,
        "k": k if k is not None else config.generate.k,
        "min_interval_ms": pacer.min_interval_ms,
        "as_of": config.time.as_of,
    }


def _append(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def run_eval(
    *,
    questions: Sequence[GoldenQuestion],
    references: Mapping[str, ReferenceRun],
    card: SchemaCard,
    config,
    provider: MeteredProvider,
    pacer: Pacer,
    provenance: dict,
    judge_provider: MeteredProvider | None = None,
    k: int | None = None,
    out_dir: Path | None = None,
    echo=None,
) -> EvalRun:
    """Run every question and score it.

    Args:
        questions: the golden set, already limited by `--limit`.
        references: the executed answer keys, from `prepare_references`.
        card: the live schema card.
        config: the validated `sqeual.toml`.
        provider: the metered seam. The offline fake under `--dry-run`.
        pacer: one pacer for the whole run, so the quota is respected across
            questions and not merely inside them.
        provenance: from `build_provenance`, written as the first record.
        judge_provider: a different family for the judge, when a second key
            exists.
        k: override `[generate] k` for this run.
        out_dir: where the four output files go. `None` runs without writing.
        echo: called with one progress line per question, for a CLI.

    Raises:
        DatabaseUnavailableError: the database went missing mid-run. Not caught:
            a broken deployment would otherwise be recorded as forty errored
            questions, which reads as a bad model rather than a bad machine.
    """
    results_path = None
    if out_dir is not None:
        results_path = Path(out_dir) / RESULTS_NAME
        _append(results_path, {"record": "header", **provenance})

    # Wrapped so a question that errors partway through still reports what it
    # spent. Both are wrapped when the judge is a different family, and the
    # per-question usage is the sum of the two deltas.
    counted = CountingProvider(provider, config.cost)
    counted_judge = (
        None if judge_provider is None else CountingProvider(judge_provider, config.cost)
    )
    counters = [counted] + ([counted_judge] if counted_judge is not None else [])

    results: list[QuestionResult] = []
    for question in questions:
        before = total_usage(counters)
        result = _run_one(
            question,
            references.get(question.id),
            card=card,
            config=config,
            provider=counted,
            pacer=pacer,
            judge_provider=counted_judge,
            k=k,
            # Bound as a default rather than captured, so the closure reads
            # this question's baseline and not whatever the loop reached later.
            spent=lambda baseline=before: total_usage(counters).since(baseline),
        )
        results.append(result)
        if results_path is not None:
            _append(results_path, {"record": "result", **result.as_json()})
        if echo is not None:
            echo(
                f"  {len(results):>3}/{len(questions)}  {question.id:<38} "
                f"{result.verdict.value}"
            )

    return EvalRun(
        results=tuple(results),
        metrics=compute_metrics(results, priced=config.cost.priced),
        provenance=provenance,
        out_dir=Path(out_dir) if out_dir is not None else None,
    )


def _run_one(
    question: GoldenQuestion,
    reference: ReferenceRun | None,
    *,
    card: SchemaCard,
    config,
    provider: MeteredProvider,
    pacer: Pacer,
    judge_provider: MeteredProvider | None,
    k: int | None,
    spent,
) -> QuestionResult:
    if question.expected is Expectation.ANSWER and (reference is None or not reference.ok):
        # Nothing is asked of a model. A case whose answer key does not work
        # cannot be scored either way, and spending a call to find that out
        # would be spending it on a question nobody can grade.
        return broken_reference_result(
            question,
            reference
            or ReferenceRun(
                question_id=question.id,
                sql=question.reference_sql or "",
                ok=False,
                normalised_sql=None,
                result=None,
                canonical=None,
                ordered_rows=None,
                reason="no reference was executed for this case",
            ),
        )

    try:
        outcome = run_ask(
            question=question.question,
            card=card,
            config=config,
            provider=provider,
            pacer=pacer,
            judge_provider=judge_provider,
            k=k,
            write=False,
        )
    except ProviderError as exc:
        # The calls this question already made are recorded rather than dropped.
        # Reporting zero would make a run's cost a total of the questions that
        # happened to finish, which is a smaller number than the bill.
        return errored_result(question, reference, str(exc), spent=spent())

    return score_question(
        question, outcome, reference, float_places=config.verify.float_places
    )


def summary_json(run: EvalRun) -> dict:
    """The whole run as one JSON-serialisable object, banner first.

    `banner` is the first key so that the marker is the first thing in the file
    after the opening brace. A JSON document cannot literally carry a banner on
    line 1, and putting it anywhere but the top would let a reader scroll past
    it — which is the entire failure the banner exists to prevent.
    """
    return {
        "banner": banner(run.metrics, run.provenance),
        "provenance": run.provenance,
        "metrics": run.metrics.as_json(),
        "results": [result.as_json() for result in run.results],
    }


def write_eval(run: EvalRun) -> Path:
    """Write `eval.json`, `eval.md` and `calibration.md` into the run directory.

    `results.jsonl` is already on disk: it is appended to as each question
    finishes, so an interrupted run leaves the questions it did answer behind.

    Raises:
        ValueError: the run has no output directory. Writing to an invented one
            would put an evaluation somewhere nobody asked for it.
    """
    if run.out_dir is None:
        raise ValueError("this evaluation was run with no output directory")
    directory = Path(run.out_dir)
    (directory / SUMMARY_JSON_NAME).write_text(
        json.dumps(summary_json(run), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (directory / SUMMARY_MD_NAME).write_text(
        render_eval(
            results=run.results, metrics=run.metrics, provenance=run.provenance
        ),
        encoding="utf-8",
    )
    (directory / CALIBRATION_MD_NAME).write_text(
        render_calibration(
            results=run.results, metrics=run.metrics, provenance=run.provenance
        ),
        encoding="utf-8",
    )
    return directory


__all__ = [
    "CALIBRATION_MD_NAME",
    "DEFAULT_EVAL_ROOT",
    "DEFAULT_GOLDENS",
    "RESULTS_NAME",
    "SUMMARY_JSON_NAME",
    "SUMMARY_MD_NAME",
    "EvalRun",
    "build_provenance",
    "eval_directory",
    "prepare_references",
    "run_eval",
    "summary_json",
    "write_eval",
]

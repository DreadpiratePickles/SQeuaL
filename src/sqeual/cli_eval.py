"""`sqeual eval` and `sqeual ask-target` — the two commands stage 08 adds.

**`eval`** runs the golden set and writes four files. Its four exit codes are a
third vocabulary, and the reason is the same one that made `ask` renumber the
first two: a caller of `eval` wants a different fact first.

    0  the evaluation ran and found nothing dangerous
    1  the evaluation ran and produced a **finding** — a question with no answer
       was answered with figures, or an instruction that would have written to
       the database was not refused. This is the CI gate.
    2  the evaluation never started — a bad golden file, a bad configuration, a
       missing database, or no key.
    3  **inconclusive**: it ran and nothing came back that can be scored. Every
       reference broken, or every question errored against a provider that was
       down. "The evidence cannot say" must not be renderable as a pass, and it
       must not be renderable as a failing gate either.

An accuracy drop is deliberately *not* a non-zero exit here. Accuracy is a
property of a model that moves under you, and a gate that goes red on it is a
gate people switch off. The two conditions that fail this command are both
things the tool itself did wrong: it showed a figure where none existed, or it
did not refuse something it must always refuse.

**`ask-target`** reads one question from stdin and prints the rendered answer to
stdout, and nothing else. That is project 1's `Target` contract — text in, text
out, non-zero on failure — so `regress` can drive SQeuaL as a
`[target] kind = "command"` without either repository importing the other's
vocabulary. It exits **0 for an abstention**, which `ask` does not: a refusal is
an answer a judge is meant to grade, and exiting non-zero would make project 1
record every correct refusal as a failed sample.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from .cli_ask import build_provider, judge_provider_for
from .cli_schema import load_card
from .eval.fake import dry_run_provider
from .eval.goldens import GoldenQuestionError, load_questions
from .eval.present import banner, interval, percent
from .eval.run import (
    DEFAULT_EVAL_ROOT,
    DEFAULT_GOLDENS,
    RESULTS_NAME,
    build_provenance,
    eval_directory,
    prepare_references,
    run_eval,
    write_eval,
)
from .pipeline import run_ask
from .providers import ProviderError
from .providers.pacing import Pacer

Echo = Callable[..., None]

EXIT_CLEAN = 0
EXIT_FINDING = 1
EXIT_CANNOT_RUN = 2
EXIT_INCONCLUSIVE = 3


def add_eval_command(subparsers: argparse._SubParsersAction, common) -> None:
    parser = subparsers.add_parser(
        "eval", parents=[common], help="run the golden questions and score the tool"
    )
    parser.add_argument(
        "--goldens",
        default=str(DEFAULT_GOLDENS),
        help="the golden questions. Defaults to ./goldens/questions.yaml.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use the scripted offline provider. No key, no network, no money.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="run only the first N questions. The file is ordered so a prefix is "
        "a stratified sample rather than an arbitrary one.",
    )
    parser.add_argument(
        "--min-interval-ms",
        type=int,
        default=0,
        help="minimum gap between model calls, across the whole run.",
    )
    parser.add_argument(
        "--k", type=int, default=None, help="override [generate] k for this run."
    )
    parser.add_argument(
        "--out",
        default=None,
        help="where the four output files go. Defaults to ./runs/eval/<timestamp>.",
    )


def add_ask_target_command(subparsers: argparse._SubParsersAction, common) -> None:
    parser = subparsers.add_parser(
        "ask-target",
        parents=[common],
        help="one question on stdin, the rendered answer on stdout. For regress.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use the scripted offline provider. No key, no network, no money.",
    )
    parser.add_argument(
        "--min-interval-ms",
        type=int,
        default=0,
        help="minimum gap between model calls, for a per-minute quota.",
    )
    parser.add_argument(
        "--k", type=int, default=None, help="override [generate] k for this run."
    )


class OutputDirectoryInUse(Exception):
    """`--out` cannot be written to: it is a file, or it already holds a run."""


def _out_directory(args: argparse.Namespace) -> Path:
    """Where the four files go, refusing a directory that already holds a run.

    `results.jsonl` is *appended to* as each question finishes — that is what
    makes an interrupted run leave its answers behind — while `eval.json` and the
    two documents are written whole at the end. Pointing `--out` at a directory
    that already holds a run therefore produces a `results.jsonl` with two runs
    in it beside a summary describing one, which is evidence that contradicts
    itself. `db build` refuses to overwrite for the same reason.

    Raises:
        OutputDirectoryInUse: the path is not a directory, or already holds a
            `results.jsonl`.
    """
    if args.out is None:
        return eval_directory(Path(DEFAULT_EVAL_ROOT))
    directory = Path(args.out)
    if directory.exists() and not directory.is_dir():
        raise OutputDirectoryInUse(
            f"--out {directory} is not a directory. An evaluation writes four files and "
            "needs somewhere to put them."
        )
    if (directory / RESULTS_NAME).exists():
        raise OutputDirectoryInUse(
            f"{directory} already holds a {RESULTS_NAME}. Appending to it would put two "
            "runs in one results file beside a summary describing one of them. Pass a "
            "different --out, or delete the directory."
        )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _summarise(run, echo: Echo) -> None:
    metrics = run.metrics
    echo("")
    echo(banner(metrics, run.provenance))
    echo("")
    echo(
        f"  false answers      {metrics.false_answers}"
        f"  ({metrics.false_answer_rate.passes}/{metrics.false_answer_rate.n} "
        f"bait and ambiguous questions)"
    )
    echo(
        f"  execution accuracy {percent(metrics.execution_accuracy.value)}"
        f"  ({metrics.execution_accuracy.passes}/{metrics.execution_accuracy.n} answered) "
        f"{interval(metrics.execution_accuracy)}"
    )
    echo(
        f"  bait caught        {percent(metrics.hallucination_catches.value)}"
        f"  ({metrics.hallucination_catches.passes}/{metrics.hallucination_catches.n})"
    )
    echo(
        f"  unsafe refused     {percent(metrics.refusals_correct.value)}"
        f"  ({metrics.refusals_correct.passes}/{metrics.refusals_correct.n})"
    )
    echo(
        f"  abstained          {percent(metrics.abstention_rate.value)}"
        f"  ({metrics.abstention_rate.passes}/{metrics.abstention_rate.n} scored)"
    )
    echo(
        f"  broken references  {metrics.broken}    errored {metrics.errored}"
        f"    judge errors {metrics.judge_errors}"
    )
    echo(f"  calibration        {[bucket.level for bucket in metrics.calibration]}")
    echo(f"  out: {run.out_dir}")


def command_eval(args: argparse.Namespace, config, echo: Echo) -> int:
    """Run the golden set and return the exit code that describes the outcome."""
    if args.k is not None and args.k < 1:
        echo(f"cannot run: --k must be at least 1, got {args.k}")
        return EXIT_CANNOT_RUN
    if args.limit is not None and args.limit < 1:
        echo(f"cannot run: --limit must be at least 1, got {args.limit}")
        return EXIT_CANNOT_RUN

    goldens_path = Path(args.goldens)
    try:
        questions = load_questions(goldens_path)
    except GoldenQuestionError as exc:
        # Exit 2 before anything runs. A case set that quietly shrinks is a green
        # build that means nothing.
        echo(f"cannot run: {exc}")
        return EXIT_CANNOT_RUN
    if args.limit is not None:
        questions = questions[: args.limit]

    card = load_card(config)
    references = prepare_references(questions, card=card, config=config)

    try:
        provider = (
            dry_run_provider(questions, references)
            if args.dry_run
            else build_provider(args, config)
        )
        judge_provider = judge_provider_for(args, config, provider)
        pacer = Pacer(args.min_interval_ms)
        provenance = build_provenance(
            questions=questions,
            goldens_path=goldens_path,
            card=card,
            config=config,
            provider=provider,
            judge_model_id=(
                provider.model_id
                if judge_provider is None
                else judge_provider.model_id
            ),
            k=args.k,
            pacer=pacer,
            dry_run=args.dry_run,
        )
        out_dir = _out_directory(args)
        echo(f"{len(questions)} question(s) -> {out_dir}")
        run = run_eval(
            questions=questions,
            references=references,
            card=card,
            config=config,
            provider=provider,
            pacer=pacer,
            provenance=provenance,
            judge_provider=judge_provider,
            k=args.k,
            out_dir=out_dir,
            echo=echo,
        )
    except (ProviderError, OutputDirectoryInUse) as exc:
        echo(f"cannot run: {exc}")
        return EXIT_CANNOT_RUN

    write_eval(run)
    _summarise(run, echo)

    metrics = run.metrics
    if metrics.scored == 0:
        echo("")
        echo(
            "INCONCLUSIVE — nothing was scored. Every case had a broken reference "
            "or errored against the provider, so this run says nothing about the "
            "tool in either direction."
        )
        return EXIT_INCONCLUSIVE

    unrefused = metrics.refusals_correct.n - metrics.refusals_correct.passes
    if metrics.false_answers or unrefused:
        echo("")
        echo(
            f"FINDING — {metrics.false_answers} false answer(s) and {unrefused} "
            "unsafe instruction(s) not refused. Both are figures or actions the "
            "tool must never produce; an accuracy drop is not a finding and does "
            "not fail this command."
        )
        return EXIT_FINDING
    return EXIT_CLEAN


def command_ask_target(args: argparse.Namespace, config, echo: Echo) -> int:
    """One question on stdin, the rendered answer on stdout, and nothing else.

    Everything diagnostic goes to stderr. Project 1's `CommandTarget` reads
    stdout and treats an empty one as a failure, so a stray progress line here
    would become part of the text a judge is grading.
    """
    question = sys.stdin.read().strip()
    if not question:
        print("ask-target: stdin held no question", file=sys.stderr)
        return EXIT_CANNOT_RUN

    try:
        provider = build_provider(args, config)
        outcome = run_ask(
            question=question,
            card=load_card(config),
            config=config,
            provider=provider,
            pacer=Pacer(args.min_interval_ms),
            judge_provider=judge_provider_for(args, config, provider),
            k=args.k,
            write=False,
        )
    except ProviderError as exc:
        print(f"ask-target: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    # Every outcome that produced a document exits 0, including an abstention.
    # A refusal is an answer a judge is meant to grade — "Abstains rather than
    # answering" is a criterion somebody writes — and a non-zero exit would make
    # project 1 record every correct refusal as a failed sample instead.
    echo(outcome.answer.markdown.rstrip())
    return EXIT_CLEAN


__all__ = [
    "EXIT_CANNOT_RUN",
    "EXIT_CLEAN",
    "EXIT_FINDING",
    "EXIT_INCONCLUSIVE",
    "OutputDirectoryInUse",
    "add_ask_target_command",
    "add_eval_command",
    "command_ask_target",
    "command_eval",
]

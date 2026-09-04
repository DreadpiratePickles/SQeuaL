"""`sqeual ask` — the command that spends money, and the one that can refuse.

Four exit codes, and they are **not** the same four the Phase A commands use.
That is deliberate and it is the one place in this repository where the
vocabulary shifts, so it is written down here rather than discovered:

    0  answered — a document with figures in it
    1  abstained, or a clarification is needed. The tool declined to show a
       number. This is a successful outcome of a working tool.
    2  the guard refused the statement after the repair budget was spent, or the
       model never produced a reply this tool could read. **Nothing ran.**
    3  could not run — bad configuration, no API key, a missing database, or a
       statement that passed every check and the database still could not answer.

Phase A's `run` uses 2 for "never started" and 3 for "execution failed". `ask`
swaps them because the interesting distinction here is different: what a caller
wants to know first is whether *the model's statement* was the problem (2) or
whether *the deployment* was (3). `docs/design.md` §37 argues it out.

`--dry-run` swaps the provider for the role-aware fake and runs the whole
pipeline offline. It proves the wiring works; it proves nothing at all about
whether a model can write SQL, and the trace says `dry_run: true` so nobody can
later mistake one for the other.
"""

import argparse
import json
from collections.abc import Callable

from .answer.run import AnswerStatus
from .cli_schema import load_card
from .config import model_id_for_ref
from .pipeline import DEFAULT_RUNS_ROOT, run_ask
from .providers import ProviderError, RoleAwareFakeProvider
from .providers.pacing import Pacer

Echo = Callable[..., None]

EXIT_ANSWERED = 0
EXIT_ABSTAINED = 1
EXIT_GUARD_BLOCKED = 2
EXIT_CANNOT_RUN = 3

EXIT_FOR_STATUS: dict[AnswerStatus, int] = {
    AnswerStatus.ANSWERED: EXIT_ANSWERED,
    AnswerStatus.ABSTAINED: EXIT_ABSTAINED,
    AnswerStatus.CLARIFICATION: EXIT_ABSTAINED,
    AnswerStatus.GUARD_BLOCKED: EXIT_GUARD_BLOCKED,
    AnswerStatus.UNUSABLE_REPLY: EXIT_GUARD_BLOCKED,
    AnswerStatus.EXECUTION_FAILED: EXIT_CANNOT_RUN,
}


def add_ask_command(subparsers: argparse._SubParsersAction, common) -> None:
    parser = subparsers.add_parser(
        "ask", parents=[common], help="answer a question in English, or refuse to"
    )
    parser.add_argument("question", help="the question, in plain English")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use the scripted offline provider. No key, no network, no money.",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the whole trace instead of the answer"
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
    parser.add_argument(
        "--no-phrasing",
        action="store_true",
        help="never ask a model for the sentence, whatever [answer] llm_phrasing says.",
    )
    parser.add_argument(
        "--runs",
        default=str(DEFAULT_RUNS_ROOT),
        help="where to write runs/<ts>/. Defaults to ./runs.",
    )


def build_provider(args: argparse.Namespace, config):
    """The scripted fake, or the metered Gemini adapter.

    The vendor module is imported lazily so that `--dry-run` needs neither the
    SDK nor a key — which is what makes the offline path genuinely offline rather
    than merely unused.
    """
    if args.dry_run:
        return RoleAwareFakeProvider()
    from .providers.gemini import metered_provider_from_env

    return metered_provider_from_env(model_id_for_ref(config.models.sql_model_ref))


def judge_provider_for(args: argparse.Namespace, config, provider):
    """A second provider only when the judge is configured to a different model.

    When the two ids match there is one provider and `same_family` records that
    the judge is grading its own family's output.
    """
    judge_id = model_id_for_ref(config.models.judge_model_ref)
    if args.dry_run or judge_id == provider.model_id:
        return None
    from .providers.gemini import metered_provider_from_env

    return metered_provider_from_env(judge_id)


def command_ask(args: argparse.Namespace, config, echo: Echo) -> int:
    """Answer one question and return the exit code that describes the outcome."""
    # `--k` overrides a configured limit, so it is validated the same way the
    # configured one is. Without this a `--k 0` runs the primary anyway (it is
    # outside the sampling loop) and writes a trace saying `k: 0` beside one
    # attempt and a real agreement fraction — a record that contradicts itself.
    if args.k is not None and args.k < 1:
        echo(f"cannot run: --k must be at least 1, got {args.k}")
        return EXIT_CANNOT_RUN

    if args.no_phrasing:
        config = _without_phrasing(config)

    card = load_card(config)
    try:
        provider = build_provider(args, config)
        outcome = run_ask(
            question=args.question,
            card=card,
            config=config,
            provider=provider,
            pacer=Pacer(args.min_interval_ms),
            judge_provider=judge_provider_for(args, config, provider),
            k=args.k,
            runs_root=args.runs,
        )
    except ProviderError as exc:
        # The run never started: no key, bad credentials, or the provider could
        # not be reached at all. Nothing about the question would change it.
        echo(f"cannot run: {exc}")
        return EXIT_CANNOT_RUN

    if args.json:
        echo(json.dumps(outcome.trace, indent=2, ensure_ascii=False))
    else:
        echo(outcome.answer.markdown.rstrip())
        echo("")
        echo(f"run: {outcome.run_dir}")
    return EXIT_FOR_STATUS[outcome.answer.status]


def _without_phrasing(config):
    """A copy of the configuration with `[answer] llm_phrasing` forced off."""
    from dataclasses import replace

    return replace(config, answer=config.answer.without_phrasing())

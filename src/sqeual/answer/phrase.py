"""Optionally let a model write the sentence — and check every number in it.

`[answer] llm_phrasing` is **off by default**, and when it is on the guarantee is
unchanged, because it is not enforced by the prompt. The model is shown the result
exactly as the reader will see it and asked for one sentence; then every numeric
token in that sentence is extracted and required to trace back to a cell, to the
row count, or to a date this program computed. A token that traces to none of
those is a figure the model invented, and the whole sentence is discarded.

**Discarded whole, never patched.** A sentence with one bad figure removed is a
sentence somebody reads as complete. `phrasing_rejected` and the offending tokens
go into the trace, so a run where a model tried to invent a number is a thing
somebody can find later rather than a thing that quietly did not happen.

The rejected alternative is stage 07's original contract: show the model only the
column names and the row count and ask for a *template* with named placeholders,
then fill it in code. That is also mechanically safe and it produces stiffer
sentences, because a template cannot say "Berlin led" without a placeholder for
"Berlin". Grounding buys the better sentence at the cost of one more check, and
the check is the part that is tested.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from ..generate.parse import strip_one_fence
from ..providers import Completion, MeteredProvider, ProviderError
from ..providers.pacing import Pacer
from .grounding import ungrounded_numbers

PHRASE_PROMPT_PATH = Path(__file__).parent / "prompts" / "phrase_v1.md"
SENTENCE_KEYS = frozenset({"sentence"})


class PhraseParseError(Exception):
    """The phrasing reply is not a valid sentence object and must not be used."""


@dataclass(frozen=True)
class Phrasing:
    """What the model wrote, and whether it survived the grounding check."""

    sentence: str | None
    accepted: bool
    rejected_tokens: tuple[str, ...]
    error: str | None
    completion: Completion | None

    @property
    def note(self) -> str:
        if self.accepted:
            return "every figure in the sentence traces to a result cell"
        if self.error:
            return f"phrasing unavailable: {self.error}"
        return (
            "phrasing_rejected: the sentence contained "
            f"{', '.join(self.rejected_tokens)}, which is in no result cell"
        )


def load_phrase_prompt(path: Path = PHRASE_PROMPT_PATH) -> str:
    """Read the phrasing system prompt.

    Raises:
        FileNotFoundError: the prompt is missing. A broken install.
    """
    return Path(path).read_text(encoding="utf-8")


def parse_sentence(raw: object) -> str:
    """Parse the phrasing reply into one sentence.

    Raises:
        PhraseParseError: on anything but a JSON object with exactly `sentence`
            holding a non-empty string.
    """
    if not isinstance(raw, str):
        raise PhraseParseError(f"model reply must be a string, got {type(raw).__name__}")
    candidate = strip_one_fence(raw.strip()).strip()
    if not candidate:
        raise PhraseParseError("model returned an empty reply")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PhraseParseError(
            f"model reply is not JSON: {exc.msg} (at position {exc.pos})"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != SENTENCE_KEYS:
        raise PhraseParseError("model reply must be a JSON object with exactly the key 'sentence'")
    sentence = payload["sentence"]
    if not isinstance(sentence, str) or not sentence.strip():
        raise PhraseParseError("'sentence' must be a non-empty string")
    return sentence.strip()


def build_phrase_user_message(*, question: str, rendered_result: str, row_count: int) -> str:
    """The user half of a phrasing request: the question and the rendered result."""
    return (
        f"<question>\n{question}\n</question>\n\n"
        f"<result>\n{rendered_result}\n</result>\n\n"
        f"<row_count>\n{row_count}\n</row_count>"
    )


def write_phrasing(
    *,
    question: str,
    rendered_result: str,
    cells,
    row_count: int,
    provider: MeteredProvider,
    pacer: Pacer,
    grounded_dates: tuple[str, ...] = (),
) -> Phrasing:
    """Ask for a sentence, then reject it whole if it contains an invented figure."""
    pacer.wait()
    try:
        completion = provider.complete(
            system=load_phrase_prompt(),
            user=build_phrase_user_message(
                question=question, rendered_result=rendered_result, row_count=row_count
            ),
            temperature=0.0,
        )
        sentence = parse_sentence(completion.text)
    except (PhraseParseError, ProviderError) as exc:
        return Phrasing(
            sentence=None,
            accepted=False,
            rejected_tokens=(),
            error=str(exc),
            completion=None,
        )

    ungrounded = ungrounded_numbers(
        sentence, cells=cells, row_count=row_count, extra=grounded_dates
    )
    return Phrasing(
        sentence=sentence if not ungrounded else None,
        accepted=not ungrounded,
        rejected_tokens=ungrounded,
        error=None,
        completion=completion,
    )

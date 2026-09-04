"""Parse a model's reply into a proposal, or refuse it.

The rejected alternative is worth naming because it is the obvious one: pull a
fenced code block out of a paragraph with a regular expression. That works until
the model writes two blocks, or explains itself in SQL comments, and then the
extractor has quietly chosen which of two statements to run — a choice nobody
made and nobody can see. So the reply is JSON, it is validated key by key at the
boundary like any other external payload, and a reply that fails validation is a
discarded candidate with a typed error rather than a repaired string.

Exactly one deviation is tolerated: a single surrounding markdown fence. It is
the one thing models produce constantly and it changes nothing about the payload.
Everything else — prose around the object, two blocks, a missing key, an extra
key, a `"false"` where a `false` belongs — is a parse failure.

The strictness is deliberately project 1's, because a second opinion about what
counts as a valid model reply is a second thing to get wrong.
"""

import json
from dataclasses import dataclass

FENCE_CHARACTER = "`"
MIN_FENCE_LENGTH = 3

PROPOSAL_KEYS = frozenset(
    {"sql", "tables", "assumptions", "clarification_needed", "clarifying_question"}
)


class GenerationParseError(Exception):
    """The model's reply is not a valid proposal and must not be used."""


@dataclass(frozen=True)
class Proposal:
    """One candidate statement, as the model described it.

    `tables` and `assumptions` are **recorded, not trusted**. A candidate that
    names tables its SQL never touches is a useful signal for stage 06, and the
    SQL itself is checked against the schema card regardless of what was claimed.
    """

    sql: str
    tables: tuple[str, ...]
    assumptions: tuple[str, ...]
    clarification_needed: bool
    clarifying_question: str | None


def strip_one_fence(text: str) -> str:
    """Remove a single surrounding markdown fence, if the reply is wrapped in one.

    A deliberate reimplementation of project 1's tolerance rather than an import
    of its private helper: the behaviour is pinned by this package's own tests,
    so the two cannot drift without one of them going red.
    """
    if not text.startswith(FENCE_CHARACTER * MIN_FENCE_LENGTH):
        return text

    opening, _, remainder = text.partition("\n")
    fence = opening[: len(opening) - len(opening.lstrip(FENCE_CHARACTER))]
    language = opening[len(fence) :].strip()
    if language and language != "json":
        return text
    if not remainder.rstrip().endswith(fence):
        return text
    closed = remainder.rstrip()
    return closed[: len(closed) - len(fence)].strip()


def _string_list(payload: dict, key: str) -> tuple[str, ...]:
    value = payload[key]
    if not isinstance(value, list):
        raise GenerationParseError(
            f"'{key}' must be a JSON array of strings, got {type(value).__name__}"
        )
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise GenerationParseError(
                f"'{key}'[{index}] must be a non-empty string, got {item!r}"
            )
    return tuple(item.strip() for item in value)


def _clarification_needed(payload: dict) -> bool:
    value = payload["clarification_needed"]
    # `isinstance(1, bool)` is False but `isinstance(True, int)` is True, so the
    # check is written this way round on purpose: a JSON `1` must not become a
    # request for clarification and a `true` must not become the integer 1.
    if not isinstance(value, bool):
        raise GenerationParseError(
            f"'clarification_needed' must be a JSON boolean, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value


def _clarifying_question(payload: dict) -> str | None:
    value = payload["clarifying_question"]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GenerationParseError(
            "'clarifying_question' must be a non-empty string or null, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value.strip()


def _sql(payload: dict, *, clarification_needed: bool) -> str:
    value = payload["sql"]
    if not isinstance(value, str):
        raise GenerationParseError(f"'sql' must be a string, got {type(value).__name__}")
    stripped = value.strip()
    # Blank SQL is legitimate only alongside a request for clarification. A blank
    # statement with `clarification_needed: false` is a model that answered
    # nothing while claiming it had answered, which must not reach the guard as
    # an empty query.
    if not stripped and not clarification_needed:
        raise GenerationParseError(
            "'sql' must be a non-empty statement unless 'clarification_needed' is true"
        )
    return stripped


def parse_proposal(raw: object) -> Proposal:
    """Parse one model reply into a `Proposal`.

    Raises:
        GenerationParseError: on a non-string reply, non-JSON text, a non-object,
            a missing or unexpected key, or any field of the wrong type. Never
            returns a partially-filled proposal: half a parsed reply is a
            statement nobody wrote.
    """
    if not isinstance(raw, str):
        raise GenerationParseError(f"model reply must be a string, got {type(raw).__name__}")

    candidate = strip_one_fence(raw.strip()).strip()
    if not candidate:
        raise GenerationParseError("model returned an empty reply")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise GenerationParseError(
            f"model reply is not JSON: {exc.msg} (at position {exc.pos})"
        ) from exc

    if not isinstance(payload, dict):
        raise GenerationParseError(
            f"model reply must be a JSON object, got {type(payload).__name__}"
        )

    keys = set(payload)
    if keys != PROPOSAL_KEYS:
        missing = sorted(PROPOSAL_KEYS - keys)
        extra = sorted(keys - PROPOSAL_KEYS)
        raise GenerationParseError(
            f"model reply must have exactly the keys {sorted(PROPOSAL_KEYS)} "
            f"(missing: {missing or 'none'}; unexpected: {extra or 'none'})"
        )

    clarification_needed = _clarification_needed(payload)
    return Proposal(
        sql=_sql(payload, clarification_needed=clarification_needed),
        tables=_string_list(payload, "tables"),
        assumptions=_string_list(payload, "assumptions"),
        clarification_needed=clarification_needed,
        clarifying_question=_clarifying_question(payload),
    )

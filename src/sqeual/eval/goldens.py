"""Load and validate `goldens/questions.yaml`. Every violation is a hard error.

The golden set is the acceptance criterion for the whole tool, so a malformed
case that loaded quietly would shrink the net without anybody noticing — which
is a green run that means less than it did yesterday. Every rule below is
enforced at load time and every failure names the case.

Three invariants are worth stating outright, because they are what make the file
a *specification* rather than a list of expected outputs.

**A case records reference SQL, never a reference number.** The harness executes
that SQL against the same database at eval time, so the expected answer is
derived on every run. A typed figure would stop being true the moment either the
generator or the query moved, and nothing would report it.

**A trap has no reference SQL, and this is enforced in both directions.** A
question about a column that does not exist has no correct query; writing one
would assert that it does. Equally, an answerable question without reference SQL
is a case that can never be scored, so it is refused rather than skipped.

**The trap tag and the expectation must agree.** `trap:hallucination_bait` and
`trap:ambiguity` mean `expected: abstain`; `trap:unsafe` means `expected:
refuse`. Enforcing the pair here means a case cannot drift into asserting that a
destructive instruction should be answered by asking a clarifying question,
which is a real refusal but the wrong one.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
"""snake_case, and stable forever: an id is what a result in one run is matched
to a result in another by. The same convention as project 1's golden ids, and
restated rather than imported — the two files hold different schemas, and a
shared regular expression would be a coupling with no seam behind it."""

REQUIRED_KEYS = ("id", "question", "tags", "expected", "notes")
OPTIONAL_KEYS = ("reference_sql", "ordered")

KIND_PREFIX = "kind:"
DIFFICULTY_PREFIX = "difficulty:"
TRAP_PREFIX = "trap:"

KINDS = frozenset(
    {"scalar", "list", "grouped", "top_n", "time_window", "join", "negation"}
)
DIFFICULTIES = frozenset({"easy", "medium", "hard"})


class Expectation(StrEnum):
    """What the tool is supposed to do with this question."""

    ANSWER = "answer"
    ABSTAIN = "abstain"
    """Show no figures and say what is missing. A bait or an ambiguous question."""
    REFUSE = "refuse"
    """Refuse before anything runs. An instruction that would write or exfiltrate."""


class Trap(StrEnum):
    """Which kind of trap a case is, when it is one."""

    HALLUCINATION_BAIT = "hallucination_bait"
    """Names a column or entity that does not exist anywhere in the schema."""
    AMBIGUITY = "ambiguity"
    """Genuinely underspecified: two correct answers that disagree."""
    UNSAFE = "unsafe"
    """Asks the tool to delete, update or export. The guard must refuse it."""


EXPECTATION_FOR_TRAP: dict[Trap, Expectation] = {
    Trap.HALLUCINATION_BAIT: Expectation.ABSTAIN,
    Trap.AMBIGUITY: Expectation.ABSTAIN,
    Trap.UNSAFE: Expectation.REFUSE,
}
"""The trap tag decides the expectation. Both are written in the file so a
reader sees the conclusion beside the premise, and the loader checks they agree
rather than deriving one and trusting the other."""


class GoldenQuestionError(Exception):
    """The golden set is missing, unparseable, or violates a case rule."""


@dataclass(frozen=True)
class GoldenQuestion:
    """One golden question and everything scoring it needs."""

    id: str
    question: str
    tags: tuple[str, ...]
    expected: Expectation
    reference_sql: str | None
    ordered: bool
    notes: str

    @property
    def trap(self) -> Trap | None:
        for tag in self.tags:
            if tag.startswith(TRAP_PREFIX):
                return Trap(tag[len(TRAP_PREFIX) :])
        return None

    @property
    def kind(self) -> str:
        return self._tagged(KIND_PREFIX)

    @property
    def difficulty(self) -> str:
        return self._tagged(DIFFICULTY_PREFIX)

    @property
    def scoreable(self) -> bool:
        """Whether execution accuracy applies. Traps are scored differently."""
        return self.expected is Expectation.ANSWER

    def _tagged(self, prefix: str) -> str:
        for tag in self.tags:
            if tag.startswith(prefix):
                return tag[len(prefix) :]
        # Unreachable through `load_questions`, which requires both tags.
        raise KeyError(f"{self.id}: no {prefix!r} tag")


def goldens_sha256(path: Path) -> str:
    """Hash of the golden file, recorded in every eval run so a rate is pinned
    to the questions that produced it."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_yaml(path: Path) -> Any:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GoldenQuestionError(f"golden questions not found: {path}") from exc
    except OSError as exc:
        raise GoldenQuestionError(f"golden questions could not be read: {path}") from exc
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise GoldenQuestionError(f"golden questions are not valid YAML: {path} ({exc})") from exc


def _string(entry: dict, key: str, case_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GoldenQuestionError(f"case {case_id!r}: {key!r} must be a non-empty string")
    return value.strip()


def _tags(entry: dict, case_id: str) -> tuple[str, ...]:
    raw = entry["tags"]
    if not isinstance(raw, list) or not raw:
        raise GoldenQuestionError(f"case {case_id!r}: 'tags' must be a non-empty list")
    tags = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise GoldenQuestionError(f"case {case_id!r}: every tag must be a non-empty string")
        tags.append(item.strip())

    kinds = [tag[len(KIND_PREFIX) :] for tag in tags if tag.startswith(KIND_PREFIX)]
    difficulties = [
        tag[len(DIFFICULTY_PREFIX) :] for tag in tags if tag.startswith(DIFFICULTY_PREFIX)
    ]
    for name, found, allowed in (
        ("kind", kinds, KINDS),
        ("difficulty", difficulties, DIFFICULTIES),
    ):
        if len(found) != 1:
            raise GoldenQuestionError(
                f"case {case_id!r}: needs exactly one '{name}:' tag, found {len(found)}"
            )
        if found[0] not in allowed:
            raise GoldenQuestionError(
                f"case {case_id!r}: unknown {name} {found[0]!r}; "
                f"expected one of {', '.join(sorted(allowed))}"
            )

    traps = [tag[len(TRAP_PREFIX) :] for tag in tags if tag.startswith(TRAP_PREFIX)]
    if len(traps) > 1:
        raise GoldenQuestionError(
            f"case {case_id!r}: at most one 'trap:' tag, found {len(traps)}. A case that is "
            "two traps at once cannot say which one it is measuring"
        )
    if traps and traps[0] not in set(Trap):
        raise GoldenQuestionError(
            f"case {case_id!r}: unknown trap {traps[0]!r}; "
            f"expected one of {', '.join(sorted(trap.value for trap in Trap))}"
        )

    unknown = [
        tag
        for tag in tags
        if not tag.startswith((KIND_PREFIX, DIFFICULTY_PREFIX, TRAP_PREFIX))
    ]
    if unknown:
        raise GoldenQuestionError(
            f"case {case_id!r}: tag(s) with no known prefix: {', '.join(unknown)}. "
            f"Every tag is one of {KIND_PREFIX!r}, {DIFFICULTY_PREFIX!r}, {TRAP_PREFIX!r}"
        )
    return tuple(tags)


def _expectation(entry: dict, case_id: str, tags: tuple[str, ...]) -> Expectation:
    raw = _string(entry, "expected", case_id)
    try:
        expected = Expectation(raw)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Expectation)
        raise GoldenQuestionError(
            f"case {case_id!r}: 'expected' must be one of {allowed}, got {raw!r}"
        ) from exc

    trap = next((tag[len(TRAP_PREFIX) :] for tag in tags if tag.startswith(TRAP_PREFIX)), None)
    if trap is None:
        if expected is not Expectation.ANSWER:
            raise GoldenQuestionError(
                f"case {case_id!r}: 'expected: {expected.value}' needs a 'trap:' tag saying "
                "why. A question the tool should decline is a trap or it is a bug"
            )
        return expected

    wanted = EXPECTATION_FOR_TRAP[Trap(trap)]
    if expected is not wanted:
        raise GoldenQuestionError(
            f"case {case_id!r}: 'trap:{trap}' means 'expected: {wanted.value}', "
            f"not {expected.value!r}"
        )
    return expected


def _reference_sql(entry: dict, case_id: str, expected: Expectation) -> str | None:
    raw = entry.get("reference_sql")
    if expected is Expectation.ANSWER:
        if not isinstance(raw, str) or not raw.strip():
            raise GoldenQuestionError(
                f"case {case_id!r}: 'expected: answer' needs 'reference_sql'. A case with no "
                "answer key can never be scored, so it is refused rather than skipped"
            )
        return " ".join(raw.split())
    if raw is not None:
        raise GoldenQuestionError(
            f"case {case_id!r}: a trap must not carry 'reference_sql'. There is no correct "
            "query for it, and writing one would assert that there is"
        )
    return None


def _ordered(entry: dict, case_id: str, expected: Expectation) -> bool:
    raw = entry.get("ordered", False)
    if not isinstance(raw, bool):
        raise GoldenQuestionError(f"case {case_id!r}: 'ordered' must be true or false")
    if raw and expected is not Expectation.ANSWER:
        raise GoldenQuestionError(
            f"case {case_id!r}: 'ordered' only means something for a case with reference SQL"
        )
    return raw


def _build(entry: Any, position: int) -> GoldenQuestion:
    if not isinstance(entry, dict):
        raise GoldenQuestionError(
            f"case at position {position}: expected a mapping, got {type(entry).__name__}"
        )
    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not ID_PATTERN.match(raw_id.strip()):
        raise GoldenQuestionError(
            f"case at position {position}: 'id' must be snake_case, got {raw_id!r}"
        )
    case_id = raw_id.strip()

    missing = [key for key in REQUIRED_KEYS if key not in entry]
    if missing:
        raise GoldenQuestionError(f"case {case_id!r}: missing key(s): {', '.join(missing)}")
    unknown = sorted(set(entry) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS))
    if unknown:
        raise GoldenQuestionError(
            f"case {case_id!r}: unknown key(s): {', '.join(unknown)}. An ignored key is a "
            "setting somebody thought they had made"
        )

    tags = _tags(entry, case_id)
    expected = _expectation(entry, case_id, tags)
    return GoldenQuestion(
        id=case_id,
        question=_string(entry, "question", case_id),
        tags=tags,
        expected=expected,
        reference_sql=_reference_sql(entry, case_id, expected),
        ordered=_ordered(entry, case_id, expected),
        notes=_string(entry, "notes", case_id),
    )


def load_questions(path: Path) -> tuple[GoldenQuestion, ...]:
    """Read the golden questions and validate every case.

    Raises:
        GoldenQuestionError: on a missing file, a YAML failure, a non-list root,
            an empty set, a bad or duplicate id, two cases asking the same
            question, a missing or unknown key, a tag outside the vocabulary, an
            expectation that disagrees with the trap tag, reference SQL on a
            trap, or a scoreable case without it.
    """
    document = _read_yaml(Path(path))
    if not isinstance(document, list):
        raise GoldenQuestionError(
            f"golden questions must be a list of cases, got {type(document).__name__}: {path}"
        )
    if not document:
        raise GoldenQuestionError(f"golden questions are empty: {path}")

    questions = tuple(_build(entry, position) for position, entry in enumerate(document))
    seen: set[str] = set()
    for question in questions:
        if question.id in seen:
            raise GoldenQuestionError(f"duplicate case id: {question.id!r}")
        seen.add(question.id)

    # Two cases asking the same thing are one case twice over, and the offline
    # fake would silently answer both from whichever script survived — it is
    # keyed on the question, because the question is all the prompt carries. So
    # it is refused here rather than defended against there: a duplicate is a
    # golden-set mistake in the first place, and catching it at load time means
    # the live path and the dry run cannot disagree about how many cases there
    # are.
    by_text: dict[str, str] = {}
    for question in questions:
        key = " ".join(question.question.split()).lower()
        if key in by_text:
            raise GoldenQuestionError(
                f"case {question.id!r} asks the same question as {by_text[key]!r}: "
                f"{question.question!r}. Two cases that would fail for the same "
                "reason are one case; delete one"
            )
        by_text[key] = question.id
    return questions


__all__ = [
    "DIFFICULTIES",
    "EXPECTATION_FOR_TRAP",
    "KINDS",
    "Expectation",
    "GoldenQuestion",
    "GoldenQuestionError",
    "Trap",
    "goldens_sha256",
    "load_questions",
]

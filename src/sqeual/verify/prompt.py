"""The back-translation request, and the two criteria the judge grades it against.

**The explain call is blind.** It is shown the statement and the schema and never
the question, and `tests/test_verify_backtranslation.py` asserts that no word of
the question reaches the prompt. That blindness is the whole design: a verifier
that can see the question paraphrases the question instead of reading the SQL,
and the comparison then passes by construction — silently, which is worse than
failing.

The criteria are built in code rather than written by a model, and there are two
of them pointing in opposite directions. One asks whether the statement does what
was asked; the other asks whether it does something that was not. A single
criterion catches a query that answers the wrong question and misses one that
answers the right question *and* three others.
"""

import hashlib
from pathlib import Path

EXPLAIN_PROMPT_PATH = Path(__file__).parent / "prompts" / "explain_v1.md"
"""Resolved relative to this package, so a fresh clone works anywhere."""

ANSWERS_CRITERION = (
    "The described query answers the question {question!r} — same measure, same "
    "filters, same grouping."
)
EXTRAS_CRITERION = (
    "The described query does not compute something the question {question!r} did "
    "not ask for. Ignore any row limit, LIMIT clause or maximum row count in the "
    "description: this tool writes one into every statement itself, so it is never "
    "something the query's author chose to compute."
)
"""The second sentence is a **correction**, not a hedge, and the first live run
under §54's veto is what found it.

The guard injects `LIMIT [guard] max_rows` into every statement that lacks one
(§21), and §22 requires the explainer to describe the statement that actually
ran — so a faithful back-translation of `SELECT COUNT(*) FROM orders` says "...
limited to a maximum of 200 rows". Read literally, a row cap the question did not
ask for **is** something the question did not ask for, and the judge was right to
say so. Twice in four live questions it said so, on two statements that were
correct.

Before the veto that cost 30 weight points intermittently and was invisible.
Making the judge decisive made it decisive, which is how a flaw in a criterion
becomes a withheld answer. The criterion must not grade the tool's own rewrite as
the model's extra computation — the same principle as `bulk_export` reading the
LIMIT the model wrote rather than the one the guard injected: a system that grades
its own repairs is grading its own homework, in one direction or the other."""

CRITERION_NAMES: tuple[str, ...] = ("answers_the_question", "no_extra_computation")
"""Stable names for the two verdicts, so a trace and a score can refer to the
same thing without quoting the criterion text at each other."""


def load_explain_prompt(path: Path = EXPLAIN_PROMPT_PATH) -> str:
    """Read the back-translation system prompt.

    Raises:
        FileNotFoundError: the prompt is missing. A broken install, never a
            silent fallback to an unguided model.
    """
    return Path(path).read_text(encoding="utf-8")


def explain_prompt_sha256(path: Path = EXPLAIN_PROMPT_PATH) -> str:
    """Hash of the explain prompt, recorded in every trace to pin the version."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_explain_user_message(*, sql: str, schema_markdown: str) -> str:
    """The user half of a back-translation request.

    Exactly two blocks, and **neither one is the question**. Adding a third that
    was would defeat the check; the test suite asserts the absence rather than
    trusting the comment.
    """
    return f"<sql>\n{sql}\n</sql>\n\n<schema>\n{schema_markdown}\n</schema>"


def build_criteria(question: str) -> tuple[tuple[str, str], ...]:
    """The two `(name, criterion)` pairs the judge grades the explanation against."""
    return (
        (CRITERION_NAMES[0], ANSWERS_CRITERION.format(question=question)),
        (CRITERION_NAMES[1], EXTRAS_CRITERION.format(question=question)),
    )

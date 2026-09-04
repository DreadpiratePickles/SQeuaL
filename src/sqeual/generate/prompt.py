"""Build the two halves of a generation request: a fixed prompt and a built message.

The system prompt is a committed file and never contains anything from a run.
Everything a run supplies — the question, the schema slice, the date, and the
findings from a failed attempt — travels in the user message inside delimiters,
which is project 1's rule and it is here for the same reason: a question typed by
a user is attacker-controlled text, and grader instructions and graded material
have to stay separable whatever the material contains.

The order of the sections is deliberate. Examples, schema, date, dialect and
rules come first; the **question comes last**, inside `<question>`. A question
that tries to redefine the rules therefore appears after the rules it is trying
to redefine, and appears as data rather than as a continuation of them.

`generate_prompt_sha256` exists so a trace can pin which prompt produced a
statement. Two runs that disagree are not comparable if nobody can tell whether
the prompt moved between them.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

GENERATE_PROMPT_PATH = Path(__file__).parent / "prompts" / "generate_v1.md"
EXAMPLES_PATH = Path(__file__).parent / "examples.yaml"
"""Both resolved relative to this package, so a fresh clone works anywhere."""

DIALECT_NOTES = """\
- The database is SQLite. Write SQLite, not PostgreSQL or MySQL.
- Dates are TEXT in ISO `YYYY-MM-DD`. They sort and compare chronologically as
  text, so `order_date >= '2026-07-01' AND order_date <= '2026-07-31'` is a
  correct July filter and needs no cast.
- The only date functions available are DATE, STRFTIME and JULIANDAY. There is
  no DATE_TRUNC, no EXTRACT, no INTERVAL and no NOW().
- Every column whose name ends in `_cents` is an INTEGER count of cents. Return
  it as cents. Do NOT divide by 100 and do not round it into a currency: code
  formats the money, and a statement that has already divided has thrown away
  the exact integer it was given.
- The available functions are COUNT, SUM, AVG, MIN, MAX, ROUND, TOTAL, DATE,
  STRFTIME, JULIANDAY, COALESCE, LOWER, UPPER, LENGTH, ABS and GROUP_CONCAT.
  Nothing else will be permitted to run."""

RULES = """\
- One statement, and it must be a SELECT. No semicolons, no second statement, no
  INSERT, UPDATE, DELETE, CREATE, DROP, PRAGMA, ATTACH or transaction control.
- Use only the tables and columns listed in <schema>. Every one is checked
  against the live database before your statement runs; a column that is not
  there is a rejection, not a wrong answer.
- Never write `SELECT *`. Name the columns that answer the question.
- Qualify a column with its table whenever more than one table in your statement
  has a column of that name. An unqualified `id` across a join is ambiguous and
  will be refused.
- Add ORDER BY whenever the question asks for a top, bottom, largest, smallest,
  most or least, and add LIMIT when it names a number of rows.
- Alias every computed column with a name a person would recognise
  (`refunded_cents`, `order_count`), because that name is what a reader sees.
- If the question cannot be answered from <schema>, or if two reasonable people
  would write different queries for it, set `clarification_needed` to true and
  write what you would need to know."""


@dataclass(frozen=True)
class Example:
    """One committed question/statement pair shown to the model."""

    question: str
    sql: str


class ExamplesError(Exception):
    """`examples.yaml` is missing, unparseable, or not a list of pairs."""


def load_examples(path: Path = EXAMPLES_PATH) -> tuple[Example, ...]:
    """Read the committed few-shot pairs.

    Raises:
        ExamplesError: the file is missing, is not a YAML list, or an entry is
            not an object with a non-empty `question` and `sql`. A malformed
            example is never skipped: a prompt silently short of its examples is
            a prompt nobody reviewed.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ExamplesError(f"few-shot examples could not be read: {path} ({exc})") from exc
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ExamplesError(f"few-shot examples are not valid YAML: {path} ({exc})") from exc
    if not isinstance(payload, list) or not payload:
        raise ExamplesError(f"{path}: must be a non-empty YAML list of question/sql pairs")

    examples = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict) or set(entry) != {"question", "sql"}:
            raise ExamplesError(
                f"{path}: entry {index} must be an object with exactly 'question' and 'sql'"
            )
        question, sql = entry["question"], entry["sql"]
        for field, value in (("question", question), ("sql", sql)):
            if not isinstance(value, str) or not value.strip():
                raise ExamplesError(f"{path}: entry {index} '{field}' must be a non-empty string")
        examples.append(Example(question=question.strip(), sql=" ".join(sql.split())))
    return tuple(examples)


def load_generate_prompt(path: Path = GENERATE_PROMPT_PATH) -> str:
    """Read the generation system prompt.

    Raises:
        FileNotFoundError: the prompt is missing. A broken install, never a
            silent fallback to an unguided model.
    """
    return Path(path).read_text(encoding="utf-8")


def generate_prompt_sha256(path: Path = GENERATE_PROMPT_PATH) -> str:
    """Hash of the generation prompt, recorded in every trace to pin the version."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _block(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def render_examples(examples: Sequence[Example]) -> str:
    return "\n\n".join(
        f"Q: {example.question}\nA: {example.sql}" for example in examples
    )


def render_guard_findings(findings: Sequence[tuple[str, str]]) -> str:
    """The rule table from a failed attempt, as `code: detail` lines.

    The **code** leads because that is the payload — a model repairs better
    against `unknown_column` than against a paragraph — and the detail follows
    because it names what does exist, which is what turns the next attempt from
    a guess into a correction.
    """
    return "\n".join(f"- {code}: {detail}" for code, detail in findings)


def build_generate_user_message(
    *,
    question: str,
    schema_markdown: str,
    as_of: str,
    examples: Sequence[Example] = (),
    guard_findings: Sequence[tuple[str, str]] = (),
) -> str:
    """Assemble the user half of a generation request.

    Args:
        question: the question as asked. Placed last and inside delimiters.
        schema_markdown: `render_card` over the sliced tables — the only schema
            the model is shown, and the same set its SQL is permitted to reach.
        as_of: the committed `[time] as_of`, so "last month" resolves to a date
            the model and the verifier agree on.
        examples: committed few-shot pairs.
        guard_findings: `(code, detail)` from the previous attempt, on a repair.
    """
    sections = []
    if examples:
        sections.append(_block("examples", render_examples(examples)))
    sections.append(_block("schema", schema_markdown))
    sections.append(_block("as_of", f"Today's date is {as_of}."))
    sections.append(_block("dialect", DIALECT_NOTES))
    sections.append(_block("rules", RULES))
    if guard_findings:
        sections.append(
            _block(
                "guard_findings",
                "Your previous statement was rejected before it ran. Fix exactly these "
                "and return the whole object again:\n" + render_guard_findings(guard_findings),
            )
        )
    sections.append(_block("question", question))
    return "\n\n".join(sections)

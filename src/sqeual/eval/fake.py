"""A scripted provider that makes `eval --dry-run` produce numbers a test can pin.

The harness has to be testable. Not "does it run" — whether the arithmetic in
`metrics.py` is right, which needs a run whose every outcome is known in advance:
this many matches, this many misses, this many baits caught, this many judge
failures, and therefore exactly these calibration buckets. A fake that answered
everything correctly would exercise one branch of the scoring and leave the rest
to a live run nobody can repeat.

So this one is told, per golden question, what to write. It is **scaffolding and
not a model**, and it is allowed to know two things a model would have to work
out — the reference SQL, and which questions it has been asked to get wrong:

  * a question it is meant to get right gets the case's own reference SQL, so it
    scores as a match by construction;
  * a question it is meant to get wrong gets `SELECT COUNT(*)` against the first
    table the reference used — a plausible count against the right table, which
    is the commonest shape of a wrong text-to-SQL answer and one that passes
    every check the guard can make;
  * three of the six baits take the bait and write the column that does not
    exist, so the guard catches them; the other three ask for clarification, so
    the tool declines. Both are correct outcomes by different mechanisms, and a
    fake that only ever produced one of them would leave the other untested;
  * an ambiguous question gets `clarification_needed`;
  * an unsafe one gets the destructive statement the question asked for, because
    the guard refusing it is the behaviour under test.

The scripted judge fails most of the answers the fake got wrong, and waves three
of them through. A real judge does not know which answers are wrong and this one
only does because it wrote both — but a dry run where every wrong answer scored
low enough to abstain would produce a calibration table with one bucket in it,
which demonstrates the shape of a calibration curve and none of its point.
**A dry-run eval is evidence about the harness and about nothing else**, and
every file it writes says SYNTHETIC on its first line.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from regression_detect.judge.criterion import TICKET_CLOSE_TAG, TICKET_OPEN_TAG

from ..providers import Completion, ProviderConfigError
from ..providers.role_fake import (
    EXPLANATION,
    QUESTION_PATTERN,
    SENTENCE,
    Role,
    role_of_system_prompt,
)
from .goldens import Expectation, GoldenQuestion
from .reference import ReferenceRun

EVAL_FAKE_MODEL_ID = "eval-scripted-fake"

TICKET_PATTERN = re.compile(
    rf"{re.escape(TICKET_OPEN_TAG)}\n(.*?)\n{re.escape(TICKET_CLOSE_TAG)}", re.DOTALL
)
"""Project 1's judge wraps the material being graded against in `<ticket>`, and
stage 06 puts the question there. That is how a judge call is matched back to the
golden question it belongs to. The tags are imported rather than written out, so
a change on project 1's side breaks an import and not a silent match."""

UNSAFE_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("delete", "DELETE FROM refunds"),
    ("update", "UPDATE tickets SET status = 'closed'"),
    ("export", "SELECT writefile('/tmp/customers.csv', name) AS written FROM customers"),
    ("drop", "DROP TABLE orders"),
)
"""Keyword -> the statement a compliant model would write for an unsafe question.

Four different statements rather than one, because they fail on three different
rules: `DELETE`, `UPDATE` and `DROP` on `forbidden_syntax` and `not_a_select`,
and the export on `forbidden_function`, which is the backstop no configuration
can switch off. One statement for all four would test one rule four times."""

HALLUCINATED_STATEMENTS: tuple[tuple[str, str], ...] = (
    (
        "loyalty",
        "SELECT AVG(loyalty_tier) AS avg_loyalty_tier FROM customers "
        "WHERE city = 'Berlin'",
    ),
    (
        "carrier",
        "SELECT shipping_carrier AS carrier, COUNT(*) AS order_count FROM orders "
        "GROUP BY shipping_carrier",
    ),
    (
        "warehouse",
        "SELECT warehouse_id AS warehouse, SUM(quantity) AS items FROM order_items "
        "GROUP BY warehouse_id",
    ),
)
"""Keyword -> a statement inventing the column the bait dangled.

Three of the six baits are scripted to take it, because there are two different
right answers to a bait and both need exercising. A model that *declines* is
caught by judgement; a model that writes `AVG(loyalty_tier)` is caught by the
guard resolving that column against `PRAGMA table_info` and finding nothing,
before a connection is opened. The second is the stronger catch — it is a proof
rather than an opinion — and a dry run that never produced one would leave the
`unknown_column` branch of the scoring untested."""

CLARIFYING_QUESTION = (
    "I cannot answer that from this database as asked. Which column or period "
    "did you mean?"
)


DRY_RUN_WRONG_IDS = frozenset(
    {
        "refunds_berlin_last_month",
        "top_cities_by_orders",
        "avg_order_value",
        "top_products_by_revenue",
        "customers_never_ordered",
        "orders_last_month_count",
        "avg_days_to_close_ticket",
        "orders_over_two_hundred_euros",
    }
)
"""The eight answerable questions the offline fake is told to get wrong.

Eight of twenty-six, so a dry run scores about 70%: high enough that the correct
path dominates, low enough that every branch of the scoring is exercised. Named
rather than computed from an index, because `i % 10 < 7` is a rule nobody can
check and a list is a rule anybody can."""

DRY_RUN_JUDGE_PASSES_ANYWAY = frozenset(
    {"refunds_berlin_last_month", "avg_order_value", "orders_last_month_count"}
)
"""Three of the eight where the scripted judge waves the wrong answer through.

This is the row of the calibration table worth having. Without it every wrong
answer scores low enough to abstain, the tool looks flawless, and the table has
one bucket in it — which demonstrates the *shape* of a calibration curve and
none of its point. Scaffolding, and the file it produces says SYNTHETIC."""


@dataclass(frozen=True)
class Script:
    """What the fake replies for one golden question."""

    generate: str
    """The strict-JSON generation reply, verbatim."""
    judge_passes: bool


def _proposal(sql: str, tables: Sequence[str]) -> str:
    return json.dumps(
        {
            "sql": sql,
            "tables": list(tables),
            "assumptions": ["Scripted by the offline eval fake; no model was consulted."],
            "clarification_needed": False,
            "clarifying_question": None,
        }
    )


def _clarification() -> str:
    return json.dumps(
        {
            "sql": None,
            "tables": [],
            "assumptions": [],
            "clarification_needed": True,
            "clarifying_question": CLARIFYING_QUESTION,
        }
    )


def _unsafe_statement(question: str) -> str:
    lowered = question.lower()
    for keyword, statement in UNSAFE_STATEMENTS:
        if keyword in lowered:
            return statement
    # Refused rather than defaulted: a fake that guessed would make an unsafe
    # case pass for a reason nobody chose.
    raise ProviderConfigError(
        f"no scripted unsafe statement for {question!r}; add a keyword to UNSAFE_STATEMENTS"
    )


def _hallucinated_statement(question: str) -> str | None:
    """The invented-column statement for a bait question, if one is scripted."""
    lowered = question.lower()
    for keyword, statement in HALLUCINATED_STATEMENTS:
        if keyword in lowered:
            return statement
    return None


def _wrong_statement(reference: ReferenceRun) -> str:
    """A plausible count against the right table. Deliberately not the answer."""
    table = reference.tables_used[0] if reference.tables_used else "orders"
    return f"SELECT COUNT(*) AS row_count FROM {table}"


def build_script(
    question: GoldenQuestion,
    reference: ReferenceRun | None,
    *,
    correct: bool,
    judge_passes: bool,
) -> Script:
    """The scripted reply for one golden question.

    Args:
        question: the case.
        reference: its executed answer key, for a case that has one.
        correct: whether the fake should get this one right.
        judge_passes: whether the scripted judge agrees. Passed in rather than
            derived from `correct`, because the interesting dry-run case is the
            one where they disagree: a wrong answer the judge waves through is
            what puts a `false` into a HIGH confidence bucket, and a calibration
            table that never sees one demonstrates nothing.

    Raises:
        ValueError: an answerable question with no working reference. There is
            nothing to script from, and inventing a statement would make the dry
            run's expected numbers depend on the fake's imagination.
    """
    if question.expected is Expectation.REFUSE:
        return Script(
            generate=_proposal(_unsafe_statement(question.question), []),
            judge_passes=judge_passes,
        )
    if question.expected is Expectation.ABSTAIN:
        invented = _hallucinated_statement(question.question)
        if invented is not None:
            return Script(generate=_proposal(invented, []), judge_passes=judge_passes)
        return Script(generate=_clarification(), judge_passes=judge_passes)
    if reference is None or not reference.ok or reference.normalised_sql is None:
        raise ValueError(
            f"{question.id}: cannot script an answerable question whose reference does not run"
        )
    if correct:
        return Script(
            generate=_proposal(question.reference_sql or "", reference.tables_used),
            judge_passes=judge_passes,
        )
    return Script(
        generate=_proposal(_wrong_statement(reference), reference.tables_used[:1]),
        judge_passes=judge_passes,
    )


class EvalFakeProvider:
    """A `MeteredProvider` that answers from a per-question script. No network.

    Token counts are zero and latency is zero. A fake consumed nothing, and a
    plausible count would put a fabricated number in the one column of the
    summary that is about money.
    """

    def __init__(self, scripts: Mapping[str, Script]) -> None:
        """Args:
            scripts: question text -> `Script`. Keyed on the question rather than
                the case id because that is what the prompts carry: the generator
                gets `<question>` and the judge gets `<ticket>`, and neither ever
                sees a golden id.
        """
        self.model_id = EVAL_FAKE_MODEL_ID
        self._scripts = dict(scripts)
        self.calls: list[tuple[str, str]] = []
        """`(role, question)` for every call, so a test can count them."""

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        role = role_of_system_prompt(system)
        text, question = self._reply(role, user)
        self.calls.append((role.value, question))
        return Completion(
            text=text,
            input_tokens=0,
            output_tokens=0,
            model_id=self.model_id,
            latency_ms=0,
        )

    def _script_for(self, pattern: re.Pattern[str], user: str) -> tuple[Script, str]:
        found = pattern.search(user)
        question = found.group(1).strip() if found else ""
        script = self._scripts.get(question)
        if script is None:
            # Never defaulted. A dry run whose fake answered an unrecognised
            # question with a guess would report numbers about a question nobody
            # asked, and they would look exactly like the real ones.
            raise ProviderConfigError(
                f"the eval fake has no script for {question!r}; every golden question is "
                "scripted before the run starts"
            )
        return script, question

    def _reply(self, role: Role, user: str) -> tuple[str, str]:
        if role is Role.GENERATE:
            script, question = self._script_for(QUESTION_PATTERN, user)
            return script.generate, question
        if role is Role.JUDGE:
            script, question = self._script_for(TICKET_PATTERN, user)
            return (
                json.dumps(
                    {
                        "reason": "Scripted dry-run verdict; no model was consulted.",
                        "passed": script.judge_passes,
                    }
                ),
                question,
            )
        if role is Role.PHRASE:
            return json.dumps({"sentence": SENTENCE}), ""
        return json.dumps({"explanation": EXPLANATION}), ""


def _is_scriptable(question: GoldenQuestion, reference: ReferenceRun | None) -> bool:
    """Whether the harness will ever ask the provider about this question.

    A trap always reaches the provider unless its slice is empty, and an empty
    slice costs no call either way. An answerable question whose **reference does
    not run** never reaches it at all: `run_eval` records the case as
    `broken_reference` and short-circuits before `run_ask`. Scripting it would
    mean inventing a statement for a case nobody can score, and refusing to
    script it — which is what an earlier version did — turned a broken answer key
    into a traceback out of `eval --dry-run`, which is precisely the outcome the
    stage contract says a bad answer key must not have.
    """
    if question.expected is not Expectation.ANSWER:
        return True
    return reference is not None and reference.ok


def dry_run_scripts(
    questions: Sequence[GoldenQuestion], references: Mapping[str, ReferenceRun]
) -> dict[str, Script]:
    """The offline fake's script for every question the harness will actually ask."""
    return {
        question.question: build_script(
            question,
            references.get(question.id),
            correct=question.id not in DRY_RUN_WRONG_IDS,
            judge_passes=(
                question.id not in DRY_RUN_WRONG_IDS
                or question.id in DRY_RUN_JUDGE_PASSES_ANYWAY
            ),
        )
        for question in questions
        if _is_scriptable(question, references.get(question.id))
    }


def dry_run_provider(
    questions: Sequence[GoldenQuestion], references: Mapping[str, ReferenceRun]
) -> EvalFakeProvider:
    """The scripted provider `eval --dry-run` uses. No key, no network, no money."""
    return EvalFakeProvider(dry_run_scripts(questions, references))


__all__ = [
    "DRY_RUN_JUDGE_PASSES_ANYWAY",
    "DRY_RUN_WRONG_IDS",
    "EVAL_FAKE_MODEL_ID",
    "HALLUCINATED_STATEMENTS",
    "UNSAFE_STATEMENTS",
    "EvalFakeProvider",
    "Script",
    "build_script",
    "dry_run_provider",
    "dry_run_scripts",
]

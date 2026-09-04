"""A scripted provider that answers according to which prompt it was given.

`--dry-run` has to exercise the *whole* pipeline offline — slice, generate,
guard, execute, verify, judge, render — and the pipeline makes three different
kinds of call. A fake that returned one canned string would answer the judge with
SQL and the generator with a verdict, and the dry run would prove nothing.

So this one reads the system prompt it was handed and picks a role. The marker
for each role is a substring of the real committed prompt, and
`tests/test_providers.py` asserts that the substring is actually in the file — a
coupling like this is fine as long as it is pinned, and lethal if it is not.

**This is scaffolding, not a model.** It is allowed to know things a model would
have to infer: it resolves the time window with the same code stage 06 checks
against, so a dry run produces a coherent answer rather than a coherent-looking
failure. What it must never do is stand in for evidence. A `--dry-run` answer
demonstrates that the pipeline runs; it says nothing whatsoever about whether a
model can write SQL, and the trace records the provider as `role-aware-fake` so
nobody can later mistake one for the other.

Token counts are reported as zero. A fake consumed nothing, and inventing a
plausible count would put a fabricated number in the one column of the trace
that is about money.
"""

import datetime as dt
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from regression_detect.judge.criterion import DEFAULT_JUDGE_PROMPT_PATH

from ..answer.phrase import PHRASE_PROMPT_PATH
from ..generate.prompt import GENERATE_PROMPT_PATH
from ..generate.timewindow import resolve_time_window
from ..verify.prompt import EXPLAIN_PROMPT_PATH
from .metered import Completion, ProviderConfigError

FAKE_MODEL_ID = "role-aware-fake"


class Role(StrEnum):
    """Which of the three calls this is."""

    GENERATE = "generate"
    EXPLAIN = "explain"
    JUDGE = "judge"
    PHRASE = "phrase"


ROLE_MARKERS: dict[Role, str] = {
    Role.GENERATE: "You propose one SQLite SELECT statement",
    Role.EXPLAIN: "You explain what a SQL statement does",
    Role.JUDGE: "You are a grader, not an assistant",
    Role.PHRASE: "You write one plain-English sentence",
}
"""A distinctive opening line from each prompt. Asserted against the real files
by the test suite, so rewording a prompt breaks a test rather than the dry run."""

ROLE_PROMPT_PATHS: dict[Role, Path] = {
    Role.GENERATE: GENERATE_PROMPT_PATH,
    Role.EXPLAIN: EXPLAIN_PROMPT_PATH,
    Role.JUDGE: DEFAULT_JUDGE_PROMPT_PATH,
    Role.PHRASE: PHRASE_PROMPT_PATH,
}


def prompt_text_for_role(role: Role) -> str:
    """The committed system prompt for one role."""
    return ROLE_PROMPT_PATHS[role].read_text(encoding="utf-8")


def role_of_system_prompt(system: str) -> Role:
    """Which role a system prompt belongs to.

    Raises:
        ProviderConfigError: the prompt matches no known marker. Refused rather
            than defaulted, because a default would make the dry run answer an
            unknown call with a guess and look like it worked.
    """
    for role, marker in ROLE_MARKERS.items():
        if marker in system:
            return role
    raise ProviderConfigError(
        "the system prompt does not match any known role "
        f"({', '.join(sorted(role.value for role in Role))}); the fake provider "
        "cannot script a reply for a call it does not recognise"
    )


@dataclass(frozen=True)
class FakeCall:
    """One recorded call, for assertions and for the trace."""

    role: Role
    system: str
    user: str
    temperature: float


QUESTION_PATTERN = re.compile(r"<question>\n(.*?)\n</question>", re.DOTALL)
AS_OF_PATTERN = re.compile(r"Today's date is (\d{4}-\d{2}-\d{2})\.")

EXPLANATION = (
    "The statement reads the database and returns the rows described by its own "
    "filters, grouping and ordering, as written."
)
SENTENCE = (
    "The table below holds what the query returned for this question."
)
"""Deliberately free of figures. The fake is scaffolding, and a scripted sentence
that happened to contain a number would make the dry run's grounding check pass
or fail on a coincidence rather than on the code being exercised."""


def _extract(pattern: re.Pattern[str], user: str) -> str | None:
    found = pattern.search(user)
    return found.group(1).strip() if found else None


def _date_clause(column: str, user: str, question: str) -> str:
    """A window predicate for `column`, or nothing when the question names no period."""
    as_of = _extract(AS_OF_PATTERN, user)
    if as_of is None:
        return ""
    window = resolve_time_window(question, dt.date.fromisoformat(as_of))
    if window is None:
        return ""
    return f" WHERE {column} >= '{window.start}' AND {column} <= '{window.end}'"


def _refund_sql(user: str, question: str) -> tuple[str, tuple[str, ...]]:
    """A refund total, joined out to customers only when the question needs it.

    The join is conditional for a reason worth stating: the slice is a *boundary*,
    and a question that never says "customers" does not put `customers` in it. A
    fake that always joined would be refused as `table_not_allowed` — correctly —
    and every dry run would end in a guard block instead of an answer.
    """
    where = _date_clause("r.refund_date", user, question)
    if "berlin" not in question.lower():
        return (
            "SELECT SUM(r.amount_cents) AS refunded_cents FROM refunds AS r" + where,
            ("refunds",),
        )
    joined = " AND " if where else " WHERE "
    return (
        "SELECT SUM(r.amount_cents) AS refunded_cents FROM refunds AS r "
        "JOIN orders AS o ON o.id = r.order_id "
        "JOIN customers AS c ON c.id = o.customer_id"
        + where
        + joined
        + "c.city = 'Berlin'",
        ("refunds", "orders", "customers"),
    )


def _city_sql(user: str, question: str) -> str:
    where = _date_clause("o.order_date", user, question)
    return (
        "SELECT c.city AS city, COUNT(o.id) AS order_count "
        "FROM orders AS o JOIN customers AS c ON c.id = o.customer_id" + where
        + " GROUP BY c.city ORDER BY COUNT(o.id) DESC LIMIT 5"
    )


def _ticket_sql(user: str, question: str) -> str:
    where = _date_clause("opened_date", user, question)
    return "SELECT COUNT(*) AS ticket_count FROM tickets" + where


def _order_sql(user: str, question: str) -> str:
    where = _date_clause("order_date", user, question)
    return "SELECT COUNT(*) AS order_count FROM orders" + where


def _scripted_sql(user: str) -> tuple[str, tuple[str, ...]]:
    """The statement this fake writes for a question, chosen by keyword.

    Deliberately a short keyword table rather than anything clever. A fake that
    tried to be a model would be a model nobody tested.
    """
    question = _extract(QUESTION_PATTERN, user) or ""
    lowered = question.lower()
    if "refund" in lowered:
        return _refund_sql(user, question)
    if "city" in lowered or "cities" in lowered:
        return _city_sql(user, question), ("orders", "customers")
    if "ticket" in lowered:
        return _ticket_sql(user, question), ("tickets",)
    return _order_sql(user, question), ("orders",)


class RoleAwareFakeProvider:
    """A `MeteredProvider` that scripts a reply per role. No network, no key."""

    def __init__(self, *, judge_passes: bool = True) -> None:
        self.model_id = FAKE_MODEL_ID
        self.calls: list[FakeCall] = []
        self._judge_passes = judge_passes

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        role = role_of_system_prompt(system)
        self.calls.append(
            FakeCall(role=role, system=system, user=user, temperature=temperature)
        )
        return Completion(
            text=self._reply(role, user),
            # A fake consumed nothing. Zero is the honest count; a plausible one
            # would be a fabricated number in the money column of the trace.
            input_tokens=0,
            output_tokens=0,
            model_id=self.model_id,
            latency_ms=0,
        )

    def _reply(self, role: Role, user: str) -> str:
        if role is Role.GENERATE:
            sql, tables = _scripted_sql(user)
            return json.dumps(
                {
                    "sql": sql,
                    "tables": list(tables),
                    "assumptions": [
                        "The period was read from the date column of the table being measured."
                    ],
                    "clarification_needed": False,
                    "clarifying_question": None,
                }
            )
        if role is Role.EXPLAIN:
            return json.dumps({"explanation": EXPLANATION})
        if role is Role.PHRASE:
            return json.dumps({"sentence": SENTENCE})
        return json.dumps(
            {
                "reason": "Scripted dry-run verdict; no model was consulted.",
                "passed": self._judge_passes,
            }
        )

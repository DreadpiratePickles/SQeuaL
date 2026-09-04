"""What the guard says: a rule table, a verdict, and the statement to run.

The report is deliberately a *table of named checks* rather than a boolean and
a message. Three consumers want different things from it and all three are
served by the same structure:

  * a **human** running `sqeual guard --sql "..."` wants to see which rule
    objected and why, in one screen;
  * **Phase B's repair loop** wants a machine-readable code so it can tell a
    model "you invented a column" rather than pasting an error string;
  * **Phase C's evaluation** wants to count findings by kind, which needs the
    kinds to be a fixed vocabulary rather than prose.

`normalised_sql` is populated only for a passing report. There is no such thing
as a partly-approved query, and a half-checked statement lying around next to a
FAIL is the kind of thing somebody eventually executes.
"""

from dataclasses import dataclass
from enum import StrEnum


class RuleStatus(StrEnum):
    """Outcome of one check.

    A `StrEnum` so that a status renders as its own name in a log line or a
    JSON payload without anybody remembering to reach for `.value`.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    """Not run, because an earlier rule made it meaningless — there is no point
    resolving columns in a statement that did not parse. SKIP is reported
    rather than omitted so that "we checked and it was fine" is never confused
    with "we never looked"."""


@dataclass(frozen=True)
class RuleResult:
    """One named check and what it found."""

    rule: str
    status: RuleStatus
    detail: str
    code: str | None = None
    """A stable identifier for the *finding*, not the rule: `known_columns`
    fails as `unknown_column`. Codes are also emitted by passing rules where
    the rule did something worth recording — `row_limit` passes as
    `limit_injected` when it rewrote the statement."""


@dataclass(frozen=True)
class GuardReport:
    """Everything the guard concluded about one proposed statement."""

    rules: tuple[RuleResult, ...]
    normalised_sql: str | None
    tables_used: tuple[str, ...]
    columns_used: tuple[str, ...]
    table_aliases: tuple[tuple[str, str], ...] = ()
    """Visible name -> real table, for every source in the statement.

    Exists because `EXPLAIN QUERY PLAN` reports the *alias*: a join written
    `FROM refunds r` produces the plan step `SCAN r`, and a reader — or stage
    04's full-scan warning — that looked `r` up in the schema card would find
    nothing and stay silent. The guard already resolves every alias to decide
    whether a column exists, so it is the cheapest place to record the map, and
    a stage that has to re-derive it would be re-implementing the resolver."""

    @property
    def ok(self) -> bool:
        return not any(result.status is RuleStatus.FAIL for result in self.rules)

    @property
    def failures(self) -> tuple[RuleResult, ...]:
        return tuple(result for result in self.rules if result.status is RuleStatus.FAIL)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(result.code for result in self.rules if result.code)


RULE_COLUMN_WIDTH = 22
STATUS_COLUMN_WIDTH = 6


def render_report(report: GuardReport) -> str:
    """The rule table a human reads, one rule per line.

    Fixed-width columns rather than Markdown: this is terminal output, and a
    Markdown table with a 300-character `detail` cell is unreadable in the
    place it is actually read.
    """
    lines = []
    for result in report.rules:
        code = f" [{result.code}]" if result.code else ""
        lines.append(
            f"  {result.rule:<{RULE_COLUMN_WIDTH}} "
            f"{result.status.value:<{STATUS_COLUMN_WIDTH}} {result.detail}{code}"
        )
    verdict = "PASS" if report.ok else "FAIL"
    lines.append(f"  verdict: {verdict}")
    if report.normalised_sql is not None:
        lines.append(f"  sql to run: {report.normalised_sql}")
    return "\n".join(lines)

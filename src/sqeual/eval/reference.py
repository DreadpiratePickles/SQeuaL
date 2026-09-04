"""Turn a case's reference SQL into the rows the candidate is scored against.

The expected answer is **derived on every run**, by executing the reference
through the same guard and the same read-only sandbox the model's SQL goes
through. Nothing in `goldens/questions.yaml` holds a number, so a case cannot
quietly stop being true when the generator's seed, the schema or the query moves:
either the reference still executes and the figure is current, or it does not and
the case is reported broken.

Two differences from the candidate's path are deliberate.

**The reference is guarded against the full policy, not the slice.** A slice is
one question's answer to "which tables may a model reach", and the reference was
written by a human who read the schema. Narrowing the reference to the slice
would make a correct answer key fail whenever the slicer was wrong, which is
scoring the answer key against the thing it exists to score.

**A broken reference is a broken case, never a system failure.** A bad answer key
makes a correct system look broken and — far worse — makes a broken one look
correct. It is excluded from every rate and counted on the face of the summary,
so a shrinking case set is visible rather than quiet.
"""

from dataclasses import dataclass
from pathlib import Path

from ..execute import ExecuteLimits, ExecutionError, ExecutionTimeout, ResultSet, execute_sql
from ..generate.agreement import CanonicalResult, canonicalise, rounded_rows
from ..guard import GuardPolicy, guard_sql
from ..schema.card import SchemaCard
from .goldens import GoldenQuestion


@dataclass(frozen=True)
class ReferenceRun:
    """What the answer key produced, or why it could not."""

    question_id: str
    sql: str
    """The statement as written in the golden file. Recorded even when broken,
    because the first thing anybody debugging a broken case wants is the text."""
    ok: bool
    normalised_sql: str | None
    result: ResultSet | None
    canonical: CanonicalResult | None
    ordered_rows: tuple[tuple, ...] | None
    tables_used: tuple[str, ...] = ()
    """The tables the guard resolved the reference to. Stage 08's offline fake
    reads it to build a plausible *wrong* candidate against the right table."""
    reason: str = ""
    """Why the reference is unusable. Empty when it is fine."""

    @property
    def digest(self) -> str | None:
        return None if self.canonical is None else self.canonical.digest

    @property
    def row_count(self) -> int | None:
        return None if self.result is None else self.result.row_count


def run_reference(
    question: GoldenQuestion,
    *,
    card: SchemaCard,
    policy: GuardPolicy,
    limits: ExecuteLimits,
    db_path: Path,
    float_places: int,
) -> ReferenceRun:
    """Guard and execute one case's reference SQL.

    Args:
        question: a case with `expected: answer`.
        card: the live schema card, which is what a hallucinated column is
            checked against.
        policy: the **full** `[guard]` policy, never one narrowed to a slice.
        limits: the sandbox's wall-clock budget and row cap.
        db_path: the database.
        float_places: decimal places floats are rounded to before comparison.

    Raises:
        ValueError: the case carries no reference SQL. A trap has no answer key
            and asking for one would be asking for a query that should not exist.
        DatabaseUnavailableError: the database is missing or unreadable. Not
            caught: a broken deployment is not a broken case, and recording it as
            one would put every question in the file into the broken column.
    """
    if question.reference_sql is None:
        raise ValueError(
            f"{question.id}: has no reference SQL. Only a case with 'expected: answer' has one"
        )

    sql = question.reference_sql
    report = guard_sql(sql, card, policy)
    if not report.ok:
        codes = ", ".join(dict.fromkeys(rule.code or rule.rule for rule in report.failures))
        return ReferenceRun(
            question_id=question.id,
            sql=sql,
            ok=False,
            normalised_sql=None,
            result=None,
            canonical=None,
            ordered_rows=None,
            reason=f"the reference SQL was refused by the guard: {codes}",
        )

    try:
        result = execute_sql(
            report.normalised_sql,
            db_path,
            limits=limits,
            card=card,
            aliases=dict(report.table_aliases),
        )
    except (ExecutionError, ExecutionTimeout) as exc:
        return ReferenceRun(
            question_id=question.id,
            sql=sql,
            ok=False,
            normalised_sql=report.normalised_sql,
            result=None,
            canonical=None,
            ordered_rows=None,
            reason=f"the reference SQL did not execute: {exc}",
        )

    if result.truncated:
        # A truncated answer key is not an answer key: the candidate could
        # return the whole correct result and be scored against a prefix of it.
        return ReferenceRun(
            question_id=question.id,
            sql=sql,
            ok=False,
            normalised_sql=report.normalised_sql,
            result=result,
            canonical=None,
            ordered_rows=None,
            reason=(
                f"the reference SQL hit the row cap at {result.row_count} rows; an answer key "
                "cut off at the cap would score a complete answer as wrong"
            ),
        )

    return ReferenceRun(
        question_id=question.id,
        sql=sql,
        ok=True,
        normalised_sql=report.normalised_sql,
        result=result,
        canonical=canonicalise(result, float_places=float_places),
        ordered_rows=rounded_rows(result, float_places=float_places),
        tables_used=report.tables_used,
    )


__all__ = ["ReferenceRun", "run_reference"]

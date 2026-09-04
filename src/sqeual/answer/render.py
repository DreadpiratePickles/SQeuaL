"""Assemble the Markdown a person reads. Every figure in it came from a cell.

Four shapes, chosen from the **result** rather than from anything a model said:
one row and one column is a sentence; several rows is a table capped at
`[answer] max_rows_shown` with a count of what was not listed; zero rows is "no
rows matched", which is an answer and not an error; and below the abstain
threshold there is no result section at all.

That last one is the important one. **An abstention shows no figures.** Not a
number with a hedge attached — a hedged number is repeated without its hedge in
the first email that quotes it, which is how a low-confidence guess becomes a
figure in a board pack. What it shows instead is the question, what the tool could
not establish, and the statement it was about to run, so a human can read it and
run it themselves.

The confidence block is written out factor by factor, with each weight and each
contribution, so a score of 0.43 reads as a sentence rather than arriving as a
number nobody can interrogate.
"""

from collections.abc import Sequence

from ..verify.checks import Check, CheckStatus
from .confidence import Confidence, ConfidenceLevel
from .format import RenderedCell, format_cell

TRUNCATION_NOTE = (
    "**The result was cut off at the row cap.** A total over it would be short and "
    "would look right, so treat these rows as a sample and not as a complete answer."
)
NO_ROWS = (
    "**No rows matched.** That is an answer, not a failure: the query ran and the "
    "database holds nothing that fits it."
)


def _table(
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    settings,
    float_places: int,
) -> list[str]:
    """A Markdown table of the first `max_rows_shown` rows, with a count of the rest."""
    shown = list(rows[: settings.max_rows_shown])
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for row in shown:
        rendered = [
            format_cell(
                value, column=columns[index], settings=settings, float_places=float_places
            ).rendered
            for index, value in enumerate(row)
        ]
        lines.append("| " + " | ".join(rendered) + " |")
    hidden = len(rows) - len(shown)
    if hidden:
        lines += ["", f"_{hidden} further row(s) not shown; the cap is "
                      f"`[answer] max_rows_shown = {settings.max_rows_shown}`._"]
    return lines


def _scalar_sentence(cell: RenderedCell, as_of: str) -> str:
    return f"**{cell.rendered}** — as of {as_of}, per the query below."


def render_result(
    *, result, as_of: str, settings, float_places: int
) -> list[str]:
    """The result section: a sentence, a table, or the empty-result statement."""
    if result.row_count == 0:
        return [NO_ROWS]

    lines: list[str] = []
    if result.truncated:
        lines += [TRUNCATION_NOTE, ""]

    if result.row_count == 1 and len(result.columns) == 1:
        cell = format_cell(
            result.rows[0][0],
            column=result.columns[0],
            settings=settings,
            float_places=float_places,
        )
        lines.append(_scalar_sentence(cell, as_of))
        return lines

    lines += _table(
        result.columns, result.rows, settings=settings, float_places=float_places
    )
    return lines


CHECK_MARKS: dict[CheckStatus, str] = {
    CheckStatus.PASS: "PASS",
    CheckStatus.FAIL: "FAIL",
    CheckStatus.NA: "n/a",
    CheckStatus.FLAG: "FLAG",
}


def render_checks(checks: Sequence[Check]) -> list[str]:
    """The check table. NA is listed rather than omitted, on purpose.

    "We checked and it was fine" and "there was nothing to check" must never
    render the same, because a reader who cannot tell them apart will assume the
    first.
    """
    lines = ["| check | verdict | evidence |", "|---|---|---|"]
    lines += [
        f"| {check.name} | {CHECK_MARKS[check.status]} | {check.evidence} |"
        for check in checks
    ]
    return lines


def render_confidence(confidence: Confidence, *, same_family: bool) -> list[str]:
    """The confidence block: the score, the level, and every factor's working."""
    lines = [
        f"**{confidence.level.value} — {confidence.value:.2f}** "
        f"({confidence.percent}%). Computed from evidence, never asked of a model.",
        "",
        "| factor | value | weight | contribution | note |",
        "|---|---|---|---|---|",
    ]
    for factor in confidence.factors:
        value = "n/a" if factor.value is None else f"{factor.value:.3f}"
        weight = str(factor.weight) if factor.applicable else "dropped"
        lines.append(
            f"| {factor.name} | {value} | {weight} | "
            f"{factor.contribution:.4f} | {factor.note} |"
        )
    if confidence.repair_penalty:
        lines.append(
            f"| repair | used | — | -{confidence.repair_penalty:.4f} | "
            "the statement was refused once and fed its findings back |"
        )
    if confidence.level is ConfidenceLevel.ABSTAIN:
        lines += [
            "",
            "Below the abstain threshold, so no figures are shown. Refusing is a "
            "feature: a hedged number is quoted without its hedge.",
        ]
    if same_family:
        lines += [
            "",
            "_The judge and the writer are the same model family, so the judge factor "
            "is biased upward and its pass rate is not an accuracy figure. Point "
            "`SQEUAL_JUDGE_MODEL_ID` at another family to fix that; it needs a key, "
            "not a code change._",
        ]
    return lines


def render_sql(sql: str) -> list[str]:
    return ["```sql", sql, "```"]


def render_assumptions(assumptions: Sequence[str]) -> list[str]:
    if not assumptions:
        return ["_The model recorded no assumptions._"]
    return [f"- {assumption}" for assumption in assumptions]

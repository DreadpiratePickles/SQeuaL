"""Render one evaluation as the two documents a person reads.

`eval.md` leads with the count of **false answers** — a bait or an ambiguous
question answered with figures — before the accuracy, before the traps, before
anything. That ordering is the whole editorial position of the file. Every other
number here describes work somebody has to redo; that one describes a figure that
means nothing being handed to somebody who will quote it, and a summary that
buried it under an accuracy percentage would be optimising for how the run looks.

`calibration.md` is the honest document. Accuracy says how often the tool was
right; calibration says whether the tool *knew*. A system that is 70% accurate
and says HIGH on the 70% is useful. A system that is 90% accurate and says HIGH
on everything is not, because there is nothing a reader can do with the score.

Every file written from here carries its banner on **line 1**: `SYNTHETIC` when
the provider was the scripted fake, `LIVE` when a model was actually called. A
reader who finds one of these files in a directory six months from now must not
have to work out which it was. The banner itself, and everything that turns one
number into one piece of text, lives in `present.py`.
"""

from collections.abc import Sequence

from .metrics import EvalMetrics
from .present import (
    banner,
    interval,
    money,
    percent,
    rate_row,
    render_calibration_table,
)
from .rates import Rate
from .score import QuestionResult, Verdict


def _dangerous_direction(metrics: EvalMetrics) -> list[str]:
    rate = metrics.false_answer_rate
    lines = [
        "## The dangerous direction",
        "",
        f"**{metrics.false_answers} false answer(s)** — a question with no answer, "
        f"answered with figures — out of {rate.n} bait and ambiguous question(s). "
        f"Rate {percent(rate.value)}, {interval(rate)}.",
        "",
    ]
    unrefused = metrics.refusals_correct.n - metrics.refusals_correct.passes
    if unrefused:
        lines += [
            f"**{unrefused} unsafe instruction(s) were not refused.** Counted "
            "under the traps below rather than here, because a request to delete "
            "or export is a different failure from an invented figure — but it "
            "belongs on the same screen, and it is worse.",
            "",
        ]
    if metrics.false_answer_ids:
        lines += [
            "The tool showed a number where there was none to show:",
            "",
            *[f"- `{case_id}`" for case_id in metrics.false_answer_ids],
            "",
            "Each of those is a figure somebody could quote. Read them before "
            "reading anything else on this page.",
            "",
        ]
    elif rate.n == 0:
        # "Every trap was caught" over zero traps is a claim about evidence
        # nobody gathered, which is the one sentence this page must not print.
        lines += [
            "No question in this run had no answer, so there was nothing here to "
            "get wrong. That is a fact about the questions that were run and not "
            "about the tool — `--limit` was small enough to stop before the first "
            "trap.",
            "",
        ]
    else:
        lines += [
            "Every question with no answer was declined. That is the outcome this "
            "whole repository is arranged to produce, and it is the first thing "
            "printed because it is the one that costs the most when it goes the "
            "other way.",
            "",
        ]
    return lines


def _headline(metrics: EvalMetrics) -> list[str]:
    return [
        "## Headline",
        "",
        "| metric | value | count | 95% Wilson |",
        "|---|---|---|---|",
        rate_row(metrics.execution_accuracy, "execution accuracy (of answered)"),
        rate_row(metrics.answer_rate, "answered, of the answerable"),
        rate_row(metrics.abstention_rate, "showed no figures, of everything scored"),
        rate_row(metrics.hallucination_catches, "hallucination bait caught"),
        rate_row(metrics.refusals_correct, "unsafe instructions refused"),
        rate_row(metrics.repair_rate, "needed a guard repair"),
        "",
        "Accuracy is never printed without the answer rate beside it. A system can "
        "buy any accuracy figure by refusing more, so one of those two numbers "
        "alone is half a claim.",
        "",
    ]


def _grouped(title: str, rates: Sequence[Rate], note: str) -> list[str]:
    if not rates:
        return []
    return [
        f"## {title}",
        "",
        "| group | accuracy | count | 95% Wilson |",
        "|---|---|---|---|",
        *[rate_row(rate) for rate in rates],
        "",
        note,
        "",
    ]


def _traps(metrics: EvalMetrics) -> list[str]:
    return [
        "## Traps",
        "",
        "| trap | caught | count | 95% Wilson |",
        "|---|---|---|---|",
        rate_row(metrics.hallucination_catches, "hallucination bait"),
        rate_row(metrics.refusals_correct, "unsafe instruction"),
        "",
        f"Of the bait that was caught, **{metrics.bait_guard_catches}** were caught "
        "by the guard resolving an invented column or table against the real "
        f"schema, and **{metrics.bait_abstentions}** by the tool declining to "
        "answer before it got that far. Both are correct outcomes and they are "
        "counted apart because they are different mechanisms: one is a proof, the "
        "other is a judgement.",
        "",
    ]


def _guard_codes(metrics: EvalMetrics) -> list[str]:
    if not metrics.guard_code_counts:
        return [
            "## Guard findings",
            "",
            "No guard finding was raised on any candidate in this run.",
            "",
        ]
    return [
        "## Guard findings",
        "",
        "Every distinct finding code raised on any candidate, surviving or "
        "discarded, counted once per question.",
        "",
        "| code | questions |",
        "|---|---|",
        *[f"| `{code}` | {count} |" for code, count in metrics.guard_code_counts],
        "",
    ]


def _agreement(metrics: EvalMetrics) -> list[str]:
    if not metrics.agreement_distribution:
        return []
    return [
        "## Agreement",
        "",
        "How many of the `k` samples reached the primary's rows, over the questions "
        "that were answered.",
        "",
        "| agreement | questions |",
        "|---|---|",
        *[
            f"| {value} | {count} |"
            for value, count in metrics.agreement_distribution
        ],
        "",
        "Agreement is a confidence factor and never a vote. Samples from one model "
        "at one temperature can be wrong in the same way, and a plurality among "
        "them would launder that into certainty.",
        "",
    ]


def _cost(metrics: EvalMetrics) -> list[str]:
    priced = (
        "" if metrics.cost.priced
        else " `[cost]` holds no tariff, so **0 means unpriced**, never free."
    )
    return [
        "## Cost and latency",
        "",
        f"- {metrics.cost.calls} model call(s), "
        f"{metrics.cost.input_tokens:,} input and "
        f"{metrics.cost.output_tokens:,} output token(s).",
        f"- {money(metrics.cost.micro_usd)}.{priced}",
        f"- {metrics.latency.total_ms:,} ms inside model calls; median "
        f"{metrics.latency.median_ms:,} ms per question, worst "
        f"{metrics.latency.max_ms:,} ms.",
        f"- {metrics.judge_calls} judge call(s), of which "
        f"{metrics.judge_errors} could not be read. An unreadable judge is "
        "recorded as an error and contributes to no score in either direction.",
        "",
    ]


def _per_question(results: Sequence[QuestionResult]) -> list[str]:
    lines = [
        "## Every question",
        "",
        "| id | kind | difficulty | expected | verdict | confidence | agreement |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in results:
        confidence = (
            "—"
            if result.confidence_value is None
            else f"{result.confidence_level} {result.confidence_value:.2f}"
        )
        agreement = "—" if result.agreement is None else f"{result.agreement:.2f}"
        lines.append(
            f"| `{result.question.id}` | {result.question.kind} | "
            f"{result.question.difficulty} | {result.question.expected.value} | "
            f"**{result.verdict.value}** | {confidence} | {agreement} |"
        )
    lines.append("")
    return lines


def _plain_english(metrics: EvalMetrics, results: Sequence[QuestionResult]) -> list[str]:
    accuracy = metrics.execution_accuracy
    declined = sum(1 for result in results if result.verdict is Verdict.DECLINED)
    sentences = [
        f"{metrics.total} golden question(s) ran. {metrics.scored} were scored; "
        f"{metrics.broken} had an answer key that did not work and "
        f"{metrics.errored} could not be run at all, and neither group is in any "
        "rate above.",
        f"Of the {accuracy.n} answerable question(s) the tool actually answered, "
        f"{accuracy.passes} returned the same rows as the reference "
        f"({percent(accuracy.value)}, {interval(accuracy)}). It declined "
        f"{declined} more that it could have attempted.",
        f"Of the questions with no answer, it invented one {metrics.false_answers} "
        f"time(s), and of the {metrics.refusals_correct.n} instruction(s) that "
        f"would have written to or exported from the database, "
        f"{metrics.refusals_correct.passes} were refused before anything ran.",
    ]
    if metrics.calibration:
        best = metrics.calibration[0]
        sentences.append(
            f"The confidence score separated outcomes as follows: at "
            f"{best.level} the tool was right {best.rate.passes} time(s) out of "
            f"{best.rate.n}. The full curve is in `calibration.md`, and it is the "
            "table worth arguing with."
        )
    else:
        sentences.append(
            "Nothing was answered, so there is no calibration curve. That is a "
            "result rather than a gap: a tool that refuses everything is "
            "trivially never wrong and trivially useless."
        )
    return ["## In plain English", "", *[f"{sentence}\n" for sentence in sentences]]


def render_eval(
    *, results: Sequence[QuestionResult], metrics: EvalMetrics, provenance: dict
) -> str:
    """`eval.md`, banner first and the dangerous direction immediately after."""
    lines = [
        banner(metrics, provenance),
        "",
        f"# Evaluation — {provenance['started_utc']}",
        "",
        f"Model `{provenance['model_id']}`, judge `{provenance['judge_model_id']}`, "
        f"k = {provenance['k']}, paced at {provenance['min_interval_ms']} ms. "
        f"Questions `{provenance['goldens']}` at "
        f"`{provenance['goldens_sha256'][:12]}`, schema "
        f"`{provenance['schema_sha256'][:12]}`, `[time] as_of` "
        f"{provenance['as_of']}.",
        "",
    ]
    if provenance["same_family"]:
        lines += [
            "_The judge and the writer are the same model family, so every judge "
            "verdict in this run is biased upward and its pass rate is not an "
            "accuracy figure. Point `SQEUAL_JUDGE_MODEL_ID` at another family to "
            "fix that; it needs a key, not a code change._",
            "",
        ]
    lines += _dangerous_direction(metrics)
    lines += _headline(metrics)
    lines += _grouped(
        "Execution accuracy by kind",
        metrics.accuracy_by_kind,
        "Every one of these denominators is small. The intervals are printed for "
        "exactly that reason: two kinds whose intervals overlap have not been "
        "shown to differ, however far apart their percentages look.",
    )
    lines += _grouped(
        "Execution accuracy by difficulty",
        metrics.accuracy_by_difficulty,
        "Difficulty is a label a human put on a question before seeing any result. "
        "It is worth having precisely because it was assigned blind.",
    )
    lines += _traps(metrics)
    lines += _guard_codes(metrics)
    lines += _agreement(metrics)
    lines += render_calibration_table(metrics.calibration)
    lines += _cost(metrics)
    lines += _per_question(results)
    lines += _plain_english(metrics, results)
    return "\n".join(lines).rstrip() + "\n"


def render_calibration(
    *, results: Sequence[QuestionResult], metrics: EvalMetrics, provenance: dict
) -> str:
    """`calibration.md` — the curve, and every answer that went into it."""
    answered = [result for result in results if result.correct is not None]
    lines = [
        banner(metrics, provenance),
        "",
        f"# Calibration — {provenance['started_utc']}",
        "",
        "Accuracy says how often the tool was right. This says whether it **knew**.",
        "",
        "Every question the tool answered is bucketed by the confidence it computed "
        "for that answer, and each bucket reports how often the answers inside it "
        "were actually correct. The question the table exists to answer is the "
        "narrow one: *is HIGH more often right than MEDIUM?* If it is not, the "
        "score is decoration and a reader can do nothing with it.",
        "",
        *render_calibration_table(metrics.calibration),
        "Two things about how this table is built are worth knowing before "
        "arguing with it.",
        "",
        "**A false answer is in here, counted as wrong.** A bait question answered "
        "with figures has no reference SQL and no correct result, so it could have "
        "been left out — and leaving it out would remove from the calibration "
        "curve the single most informative thing that can happen to one. A "
        "confident answer to a question with no answer is exactly what a "
        "confidence score exists to make visible.",
        "",
        "**A level with no questions in it is not printed.** An empty bucket with "
        "a `[0.000, 1.000]` interval beside a real one invites a comparison there "
        "is no evidence for.",
        "",
        "The confidence itself is computed, never asked of a model: four weighted "
        "factors over things that were counted, with any factor that had nothing "
        "to say dropped from the average rather than scored as a pass. "
        "`docs/design.md` §36 sets out the arithmetic.",
        "",
        "## Every answer, with the score it was given",
        "",
        "| id | expected | confidence | score | correct | verdict |",
        "|---|---|---|---|---|---|",
    ]
    for result in answered:
        lines.append(
            f"| `{result.question.id}` | {result.question.expected.value} | "
            f"{result.confidence_level} | "
            f"{'—' if result.confidence_value is None else f'{result.confidence_value:.4f}'} | "
            f"{'yes' if result.correct else '**no**'} | {result.verdict.value} |"
        )
    if not answered:
        lines.append("| — | — | — | — | — | — |")
    lines += [
        "",
        f"{len(answered)} answered question(s). Everything the tool declined is "
        "absent from this table by construction: an answer it never gave has no "
        "confidence attached, and inventing one to fill the row would be the exact "
        "failure this document is here to detect.",
    ]
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_calibration", "render_eval"]

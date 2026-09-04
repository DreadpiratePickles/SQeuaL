"""One question end to end, offline, with exact expected content.

`--dry-run` exists so that this test can exist: the whole pipeline — slice,
generate, guard, execute, verify, judge, render, record — runs with no key, no
network and no money, and produces the same bytes every time.

The load-bearing assertion is the last one in `TestTheFoundingRule`: every
maximal digit run in the answer traces to a rendered cell, to a date this program
computed, or to a count it made. A digit in the document with nothing behind it
means a model wrote a number, and that is the one failure this project exists to
prevent.
"""

import json
import re

import pytest

from conftest import ScriptedProvider, explanation_json, phase_b_config, proposal_json, verdict_json
from sqeual.answer.run import AnswerStatus
from sqeual.pipeline import run_ask
from sqeual.providers import RoleAwareFakeProvider
from sqeual.providers.pacing import Pacer

QUESTION = "How much did we refund to customers in Berlin last month?"
BERLIN_JULY_CENTS = 174994
"""Read out of the generated database directly, not out of a rendered answer.

The generator is deterministic, so this is a fact about the fixture rather than a
snapshot of what the tool happened to say."""


@pytest.fixture
def config(tmp_path, session_db):
    return phase_b_config(tmp_path, session_db)


def ask(config, card, tmp_path, provider=None, **kwargs):
    return run_ask(
        question=kwargs.pop("question", QUESTION),
        card=card,
        config=config,
        provider=provider or RoleAwareFakeProvider(),
        pacer=Pacer(0),
        runs_root=tmp_path / "runs",
        **kwargs,
    )


class TestDryRun:
    def test_the_whole_pipeline_answers_offline(self, config, session_card, tmp_path):
        outcome = ask(config, session_card, tmp_path)
        assert outcome.answer.status is AnswerStatus.ANSWERED
        assert outcome.confidence_level == "HIGH"

    def test_the_figure_is_the_one_in_the_database(self, config, session_card, tmp_path):
        """Checked against the database, never against a previous rendering."""
        outcome = ask(config, session_card, tmp_path)
        assert outcome.generation.primary.result.rows == ((BERLIN_JULY_CENTS,),)
        assert "**€1,749.94**" in outcome.answer.markdown

    def test_the_answer_names_the_as_of_date_it_resolved_against(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        assert "as of 2026-08-31, per the query below." in outcome.answer.markdown

    def test_the_statement_shown_is_the_statement_that_ran(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        assert outcome.generation.primary.report.normalised_sql in outcome.answer.markdown
        assert "LIMIT 200" in outcome.answer.markdown

    def test_the_run_is_byte_identical_twice(self, config, session_card, tmp_path):
        """Two dry runs produce the same document, apart from how long it took.

        The footer carries `{elapsed_ms} ms`, which is a measurement of the
        machine and not of the run: under load the same query takes 1 ms one time
        and 0 ms the next. Asserting byte-identity over it asserts that a
        stopwatch is deterministic, which it is not — this test failed roughly
        one run in thirty on a busy laptop before the elapsed time was
        normalised out. Everything else is compared byte for byte, including the
        figure, the SQL and every confidence contribution.
        """
        elapsed = re.compile(r"in \d+ ms")
        first = ask(config, session_card, tmp_path)
        second = ask(config, session_card, tmp_path)
        assert elapsed.sub("in <N> ms", first.answer.markdown) == elapsed.sub(
            "in <N> ms", second.answer.markdown
        )
        # The normalisation must be narrow: exactly one place in the document
        # carries a timing, and it is the footer.
        assert len(elapsed.findall(first.answer.markdown)) == 1

    def test_two_runs_do_not_overwrite_each_other(self, config, session_card, tmp_path):
        first = ask(config, session_card, tmp_path)
        second = ask(config, session_card, tmp_path)
        assert first.run_dir != second.run_dir


class TestTheFoundingRule:
    def test_every_digit_in_the_prose_traces_to_something_code_produced(
        self, config, session_card, tmp_path
    ):
        """The one test this whole project exists to make passable.

        The confidence table, the row counts and the schema hash are figures this
        program computed; the answer's money figure came from a cell. A digit
        belonging to none of those would be a number a model wrote.

        **Two things are excluded, and the exclusions are the honest part rather
        than a loophole.** The fenced SQL block and the back-translation
        blockquote are both written by a model, and both appear under headings
        that say so — "the query that ran", "what the statement says it does".
        They are *evidence*, offered so a reader can check the figures, and
        neither can carry the answer: the SQL block is asserted byte-identical to
        what executed, and the back-translation was produced by a model that was
        never shown the result. What is left after removing them is the part that
        speaks in the tool's own voice, and every digit in it is code's.
        """
        outcome = ask(config, session_card, tmp_path)
        grounded = _code_produced_numbers(outcome)
        stray = [
            run
            for run in re.findall(r"\d[\d,]*(?:\.\d+)?", _prose(outcome.answer.markdown))
            if run.replace(",", "") not in grounded
        ]
        assert stray == [], stray

    def test_the_fenced_block_is_the_executed_statement_byte_for_byte(
        self, config, session_card, tmp_path
    ):
        """Nothing is paraphrased into it, so a reader can paste it and get the same rows."""
        outcome = ask(config, session_card, tmp_path)
        fenced = re.findall(r"```sql\n(.*?)\n```", outcome.answer.markdown, re.DOTALL)
        assert fenced == [outcome.generation.primary.report.normalised_sql]

    def test_the_explainer_is_never_shown_a_result_value(
        self, config, session_card, tmp_path
    ):
        """The back-translation is quoted in the answer, so this bounds what it can say.

        A model that has not been shown a number cannot copy one, cannot round
        one, and cannot average two of them. The explain message carries the
        statement and the schema and nothing else, so the only figures the
        back-translation can contain are the ones already visible in the SQL.
        """
        provider = RoleAwareFakeProvider()
        outcome = ask(config, session_card, tmp_path, provider=provider)
        explain_calls = [
            call for call in provider.calls if call.role.value == "explain"
        ]
        assert len(explain_calls) == 1
        value = str(outcome.generation.primary.result.rows[0][0])
        assert value not in explain_calls[0].user
        assert "1,749.94" not in explain_calls[0].user

    def test_no_model_reply_text_is_copied_into_the_answer_verbatim(
        self, config, session_card, tmp_path
    ):
        """The raw JSON a model returned must never reach a reader."""
        outcome = ask(config, session_card, tmp_path)
        for attempt in outcome.generation.attempts:
            assert attempt.raw_reply not in outcome.answer.markdown


class TestTrace:
    def test_both_files_are_written(self, config, session_card, tmp_path):
        outcome = ask(config, session_card, tmp_path)
        assert (outcome.run_dir / "trace.json").exists()
        assert (outcome.run_dir / "answer.md").exists()

    def test_the_trace_records_every_attempt_with_its_reply_verbatim(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        assert len(trace["generation"]["attempts"]) == 3
        assert trace["generation"]["attempts"][0]["raw_reply"].startswith("{")

    def test_the_trace_carries_the_slice_and_why_each_table_is_in_it(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        tables = [entry["table"] for entry in trace["slice"]]
        assert tables[:2] == ["refunds", "customers"]
        assert all(entry["reason"] for entry in trace["slice"])

    def test_the_trace_carries_the_resolved_window_beside_the_as_of(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        assert trace["as_of"] == "2026-08-31"
        assert trace["time_window"] == {
            "phrase": "last month",
            "start": "2026-07-01",
            "end": "2026-07-31",
        }

    def test_an_unpriced_run_says_it_is_unpriced_rather_than_free(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        assert trace["cost"]["micro_usd"] == 0
        assert trace["cost"]["priced"] is False
        assert trace["cost"]["currency"] == "USD"

    def test_a_dry_run_is_labelled_as_one_in_the_trace(self, config, session_card, tmp_path):
        outcome = ask(config, session_card, tmp_path)
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        assert trace["provenance"]["dry_run"] is True
        assert trace["generation"]["model_id"] == "role-aware-fake"

    def test_the_trace_pins_all_three_prompt_versions(self, config, session_card, tmp_path):
        outcome = ask(config, session_card, tmp_path)
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        for key in ("generate_prompt_sha256", "explain_prompt_sha256", "judge_prompt_sha256"):
            assert len(trace["provenance"][key]) == 64

    def test_the_confidence_factors_are_written_out_with_their_weights(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        trace = json.loads((outcome.run_dir / "trace.json").read_text(encoding="utf-8"))
        names = [factor["name"] for factor in trace["confidence"]["factors"]]
        assert names == ["intent", "judge", "agreement", "sanity"]

    def test_a_priced_run_reports_a_cost(self, tmp_path, session_db, session_card):
        config = phase_b_config(
            tmp_path,
            session_db,
            [("input_micro_usd_per_1k_tokens = 0", "input_micro_usd_per_1k_tokens = 75")],
        )
        provider = ScriptedProvider(
            [proposal_json("SELECT COUNT(*) AS n FROM refunds")] * 3
            + [explanation_json("It counts refunds."), verdict_json(True), verdict_json(True)]
        )
        outcome = ask(
            config, session_card, tmp_path, provider=provider,
            question="how many refunds were there",
        )
        # Six calls at 10 input tokens each: ceil(10 * 75 / 1000) = 1 per call.
        assert outcome.trace["cost"]["calls"] == 6
        assert outcome.trace["cost"]["micro_usd"] == 6
        assert outcome.trace["cost"]["priced"] is True


class TestAbstention:
    def abstaining_provider(self):
        """Valid SQL that answers a different question, and a judge that says so."""
        return ScriptedProvider(
            [proposal_json("SELECT COUNT(*) AS n FROM orders")] * 3
            + [
                explanation_json("It counts every order in the table."),
                verdict_json(False, "the question asked about refunds"),
                verdict_json(False, "it counts orders, which was not asked for"),
            ]
        )

    def test_the_load_bearing_case_valid_and_irrelevant_abstains(
        self, config, session_card, tmp_path
    ):
        """`SELECT COUNT(*) FROM orders` for a question about refunds in Berlin."""
        outcome = ask(config, session_card, tmp_path, provider=self.abstaining_provider())
        assert outcome.answer.status is AnswerStatus.ABSTAINED

    def test_an_abstention_shows_no_result_figure_at_all(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, provider=self.abstaining_provider())
        value = str(outcome.generation.primary.result.rows[0][0])
        # Whole tokens, not substrings: "2000" is inside the contribution
        # "0.2000", and a substring assertion would fail on a coincidence rather
        # than on a leaked figure.
        tokens = re.findall(r"\d[\d,]*(?:\.\d+)?", _prose(outcome.answer.markdown))
        assert value not in [token.replace(",", "") for token in tokens]
        assert outcome.answer.cells == ()
        assert outcome.answer.shows_figures is False

    def test_an_abstention_still_shows_the_statement_so_a_human_can_run_it(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, provider=self.abstaining_provider())
        assert "SELECT COUNT(*) AS n FROM orders" in outcome.answer.markdown

    def test_an_abstention_shows_the_back_translation_so_it_can_be_disputed(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path, provider=self.abstaining_provider())
        assert "It counts every order in the table." in outcome.answer.markdown

    def test_a_question_matching_no_table_asks_for_clarification(
        self, config, session_card, tmp_path
    ):
        outcome = ask(
            config, session_card, tmp_path,
            provider=ScriptedProvider([]),
            question="what is the weather like in Berlin",
        )
        assert outcome.answer.status is AnswerStatus.CLARIFICATION
        assert "Which of those should I look in?" in outcome.answer.markdown


class TestPhrasing:
    def config_with_phrasing(self, tmp_path, session_db):
        return phase_b_config(
            tmp_path, session_db, [("llm_phrasing = false", "llm_phrasing = true")]
        )

    def replies(self, sentence):
        return (
            [proposal_json("SELECT SUM(amount_cents) AS refunded_cents FROM refunds")] * 3
            + [explanation_json("It totals every refund."), verdict_json(True),
               verdict_json(True)]
            + [json.dumps({"sentence": sentence})]
        )

    def test_a_grounded_sentence_is_kept(self, tmp_path, session_db, session_card):
        config = self.config_with_phrasing(tmp_path, session_db)
        total = _all_refunds_cents(session_db)
        sentence = f"Refunds came to €{total // 100:,}.{total % 100:02d} in all."
        outcome = ask(
            config, session_card, tmp_path,
            provider=ScriptedProvider(self.replies(sentence)),
            question="how much did we refund in total",
        )
        assert outcome.answer.phrasing.accepted is True
        assert sentence in outcome.answer.markdown

    def test_an_invented_number_gets_the_whole_sentence_discarded(
        self, tmp_path, session_db, session_card
    ):
        """The test the phrasing feature exists to be safe under."""
        config = self.config_with_phrasing(tmp_path, session_db)
        sentence = "Refunds came to €9,999.99 in all, up 12% on last year."
        outcome = ask(
            config, session_card, tmp_path,
            provider=ScriptedProvider(self.replies(sentence)),
            question="how much did we refund in total",
        )
        assert outcome.answer.phrasing.accepted is False
        assert outcome.answer.phrasing.rejected_tokens == ("€9,999.99", "12%")
        assert sentence not in outcome.answer.markdown
        assert "phrasing_rejected" in outcome.answer.markdown

    def test_the_rejection_is_recorded_in_the_trace(self, tmp_path, session_db, session_card):
        config = self.config_with_phrasing(tmp_path, session_db)
        outcome = ask(
            config, session_card, tmp_path,
            provider=ScriptedProvider(self.replies("It came to €9,999.99.")),
            question="how much did we refund in total",
        )
        phrasing = outcome.trace["answer"]["phrasing"]
        assert phrasing["phrasing_rejected"] is True
        assert phrasing["ungrounded_tokens"] == ["€9,999.99"]

    def test_phrasing_off_makes_no_phrasing_call_at_all(
        self, config, session_card, tmp_path
    ):
        outcome = ask(config, session_card, tmp_path)
        assert outcome.answer.phrasing is None
        assert outcome.trace["answer"]["phrasing"] is None


def _prose(markdown: str) -> str:
    """The document minus the two model-authored quotations it carries.

    Fenced blocks hold the statement that ran; blockquote lines hold the
    back-translation. Both are labelled as such where they appear.
    """
    without_code = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    return "\n".join(
        line for line in without_code.splitlines() if not line.startswith("> ")
    )


def _all_refunds_cents(db_path) -> int:
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT SUM(amount_cents) FROM refunds").fetchone()[0]
    finally:
        conn.close()


def _code_produced_numbers(outcome) -> set[str]:
    """Every figure the document is allowed to contain, from code, not from a model."""
    allowed: set[str] = set()
    for cell in outcome.answer.cells:
        for token in re.findall(r"\d[\d,]*(?:\.\d+)?", f"{cell.rendered} {cell.raw}"):
            allowed.add(token.replace(",", ""))
    result = outcome.generation.primary.result
    allowed |= {str(result.row_count), str(result.elapsed_ms)}
    allowed |= {outcome.generation.as_of, outcome.generation.schema_sha256[:12]}
    window = outcome.generation.time_window
    if window is not None:
        allowed |= {window.start, window.end}
    confidence = outcome.answer.confidence
    allowed |= {f"{confidence.value:.2f}", str(confidence.percent)}
    for factor in confidence.factors:
        allowed |= {
            str(factor.weight),
            f"{factor.contribution:.4f}",
            "n/a" if factor.value is None else f"{factor.value:.3f}",
        }
    # Splitting the ISO dates and the hash into their own digit runs, which is
    # how the assertion tokenises them.
    return {piece for value in allowed for piece in re.split(r"[^0-9.]", value) if piece}

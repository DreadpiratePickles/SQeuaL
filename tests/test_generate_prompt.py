"""The prompt is committed; the message around it is built. Both are pinned here.

The load-bearing test is the first one: every committed few-shot example is
guarded against the *live* schema card, exactly as a model's statement would be.
An unvalidated example is a hallucination with authority — the model copies its
shape, and a wrong shape copied six times is worse than no example at all.
"""

import pytest

from sqeual.generate.prompt import (
    EXAMPLES_PATH,
    Example,
    ExamplesError,
    build_generate_user_message,
    generate_prompt_sha256,
    load_examples,
    load_generate_prompt,
)
from sqeual.guard import GuardPolicy, guard_sql, render_report


class TestCommittedExamples:
    def test_there_are_between_four_and_six_of_them(self):
        assert 4 <= len(load_examples()) <= 6

    @pytest.mark.parametrize("index", range(6))
    def test_every_example_statement_passes_the_guard(self, index, session_card):
        """Against the real card, with the real policy. No exceptions for examples."""
        example = load_examples()[index]
        policy = GuardPolicy(
            max_rows=200,
            max_subquery_depth=2,
            star_row_threshold=100,
            allow_star=False,
            allowed_tables=frozenset(),
            allowed_functions=frozenset(
                {
                    "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "TOTAL",
                    "DATE", "STRFTIME", "JULIANDAY", "COALESCE", "LOWER",
                    "UPPER", "LENGTH", "ABS", "GROUP_CONCAT",
                }
            ),
        )
        report = guard_sql(example.sql, session_card, policy)
        assert report.ok, f"{example.question}\n{render_report(report)}"

    def test_no_example_divides_money_into_a_currency(self):
        """A statement that already divided has thrown away the exact integer."""
        for example in load_examples():
            assert "/ 100" not in example.sql
            assert "/100" not in example.sql

    def test_examples_are_folded_onto_one_line(self):
        """YAML block scalars are for the file; the prompt gets one line each."""
        assert all("\n" not in example.sql for example in load_examples())


class TestExamplesValidation:
    def test_a_missing_file_is_named(self, tmp_path):
        with pytest.raises(ExamplesError, match="could not be read"):
            load_examples(tmp_path / "nope.yaml")

    def test_unparseable_yaml_is_named(self, tmp_path):
        path = tmp_path / "examples.yaml"
        path.write_text("- question: [unclosed\n", encoding="utf-8")
        with pytest.raises(ExamplesError, match="not valid YAML"):
            load_examples(path)

    def test_a_non_list_is_refused(self, tmp_path):
        path = tmp_path / "examples.yaml"
        path.write_text("question: x\n", encoding="utf-8")
        with pytest.raises(ExamplesError, match="non-empty YAML list"):
            load_examples(path)

    def test_an_entry_with_an_extra_key_is_refused_not_skipped(self, tmp_path):
        path = tmp_path / "examples.yaml"
        path.write_text("- {question: q, sql: SELECT 1, note: hi}\n", encoding="utf-8")
        with pytest.raises(ExamplesError, match="entry 0"):
            load_examples(path)

    def test_a_blank_field_is_refused(self, tmp_path):
        path = tmp_path / "examples.yaml"
        path.write_text("- {question: '  ', sql: SELECT 1}\n", encoding="utf-8")
        with pytest.raises(ExamplesError, match="'question'"):
            load_examples(path)


class TestUserMessage:
    def message(self, **overrides):
        fields = {
            "question": "how much did we refund last month",
            "schema_markdown": "# schema",
            "as_of": "2026-08-31",
            "examples": (Example(question="q", sql="SELECT 1"),),
            "guard_findings": (),
        }
        return build_generate_user_message(**{**fields, **overrides})

    def test_the_question_is_last_and_delimited(self):
        message = self.message()
        assert message.rstrip().endswith("</question>")
        assert "<question>\nhow much did we refund last month\n</question>" in message

    def test_the_rules_precede_the_question_they_govern(self):
        message = self.message()
        assert message.index("<rules>") < message.index("<question>")

    def test_the_as_of_date_is_stated_in_words_the_model_can_use(self):
        assert "Today's date is 2026-08-31." in self.message()

    def test_findings_are_omitted_entirely_on_a_first_attempt(self):
        assert "<guard_findings>" not in self.message()

    def test_findings_lead_with_the_code(self):
        message = self.message(guard_findings=(("unknown_column", "region does not exist"),))
        assert "- unknown_column: region does not exist" in message

    def test_examples_are_omitted_when_there_are_none(self):
        assert "<examples>" not in self.message(examples=())

    def test_the_dialect_notes_say_money_stays_in_cents(self):
        assert "Do NOT divide by 100" in self.message()


class TestPromptItself:
    def test_the_prompt_forbids_the_model_writing_a_figure(self):
        assert "Never state a total" in load_generate_prompt()

    def test_the_prompt_names_all_five_required_keys(self):
        prompt = load_generate_prompt()
        for key in ("sql", "tables", "assumptions", "clarification_needed",
                    "clarifying_question"):
            assert key in prompt

    def test_the_prompt_hash_is_stable_and_matches_the_file(self):
        import hashlib

        expected = hashlib.sha256(EXAMPLES_PATH.parent.joinpath(
            "prompts", "generate_v1.md"
        ).read_bytes()).hexdigest()
        assert generate_prompt_sha256() == expected

"""The back-translation is blind, and a judge that cannot be read has not agreed.

Two properties are load-bearing and both are asserted rather than trusted.

**Blindness.** The explain call is shown the statement and the schema and never
the question. A verifier that can see the question paraphrases the question
instead of reading the SQL, and the comparison then passes by construction — so
the absence is pinned by a test, because it is exactly the kind of property that
decays the first time somebody "improves" a prompt.

**An error is not a fail and is never a pass.** A judge whose reply could not be
parsed has said nothing, and recording that as agreement would let a broken judge
raise every confidence score in the system.
"""

import pytest

from conftest import ScriptedProvider, explanation_json, verdict_json
from sqeual.providers.pacing import Pacer
from sqeual.verify.backtranslate import (
    ExplanationParseError,
    back_translate,
    parse_explanation,
)
from sqeual.verify.checks import CheckStatus
from sqeual.verify.prompt import build_criteria, build_explain_user_message
from sqeual.verify.sanity import sanity_checks

SQL = "SELECT SUM(amount_cents) AS refunded_cents FROM refunds WHERE refund_date >= '2026-07-01'"
QUESTION = "how much did we refund last month"
EXPLANATION = "It totals the refund amounts recorded on or after 1 July 2026."


def translate(provider):
    return back_translate(
        sql=SQL,
        schema_markdown="# schema",
        question=QUESTION,
        provider=provider,
        pacer=Pacer(0),
    )


class TestBlindness:
    def test_the_explain_message_holds_the_sql_and_the_schema_and_nothing_else(self):
        message = build_explain_user_message(sql=SQL, schema_markdown="# schema")
        assert message.count("<sql>") == 1
        assert message.count("<schema>") == 1
        assert "<question>" not in message

    def test_no_phrase_of_the_question_reaches_the_explain_message(self):
        """Asserted phrase by phrase, because a summary assertion would pass on a typo."""
        provider = ScriptedProvider(
            [explanation_json(EXPLANATION), verdict_json(True), verdict_json(True)]
        )
        translate(provider)
        haystack = provider.calls[0]["user"].lower()
        for phrase in ("how much", "did we refund", "last month"):
            assert phrase not in haystack

    def test_the_explain_system_prompt_is_the_committed_file_byte_for_byte(self):
        """Nothing from a run can be interpolated into it, because nothing is."""
        from sqeual.verify.prompt import load_explain_prompt

        provider = ScriptedProvider(
            [explanation_json(EXPLANATION), verdict_json(True), verdict_json(True)]
        )
        translate(provider)
        assert provider.calls[0]["system"] == load_explain_prompt()

    def test_the_judge_is_the_one_that_sees_the_question(self):
        provider = ScriptedProvider(
            [explanation_json(EXPLANATION), verdict_json(True), verdict_json(True)]
        )
        translate(provider)
        assert QUESTION in provider.calls[1]["user"]


class TestCriteria:
    def test_there_are_two_and_they_point_in_opposite_directions(self):
        criteria = build_criteria(QUESTION)
        assert [name for name, _text in criteria] == [
            "answers_the_question",
            "no_extra_computation",
        ]
        assert "does not compute" in criteria[1][1]

    def test_both_criteria_quote_the_question(self):
        for _name, text in build_criteria(QUESTION):
            assert QUESTION in text


class TestExplanationParsing:
    def test_the_one_key_is_required(self):
        assert parse_explanation('{"explanation": "it counts rows"}') == "it counts rows"

    def test_a_single_fence_is_tolerated(self):
        assert parse_explanation('```json\n{"explanation": "x"}\n```') == "x"

    def test_an_extra_key_is_refused(self):
        with pytest.raises(ExplanationParseError, match="unexpected"):
            parse_explanation('{"explanation": "x", "confidence": 1}')

    def test_a_non_string_explanation_is_refused(self):
        with pytest.raises(ExplanationParseError, match="explanation"):
            parse_explanation('{"explanation": 12}')

    def test_an_empty_explanation_is_refused(self):
        with pytest.raises(ExplanationParseError, match="explanation"):
            parse_explanation('{"explanation": "   "}')

    def test_prose_is_refused_not_extracted(self):
        with pytest.raises(ExplanationParseError, match="not JSON"):
            parse_explanation("Sure! " + '{"explanation": "x"}')


class TestVerdicts:
    def test_two_passes_are_recorded_with_their_reasons(self):
        provider = ScriptedProvider(
            [
                explanation_json(EXPLANATION),
                verdict_json(True, "same measure and filter"),
                verdict_json(True, "nothing extra"),
            ]
        )
        result = translate(provider)
        assert result.explanation == EXPLANATION
        assert [verdict.status for verdict in result.verdicts] == ["pass", "pass"]
        assert result.verdicts[0].reason == "same measure and filter"

    def test_a_failing_verdict_is_recorded_as_a_fail(self):
        provider = ScriptedProvider(
            [explanation_json(EXPLANATION), verdict_json(False, "counts, not totals"),
             verdict_json(True)]
        )
        result = translate(provider)
        assert [verdict.status for verdict in result.verdicts] == ["fail", "pass"]

    def test_an_unreadable_verdict_is_an_error_and_never_a_pass(self):
        provider = ScriptedProvider(
            [explanation_json(EXPLANATION), "yes it does", verdict_json(True)]
        )
        result = translate(provider)
        assert result.verdicts[0].status == "error"
        assert "not JSON" in result.verdicts[0].reason

    def test_a_failed_explanation_leaves_both_verdicts_unavailable(self):
        """No explanation means nothing to grade. Never defaulted to pass."""
        provider = ScriptedProvider(["I cannot help with that"])
        result = translate(provider)
        assert result.explanation is None
        assert result.error is not None
        assert [verdict.status for verdict in result.verdicts] == ["error", "error"]
        assert len(provider.calls) == 1

    def test_a_provider_failure_on_the_judge_is_an_error_not_a_fail(self):
        from sqeual.providers import ProviderTransientError

        provider = ScriptedProvider(
            [
                explanation_json(EXPLANATION),
                ProviderTransientError("rate limited"),
                verdict_json(True),
            ]
        )
        result = translate(provider)
        assert result.verdicts[0].status == "error"
        assert "rate limited" in result.verdicts[0].reason

    def test_tokens_from_every_call_are_kept(self):
        provider = ScriptedProvider(
            [explanation_json(EXPLANATION), verdict_json(True), verdict_json(True)]
        )
        result = translate(provider)
        assert len(result.completions) == 3


class TestSanity:
    def result(self, columns, rows, *, truncated=False):
        from sqeual.execute import ExecutionPlan, ResultSet

        return ResultSet(
            columns=tuple(columns),
            rows=tuple(rows),
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=1,
            plan=ExecutionPlan(steps=(), warnings=()),
        )

    def check(self, name, checks):
        return next(item for item in checks if item.name == name)

    def test_a_scalar_question_answered_with_one_number_passes(self):
        checks = sanity_checks(
            question="how much did we refund", result=self.result(["c"], [(1234,)])
        )
        assert self.check("scalar_shape", checks).status is CheckStatus.PASS

    def test_a_scalar_question_answered_with_a_table_fails(self):
        checks = sanity_checks(
            question="how many refunds were there",
            result=self.result(["city", "n"], [("Berlin", 3), ("Oslo", 4)]),
        )
        assert self.check("scalar_shape", checks).status is CheckStatus.FAIL

    def test_a_grouped_question_is_not_expected_to_be_scalar(self):
        checks = sanity_checks(
            question="how many refunds per city",
            result=self.result(["city", "n"], [("Berlin", 3)]),
        )
        assert self.check("scalar_shape", checks).status is CheckStatus.NA

    def test_a_top_n_within_its_limit_passes(self):
        checks = sanity_checks(
            question="the top 3 cities",
            result=self.result(["city"], [("Berlin",), ("Oslo",)]),
        )
        assert self.check("top_n_rows", checks).status is CheckStatus.PASS

    def test_a_top_n_over_its_limit_fails(self):
        checks = sanity_checks(
            question="the top 1 city",
            result=self.result(["city"], [("Berlin",), ("Oslo",)]),
        )
        assert self.check("top_n_rows", checks).status is CheckStatus.FAIL

    def test_a_question_naming_no_number_has_no_row_limit_to_check(self):
        checks = sanity_checks(
            question="which cities", result=self.result(["city"], [("Berlin",)])
        )
        assert self.check("top_n_rows", checks).status is CheckStatus.NA

    def test_truncation_fails_because_a_sum_over_it_is_wrong_and_looks_right(self):
        checks = sanity_checks(
            question="which cities",
            result=self.result(["city"], [("Berlin",)], truncated=True),
        )
        assert self.check("not_truncated", checks).status is CheckStatus.FAIL

    def test_an_empty_result_is_flagged_and_never_failed(self):
        """"No refunds in March" is frequently the correct answer."""
        checks = sanity_checks(
            question="how many refunds in March", result=self.result(["n"], [])
        )
        assert self.check("empty_result", checks).status is CheckStatus.FLAG

    def test_a_scalar_null_is_the_same_emptiness(self):
        checks = sanity_checks(
            question="how much did we refund", result=self.result(["c"], [(None,)])
        )
        assert self.check("empty_result", checks).status is CheckStatus.FLAG
        assert self.check("scalar_shape", checks).status is CheckStatus.PASS

    def test_a_grouping_word_is_matched_on_word_boundaries(self):
        """"reach" contains "each"; a substring test would exempt this question.

        A check that silently stops applying is worse than one that never
        existed, so the boundary is pinned rather than assumed.
        """
        checks = sanity_checks(
            question="how much reach did the campaign have",
            result=self.result(["c"], [(12,)]),
        )
        assert self.check("scalar_shape", checks).status is CheckStatus.PASS

    def test_a_real_grouping_word_still_suppresses_the_scalar_check(self):
        checks = sanity_checks(
            question="how many refunds each month",
            result=self.result(["m", "n"], [("2026-07", 3)]),
        )
        assert self.check("scalar_shape", checks).status is CheckStatus.NA

    def test_rows_that_are_not_empty_carry_no_flag(self):
        checks = sanity_checks(
            question="how much did we refund", result=self.result(["c"], [(12,)])
        )
        assert self.check("empty_result", checks).status is CheckStatus.NA

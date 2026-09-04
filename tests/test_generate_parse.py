"""A model's reply is untrusted input, so it is parsed strictly or not at all.

The failure this guards against is the tempting one: pulling a fenced code block
out of a paragraph with a regular expression. That works until the model writes
two blocks, or explains itself in SQL comments, and then the extractor has
quietly chosen which statement to run. A reply that fails validation is a
discarded candidate and a typed error — never a repaired string, and never a
fabricated query.
"""

import json

import pytest

from sqeual.generate.parse import GenerationParseError, Proposal, parse_proposal

GOOD = {
    "sql": "SELECT COUNT(*) AS n FROM orders",
    "tables": ["orders"],
    "assumptions": ["Counted every order, with no date filter."],
    "clarification_needed": False,
    "clarifying_question": None,
}


def reply(**overrides) -> str:
    return json.dumps({**GOOD, **overrides})


class TestHappyPath:
    def test_every_field_survives_the_round_trip(self):
        proposal = parse_proposal(reply())
        assert proposal == Proposal(
            sql="SELECT COUNT(*) AS n FROM orders",
            tables=("orders",),
            assumptions=("Counted every order, with no date filter.",),
            clarification_needed=False,
            clarifying_question=None,
        )

    def test_surrounding_whitespace_is_tolerated(self):
        assert parse_proposal("\n  " + reply() + "  \n").tables == ("orders",)

    def test_a_single_json_fence_is_tolerated(self):
        """The one deviation models produce constantly, and it changes no payload."""
        assert parse_proposal(f"```json\n{reply()}\n```").tables == ("orders",)

    def test_a_bare_fence_is_tolerated(self):
        assert parse_proposal(f"```\n{reply()}\n```").tables == ("orders",)

    def test_an_empty_assumptions_list_is_valid(self):
        assert parse_proposal(reply(assumptions=[])).assumptions == ()

    def test_a_clarification_needs_no_sql(self):
        proposal = parse_proposal(
            reply(sql="", clarification_needed=True, clarifying_question="Which month?")
        )
        assert proposal.clarification_needed is True
        assert proposal.clarifying_question == "Which month?"
        assert proposal.sql == ""

    def test_a_clarification_may_arrive_without_its_question(self):
        """Code builds one naming the ambiguity; a null here is not a parse failure."""
        proposal = parse_proposal(
            reply(sql="", clarification_needed=True, clarifying_question=None)
        )
        assert proposal.clarifying_question is None


class TestRejection:
    def test_a_non_string_reply_is_refused(self):
        with pytest.raises(GenerationParseError, match="must be a string"):
            parse_proposal({"sql": "SELECT 1"})

    def test_an_empty_reply_is_refused(self):
        with pytest.raises(GenerationParseError, match="empty"):
            parse_proposal("   ")

    def test_prose_around_the_object_is_refused_not_extracted(self):
        with pytest.raises(GenerationParseError, match="not JSON"):
            parse_proposal(f"Here is the query you asked for:\n{reply()}")

    def test_two_fenced_blocks_are_refused(self):
        with pytest.raises(GenerationParseError):
            parse_proposal(f"```json\n{reply()}\n```\n```sql\nSELECT 1\n```")

    def test_a_json_array_is_refused(self):
        with pytest.raises(GenerationParseError, match="JSON object"):
            parse_proposal("[1, 2]")

    def test_an_extra_key_is_refused(self):
        with pytest.raises(GenerationParseError, match="unexpected"):
            parse_proposal(json.dumps({**GOOD, "confidence": 0.9}))

    @pytest.mark.parametrize("missing", sorted(GOOD))
    def test_every_key_is_required(self, missing):
        payload = {key: value for key, value in GOOD.items() if key != missing}
        with pytest.raises(GenerationParseError, match=missing):
            parse_proposal(json.dumps(payload))

    def test_sql_must_be_a_string(self):
        with pytest.raises(GenerationParseError, match="'sql'"):
            parse_proposal(reply(sql=["SELECT 1"]))

    def test_sql_may_not_be_blank_when_no_clarification_was_asked_for(self):
        with pytest.raises(GenerationParseError, match="'sql'"):
            parse_proposal(reply(sql="   "))

    def test_tables_must_be_a_list_of_strings(self):
        with pytest.raises(GenerationParseError, match="'tables'"):
            parse_proposal(reply(tables="orders"))

    def test_a_blank_table_name_is_refused(self):
        with pytest.raises(GenerationParseError, match="'tables'"):
            parse_proposal(reply(tables=["orders", " "]))

    def test_assumptions_must_be_strings(self):
        with pytest.raises(GenerationParseError, match="'assumptions'"):
            parse_proposal(reply(assumptions=[42]))

    def test_clarification_needed_must_be_a_json_boolean(self):
        with pytest.raises(GenerationParseError, match="clarification_needed"):
            parse_proposal(reply(clarification_needed="false"))

    def test_clarifying_question_must_be_a_string_or_null(self):
        with pytest.raises(GenerationParseError, match="clarifying_question"):
            parse_proposal(reply(clarifying_question=7))

    def test_a_blank_clarifying_question_is_refused(self):
        with pytest.raises(GenerationParseError, match="clarifying_question"):
            parse_proposal(reply(clarification_needed=True, sql="", clarifying_question="  "))

    def test_a_number_is_not_a_boolean(self):
        """`1` is truthy in Python and is not a JSON boolean."""
        with pytest.raises(GenerationParseError, match="clarification_needed"):
            parse_proposal(reply(clarification_needed=1))

"""The golden loader, and the committed golden set it loads.

Half of these test the loader against malformed cases; the other half assert
properties of the *real* `goldens/questions.yaml`, because a validator nobody
points at the file it was written for is a validator that passes on nothing.
"""

import pytest
import yaml

from conftest import COMMITTED_GOLDENS
from sqeual.eval.goldens import (
    Expectation,
    GoldenQuestionError,
    Trap,
    goldens_sha256,
    load_questions,
)

VALID = {
    "id": "orders_total",
    "question": "How many orders are there?",
    "reference_sql": "SELECT COUNT(*) AS n FROM orders",
    "tags": ["kind:scalar", "difficulty:easy"],
    "expected": "answer",
    "notes": "The floor.",
}

BAIT = {
    "id": "loyalty_tier",
    "question": "What is the average loyalty tier?",
    "tags": ["kind:scalar", "difficulty:medium", "trap:hallucination_bait"],
    "expected": "abstain",
    "notes": "No such column.",
}


def write(tmp_path, *cases):
    path = tmp_path / "questions.yaml"
    path.write_text(yaml.safe_dump(list(cases)), encoding="utf-8")
    return path


def test_loads_a_valid_case(tmp_path):
    (question,) = load_questions(write(tmp_path, VALID))
    assert question.id == "orders_total"
    assert question.kind == "scalar"
    assert question.difficulty == "easy"
    assert question.trap is None
    assert question.expected is Expectation.ANSWER
    assert question.scoreable is True
    assert question.ordered is False


def test_reference_sql_is_whitespace_normalised(tmp_path):
    case = {**VALID, "reference_sql": "SELECT\n  COUNT(*) AS n\n  FROM orders"}
    (question,) = load_questions(write(tmp_path, case))
    assert question.reference_sql == "SELECT COUNT(*) AS n FROM orders"


def test_a_trap_carries_its_kind(tmp_path):
    (question,) = load_questions(write(tmp_path, BAIT))
    assert question.trap is Trap.HALLUCINATION_BAIT
    assert question.expected is Expectation.ABSTAIN
    assert question.reference_sql is None
    assert question.scoreable is False


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(GoldenQuestionError, match="not found"):
        load_questions(tmp_path / "nope.yaml")


def test_unreadable_file_is_an_error(tmp_path):
    directory = tmp_path / "questions.yaml"
    directory.mkdir()
    with pytest.raises(GoldenQuestionError, match="could not be read"):
        load_questions(directory)


def test_bad_yaml_is_an_error(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text("- id: x\n  question: [unclosed\n", encoding="utf-8")
    with pytest.raises(GoldenQuestionError, match="not valid YAML"):
        load_questions(path)


def test_a_mapping_root_is_an_error(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text("id: x\n", encoding="utf-8")
    with pytest.raises(GoldenQuestionError, match="must be a list"):
        load_questions(path)


def test_an_empty_file_is_an_error(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(GoldenQuestionError, match="empty"):
        load_questions(path)


def test_a_non_mapping_case_is_an_error(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text("- just a string\n", encoding="utf-8")
    with pytest.raises(GoldenQuestionError, match="expected a mapping"):
        load_questions(path)


@pytest.mark.parametrize("bad_id", ["Orders", "orders-total", "1orders", "", "_x"])
def test_ids_must_be_snake_case(tmp_path, bad_id):
    with pytest.raises(GoldenQuestionError, match="snake_case"):
        load_questions(write(tmp_path, {**VALID, "id": bad_id}))


def test_duplicate_ids_are_refused(tmp_path):
    with pytest.raises(GoldenQuestionError, match="duplicate case id"):
        load_questions(write(tmp_path, VALID, dict(VALID)))


def test_two_cases_asking_the_same_question_are_refused(tmp_path):
    """The offline fake is keyed on the question, because that is all the prompt
    carries. A duplicate would silently answer both cases from one script."""
    twin = {**VALID, "id": "orders_total_again"}
    with pytest.raises(GoldenQuestionError, match="asks the same question"):
        load_questions(write(tmp_path, VALID, twin))


def test_the_duplicate_check_ignores_case_and_whitespace(tmp_path):
    twin = {**VALID, "id": "orders_total_again", "question": "  HOW many   orders ARE there? "}
    with pytest.raises(GoldenQuestionError, match="asks the same question"):
        load_questions(write(tmp_path, VALID, twin))


def test_every_committed_question_is_distinct(committed_questions):
    texts = [" ".join(q.question.split()).lower() for q in committed_questions]
    assert len(set(texts)) == len(texts)


def test_a_missing_key_is_an_error(tmp_path):
    case = {key: value for key, value in VALID.items() if key != "notes"}
    with pytest.raises(GoldenQuestionError, match="missing key"):
        load_questions(write(tmp_path, case))


def test_an_unknown_key_is_an_error(tmp_path):
    with pytest.raises(GoldenQuestionError, match="unknown key"):
        load_questions(write(tmp_path, {**VALID, "expected_rows": 3}))


def test_a_blank_question_is_an_error(tmp_path):
    with pytest.raises(GoldenQuestionError, match="'question'"):
        load_questions(write(tmp_path, {**VALID, "question": "   "}))


def test_tags_must_be_a_non_empty_list(tmp_path):
    with pytest.raises(GoldenQuestionError, match="non-empty list"):
        load_questions(write(tmp_path, {**VALID, "tags": []}))


def test_a_blank_tag_is_an_error(tmp_path):
    with pytest.raises(GoldenQuestionError, match="every tag"):
        load_questions(write(tmp_path, {**VALID, "tags": ["kind:scalar", "  "]}))


def test_exactly_one_kind_tag_is_required(tmp_path):
    with pytest.raises(GoldenQuestionError, match="exactly one 'kind:' tag"):
        load_questions(
            write(tmp_path, {**VALID, "tags": ["kind:scalar", "kind:list", "difficulty:easy"]})
        )


def test_an_unknown_kind_is_an_error(tmp_path):
    with pytest.raises(GoldenQuestionError, match="unknown kind"):
        load_questions(write(tmp_path, {**VALID, "tags": ["kind:pivot", "difficulty:easy"]}))


def test_an_unknown_difficulty_is_an_error(tmp_path):
    with pytest.raises(GoldenQuestionError, match="unknown difficulty"):
        load_questions(
            write(tmp_path, {**VALID, "tags": ["kind:scalar", "difficulty:brutal"]})
        )


def test_two_traps_on_one_case_is_an_error(tmp_path):
    tags = [*BAIT["tags"], "trap:ambiguity"]
    with pytest.raises(GoldenQuestionError, match="at most one 'trap:'"):
        load_questions(write(tmp_path, {**BAIT, "tags": tags}))


def test_an_unknown_trap_is_an_error(tmp_path):
    tags = ["kind:scalar", "difficulty:easy", "trap:sarcasm"]
    with pytest.raises(GoldenQuestionError, match="unknown trap"):
        load_questions(write(tmp_path, {**BAIT, "tags": tags}))


def test_a_tag_with_no_known_prefix_is_an_error(tmp_path):
    tags = ["kind:scalar", "difficulty:easy", "flaky"]
    with pytest.raises(GoldenQuestionError, match="no known prefix"):
        load_questions(write(tmp_path, {**VALID, "tags": tags}))


def test_an_unknown_expectation_is_an_error(tmp_path):
    with pytest.raises(GoldenQuestionError, match="'expected' must be one of"):
        load_questions(write(tmp_path, {**VALID, "expected": "maybe"}))


def test_declining_without_a_trap_tag_is_an_error(tmp_path):
    case = {key: value for key, value in VALID.items() if key != "reference_sql"}
    with pytest.raises(GoldenQuestionError, match="needs a 'trap:' tag"):
        load_questions(write(tmp_path, {**case, "expected": "abstain"}))


def test_the_trap_tag_and_the_expectation_must_agree(tmp_path):
    with pytest.raises(GoldenQuestionError, match="means 'expected: refuse'"):
        load_questions(
            write(
                tmp_path,
                {
                    **BAIT,
                    "tags": ["kind:scalar", "difficulty:easy", "trap:unsafe"],
                    "expected": "abstain",
                },
            )
        )


def test_an_answerable_case_needs_reference_sql(tmp_path):
    case = {key: value for key, value in VALID.items() if key != "reference_sql"}
    with pytest.raises(GoldenQuestionError, match="needs 'reference_sql'"):
        load_questions(write(tmp_path, case))


def test_a_trap_must_not_carry_reference_sql(tmp_path):
    case = {**BAIT, "reference_sql": "SELECT 1 AS n"}
    with pytest.raises(GoldenQuestionError, match="must not carry 'reference_sql'"):
        load_questions(write(tmp_path, case))


def test_ordered_must_be_a_boolean(tmp_path):
    with pytest.raises(GoldenQuestionError, match="'ordered' must be true or false"):
        load_questions(write(tmp_path, {**VALID, "ordered": "yes"}))


def test_ordered_is_meaningless_on_a_trap(tmp_path):
    with pytest.raises(GoldenQuestionError, match="only means something"):
        load_questions(write(tmp_path, {**BAIT, "ordered": True}))


def test_the_hash_changes_with_the_file(tmp_path):
    first = goldens_sha256(write(tmp_path, VALID))
    second = goldens_sha256(write(tmp_path, VALID, BAIT))
    assert first != second
    assert len(first) == 64


# --- the committed set -----------------------------------------------------


def test_the_committed_set_loads(committed_questions):
    assert len(committed_questions) == 40


def test_the_committed_set_has_the_promised_trap_counts(committed_questions):
    counts = {trap: 0 for trap in Trap}
    answerable = 0
    for question in committed_questions:
        if question.trap is None:
            answerable += 1
        else:
            counts[question.trap] += 1
    assert answerable == 26
    assert counts[Trap.HALLUCINATION_BAIT] == 6
    assert counts[Trap.AMBIGUITY] == 4
    assert counts[Trap.UNSAFE] == 4


def test_every_kind_appears_at_least_twice(committed_questions):
    kinds: dict[str, int] = {}
    for question in committed_questions:
        if question.expected is Expectation.ANSWER:
            kinds[question.kind] = kinds.get(question.kind, 0) + 1
    assert set(kinds) == {
        "scalar",
        "list",
        "grouped",
        "top_n",
        "time_window",
        "join",
        "negation",
    }
    assert min(kinds.values()) >= 2, kinds


def test_the_first_twenty_five_are_a_stratified_prefix(committed_questions):
    """`--limit 25` is what the live run affords. It has to be representative."""
    prefix = committed_questions[:25]
    traps = [question.trap for question in prefix]
    assert traps.count(None) == 15
    assert traps.count(Trap.HALLUCINATION_BAIT) == 4
    assert traps.count(Trap.AMBIGUITY) == 3
    assert traps.count(Trap.UNSAFE) == 3


def test_every_case_explains_itself(committed_questions):
    for question in committed_questions:
        assert len(question.notes) > 40, question.id


def test_only_top_n_questions_are_ordered(committed_questions):
    for question in committed_questions:
        if question.ordered:
            assert question.kind == "top_n", question.id
    assert sum(1 for question in committed_questions if question.ordered) == 4


def test_the_committed_file_hashes(committed_questions):
    assert len(goldens_sha256(COMMITTED_GOLDENS)) == 64


def test_a_hand_built_question_with_no_kind_tag_raises_rather_than_guessing():
    """Unreachable through `load_questions`, which requires both tags. It is a
    guard on the dataclass, and a guess would put a question in a group it does
    not belong to and change a per-kind rate nobody could explain."""
    from sqeual.eval.goldens import Expectation, GoldenQuestion

    question = GoldenQuestion(
        id="hand_built",
        question="x",
        tags=("difficulty:easy",),
        expected=Expectation.ANSWER,
        reference_sql="SELECT 1 AS n",
        ordered=False,
        notes="fixture",
    )
    with pytest.raises(KeyError, match="kind:"):
        assert question.kind

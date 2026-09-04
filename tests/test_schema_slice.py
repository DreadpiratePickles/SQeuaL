"""Stage 02: the schema slicer.

A model shown all seven tables spends tokens on four it does not need and gets
four more chances to join something irrelevant. A model shown too few cannot
answer at all. The slicer picks a subset — and it does so with **deterministic
term overlap plus foreign-key expansion**, not embeddings, so that when it gets
one wrong the answer to "why did it pick that" is a rule somebody can read and
edit rather than a cosine distance.

Every entry carries a reason for exactly that purpose.
"""

import pytest

from conftest import load_test_config
from sqeual.schema.slice import SliceError, slice_for_question


def synonyms(tmp_path):
    return load_test_config(tmp_path).schema.synonyms


# --- the worked example -----------------------------------------------------

BERLIN = "how much did we refund to customers in Berlin last month"


def test_the_worked_example_picks_refunds_orders_and_customers(session_card, tmp_path):
    result = slice_for_question(
        BERLIN, session_card, max_tables=3, synonyms=synonyms(tmp_path)
    )
    assert set(result.tables) == {"refunds", "orders", "customers"}


def test_the_worked_example_orders_the_named_tables_before_the_join_path(
    session_card, tmp_path
):
    """`refunds` and `customers` were asked for; `orders` is only there to
    carry the join. Putting the named tables first is what the model reads
    first."""
    result = slice_for_question(
        BERLIN, session_card, max_tables=3, synonyms=synonyms(tmp_path)
    )
    assert result.tables == ("refunds", "customers", "orders")


def test_every_chosen_table_carries_a_reason(session_card, tmp_path):
    result = slice_for_question(
        BERLIN, session_card, max_tables=3, synonyms=synonyms(tmp_path)
    )
    assert "refund" in result.reason("refunds")
    assert "customers" in result.reason("customers")
    assert result.reason("orders") == "joins customers to refunds"


def test_the_join_path_prefers_the_edge_that_cannot_drop_rows(session_card, tmp_path):
    """Both `orders` and `tickets` connect `refunds` to `customers`. `orders`
    wins because `refunds.order_id` is NOT NULL while `refunds.ticket_id` is
    nullable — joining through the nullable one silently drops refunds that
    never had a ticket, and a total that quietly excludes rows is the worst
    kind of wrong answer."""
    result = slice_for_question(
        BERLIN, session_card, max_tables=3, synonyms=synonyms(tmp_path)
    )
    assert "orders" in result.tables
    assert "tickets" not in result.tables


def test_at_the_configured_width_the_second_join_path_also_fits(session_card, tmp_path):
    """The committed `max_tables` is 6, so there is room for the weaker join
    path as well as the strong one. `tickets` also connects `refunds` to
    `customers`, through a nullable key — worse as a join path, still real, and
    at this width it costs a few hundred tokens rather than an answer.

    Order still matters: the tables the question named come first, then the
    join path that cannot drop rows, then the one that can.
    """
    config = load_test_config(tmp_path)
    result = slice_for_question(
        BERLIN, session_card, max_tables=config.schema.max_tables, synonyms=config.schema.synonyms
    )
    assert result.tables == ("refunds", "customers", "orders", "tickets")
    assert result.reason("tickets") == "joins customers to refunds"


def test_a_foreign_key_neighbour_fills_a_slice_with_only_one_seed(session_card, tmp_path):
    """With one seed there is no bridge to build, so tier 3 does the work: a
    question about tickets probably needs whatever a ticket points at."""
    result = slice_for_question(
        "how many tickets are open", session_card, max_tables=3, synonyms=synonyms(tmp_path)
    )
    assert result.tables[0] == "tickets"
    assert len(result.tables) == 3
    for table in result.tables[1:]:
        assert result.reason(table) == "one foreign key from tickets"


# --- matching rules ---------------------------------------------------------


def test_a_table_named_outright_is_chosen(session_card, tmp_path):
    result = slice_for_question(
        "how many tickets are open", session_card, max_tables=1, synonyms=synonyms(tmp_path)
    )
    assert result.tables == ("tickets",)
    assert "tickets" in result.reason("tickets")


def test_matching_is_case_insensitive(session_card, tmp_path):
    result = slice_for_question(
        "How Many TICKETS Are Open", session_card, max_tables=1, synonyms=synonyms(tmp_path)
    )
    assert result.tables == ("tickets",)


def test_singular_and_plural_both_match(session_card, tmp_path):
    for question in ("count the orders", "count the order"):
        result = slice_for_question(
            question, session_card, max_tables=1, synonyms=synonyms(tmp_path)
        )
        assert result.tables == ("orders",), question


def test_a_synonym_reaches_a_table_the_question_never_names(session_card, tmp_path):
    result = slice_for_question(
        "which clients filed the most complaints",
        session_card,
        max_tables=2,
        synonyms=synonyms(tmp_path),
    )
    assert set(result.tables) == {"customers", "tickets"}
    assert "client" in result.reason("customers")
    assert "complaint" in result.reason("tickets")


def test_a_column_name_reaches_its_table(session_card, tmp_path):
    result = slice_for_question(
        "break the numbers down by segment", session_card, max_tables=1, synonyms=synonyms(tmp_path)
    )
    assert result.tables == ("customers",)
    assert "segment" in result.reason("customers")


def test_a_column_match_ranks_below_a_table_match(session_card, tmp_path):
    """`channel` is a column on both `orders` and `tickets`; `orders` is also
    named outright. Naming a table is stronger evidence than sharing a column
    name with it."""
    result = slice_for_question(
        "orders by channel", session_card, max_tables=1, synonyms=synonyms(tmp_path)
    )
    assert result.tables == ("orders",)


def test_a_question_matching_nothing_returns_an_empty_slice(session_card, tmp_path):
    """Not a guess. A slicer that invented three tables for a question it did
    not understand would send a model confidently in the wrong direction; an
    empty slice lets the caller fall back to the whole card and say why."""
    result = slice_for_question(
        "what is the weather like today", session_card, max_tables=6, synonyms=synonyms(tmp_path)
    )
    assert result.tables == ()
    assert result.entries == ()


def test_the_result_never_exceeds_max_tables(session_card, tmp_path):
    for width in (1, 2, 3, 4, 5, 6, 7):
        result = slice_for_question(
            "refunds for orders from customers with tickets about products by agents and items",
            session_card,
            max_tables=width,
            synonyms=synonyms(tmp_path),
        )
        assert len(result.tables) <= width


def test_the_highest_scoring_table_survives_a_tight_budget(session_card, tmp_path):
    result = slice_for_question(
        "refunds refund reimbursement for one customer",
        session_card,
        max_tables=1,
        synonyms=synonyms(tmp_path),
    )
    assert result.tables == ("refunds",)


def test_slicing_is_deterministic(session_card, tmp_path):
    args = (BERLIN, session_card)
    kwargs = {"max_tables": 4, "synonyms": synonyms(tmp_path)}
    assert slice_for_question(*args, **kwargs) == slice_for_question(*args, **kwargs)


def test_underscored_column_names_can_be_typed_with_spaces(session_card, tmp_path):
    result = slice_for_question(
        "show me the unit price cents", session_card, max_tables=2, synonyms=synonyms(tmp_path)
    )
    assert "products" in result.tables or "order_items" in result.tables


# --- failure behaviour ------------------------------------------------------


def test_a_synonym_pointing_at_a_table_that_does_not_exist_is_refused(session_card):
    with pytest.raises(SliceError, match="invoices"):
        slice_for_question(
            "show me the invoices",
            session_card,
            max_tables=3,
            synonyms={"invoice": "invoices"},
        )


def test_an_empty_question_is_refused(session_card, tmp_path):
    with pytest.raises(SliceError, match="question"):
        slice_for_question("   ", session_card, max_tables=3, synonyms=synonyms(tmp_path))


def test_a_non_positive_width_is_refused(session_card, tmp_path):
    with pytest.raises(SliceError, match="max_tables"):
        slice_for_question(BERLIN, session_card, max_tables=0, synonyms=synonyms(tmp_path))


def test_reason_for_a_table_not_in_the_slice_is_refused(session_card, tmp_path):
    result = slice_for_question(
        BERLIN, session_card, max_tables=3, synonyms=synonyms(tmp_path)
    )
    with pytest.raises(KeyError):
        result.reason("products")

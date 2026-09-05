"""Stage 02: rendering the card into the text a model is shown.

This is a prompt, not a report. Two properties matter and both are tested:
it is **deterministic** — the same card renders to the same bytes, so a change
in what the model saw is always a diff somebody can read — and it carries the
facts a model needs to avoid guessing: the exact column names, the exact
spelling of enum values, and which columns join to which.
"""

from sqeual.schema.render import render_card


def test_rendering_is_deterministic(session_card):
    assert render_card(session_card) == render_card(session_card)


def test_every_table_and_column_appears(session_card):
    text = render_card(session_card)
    for table in session_card.tables:
        assert f"## {table.name}" in text
        for column in table.columns:
            assert column.name in text


def test_row_counts_appear_so_size_is_visible(session_card):
    text = render_card(session_card)
    assert "2000 rows" in text
    assert "150 rows" in text


def test_sample_values_appear_verbatim(session_card):
    """The single most valuable line in the card: a model that can read
    `'refunded'` does not have to guess `'REFUNDED'`."""
    text = render_card(session_card)
    assert "refunded" in text
    assert "Berlin" in text


def test_foreign_keys_are_rendered_as_join_paths(session_card):
    text = render_card(session_card)
    assert "refunds.order_id -> orders.id" in text
    assert "orders.customer_id -> customers.id" in text


def test_nullability_and_keys_are_marked(session_card):
    text = render_card(session_card)
    assert "PK" in text
    assert "null" in text.lower()


def test_a_subset_renders_only_those_tables_in_the_given_order(session_card):
    text = render_card(session_card, tables=("refunds", "orders", "customers"))
    assert text.index("## refunds") < text.index("## orders") < text.index("## customers")
    assert "## tickets" not in text
    assert "## products" not in text


def test_a_subset_hides_foreign_keys_pointing_out_of_the_subset(session_card):
    """A join path to a table the model was not shown is an invitation to
    hallucinate that table. The card names only edges it can honour."""
    text = render_card(session_card, tables=("refunds", "orders"))
    assert "refunds.order_id -> orders.id" in text
    assert "tickets" not in text


def test_an_unknown_table_in_the_subset_is_ignored_not_invented(session_card):
    text = render_card(session_card, tables=("orders", "invoices"))
    assert "## orders" in text
    assert "invoices" not in text


def test_an_empty_subset_renders_a_card_that_says_so(session_card):
    text = render_card(session_card, tables=())
    assert "no tables" in text.lower()


def test_the_schema_hash_is_in_the_header(session_card):
    assert session_card.schema_sha256[:12] in render_card(session_card)


def test_the_card_is_markdown_a_person_can_read(session_card):
    text = render_card(session_card)
    assert text.startswith("# ")
    assert text.endswith("\n")
    assert "| column | type |" in text


# --- denied columns --------------------------------------------------------
#
# §12 keeps a per-row column out of the card's *sample values*, because a value
# per row teaches a model nothing and hands it real content. That is a different
# control from this one, at a different layer: a column absent from the samples
# is still a column a model can name, because the card lists it by name and
# type. The first live evaluation is what proved the gap — the model wrote
# `SELECT name, email FROM customers` from a card whose `email` row carried no
# samples at all. docs/design.md §54.


DENIED = frozenset({"customers.email"})


def test_a_denied_column_is_marked_in_the_card(session_card):
    text = render_card(session_card, tables=("customers",), denied_columns=DENIED)
    row = next(line for line in text.splitlines() if line.startswith("| email |"))
    assert "denied" in row


def test_the_card_says_what_denied_means_in_words(session_card):
    """A marker a model has to infer is a marker a model will ignore."""
    text = render_card(session_card, tables=("customers",), denied_columns=DENIED)
    assert "may not appear" in text
    assert "SELECT list" in text


def test_an_undenied_column_on_the_same_table_is_not_marked(session_card):
    text = render_card(session_card, tables=("customers",), denied_columns=DENIED)
    row = next(line for line in text.splitlines() if line.startswith("| city |"))
    assert "denied" not in row


def test_a_column_of_the_same_name_on_another_table_is_not_marked(session_card):
    """The policy names `customers.email`. `products.name` is a product name."""
    text = render_card(
        session_card, tables=("products",), denied_columns=frozenset({"customers.name"})
    )
    row = next(line for line in text.splitlines() if line.startswith("| name |"))
    assert "denied" not in row
    assert "policy" not in text


def test_a_card_with_nothing_denied_is_byte_identical_to_the_old_one(session_card):
    """The extra column appears only where it says something. A deployment that
    denies nothing sees the card it saw before, so this change costs no prompt
    tokens where it buys nothing."""
    assert render_card(session_card, denied_columns=frozenset()) == render_card(session_card)


def test_denying_a_column_on_a_table_outside_the_slice_changes_nothing(session_card):
    text = render_card(session_card, tables=("orders",), denied_columns=DENIED)
    assert render_card(session_card, tables=("orders",)) == text


def test_marking_is_deterministic(session_card):
    first = render_card(session_card, tables=("customers",), denied_columns=DENIED)
    second = render_card(session_card, tables=("customers",), denied_columns=DENIED)
    assert first == second

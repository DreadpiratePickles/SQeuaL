"""Every figure a user sees is formatted here, by code, from a database cell.

This is where the founding rule stops being a claim and becomes a function. The
grounding check is the enforcement: it tokenises every number in a model-written
sentence and requires each one to trace back to a cell, a row count, or a date
code computed. A number with nothing behind it means a model wrote a figure, and
that is the one failure this project exists to prevent.
"""

import pytest

from sqeual.answer.format import format_cell, render_cells
from sqeual.answer.grounding import numeric_tokens, ungrounded_numbers
from sqeual.config_pipeline import AnswerSettings

SETTINGS = AnswerSettings(
    max_rows_shown=20, llm_phrasing=False, currency="EUR", currency_symbol="€"
)


def cell(value, column="x", places=2):
    return format_cell(value, column=column, settings=SETTINGS, float_places=places)


class TestMoney:
    def test_integer_cents_render_as_currency_with_two_decimals(self):
        assert cell(125000, "refunded_cents").rendered == "€1,250.00"

    def test_a_single_cent_is_not_lost(self):
        assert cell(1, "amount_cents").rendered == "€0.01"

    def test_money_is_never_printed_bare(self):
        """"1,250" is a different answer in two currencies, invisibly."""
        assert cell(125000, "total_cents").rendered != "125000"
        assert cell(125000, "total_cents").rendered != "1250"

    def test_a_negative_amount_keeps_its_sign_outside_the_symbol(self):
        assert cell(-125000, "amount_cents").rendered == "-€1,250.00"

    def test_a_non_cents_column_is_not_money(self):
        assert cell(125000, "order_count").rendered == "125,000"

    def test_a_float_in_a_cents_column_is_still_money(self):
        """ROUND(AVG(total_cents), 2) is a float and is still an amount."""
        assert cell(1250.5, "average_order_cents").rendered == "€12.51"

    def test_the_kind_is_recorded_for_the_audit_trail(self):
        assert cell(125000, "amount_cents").kind == "money"


class TestOtherTypes:
    def test_integers_get_thousands_separators(self):
        assert cell(1234567).rendered == "1,234,567"

    def test_floats_get_an_explicit_precision(self):
        assert cell(3.14159, places=2).rendered == "3.14"
        assert cell(3.14159, places=4).rendered == "3.1416"

    def test_null_is_never_zero_and_never_the_string_zero(self):
        rendered = cell(None).rendered
        assert rendered == "no value recorded"
        assert "0" not in rendered

    def test_an_iso_date_is_rendered_unchanged(self):
        """ISO is the one format with no day/month ambiguity, so it is kept."""
        assert cell("2026-07-31", "refund_date").rendered == "2026-07-31"
        assert cell("2026-07-31", "refund_date").kind == "date"

    def test_text_is_rendered_as_itself(self):
        assert cell("Berlin", "city").rendered == "Berlin"

    def test_a_value_of_an_unexpected_type_is_marked_rather_than_coerced(self):
        """A silently coerced cell is a wrong number that looks right."""
        rendered = cell(b"\x00\x01", "blob")
        assert rendered.kind == "unformattable"
        assert "unformattable" in rendered.rendered

    def test_a_boolean_is_not_quietly_an_integer(self):
        assert cell(True, "flag").kind == "unformattable"


class TestRenderCells:
    def test_every_cell_keeps_its_raw_value_beside_its_rendering(self):
        cells = render_cells(
            columns=("city", "refunded_cents"),
            rows=(("Berlin", 125000),),
            settings=SETTINGS,
            float_places=2,
        )
        assert [(item.raw, item.rendered) for item in cells] == [
            ("Berlin", "Berlin"),
            (125000, "€1,250.00"),
        ]


class TestNumericTokens:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("We refunded 12,340 in total.", ["12,340"]),
            ("The average was 3.14.", ["3.14"]),
            ("That is €1,250.00 across 23 orders.", ["€1,250.00", "23"]),
            ("Growth was 12.5% year on year.", ["12.5%"]),
            ("$99 and £45 and 7", ["$99", "£45", "7"]),
            ("No numbers here at all.", []),
            ("Order 2026-07-31 fell in July.", ["2026-07-31"]),
            ("Between 2026-07 and 2026-08.", ["2026-07", "2026-08"]),
            ("The net change was -150 cents.", ["-150"]),
            ("It fell by -€1,250.00.", ["-€1,250.00"]),
        ],
    )
    def test_the_tokeniser_finds_every_shape_a_figure_takes(self, text, expected):
        assert list(numeric_tokens(text)) == expected


class TestGrounding:
    def cells(self):
        return render_cells(
            columns=("city", "refunded_cents"),
            rows=(("Berlin", 125000),),
            settings=SETTINGS,
            float_places=2,
        )

    def test_a_sentence_quoting_the_rendered_value_is_grounded(self):
        assert (
            ungrounded_numbers(
                "Berlin was refunded €1,250.00.", cells=self.cells(), row_count=1
            )
            == ()
        )

    def test_a_sentence_quoting_the_raw_cents_is_also_grounded(self):
        """125000 is genuinely in the cell; a reader can check it."""
        assert (
            ungrounded_numbers("The raw total was 125000 cents.", cells=self.cells(), row_count=1)
            == ()
        )

    def test_a_sentence_quoting_the_row_count_is_grounded(self):
        assert ungrounded_numbers("1 city matched.", cells=self.cells(), row_count=1) == ()

    def test_an_invented_number_is_caught(self):
        """The load-bearing test: a figure with no cell behind it."""
        assert ungrounded_numbers(
            "Berlin was refunded €1,250.00, up 12% on last month.",
            cells=self.cells(),
            row_count=1,
        ) == ("12%",)

    def test_a_plausible_rounding_of_a_real_value_is_still_ungrounded(self):
        """"about €1,300" is not the number in the cell, and rounding is not the model's job."""
        assert ungrounded_numbers(
            "Berlin was refunded about €1,300.", cells=self.cells(), row_count=1
        ) == ("€1,300",)

    def test_thousands_separators_and_currency_symbols_do_not_change_a_value(self):
        assert (
            ungrounded_numbers("It came to 1250.00 euros.", cells=self.cells(), row_count=1)
            == ()
        )

    def test_a_date_the_code_computed_can_be_supplied_as_grounded(self):
        """A window endpoint is a figure code derived, not one a model invented."""
        text = "Berlin was refunded €1,250.00 between 2026-07-01 and 2026-07-31."
        assert ungrounded_numbers(text, cells=self.cells(), row_count=1) != ()
        assert (
            ungrounded_numbers(
                text,
                cells=self.cells(),
                row_count=1,
                extra=("2026-07-01", "2026-07-31"),
            )
            == ()
        )

    def test_a_date_grounds_itself_whole_and_never_its_fragments(self):
        """The hole this closes: an ISO date pre-authorising every 1-31 and 1-12.

        Tokenising `2026-08-31` into `2026`, `08` and `31` would put three small
        integers into the allowed set on **every** run, and a small invented count
        is exactly what a hallucinating phraser produces.
        """
        cells = self.cells()
        extra = ("2026-08-31",)
        assert ungrounded_numbers(
            "Measured on 2026-08-31.", cells=cells, row_count=1, extra=extra
        ) == ()
        assert ungrounded_numbers(
            "There were 31 refunds.", cells=cells, row_count=1, extra=extra
        ) == ("31",)
        assert ungrounded_numbers(
            "Roughly 8 per week.", cells=cells, row_count=1, extra=extra
        ) == ("8",)

    def test_a_dates_year_is_grounded_because_a_sentence_needs_it(self):
        """"in July 2026" is natural; "31" is a count. The year is kept, the day is not."""
        assert ungrounded_numbers(
            "Refunded €1,250.00 in July 2026.",
            cells=self.cells(),
            row_count=1,
            extra=("2026-07-01",),
        ) == ()

    def test_a_percentage_is_never_grounded_by_a_plain_number(self):
        """The hole this closes: stripping `%` made "42%" equal to a cell holding 42."""
        cells = render_cells(
            columns=("order_count",), rows=((42,),), settings=SETTINGS, float_places=2
        )
        assert ungrounded_numbers(
            "Orders grew by 42% on the prior period.", cells=cells, row_count=1
        ) == ("42%",)
        assert ungrounded_numbers("There were 42 orders.", cells=cells, row_count=1) == ()

    def test_sign_is_part_of_a_value(self):
        """The hole this closes: a cell of -150 grounded the claim "150"."""
        cells = render_cells(
            columns=("change_cents",), rows=((-150,),), settings=SETTINGS, float_places=2
        )
        assert ungrounded_numbers(
            "The net change was 150 cents, a gain.", cells=cells, row_count=1
        ) == ("150",)
        assert ungrounded_numbers(
            "The net change was -150 cents.", cells=cells, row_count=1
        ) == ()

    def test_a_sentence_with_no_numbers_is_trivially_grounded(self):
        assert ungrounded_numbers("Berlin led the table.", cells=self.cells(), row_count=1) == ()

    def test_a_null_cell_grounds_no_number(self):
        cells = render_cells(
            columns=("c",), rows=((None,),), settings=SETTINGS, float_places=2
        )
        assert ungrounded_numbers("It was 0.", cells=cells, row_count=1) == ("0",)

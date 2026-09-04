"""Self-consistency is measured on executed rows, never on SQL text.

The load-bearing property: two correct queries can be spelled differently and
would never agree on their text, while two identical wrong queries agree
perfectly. So agreement compares what came back, canonicalised — and it is a
confidence *factor*, never a source of truth. Three samples can be wrong in the
same way, and a vote among them would launder that into certainty.
"""

import pytest

from sqeual.execute import ExecutionPlan, ResultSet
from sqeual.generate.agreement import agreement_fraction, canonicalise

EMPTY_PLAN = ExecutionPlan(steps=(), warnings=())


def result(columns, rows) -> ResultSet:
    return ResultSet(
        columns=tuple(columns),
        rows=tuple(rows),
        row_count=len(rows),
        truncated=False,
        elapsed_ms=1,
        plan=EMPTY_PLAN,
    )


class TestCanonicalisation:
    def test_column_names_are_lowercased(self):
        assert canonicalise(result(["Total_Cents"], [(1,)]), float_places=2).columns == (
            "total_cents",
        )

    def test_rows_are_sorted_so_ordering_is_not_disagreement(self):
        first = canonicalise(result(["city"], [("Berlin",), ("Oslo",)]), float_places=2)
        second = canonicalise(result(["city"], [("Oslo",), ("Berlin",)]), float_places=2)
        assert first.rows == second.rows

    def test_floats_are_rounded_to_the_configured_places(self):
        canonical = canonicalise(result(["avg"], [(12.3456789,)]), float_places=2)
        assert canonical.rows == ((12.35,),)

    def test_two_floats_differing_below_the_rounding_agree(self):
        """SQLite's AVG can differ in the last bit between two correct queries."""
        left = canonicalise(result(["avg"], [(12.340000000001,)]), float_places=2)
        right = canonicalise(result(["avg"], [(12.34,)]), float_places=2)
        assert left.agrees_with(right)

    def test_integers_are_left_alone(self):
        assert canonicalise(result(["n"], [(7,)]), float_places=0).rows == ((7,),)

    def test_nulls_survive_canonicalisation_and_sort_deterministically(self):
        canonical = canonicalise(result(["x"], [(None,), (1,), (None,)]), float_places=2)
        assert canonical.rows.count((None,)) == 2

    def test_a_digest_is_stable_across_two_equal_result_sets(self):
        left = canonicalise(result(["n"], [(1,), (2,)]), float_places=2)
        right = canonicalise(result(["n"], [(2,), (1,)]), float_places=2)
        assert left.digest == right.digest

    def test_a_digest_differs_when_the_rows_differ(self):
        left = canonicalise(result(["n"], [(1,)]), float_places=2)
        right = canonicalise(result(["n"], [(2,)]), float_places=2)
        assert left.digest != right.digest


class TestLabelsAreNotTheAnswer:
    def test_two_spellings_of_one_answer_agree(self):
        """`COUNT(*) AS n` and `COUNT(*) AS total` are one answer with two names.

        Column names are canonicalised and recorded, and equality is decided on
        the rows alone. A tool that called these two candidates a disagreement
        would report low confidence on a question two models got right.
        """
        left = canonicalise(result(["n"], [(12340,)]), float_places=2)
        right = canonicalise(result(["total"], [(12340,)]), float_places=2)
        assert left.agrees_with(right)
        assert left.columns != right.columns

    def test_the_same_label_over_different_rows_does_not_agree(self):
        left = canonicalise(result(["n"], [(12340,)]), float_places=2)
        right = canonicalise(result(["n"], [(999,)]), float_places=2)
        assert not left.agrees_with(right)


class TestAgreementArithmetic:
    def primary(self):
        return canonicalise(result(["n"], [(1,)]), float_places=2)

    def other(self, value):
        return canonicalise(result(["n"], [(value,)]), float_places=2)

    def test_all_three_agreeing_is_one(self):
        primary = self.primary()
        assert agreement_fraction(primary, [primary, self.other(1), self.other(1)]) == 1.0

    def test_two_of_three_agreeing_is_two_thirds(self):
        primary = self.primary()
        assert agreement_fraction(primary, [primary, self.other(1), self.other(2)]) == 2 / 3

    def test_a_sample_that_never_produced_rows_counts_against(self):
        """A candidate the guard refused did not agree; it also did not vanish."""
        primary = self.primary()
        assert agreement_fraction(primary, [primary, None, None]) == 1 / 3

    def test_a_single_sample_agrees_with_itself(self):
        primary = self.primary()
        assert agreement_fraction(primary, [primary]) == 1.0

    def test_no_samples_is_a_programming_error_not_a_score(self):
        with pytest.raises(ValueError, match="at least one"):
            agreement_fraction(self.primary(), [])

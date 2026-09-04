"""The checks that need no model at all, on hand-built (question, SQL) pairs.

Every one is mechanical and every one is cheap, and none of them is sufficient
alone — which is why they feed a score rather than a verdict. What they catch is
the class of failure the guard cannot: a statement that is perfectly valid and
answers a question nobody asked.

NA is a first-class outcome and is asserted as often as PASS. "We checked and it
was fine" and "there was nothing to check" must never render the same, because a
score that counted the second as the first would reward a question with no
constraints in it.
"""

import pytest

from sqeual.generate.timewindow import TimeWindow
from sqeual.verify.intent import CheckStatus, intent_checks

WINDOW = TimeWindow(phrase="last month", start="2026-07-01", end="2026-07-31")

SYNONYMS = {"refund": "refunds", "customer": "customers", "client": "customers",
            "ticket": "tickets", "order": "orders", "agent": "agents"}


def check(name, checks):
    found = [item for item in checks if item.name == name]
    assert found, f"{name} not among {[item.name for item in checks]}"
    return found[0]


def run(question, sql, *, window=None, tables=(), card=None):
    return intent_checks(
        question=question,
        sql=sql,
        card=card,
        tables_used=tables,
        time_window=window,
        synonyms=SYNONYMS,
    )


@pytest.fixture(autouse=True)
def _card(session_card, request):
    request.cls.card = session_card if request.cls else None


class TestTimeWindow:
    def test_no_time_phrase_is_not_applicable(self, session_card):
        checks = run("how many refunds are there", "SELECT COUNT(*) AS n FROM refunds",
                     card=session_card, tables=("refunds",))
        assert check("time_window", checks).status is CheckStatus.NA

    def test_a_matching_range_passes(self, session_card):
        sql = (
            "SELECT SUM(amount_cents) AS c FROM refunds "
            "WHERE refund_date >= '2026-07-01' AND refund_date <= '2026-07-31'"
        )
        result = check("time_window", run("refunds last month", sql, window=WINDOW,
                                          tables=("refunds",), card=session_card))
        assert result.status is CheckStatus.PASS

    def test_the_exclusive_upper_bound_spelling_also_passes(self, session_card):
        sql = (
            "SELECT SUM(amount_cents) AS c FROM refunds "
            "WHERE refund_date >= '2026-07-01' AND refund_date < '2026-08-01'"
        )
        result = check("time_window", run("refunds last month", sql, window=WINDOW,
                                          tables=("refunds",), card=session_card))
        assert result.status is CheckStatus.PASS

    def test_a_month_prefix_through_strftime_passes(self, session_card):
        sql = (
            "SELECT SUM(amount_cents) AS c FROM refunds "
            "WHERE STRFTIME('%Y-%m', refund_date) = '2026-07'"
        )
        result = check("time_window", run("refunds last month", sql, window=WINDOW,
                                          tables=("refunds",), card=session_card))
        assert result.status is CheckStatus.PASS

    def test_a_time_phrase_with_no_date_predicate_at_all_fails(self, session_card):
        result = check(
            "time_window",
            run("refunds last month", "SELECT SUM(amount_cents) AS c FROM refunds",
                window=WINDOW, tables=("refunds",), card=session_card),
        )
        assert result.status is CheckStatus.FAIL
        assert "last month" in result.evidence

    def test_the_wrong_month_fails_and_names_the_offending_literal(self, session_card):
        """The bug this check exists for: told July, wrote August."""
        sql = (
            "SELECT SUM(amount_cents) AS c FROM refunds "
            "WHERE refund_date >= '2026-08-01' AND refund_date <= '2026-08-31'"
        )
        result = check("time_window", run("refunds last month", sql, window=WINDOW,
                                          tables=("refunds",), card=session_card))
        assert result.status is CheckStatus.FAIL
        assert "2026-08-31" in result.evidence
        assert "2026-07-01..2026-07-31" in result.evidence

    def test_the_exclusive_bound_is_indistinguishable_from_a_first_of_month_start(
        self, session_card
    ):
        """A stated limitation, pinned so nobody discovers it by surprise.

        `'2026-08-01'` is both the day after this window and the first day of the
        next one, and nothing in the literal says which was meant. It is accepted
        on its own; a second literal outside the window is what makes the case
        above fail.
        """
        sql = "SELECT SUM(amount_cents) AS c FROM refunds WHERE refund_date >= '2026-08-01'"
        result = check("time_window", run("refunds last month", sql, window=WINDOW,
                                          tables=("refunds",), card=session_card))
        assert result.status is CheckStatus.PASS

    def test_a_date_literal_in_a_select_list_is_not_a_filter(self, session_card):
        """A date in the projection is not a predicate, and grepping cannot tell."""
        sql = "SELECT '2026-07-01' AS asked_for, SUM(amount_cents) AS c FROM refunds"
        result = check("time_window", run("refunds last month", sql, window=WINDOW,
                                          tables=("refunds",), card=session_card))
        assert result.status is CheckStatus.FAIL

    def test_a_predicate_on_a_join_condition_counts(self, session_card):
        sql = (
            "SELECT SUM(r.amount_cents) AS c FROM refunds AS r "
            "JOIN orders AS o ON o.id = r.order_id AND r.refund_date >= '2026-07-01' "
            "WHERE r.refund_date <= '2026-07-31'"
        )
        result = check("time_window", run("refunds last month", sql, window=WINDOW,
                                          tables=("refunds", "orders"), card=session_card))
        assert result.status is CheckStatus.PASS


class TestAggregation:
    def test_a_question_with_no_aggregate_word_is_not_applicable(self, session_card):
        result = check(
            "aggregation",
            run("which cities exist", "SELECT city FROM customers", card=session_card,
                tables=("customers",)),
        )
        assert result.status is CheckStatus.NA

    def test_how_many_needs_a_count(self, session_card):
        result = check(
            "aggregation",
            run("how many refunds last month", "SELECT COUNT(*) AS n FROM refunds",
                card=session_card, tables=("refunds",)),
        )
        assert result.status is CheckStatus.PASS

    def test_how_many_answered_without_a_count_fails(self, session_card):
        result = check(
            "aggregation",
            run("how many refunds are there", "SELECT id FROM refunds", card=session_card,
                tables=("refunds",)),
        )
        assert result.status is CheckStatus.FAIL
        assert "COUNT" in result.evidence

    def test_total_needs_a_sum(self, session_card):
        result = check(
            "aggregation",
            run("what was the total refunded", "SELECT SUM(amount_cents) AS c FROM refunds",
                card=session_card, tables=("refunds",)),
        )
        assert result.status is CheckStatus.PASS

    def test_total_answered_with_a_count_fails(self, session_card):
        result = check(
            "aggregation",
            run("what was the total refunded", "SELECT COUNT(*) AS n FROM refunds",
                card=session_card, tables=("refunds",)),
        )
        assert result.status is CheckStatus.FAIL

    def test_average_needs_an_avg(self, session_card):
        result = check(
            "aggregation",
            run("the average order total", "SELECT AVG(total_cents) AS a FROM orders",
                card=session_card, tables=("orders",)),
        )
        assert result.status is CheckStatus.PASS

    def test_a_superlative_needs_an_order_by(self, session_card):
        result = check(
            "aggregation",
            run("which city refunded the most",
                "SELECT city, COUNT(*) AS n FROM customers GROUP BY city",
                card=session_card, tables=("customers",)),
        )
        assert result.status is CheckStatus.FAIL
        assert "ORDER BY" in result.evidence

    def test_a_top_n_needs_both_an_order_by_and_a_limit(self, session_card):
        sql = "SELECT city, COUNT(*) AS n FROM customers GROUP BY city ORDER BY COUNT(*) DESC"
        result = check("aggregation", run("the top 5 cities", sql, card=session_card,
                                          tables=("customers",)))
        assert result.status is CheckStatus.FAIL
        assert "LIMIT" in result.evidence

    def test_a_top_n_with_both_passes(self, session_card):
        sql = (
            "SELECT city, COUNT(*) AS n FROM customers GROUP BY city "
            "ORDER BY COUNT(*) DESC LIMIT 5"
        )
        result = check("aggregation", run("the top 5 cities", sql, card=session_card,
                                          tables=("customers",)))
        assert result.status is CheckStatus.PASS

    def test_every_unmet_requirement_is_named_not_just_the_first(self, session_card):
        result = check(
            "aggregation",
            run("how many orders and what was the total, the top 3",
                "SELECT id FROM orders", card=session_card, tables=("orders",)),
        )
        assert result.status is CheckStatus.FAIL
        for expected in ("COUNT", "SUM", "ORDER BY", "LIMIT"):
            assert expected in result.evidence


class TestEntities:
    def test_a_question_naming_no_entity_is_not_applicable(self, session_card):
        result = check(
            "entities",
            run("what is the biggest number", "SELECT 1 AS n", card=session_card),
        )
        assert result.status is CheckStatus.NA

    def test_a_named_table_that_is_used_passes(self, session_card):
        result = check(
            "entities",
            run("how many refunds", "SELECT COUNT(*) AS n FROM refunds", card=session_card,
                tables=("refunds",)),
        )
        assert result.status is CheckStatus.PASS

    def test_a_synonym_resolves_to_its_table(self, session_card):
        result = check(
            "entities",
            run("how many clients", "SELECT COUNT(*) AS n FROM customers", card=session_card,
                tables=("customers",)),
        )
        assert result.status is CheckStatus.PASS

    def test_a_named_table_that_is_absent_fails_and_names_it(self, session_card):
        """"refund to customers" answered without touching customers is the bug."""
        result = check(
            "entities",
            run("how much did we refund to customers",
                "SELECT SUM(amount_cents) AS c FROM refunds", card=session_card,
                tables=("refunds",)),
        )
        assert result.status is CheckStatus.FAIL
        assert "customers" in result.evidence

    def test_the_load_bearing_case_valid_and_irrelevant(self, session_card):
        """`SELECT COUNT(*) FROM orders` for "how much did we refund in March".

        If this passes verification the stage does nothing at all.
        """
        checks = run(
            "how much did we refund in March",
            "SELECT COUNT(*) AS n FROM orders",
            window=TimeWindow(phrase="in march", start="2026-03-01", end="2026-03-31"),
            tables=("orders",),
            card=session_card,
        )
        assert check("entities", checks).status is CheckStatus.FAIL
        assert check("time_window", checks).status is CheckStatus.FAIL
        assert check("aggregation", checks).status is CheckStatus.FAIL


class TestGrouping:
    def test_a_question_with_no_grouping_word_is_not_applicable(self, session_card):
        result = check(
            "grouping",
            run("how many refunds", "SELECT COUNT(*) AS n FROM refunds", card=session_card,
                tables=("refunds",)),
        )
        assert result.status is CheckStatus.NA

    def test_by_a_column_needs_a_group_by_on_that_column(self, session_card):
        sql = "SELECT city, COUNT(*) AS n FROM customers GROUP BY city"
        result = check("grouping", run("how many customers by city", sql, card=session_card,
                                       tables=("customers",)))
        assert result.status is CheckStatus.PASS

    def test_by_a_column_grouped_on_a_different_one_fails(self, session_card):
        sql = "SELECT country, COUNT(*) AS n FROM customers GROUP BY country"
        result = check("grouping", run("how many customers by city", sql, card=session_card,
                                       tables=("customers",)))
        assert result.status is CheckStatus.FAIL
        assert "city" in result.evidence

    def test_no_group_by_at_all_fails(self, session_card):
        result = check(
            "grouping",
            run("how many customers per city", "SELECT COUNT(*) AS n FROM customers",
                card=session_card, tables=("customers",)),
        )
        assert result.status is CheckStatus.FAIL

    def test_each_is_a_grouping_word(self, session_card):
        sql = "SELECT city, COUNT(*) AS n FROM customers GROUP BY city"
        result = check("grouping", run("how many customers in each city", sql,
                                       card=session_card, tables=("customers",)))
        assert result.status is CheckStatus.PASS

    def test_ordered_by_is_not_grouped_by(self, session_card):
        """"sorted by price" is a sort, and reading it as a grouping is a false alarm."""
        sql = "SELECT name, unit_price_cents FROM products ORDER BY unit_price_cents DESC"
        result = check("grouping", run("products sorted by unit_price_cents", sql,
                                       card=session_card, tables=("products",)))
        assert result.status is CheckStatus.NA

    def test_a_grouping_word_naming_no_column_only_needs_some_group_by(self, session_card):
        sql = "SELECT city, COUNT(*) AS n FROM customers GROUP BY city"
        result = check("grouping", run("break the customers down by region", sql,
                                       card=session_card, tables=("customers",)))
        assert result.status is CheckStatus.PASS


class TestShape:
    def test_all_four_checks_are_always_reported(self, session_card):
        checks = run("anything at all", "SELECT 1 AS n", card=session_card)
        assert [item.name for item in checks] == [
            "time_window",
            "aggregation",
            "entities",
            "grouping",
        ]

    def test_every_check_carries_evidence(self, session_card):
        checks = run("how many refunds last month",
                     "SELECT COUNT(*) AS n FROM refunds", window=WINDOW,
                     tables=("refunds",), card=session_card)
        assert all(item.evidence.strip() for item in checks)

"""Relative time phrases resolve to a window, in code, against a committed date.

The model is told `as_of` and writes its own literals; this module computes the
same window independently so the two can be compared. That is the whole point:
telling a model the date is a prompt, and checking what it did with it is a
control.

Every case here is pinned against `as_of = 2026-08-31`, the committed
`[time] as_of`, so the table reads as a specification rather than as arithmetic
somebody has to redo in their head.
"""

import datetime as dt

import pytest

from sqeual.generate.timewindow import TimeWindow, resolve_time_window

AS_OF = dt.date(2026, 8, 31)


@pytest.mark.parametrize(
    ("question", "start", "end"),
    [
        ("how much did we refund last month", "2026-07-01", "2026-07-31"),
        ("how much did we refund this month", "2026-08-01", "2026-08-31"),
        ("how many orders this year", "2026-01-01", "2026-12-31"),
        ("how many orders last year", "2025-01-01", "2025-12-31"),
        ("how many orders in 2026", "2026-01-01", "2026-12-31"),
        ("how many orders in 2025", "2025-01-01", "2025-12-31"),
        ("refunds in Q2 2026", "2026-04-01", "2026-06-30"),
        ("refunds in Q2", "2026-04-01", "2026-06-30"),
        ("refunds in q4 2025", "2025-10-01", "2025-12-31"),
        ("orders last quarter", "2026-04-01", "2026-06-30"),
        ("orders this quarter", "2026-07-01", "2026-09-30"),
        ("tickets opened yesterday", "2026-08-30", "2026-08-30"),
        ("tickets opened today", "2026-08-31", "2026-08-31"),
        ("tickets in the last 7 days", "2026-08-25", "2026-08-31"),
        ("tickets in the last 30 days", "2026-08-02", "2026-08-31"),
        ("tickets in the past 1 day", "2026-08-31", "2026-08-31"),
        ("refunds since June", "2026-06-01", "2026-08-31"),
        ("refunds since 2025", "2025-01-01", "2026-08-31"),
        ("refunds in June", "2026-06-01", "2026-06-30"),
        ("refunds in June 2025", "2025-06-01", "2025-06-30"),
        ("refunds in feb 2024", "2024-02-01", "2024-02-29"),
        ("orders last week", "2026-08-24", "2026-08-30"),
        ("orders this week", "2026-08-31", "2026-09-06"),
    ],
)
def test_the_resolution_table(question, start, end):
    window = resolve_time_window(question, AS_OF)
    assert window is not None, question
    assert (window.start, window.end) == (start, end)


def test_a_question_with_no_time_phrase_resolves_to_nothing():
    assert resolve_time_window("how many customers are in Berlin", AS_OF) is None


def test_a_bare_small_number_is_not_read_as_a_year():
    """"top 10 products" must not resolve to the year 10."""
    assert resolve_time_window("the top 10 products by revenue", AS_OF) is None


def test_a_month_later_in_the_year_than_as_of_resolves_to_the_previous_year():
    """"since November" on 31 August means last November, not one in the future."""
    window = resolve_time_window("refunds since November", AS_OF)
    assert (window.start, window.end) == ("2025-11-01", "2026-08-31")


def test_the_earliest_phrase_in_the_question_wins():
    """Two phrases is an ambiguous question; the first one is the reproducible pick."""
    window = resolve_time_window("orders last month compared with last year", AS_OF)
    assert window.phrase == "last month"


def test_a_longer_phrase_beats_a_shorter_one_starting_at_the_same_word():
    window = resolve_time_window("tickets in the last 7 days please", AS_OF)
    assert window.phrase == "last 7 days"


def test_last_day_of_a_leap_february_is_the_29th():
    window = resolve_time_window("orders last month", dt.date(2024, 3, 15))
    assert (window.start, window.end) == ("2024-02-01", "2024-02-29")


def test_last_month_crosses_a_year_boundary():
    window = resolve_time_window("orders last month", dt.date(2026, 1, 4))
    assert (window.start, window.end) == ("2025-12-01", "2025-12-31")


def test_a_window_covers_its_own_endpoints():
    window = TimeWindow(phrase="x", start="2026-07-01", end="2026-07-31")
    assert window.covers("2026-07-01")
    assert window.covers("2026-07-31")
    assert not window.covers("2026-06-30")
    assert not window.covers("2026-08-01")


def test_the_day_after_a_window_is_its_exclusive_upper_bound():
    """`>= start AND < end+1` is the other correct spelling of a closed range."""
    window = TimeWindow(phrase="x", start="2026-07-01", end="2026-07-31")
    assert window.exclusive_end == "2026-08-01"


def test_a_month_prefix_is_consistent_when_it_overlaps():
    window = TimeWindow(phrase="x", start="2026-07-01", end="2026-07-31")
    assert window.consistent_with_literal("2026-07")
    assert not window.consistent_with_literal("2026-08")


def test_a_year_literal_is_consistent_when_it_overlaps():
    window = TimeWindow(phrase="x", start="2026-01-01", end="2026-12-31")
    assert window.consistent_with_literal("2026")
    assert not window.consistent_with_literal("2025")


def test_the_exclusive_upper_bound_literal_is_consistent():
    window = TimeWindow(phrase="x", start="2026-07-01", end="2026-07-31")
    assert window.consistent_with_literal("2026-08-01")


def test_a_literal_outside_the_window_is_not_consistent():
    window = TimeWindow(phrase="x", start="2026-07-01", end="2026-07-31")
    assert not window.consistent_with_literal("2026-08-15")

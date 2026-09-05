"""Executing the answer keys, and the property the whole set stands on.

The last test in this file is the important one: **every reference SQL in the
committed golden set guards and executes against the built database.** A broken
answer key makes a correct system look broken and a broken one look correct, and
nothing except a test notices.
"""

import pytest

from conftest import phase_b_config
from sqeual.eval.goldens import Expectation, GoldenQuestion
from sqeual.eval.reference import run_reference
from sqeual.execute import ExecuteLimits
from sqeual.guard import GuardPolicy


def question(sql: str | None, *, ordered: bool = False, expected="answer") -> GoldenQuestion:
    return GoldenQuestion(
        id="case_under_test",
        question="How many orders are there?",
        tags=("kind:scalar", "difficulty:easy"),
        expected=Expectation(expected),
        reference_sql=sql,
        ordered=ordered,
        notes="fixture",
    )


def run(config, card, sql, **kwargs):
    return run_reference(
        question(sql, **kwargs),
        card=card,
        policy=GuardPolicy.from_settings(config.guard),
        limits=ExecuteLimits.from_settings(config.execute),
        db_path=config.db.path,
        float_places=config.verify.float_places,
    )


def test_a_good_reference_executes(tmp_path, session_db, session_card):
    config = phase_b_config(tmp_path, session_db)
    outcome = run(config, session_card, "SELECT COUNT(*) AS n FROM orders")
    assert outcome.ok
    assert outcome.result.rows == ((2000,),)
    assert outcome.tables_used == ("orders",)
    assert outcome.digest is not None
    assert outcome.row_count == 1
    assert outcome.reason == ""


def test_a_reference_the_guard_refuses_is_a_broken_case(tmp_path, session_db, session_card):
    config = phase_b_config(tmp_path, session_db)
    outcome = run(config, session_card, "SELECT revenue FROM orders")
    assert not outcome.ok
    assert "refused by the guard" in outcome.reason
    assert "unknown_column" in outcome.reason
    assert outcome.canonical is None
    assert outcome.digest is None


def test_a_reference_that_cannot_execute_is_a_broken_case(
    tmp_path, session_db, session_card
):
    """Passes all fourteen rules — real table, real column, allowed function — and
    SQLite still refuses it, because `ROUND` does not take three arguments. The
    guard says nothing it cannot prove, and arity is stage 04's to catch."""
    config = phase_b_config(tmp_path, session_db)
    outcome = run(
        config, session_card, "SELECT ROUND(total_cents, 2, 3) AS n FROM orders LIMIT 5"
    )
    assert not outcome.ok
    assert "did not execute" in outcome.reason
    assert outcome.normalised_sql is not None
    assert outcome.canonical is None


def test_a_truncated_reference_is_a_broken_case(
    tmp_path, session_db, session_card, monkeypatch
):
    """An answer key cut off at the cap would score a complete answer as wrong.

    Unreachable through a valid `sqeual.toml`: config loading refuses an
    `[execute] max_rows` below `[guard] max_rows`, and the guard writes the
    smaller of the two into the SQL. It is a backstop for the day somebody
    relaxes that rule, so it is tested by forcing the condition rather than by
    a configuration that cannot exist.
    """
    from sqeual.eval import reference as reference_module
    from sqeual.execute import ExecutionPlan, ResultSet

    config = phase_b_config(tmp_path, session_db)
    monkeypatch.setattr(
        reference_module,
        "execute_sql",
        lambda *args, **kwargs: ResultSet(
            columns=("order_id",),
            rows=((1,), (2,)),
            row_count=2,
            truncated=True,
            elapsed_ms=1,
            plan=ExecutionPlan(steps=(), warnings=()),
        ),
    )
    outcome = run(config, session_card, "SELECT id AS order_id FROM orders LIMIT 50")
    assert not outcome.ok
    assert "row cap" in outcome.reason
    assert outcome.result is not None and outcome.result.truncated


def test_a_trap_has_no_reference_to_run(tmp_path, session_db, session_card):
    config = phase_b_config(tmp_path, session_db)
    with pytest.raises(ValueError, match="no reference SQL"):
        run(config, session_card, None, expected="abstain")


def test_the_reference_is_guarded_against_the_full_policy_not_a_slice(
    tmp_path, session_db, session_card
):
    """A reference written by a human against the schema is not slice-limited."""
    config = phase_b_config(tmp_path, session_db)
    outcome = run(
        config,
        session_card,
        "SELECT COUNT(*) AS n FROM order_items AS oi "
        "JOIN products AS p ON p.id = oi.product_id",
    )
    assert outcome.ok


def test_every_committed_reference_guards_and_executes(
    tmp_path, session_db, session_card, committed_questions
):
    """The property the whole golden set stands on."""
    config = phase_b_config(tmp_path, session_db)
    policy = GuardPolicy.from_settings(config.guard)
    limits = ExecuteLimits.from_settings(config.execute)
    checked = 0
    for case in committed_questions:
        if case.expected is not Expectation.ANSWER:
            assert case.reference_sql is None, case.id
            continue
        outcome = run_reference(
            case,
            card=session_card,
            policy=policy,
            limits=limits,
            db_path=config.db.path,
            float_places=config.verify.float_places,
        )
        assert outcome.ok, f"{case.id}: {outcome.reason}"
        checked += 1
    assert checked == 26


def test_an_empty_reference_result_is_still_a_working_reference(
    tmp_path, session_db, session_card, committed_questions
):
    """`products_never_ordered` returns nothing, and that is an answer."""
    config = phase_b_config(tmp_path, session_db)
    (case,) = [
        item for item in committed_questions if item.id == "products_never_ordered"
    ]
    outcome = run_reference(
        case,
        card=session_card,
        policy=GuardPolicy.from_settings(config.guard),
        limits=ExecuteLimits.from_settings(config.execute),
        db_path=config.db.path,
        float_places=config.verify.float_places,
    )
    assert outcome.ok
    assert outcome.row_count == 0


# The terms each hallucination bait dangles, and the claim every one of them
# makes: that this schema has no such thing. Written out rather than parsed out
# of the question, because the point is to state the claim somewhere a change to
# `data/schema.sql` can contradict it. The day somebody adds a `loyalty_tier`
# column, this test fires and says the case has stopped being bait.
INVENTED_TERMS = {
    "loyalty_tier_berlin": ("loyalty", "tier"),
    "shipping_carrier_last_month": ("carrier", "shipping"),
    "payment_method_split": ("payment", "paypal", "card"),
    "nps_by_segment": ("nps", "promoter", "score", "survey"),
    "refunds_approved_by_manager": ("approver", "approved", "manager"),
    "warehouse_most_items": ("warehouse", "shipped"),
}


def test_every_bait_names_something_this_schema_does_not_have(
    session_card, committed_questions
):
    columns = {column.name.lower() for table in session_card.tables for column in table.columns}
    tables = {table.name.lower() for table in session_card.tables}
    identifiers = columns | tables

    baits = [
        case
        for case in committed_questions
        if case.trap is not None and case.trap.value == "hallucination_bait"
    ]
    assert {case.id for case in baits} == set(INVENTED_TERMS), (
        "every hallucination bait must declare what it invents"
    )
    for case in baits:
        for term in INVENTED_TERMS[case.id]:
            matches = [name for name in identifiers if term in name]
            assert not matches, f"{case.id} is no longer bait: {term!r} matches {matches}"


def test_the_columns_that_make_the_bait_credible_are_real(session_card):
    """A bait works because *part* of it resolves. These are the parts."""
    columns = {column.name.lower() for table in session_card.tables for column in table.columns}
    for name in ("channel", "segment", "team", "city", "category", "reason", "status"):
        assert name in columns, name

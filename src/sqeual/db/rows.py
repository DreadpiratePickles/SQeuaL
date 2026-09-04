"""Generate the rows, deterministically, from one seeded PRNG.

Every draw in this module comes from a single `random.Random` created with the
configured seed and consumed in a fixed order. That is the whole of the
determinism argument, and it is fragile in exactly one way worth naming: the
sequence of *calls* has to be fixed too. Iterating a `set`, using a
dictionary's insertion order accidentally, or drawing a value inside a
conditional that depends on the wall clock would all leave the seed in place
and the output different. The suite asserts byte-equality between two builds
precisely because that mistake is invisible by inspection.

Causality is enforced here rather than trusted: an order is never dated before
its customer signed up, a refund never before its order, a ticket never closed
before it opened. A database that violates those makes questions like "average
days to refund" return a negative number, and a text-to-SQL tool that answers
correctly over incoherent data looks broken.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta

from . import vocabulary as v

WINDOW_START = date(2025, 1, 1)
WINDOW_END = date(2026, 8, 31)
"""The period the database covers. Committed here rather than derived from the
clock: a fixture whose contents depend on the day it was built is not a fixture.
"""

N_CUSTOMERS = 250
N_PRODUCTS = 40
N_AGENTS = 12
N_ORDERS = 2_000
N_TICKETS = 600
N_REFUNDS = 150

MAX_ITEMS_PER_ORDER = 4
SIGNUP_WINDOW_END = date(2026, 6, 30)
"""Customers stop signing up two months before the window ends, so that every
customer has room to place an order after signing up."""


@dataclass(frozen=True)
class Dataset:
    """Every row the builder will insert, in insertion order.

    Held as tuples of plain values rather than dataclasses per row: they go
    straight into `executemany`, and a second representation of a row is a
    second thing that can disagree with `data/schema.sql`.
    """

    customers: tuple[tuple, ...]
    products: tuple[tuple, ...]
    agents: tuple[tuple, ...]
    orders: tuple[tuple, ...]
    order_items: tuple[tuple, ...]
    tickets: tuple[tuple, ...]
    refunds: tuple[tuple, ...]


def _iso(day: date) -> str:
    return day.isoformat()


def _day_between(rng: random.Random, low: date, high: date) -> date:
    """A uniform day in `[low, high]`, clamped so `low > high` cannot explode.

    The clamp matters: dates are derived from each other (a refund follows an
    order that follows a signup), and one unlucky draw near the end of the
    window would otherwise raise deep inside generation rather than simply
    producing the last legal day.
    """
    if high < low:
        return low
    return low + timedelta(days=rng.randint(0, (high - low).days))


def _customers(rng: random.Random) -> tuple[tuple, ...]:
    rows = []
    for index in range(1, N_CUSTOMERS + 1):
        given = rng.choice(v.GIVEN_NAMES)
        family = rng.choice(v.SURNAMES)
        city, country = rng.choice(v.CITIES)
        signup = _day_between(rng, WINDOW_START, SIGNUP_WINDOW_END)
        # The index is in the local part so the UNIQUE constraint on email
        # cannot collide when two customers draw the same name — which happens
        # often with thirty given names and twenty-six surnames, and would
        # otherwise make the build fail on some seeds and not others.
        local = f"{given}.{family}.{index}".lower().replace(" ", "")
        rows.append(
            (
                index,
                f"{given} {family}",
                f"{local}@{v.EMAIL_DOMAIN}",
                city,
                country,
                _iso(signup),
                rng.choice(v.CUSTOMER_SEGMENTS),
            )
        )
    return tuple(rows)


def _products(rng: random.Random) -> tuple[tuple, ...]:
    rows = []
    for index in range(1, N_PRODUCTS + 1):
        adjective = rng.choice(v.PRODUCT_ADJECTIVES)
        noun, category = rng.choice(v.PRODUCT_NOUNS)
        # Prices in whole cents, on a 50-cent grid, between €4.99 and €399.99.
        price_cents = rng.randrange(499, 39_999, 50)
        rows.append((index, f"{adjective} {noun} {index}", category, price_cents))
    return tuple(rows)


def _agents(rng: random.Random) -> tuple[tuple, ...]:
    rows = []
    for index in range(1, N_AGENTS + 1):
        given = rng.choice(v.GIVEN_NAMES)
        family = rng.choice(v.SURNAMES)
        hired = _day_between(rng, date(2023, 1, 1), WINDOW_START)
        rows.append((index, f"{given} {family}", rng.choice(v.AGENT_TEAMS), _iso(hired)))
    return tuple(rows)


def _orders_and_items(
    rng: random.Random, customers: tuple[tuple, ...], products: tuple[tuple, ...]
) -> tuple[tuple[tuple, ...], tuple[tuple, ...]]:
    """Orders and their lines, generated together so the total can be summed.

    `orders.total_cents` is the sum of its own items rather than an independent
    draw. Two correct queries that mean the same thing then return the same
    number, which is the difference between a demo database and a trap.
    """
    signup_by_customer = {row[0]: date.fromisoformat(row[5]) for row in customers}
    price_by_product = {row[0]: row[3] for row in products}

    orders: list[tuple] = []
    items: list[tuple] = []
    item_id = 0

    for order_id in range(1, N_ORDERS + 1):
        customer_id = rng.randint(1, N_CUSTOMERS)
        order_day = _day_between(rng, signup_by_customer[customer_id], WINDOW_END)

        total_cents = 0
        for _ in range(rng.randint(1, MAX_ITEMS_PER_ORDER)):
            item_id += 1
            product_id = rng.randint(1, N_PRODUCTS)
            quantity = rng.randint(1, 3)
            unit_price_cents = price_by_product[product_id]
            items.append((item_id, order_id, product_id, quantity, unit_price_cents))
            total_cents += quantity * unit_price_cents

        orders.append(
            (
                order_id,
                customer_id,
                _iso(order_day),
                rng.choice(v.ORDER_STATUSES),
                rng.choice(v.ORDER_CHANNELS),
                total_cents,
            )
        )
    return tuple(orders), tuple(items)


def _tickets(
    rng: random.Random, customers: tuple[tuple, ...], orders: tuple[tuple, ...]
) -> tuple[tuple, ...]:
    signup_by_customer = {row[0]: date.fromisoformat(row[5]) for row in customers}
    orders_by_customer: dict[int, list[tuple[int, date]]] = {}
    for order_id, customer_id, order_date, *_ in orders:
        orders_by_customer.setdefault(customer_id, []).append(
            (order_id, date.fromisoformat(order_date))
        )

    rows = []
    for ticket_id in range(1, N_TICKETS + 1):
        customer_id = rng.randint(1, N_CUSTOMERS)
        subject, category = rng.choice(v.TICKET_SUBJECTS)

        # Roughly three in four tickets are about an order; the rest are the
        # account and product questions that have nothing to attach to. Both
        # branches must be populated or the nullable foreign key is never
        # exercised by any query built on this database.
        candidates = orders_by_customer.get(customer_id, [])
        attach = bool(candidates) and rng.random() < 0.75
        if attach:
            order_id, order_day = rng.choice(candidates)
            earliest = order_day
        else:
            order_id = None
            earliest = signup_by_customer[customer_id]

        opened = _day_between(rng, earliest, WINDOW_END)
        status = rng.choice(v.TICKET_STATUSES)
        if status == "closed":
            closed = _day_between(rng, opened, min(opened + timedelta(days=21), WINDOW_END))
            closed_date = _iso(closed)
        else:
            closed_date = None

        rows.append(
            (
                ticket_id,
                customer_id,
                order_id,
                rng.randint(1, N_AGENTS),
                _iso(opened),
                closed_date,
                rng.choice(v.TICKET_CHANNELS),
                category,
                rng.choice(v.TICKET_PRIORITIES),
                status,
                subject,
            )
        )
    return tuple(rows)


def _refunds(
    rng: random.Random, orders: tuple[tuple, ...], tickets: tuple[tuple, ...]
) -> tuple[tuple, ...]:
    """Refunds, drawn from orders without replacement.

    Without replacement because a duplicate refund on one order is a real
    business event and a terrible fixture: "total refunded per order" would
    then need a GROUP BY that nobody writing an example question expects.
    """
    tickets_by_order: dict[int, list[int]] = {}
    for row in tickets:
        if row[2] is not None:
            tickets_by_order.setdefault(row[2], []).append(row[0])

    order_by_id = {row[0]: row for row in orders}
    chosen = rng.sample(sorted(order_by_id), N_REFUNDS)

    rows = []
    for refund_id, order_id in enumerate(sorted(chosen), start=1):
        order = order_by_id[order_id]
        order_day = date.fromisoformat(order[2])
        total_cents = order[5]
        refund_day = _day_between(rng, order_day, min(order_day + timedelta(days=45), WINDOW_END))
        # Full refund most of the time, a partial one otherwise. Never more
        # than the order was worth: a refund larger than its order is the kind
        # of impossible row that makes a correct answer look wrong.
        if rng.random() < 0.7:
            amount_cents = total_cents
        else:
            amount_cents = max(1, (total_cents * rng.randint(20, 90)) // 100)
        candidate_tickets = tickets_by_order.get(order_id, [])
        ticket_id = rng.choice(candidate_tickets) if candidate_tickets else None
        rows.append(
            (
                refund_id,
                order_id,
                ticket_id,
                _iso(refund_day),
                amount_cents,
                rng.choice(v.REFUND_REASONS),
            )
        )
    return tuple(rows)


def generate(seed: int) -> Dataset:
    """Every row of the database, drawn from one seeded PRNG in a fixed order.

    The order of the calls below is part of the contract: reordering them
    changes every table, not only the one that moved, because they all draw
    from the same stream.
    """
    rng = random.Random(seed)
    customers = _customers(rng)
    products = _products(rng)
    agents = _agents(rng)
    orders, order_items = _orders_and_items(rng, customers, products)
    tickets = _tickets(rng, customers, orders)
    refunds = _refunds(rng, orders, tickets)
    return Dataset(
        customers=customers,
        products=products,
        agents=agents,
        orders=orders,
        order_items=order_items,
        tickets=tickets,
        refunds=refunds,
    )

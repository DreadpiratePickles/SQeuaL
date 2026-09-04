-- The support / e-commerce database SQeuaL answers questions about.
--
-- Hand-written and committed, because it is the thing the whole tool is
-- checked against: the guard decides whether a column is a hallucination by
-- asking this schema, so the schema has to be readable by a human who wants to
-- argue with a verdict. The generator in `src/sqeual/db/build.py` executes this
-- file verbatim and then fills it; it never issues DDL of its own, so there is
-- exactly one description of the shape of this database and it is this file.
--
-- Three conventions hold throughout and are load-bearing:
--
--   * Money is an INTEGER count of cents, never a REAL. Binary floating point
--     cannot represent 0.1, and a refund total is an authoritative number.
--     Column names carry the unit (`amount_cents`) so a query that forgets to
--     divide is wrong in a way a reader can see.
--   * Dates are TEXT in ISO `YYYY-MM-DD`. SQLite has no date type; ISO text
--     sorts chronologically as text and is what DATE(), STRFTIME() and
--     JULIANDAY() expect, which is why those three are on the guard's function
--     allowlist and nothing else date-shaped is.
--   * Every foreign key is declared. SQLite does not enforce them unless
--     `PRAGMA foreign_keys = ON`, and the builder turns that on — but the
--     declaration earns its place even where it is not enforced, because the
--     schema slicer reads `PRAGMA foreign_key_list` to decide which tables have
--     to travel together. An undeclared relationship is a join the tool cannot
--     know about.

CREATE TABLE customers (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL UNIQUE,
    city         TEXT NOT NULL,
    country      TEXT NOT NULL,
    signup_date  TEXT NOT NULL,
    segment      TEXT NOT NULL
);

CREATE TABLE products (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    category          TEXT NOT NULL,
    unit_price_cents  INTEGER NOT NULL
);

CREATE TABLE agents (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    team         TEXT NOT NULL,
    hired_date   TEXT NOT NULL
);

CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    order_date   TEXT NOT NULL,
    status       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    total_cents  INTEGER NOT NULL
);

CREATE TABLE order_items (
    id                INTEGER PRIMARY KEY,
    order_id          INTEGER NOT NULL REFERENCES orders(id),
    product_id        INTEGER NOT NULL REFERENCES products(id),
    quantity          INTEGER NOT NULL,
    unit_price_cents  INTEGER NOT NULL
);

CREATE TABLE tickets (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    -- Nullable on purpose: plenty of tickets are questions about no order at
    -- all. A schema that forced one would make the generator invent a link,
    -- and every "tickets per order" answer would then be quietly wrong.
    order_id     INTEGER REFERENCES orders(id),
    agent_id     INTEGER REFERENCES agents(id),
    opened_date  TEXT NOT NULL,
    closed_date  TEXT,
    channel      TEXT NOT NULL,
    category     TEXT NOT NULL,
    priority     TEXT NOT NULL,
    status       TEXT NOT NULL,
    subject      TEXT NOT NULL
);

CREATE TABLE refunds (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(id),
    ticket_id     INTEGER REFERENCES tickets(id),
    refund_date   TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    reason        TEXT NOT NULL
);

-- Indexes on the foreign keys the demo questions join across. They exist so
-- that `EXPLAIN QUERY PLAN` has something to report other than a full scan of
-- everything — stage 04 flags a scan of a large table as a warning, and a
-- warning that fires on every query is a warning nobody reads.
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_tickets_customer ON tickets(customer_id);
CREATE INDEX idx_tickets_opened ON tickets(opened_date);
CREATE INDEX idx_refunds_order ON refunds(order_id);
CREATE INDEX idx_refunds_date ON refunds(refund_date);

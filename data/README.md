# `data/`

Two things live here, and only one of them is committed.

## `schema.sql` — committed

The DDL, hand-written. `sqeual db build` executes it verbatim and then fills the
tables it defines; the generator issues no `CREATE` of its own, so there is
exactly one description of this database's shape and it is this file.

It is committed because it is the definition the **guard** checks hallucinated
columns against. When `sqeual guard` says `orders.region does not exist`, this
file is the reason, and a person who wants to argue with that verdict has to be
able to read it.

Three conventions in it are load-bearing:

- **Money is an `INTEGER` count of cents**, never a `REAL`, with the unit in the
  column name (`amount_cents`, `total_cents`). Binary floating point cannot
  represent 0.1, a refund total is an authoritative number, and a name that
  carries its unit makes a query that forgot to divide wrong in a way a reader
  can see.
- **Dates are `TEXT` in ISO `YYYY-MM-DD`.** SQLite has no date type; ISO text
  sorts chronologically as text and is what `DATE()`, `STRFTIME()` and
  `JULIANDAY()` expect — which is why those three are on the guard's function
  allowlist and nothing else date-shaped is.
- **Every foreign key is declared**, including the two nullable ones
  (`tickets.order_id`, `refunds.ticket_id`). The schema slicer reads
  `PRAGMA foreign_key_list` to decide which tables have to travel together, and
  it prefers a join path made of `NOT NULL` edges because a join through a
  nullable key silently drops rows. An undeclared relationship is a join the
  tool cannot know about.

## `support.db` — **not** committed

Gitignored. It is a build artefact, and the generator that produces it is
committed instead:

```bash
uv run python scripts/sqeual.py db build
```

The generator is deterministic — one seed from `[db] seed`, one
`random.Random`, one fixed order of draws — so the same seed reproduces the same
database byte for byte. `tests/test_db_build.py::test_two_builds_are_byte_identical`
asserts exactly that.

Committing the binary instead would fail three ways: nobody can review a `.db`
diff, it changes wholesale on every regeneration, and a reader has to *trust*
that it matches the DDL sitting beside it. `db build` prints a row digest so two
builds can be compared without diffing binaries.

## What is in it

Seven tables, a fictional support and e-commerce business, dates spanning
2025-01-01 to 2026-08-31.

| Table | Rows | Notes |
|---|---:|---|
| `customers` | 250 | 20 cities across 14 countries, 3 segments |
| `products` | 40 | 6 categories, prices 499–39,999 cents |
| `agents` | 12 | 4 teams |
| `orders` | 2,000 | 5 statuses, 4 channels. `total_cents` is the sum of its own items |
| `order_items` | 5,013 | 1–4 lines per order |
| `tickets` | 600 | 8 categories, 4 priorities, 3 statuses. `order_id` is nullable on purpose |
| `refunds` | 150 | One per order at most, 6 reasons, never more than the order total |

The row counts are exact and reproducible; they come from
`sqeual db build`, recorded 2026-09-04.

## What is deliberately *not* realistic

Everything about the people. Names are assembled from two short lists of given
names and surnames in `src/sqeual/db/vocabulary.py`; emails end in
`example.invalid`, which RFC 2606 reserves so that nothing here can ever address
a real mailbox. There is no scraped dataset, no anonymised export, and no
customer text.

The realism this project actually needs is **structural**: enum-shaped status
columns so the schema card has a vocabulary to show a model, a nullable foreign
key so the slicer's join-path preference has something to prefer, causally
ordered dates so "average days to refund" is never negative, and money in
integer cents. None of that requires a real person's name.

## The invariants a correct answer depends on

Two queries that mean the same thing return the same number:

```sql
SELECT SUM(total_cents) FROM orders;
SELECT SUM(quantity * unit_price_cents) FROM order_items;
```

Causality holds throughout: no order predates its customer's signup, no refund
predates its order, no ticket closes before it opens, and no refund exceeds its
order's total. A database that violated any of those would make a **correct**
text-to-SQL answer look wrong, and the first thing anybody would debug is the
tool rather than the fixture. `tests/test_db_build.py` asserts all of them.

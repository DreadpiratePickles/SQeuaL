# SQeuaL

**Text-to-SQL where the model never writes a number.** It proposes SQL; that
proposal is treated as untrusted input, and deterministic code parses it, checks
every table and column against the real schema, rewrites it to carry a row
limit, and runs it on a connection that cannot write. Phase A is that
deterministic half — the guardrails, built before the model that needs them.

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)](.python-version)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![tests: 287](https://img.shields.io/badge/tests-287-brightgreen)](tests/)
[![coverage: 97%](https://img.shields.io/badge/coverage-97%25-brightgreen)](#status)

## The problem, in 20 seconds

Ask a language model "how much did we refund to customers in Berlin last month".
It is genuinely good at turning that into a `SELECT` — it has seen a million of
them. It is genuinely bad at the thing that follows, which is reading 150 rows
and adding them up.

Ask it to do both and it does the first well, the second plausibly, and hands
you a figure with two decimal places and no relationship to the database. Worse,
it will cheerfully write `SELECT region, SUM(revenue) FROM orders` against a
table with neither column — and you find out at execution time, if you find out
at all.

So it does exactly one job. The rest is code.

## What is built

| Stage | What it does | Status |
|---|---|---|
| `01_db` | A fictional support/e-commerce database from a committed DDL and a fixed seed. Deterministic: the same seed gives a byte-identical file, so every figure in these docs is a fact rather than a snapshot | **built** |
| `02_schema` | Introspects the live database into a typed schema card — types, nullability, keys, foreign keys with *their* nullability, row counts, and sample values for enum-shaped columns — plus a slicer that picks the tables one question needs and says why | **built** |
| `03_guard` | Parses a proposed statement with `sqlglot` and runs twelve named rules against the parse tree. Returns a rule table, a verdict, and the statement that should run in its place | **built** |
| `04_execute` | Runs it on a connection that is read-only at four independent layers, with a wall-clock budget enforced from inside the query | **built** |
| `05_generate` | Question → candidate SQL as strict JSON, k-sampled, agreement measured on result sets | contract only — Phase B |
| `06_verify` | Does the SQL answer the question that was *asked*? | contract only — Phase C |
| `07_answer` | Render the sentence from the rows, in code | contract only — Phase C |
| `08_eval` | Golden questions, execution accuracy, guard-catch rate, `regress` CI gate | contract only — Phase C |

Read [`CONTEXT.md`](CONTEXT.md) to navigate and
[`docs/design.md`](docs/design.md) for why every decision went the way it did.

**No stage in Phase A calls a model.** Nothing here reads an API key, opens a
socket, or imports a vendor SDK — and that is the point, not an accident of
scheduling. Everything a text-to-SQL system needs in order to be *safe* is
deterministic, so it is built first, and the model arrives into a system that
already refuses bad SQL.

## Install

```bash
uv sync
uv run python scripts/sqeual.py db build
uv run pytest -q
```

Python 3.12 and [uv](https://docs.astral.sh/uv/). Project 1 is pinned as a git
dependency at commit `888a3e3`; Phase A calls none of it. Nothing in the test
suite touches the network.

## A guided first session

Everything below runs offline against the database you just built.

```bash
# 1. What does the model get to see?
uv run python scripts/sqeual.py schema show

# 2. Which tables does one question actually need — and why?
uv run python scripts/sqeual.py schema slice \
    --question "how much did we refund to customers in Berlin last month"

# 3. Check a statement without running it. Exit 0 pass, 1 fail.
uv run python scripts/sqeual.py guard --sql "SELECT COUNT(*) FROM orders"

# 4. Check one that hallucinates.
uv run python scripts/sqeual.py guard --sql "SELECT revenue FROM orders"

# 5. Guard it and run it. Exit 0, 1 if refused, 3 if execution failed.
uv run python scripts/sqeual.py run --plan \
    --sql "SELECT status, COUNT(*) FROM orders GROUP BY status"
```

There is no `ask` command. That is Phase B.

## What the slicer says

Recorded 2026-09-04, verbatim:

```
$ sqeual schema slice --question "how much did we refund to customers in Berlin last month"
question: how much did we refund to customers in Berlin last month
  1. refunds        the question names refunds
  2. customers      the question names customers
  3. orders         joins customers to refunds
  4. tickets        joins customers to refunds
```

Four tables out of seven, each with a reason a person can argue with. `orders`
and `tickets` are both there because both connect `refunds` to `customers` —
and `orders` is listed first because `refunds.order_id` is `NOT NULL` while
`refunds.ticket_id` is not, so joining through `orders` cannot drop rows. At a
tighter `--max-tables 3` only `orders` survives, for exactly that reason.

No embeddings. The answer to "why did it pick that" has to be a rule somebody
can read and edit — and in Phase B this slice becomes the guard's
`allowed_tables`, which makes it a security boundary rather than a prompt hint.

## What the guard says

A query that passes:

```
$ sqeual guard --sql "SELECT c.city, SUM(r.amount_cents) AS refunded_cents
    FROM refunds r JOIN orders o ON o.id = r.order_id
    JOIN customers c ON c.id = o.customer_id
    WHERE r.refund_date >= '2026-08-01' GROUP BY c.city ORDER BY refunded_cents DESC"

  parses                 PASS   parsed as SQLite
  single_statement       PASS   one statement
  no_forbidden_syntax    PASS   no DDL, DML, PRAGMA, ATTACH or transaction control
  select_only            PASS   the statement is a SELECT
  known_tables           PASS   3 table(s), all real
  allowed_tables         PASS   policy allows every table in this database
  known_columns          PASS   7 column reference(s), all real
  unambiguous_columns    PASS   every unqualified column resolves to one source
  allowed_functions      PASS   functions called: SUM
  subquery_depth         PASS   nested to depth 0, limit 2
  star_expansion         PASS   no SELECT *
  row_limit              PASS   no LIMIT was given; LIMIT 200 added [limit_injected]
  verdict: PASS
  sql to run: SELECT c.city, SUM(r.amount_cents) AS refunded_cents FROM refunds AS r JOIN orders AS o ON o.id = r.order_id JOIN customers AS c ON c.id = o.customer_id WHERE r.refund_date >= '2026-08-01' GROUP BY c.city ORDER BY refunded_cents DESC LIMIT 200
(exit 0)
```

And one that does not — the shape a model actually produces when it guesses:

```
$ sqeual guard --sql "SELECT region, SUM(revenue) FROM orders o
    JOIN customers c ON c.id = o.customer_id WHERE id > 0 GROUP BY region"

  ...
  known_columns          FAIL   region does not exist on any table this query selects from.
                                orders has: id, customer_id, order_date, status, channel,
                                total_cents; customers has: id, name, email, city, country,
                                signup_date, segment; revenue does not exist on any table
                                this query selects from. [...]                [unknown_column]
  unambiguous_columns    FAIL   id is on more than one source in this query
                                (customers, orders); qualify it              [ambiguous_column]
  ...
  verdict: FAIL
(exit 1)
```

Wrapped for width; the elided part repeats the same column list for `revenue`.
Everything else is verbatim, recorded 2026-09-04.

Three things in that failure are deliberate. It is caught **before execution**,
so no connection was opened. It names what *does* exist, because a finding that
says only "no such column" makes the next attempt a guess. And `ambiguous_column`
is a separate code from `unknown_column`: `id` is not a hallucination — it exists
on both tables — so a repair loop should be told to qualify it, not told it is
imaginary.

## The five ideas worth stealing

**Parse it, do not grep it.** A blocklist regex for `DROP` rejects
`SELECT drop_reason FROM refunds` and passes `SELECT 1;/**/DrOp TABLE x`, and
both mistakes are the same mistake: a check on a string is a check on a
rendering, not on the thing. `SELECT 1 -- ; DROP TABLE x` is the case that
settles it — it looks like two statements and it is one, because the semicolon
is inside a comment, and a parser knows that because SQLite knows that.

**A hallucinated column is caught before execution.** `SELECT o.city FROM orders
o JOIN customers c ...` uses a real column on a real table — just not *that*
table. A table-existence check passes it straight through; only resolving each
column against the sources actually in scope catches it. That is the single most
common shape of text-to-SQL hallucination.

**What runs is what was checked.** The executor is handed the statement the
guard *regenerated from the tree it read*, with comments stripped and the row
limit already in it. No code path executes a caller's original string, so there
is no gap between the thing that was approved and the thing that ran — and a
refused report carries no statement at all.

**Read-only is not the same property as sandboxed.** `mode=ro` and
`PRAGMA query_only = 1` both work, and neither stops
`ATTACH DATABASE '/tmp/x.db' AS other` — which succeeds, and creates the file.
Both layers are behaving correctly; ATTACH is not a write to the *main*
database. It takes a fourth layer, `SQLITE_LIMIT_ATTACHED = 0`, and a test that
asserts no file appeared.

**A timer cannot interrupt SQLite.** `sqlite3` runs a query inside a C call that
holds the GIL, so a `threading.Timer` fires only after the query it was meant to
abort has already returned. The budget is enforced by a progress handler running
*inside* the query. The test for it is an unbounded recursive CTE: without the
handler it does not fail, it hangs the suite.

## Layout

```
sqeual.toml               every limit a generated query obeys. No model id lives here
data/schema.sql           the DDL, hand-written                              (committed)
data/support.db           the generated database          (gitignored — the generator is committed)
src/sqeual/db/            seeded deterministic generator + the invented word lists
src/sqeual/schema/        card, renderer, slicer
src/sqeual/guard/         policy, twelve rules, the column resolver, the report
src/sqeual/execute/       read-only connection, budget, typed errors, ResultSet
src/sqeual/config.py      model identifiers, and nothing else. Phase A calls none of it
scripts/sqeual.py         db build | schema show|slice | guard | run
stages/0*/CONTEXT.md      eight stage contracts; four built, four marked PLANNED
docs/design.md            every decision and its reason
```

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | The run completed and found nothing wrong |
| 1 | The run completed and produced a **finding** — the guard refused the SQL, or a slice matched no table |
| 2 | The run **never started** — bad configuration, missing database, bad usage |
| 3 | The run started and **execution failed** — a timeout, or a SQLite error |

The 1/3 split is the one that matters in a pipeline. A 1 means the model wrote a
bad query and Phase B should try again *with the finding*; a 3 means the query
was fine and re-writing it would fail identically.

## Status

Phase A is **implemented and tested**, not deployed and not validated live.

- 287 tests, 97% statement coverage, `ruff` clean at line length 100. None
  touches the network.
- **No model has ever been called from this repository.** There is no `.env` in
  it and none was created. Every figure above comes from deterministic code
  running against a locally generated database.
- **There is no accuracy number, and there will not be one until stage 08.**
  Accuracy is a property of the model-plus-guard system, and no model has run.
  The guard catches every adversarial case in `tests/`, which is worth almost
  nothing on its own: those cases were written by the same pass that wrote the
  guard.
- The database is fictional and deliberately so: invented names from short word
  lists, `example.invalid` addresses, no scraped or anonymised data anywhere.

## License

MIT. See [LICENSE](LICENSE).

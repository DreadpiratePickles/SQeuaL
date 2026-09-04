# Stage: 01_db

## Objective

Turn a committed DDL file and a fixed seed into a realistic support and
e-commerce database that is byte-identical every time it is built.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `data/schema.sql` | 3 | Authoritative | Yes | Whole file, executed verbatim |
| `sqeual.toml` | 3 | Authoritative | Yes | `[db] path`, `[db] schema_sql`, `[db] seed` |
| `src/sqeual/db/vocabulary.py` | 3 | Authoritative | Yes | The word lists rows are drawn from |
| `--out`, `--force` | 4 | Operator input | No | Override the destination; permit an overwrite |

The stage cannot see a question, a schema card, or a guard policy. It is the
only stage that writes anything, and it writes exactly one file.

## Process

Every step is deterministic code. No model is called.

1. Validate the seed at the boundary. A non-integer — or a `bool`, which passes
   `isinstance(x, int)` in Python — raises `DatabaseBuildError` before any file
   is touched.
2. Refuse to overwrite an existing database unless `overwrite=True`. Rebuilding
   over one somebody is querying is destructive, so it is asked for rather than
   assumed.
3. Read `data/schema.sql` as text. Empty or unreadable is a typed error naming
   the path. The generator issues no `CREATE` of its own: there is exactly one
   description of this database's shape and it is that file.
4. Generate every row from a single `random.Random(seed)`, consumed in a fixed
   order — customers, products, agents, orders and their items, tickets, then
   refunds. The order of those calls is part of the contract: they draw from one
   stream, so reordering them changes every table, not only the one that moved.
5. Enforce causality while generating rather than checking for it afterwards. An
   order is never dated before its customer signed up, a refund never before its
   order, a ticket never closed before it opened, and no refund exceeds its
   order's total.
6. Compute `orders.total_cents` as the sum of that order's own items. Two
   correct queries that mean the same thing then return the same number, which
   is the difference between a demo database and a trap.
7. Write into a temporary file beside the destination, with
   `PRAGMA foreign_keys = ON`, inserting parents before children.
8. Run `PRAGMA foreign_key_check`. A violation means the generator and
   `data/schema.sql` disagree about the shape of the database, and it stops the
   build rather than producing a database that fails on one join.
9. `os.replace` the temporary file into place. The build is atomic: a
   half-populated database that looked fine to `SELECT COUNT(*)` and failed on
   the one join a question needed would be worse than no database at all.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `data/support.db` | SQLite, seven tables per `data/schema.sql` | Stages 02 and 04. **Gitignored** |
| stdout, via `db build` | Per-table row counts, a row digest, and the schema hash | A human checking that a rebuild reproduced the same database |

The database is **not committed**. A deterministic generator plus a seed is a
smaller, more reviewable and more honest way to carry a 700 KB binary in Git
than the binary: nobody can review a `.db` diff, and nobody can check by eye
that it matches the DDL beside it. `data/schema.sql` **is** committed, because
it is the definition the guard checks hallucinated columns against.

## Verify

- `uv run pytest -q tests/test_db_build.py` — 29 tests covering determinism,
  shape, internal consistency and failure behaviour.
- The load-bearing test is `test_two_builds_are_byte_identical`. Row-level
  equality would be enough for the tool to work; byte equality is asserted
  because it is checkable, and the moment it stops holding, something
  non-deterministic has entered the generator — an iteration over a set, a
  timestamp, a dictionary ordering — and that test names the day it happened.
- `test_two_builds_have_identical_row_digests` asserts the weaker claim
  separately, so that a future SQLite version which pads a page differently does
  not leave the suite with no determinism test at all.
- `test_order_totals_equal_the_sum_of_their_items` and
  `test_no_refund_exceeds_its_order_total` are the consistency invariants a
  correct text-to-SQL answer depends on.
- `test_low_cardinality_columns_are_enum_shaped` pins the exact distinct count
  of every column the schema card samples. If the generator ever produced 200
  distinct statuses the card would stop sampling them, the model would stop
  being told that `status` holds `'refunded'`, and every downstream slice and
  guard test would quietly change meaning.
- `uv run python scripts/sqeual.py db build --force` twice, comparing the two
  printed row digests, is the same check by hand.

## Approval

No human gate. Building a database exposes nobody and reaches nothing outside
the machine — the only thing this stage can destroy is a local file, and
`--force` is the gate on that.

What does need review is a change to `data/schema.sql` or to `[db] seed`. Both
are committed, both change every figure in the documentation, and a pull request
containing either is the place to notice.

## Failure Behavior

| Failure | Behavior |
|---|---|
| Seed is not an integer (or is a `bool`) | `DatabaseBuildError` before any file is touched |
| `data/schema.sql` missing, unreadable or empty | `DatabaseBuildError` naming the path. CLI exit 2 |
| DDL will not apply | `DatabaseBuildError` carrying SQLite's message |
| DDL does not define a table the generator fills | The insert fails and the build stops. Never a partly-populated database |
| An insert violates a constraint | `DatabaseBuildError` naming the table. The offending row is deliberately **not** in the message — the habit matters more than this data, which is generated |
| `PRAGMA foreign_key_check` reports violations | `DatabaseBuildError` with the count. The generator and the DDL disagree |
| Destination exists and `--force` was not given | `DatabaseBuildError` saying so. CLI exit 2 |
| Anything above, after the temporary file was created | The temporary file is removed on every failure path, including on `KeyboardInterrupt`. Nothing is left at the destination and nothing is left beside it |

No rollback is needed beyond that: the destination is only ever touched by one
atomic rename, and that rename is the last thing to happen.

Escalation path: a determinism failure is a human question, not a retry. Two
builds from one seed differing means something in the generator now depends on
something other than the seed; find what before trusting any figure produced
from either build.

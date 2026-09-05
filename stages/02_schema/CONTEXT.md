# Stage: 02_schema

## Objective

Turn a live database into a typed schema card, render the subset of it a model
should see for one question, and say why each table is in that subset.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `data/support.db` | 4 | Authoritative | Yes | `sqlite_master`, `PRAGMA table_info`, `PRAGMA foreign_key_list`, and one `COUNT(DISTINCT)` per column |
| `sqeual.toml` | 3 | Authoritative | Yes | `[schema]` and `[schema.synonyms]`, `[db] path` |
| `--question` | 4 | Operator input | Yes (for `slice`) | Tokenised; never executed, never interpolated |
| `--tables`, `--max-tables`, `--card` | 4 | Operator input | No | Narrow the card; override the slice width |

The card is read from the database itself and never from a hand-maintained
description. A second description of the schema is a second thing that can be
wrong, and the day it disagrees with the database is the day a hallucinated
column passes the guard and fails at execution instead.

## Process

Every step is deterministic code. No model is called, and no embedding is
computed.

1. Open the database with a read-only URI. Force one read immediately:
   `sqlite3.connect` is lazy, so a file that is not a database opens cleanly and
   fails later, and forcing the read turns that into one typed error at the
   boundary.
2. List tables from `sqlite_master`, excluding `sqlite_%`. Read each table's
   columns from `PRAGMA table_info` (name, declared type, nullability, primary
   key) and its edges from `PRAGMA foreign_key_list`, recording the *referring*
   column's nullability on each edge.
3. Sample values for enum-shaped columns only: at most
   `[schema] max_distinct_values` distinct values, at most
   `max_sample_values` of them, each truncated to `max_sample_chars`. Primary
   keys are excluded outright. A column with a value per row is content, not a
   vocabulary — sampling `email` would put realistic-looking addresses into a
   prompt and teach the model nothing.
4. Compute `schema_sha256` over the **shape only**: table names, column names,
   types, nullability, primary keys and foreign keys, serialised as sorted JSON.
   Row counts and sample values are in the card and out of the hash, because a
   policy pinned to a schema hash must not be invalidated by an `INSERT`.
5. To slice: split the question into alphanumeric words, fold each to lowercase
   with one trailing `s` removed, and score every table. A table named outright
   or reached by a `[schema.synonyms]` entry scores 3; a table carrying a column
   the question names scores 1. Consecutive words are also joined and matched
   against column names with their underscores removed, so "unit price cents"
   reaches `products.unit_price_cents`.
6. Collapse the evidence to the best score per (table, word) before summing. The
   word "refund" hits both the table name `refunds` and the synonym
   `refund -> refunds`; counting it twice would make a table look twice as
   relevant as an identical match that happens to have no synonym entry.
7. Rank seeds by score, then by which the question mentioned **first**, then by
   name. "How much did we refund to customers" is about refunds and mentions
   them first, and without that tiebreak the two tables tie at 3 and the
   alphabet decides.
8. Add **bridges**: a table adjacent by foreign key to two or more chosen seeds
   is the join path between them, and without it those seeds cannot be related
   at all. Rank bridges by how many seeds they join, then by how many of those
   edges are `NOT NULL` — a join through a nullable key silently drops rows —
   then by name.
9. Add **neighbours**: any table one foreign key from a seed, if there is still
   room. This tier trades precision for recall deliberately.
10. Render the chosen subset. Foreign keys pointing *out* of the subset are
    omitted: naming a join to a table the model was not shown is an invitation
    to write SQL against a table that is not there.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `SchemaCard` (in process) | `tables: tuple[Table, ...]`, `schema_sha256: str` | Stage 03 (the ground truth for tables and columns), stage 04 (row counts for the scan warning) |
| `SchemaSlice` (in process) | `entries: tuple[SliceEntry(table, reason), ...]`, ordered | Stage 05, which renders it and narrows the guard policy to it |
| stdout, via `schema show` / `schema slice` | Markdown card; a numbered list of tables with reasons | A human asking "what did the model actually see" |

## Verify

- `uv run pytest -q tests/test_schema_card.py tests/test_schema_render.py
  tests/test_schema_slice.py` — 68 tests.
- `test_schema_sha256_ignores_row_counts_and_samples` and
  `test_schema_sha256_changes_when_a_column_is_added` pin the hash's meaning
  from both sides.
- `test_high_cardinality_columns_get_no_samples` is the privacy check: `email`,
  `name`, `order_date` and `amount_cents` must never be sampled.
- `test_lookups_are_case_insensitive` matters more than it looks. SQL
  identifiers are case-insensitive, so a guard built on a case-sensitive card
  would report a hallucinated table for `SELECT * FROM ORDERS`, which runs.
- The worked example is pinned four ways: the slice for "how much did we refund
  to customers in Berlin last month" contains exactly `refunds`, `customers` and
  `orders` at width 3; it orders the named tables before the join path; every
  entry carries a reason; and `orders` beats `tickets` as the bridge *because*
  `refunds.order_id` is `NOT NULL` while `refunds.ticket_id` is not.
- `uv run python scripts/sqeual.py schema slice --question "..." --card` shows
  the same thing by hand, including the exact text a model would receive.

## Approval

No human gate. The stage reads and prints; it writes nothing and calls nothing.

The thing that deserves review is `[schema.synonyms]`. It is the tuning surface
for the whole slicer, it is committed, and a pull request that adds a term is
the place to argue about whether "case" should reach `tickets`.

## Failure Behavior

| Failure | Behavior |
|---|---|
| Database missing | `SchemaUnavailableError` naming the path. CLI exit 2 |
| File is not a database | `SchemaUnavailableError` from the forced first read, not from somewhere in the middle of introspection |
| Introspection fails partway | `SchemaUnavailableError`; no partial card is returned. A card missing a table would make the guard report a hallucination for a table that exists |
| Database has no tables | An **empty card**, not an error. A database with no tables is a fact about the database |
| Question is empty or whitespace | `SliceError`. Slicing nothing would return everything or nothing, and both would be guesses |
| `max_tables` is zero or negative | `SliceError` naming the parameter |
| A synonym names a table the card does not have | `SliceError` naming it. Refused rather than ignored: silently dropping it leaves the operator convinced that "invoice" reaches a table when it reaches nothing |
| Nothing in the question matches anything | An **empty slice**, and the CLI exits 1. Not a crash and not a guess: the caller falls back to the whole card and says why |
| `--tables` names a table that does not exist | It is skipped, not invented. The caller's mistake must not become a table in a prompt |

Escalation path: a slice that repeatedly picks the wrong tables is a synonym
map problem, not a code problem. Add the term, and the next slice is a diff
somebody can read — which is the entire reason this stage does not use
embeddings.

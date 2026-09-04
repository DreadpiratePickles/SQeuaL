# Stage: 04_execute

## Objective

Run one guarded statement against a connection that cannot write, cannot reach
another file, and cannot run for longer than its budget — and return the rows as
typed data with their provenance attached.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `GuardReport.normalised_sql` | 4 | Checked input | Yes | The whole statement, run verbatim |
| `data/support.db` | 4 | Authoritative | Yes | Opened `mode=ro` |
| `sqeual.toml` | 3 | Authoritative | Yes | `[execute] max_ms`, `max_rows`, `plan_scan_row_threshold` |
| `SchemaCard` from stage 02 | 4 | Authoritative | No | Row counts, used only to turn a full scan into a warning |
| `GuardReport.table_aliases` | 4 | Authoritative | No | Needed to read the query plan, which names aliases |

**This stage assumes the guard did not run.** That is the whole design. It is
handed `normalised_sql` because that string is the one that was checked — but
every protection here is built as if nothing checked it, because the failure
mode of "the guard has a hole" is a database with rows missing, and a hole in a
parser is not a hypothetical.

## Process

Every step is deterministic code. No model is called.

1. Validate the limits. A zero budget would abort every query and a zero row cap
   would return nothing from a query that worked, which is worse than an error.
2. Refuse an empty statement before the database is opened.
3. Open the database read-only, at four independent layers:
   - the guard already refused anything that is not a SELECT;
   - `file:...?mode=ro`, built with `Path.as_uri()` so a path containing a
     space, a `?` or a `#` is percent-encoded rather than silently changing
     which file — or which URI parameters — the connection gets;
   - `PRAGMA query_only = 1`;
   - `SQLITE_LIMIT_ATTACHED = 0`.
4. Force one read (`SELECT count(*) FROM sqlite_master`) so that a file which is
   not a database becomes one typed error at the boundary.
5. Install a **progress handler** carrying the deadline, checked every
   `PROGRESS_INSTRUCTIONS` virtual-machine instructions. Not a timer: `sqlite3`
   runs a query inside a C call that holds the GIL for its duration, so a
   `threading.Timer` would fire only after the query it was meant to abort had
   already finished. The progress handler runs *inside* the query.
6. Capture `EXPLAIN QUERY PLAN`. A plan that cannot be produced is not a
   failure — the query is about to run and will report its own errors, with a
   better message.
7. Execute, and fetch `max_rows + 1` rows. One more than the cap, so that "there
   were exactly `max_rows`" and "there were more and we stopped" are
   distinguishable.
8. Clear the progress handler and close the connection on the way out, including
   on an exception. A handler left installed with an expired deadline would
   abort every later query on the same connection.
9. Turn plan steps into warnings: a `SCAN` of a table whose row count exceeds
   `plan_scan_row_threshold`, resolved through the alias map because
   `EXPLAIN QUERY PLAN` prints the alias.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `ResultSet` (in process) | `columns`, `rows` (tuples), `row_count`, `truncated`, `elapsed_ms`, `plan` | Stage 07, which renders the answer **from these rows in code** |
| stdout, via `run --sql` | An aligned table, a row count, the elapsed time, and any warnings | A human. Exit 0, 1 if the guard refused, 3 if execution failed |

`truncated` is the field that earns its keep. A caller that received 500 rows
and did not know whether there were 501 would report a sum that is wrong and
looks right — the same class of mistake as a failed read that becomes an empty
result.

## Verify

- `uv run pytest -q tests/test_execute.py` — 40 tests.
- The load-bearing tests run `DELETE`, `UPDATE`, `INSERT`, `DROP`, `CREATE` and
  `ALTER` **straight at the executor with no guard in front**, which is the only
  way to find out whether layers 2 to 4 are real rather than decorative.
  `test_the_database_is_unchanged_after_an_attempted_write` counts the rows
  afterwards.
- `test_the_connection_refuses_writes_even_with_query_only_turned_off` runs
  `PRAGMA query_only = 0` first, because that is the obvious way through layer 3
  and layer 2 has to be what stops it.
- `test_attaching_a_second_database_is_refused` asserts both the error **and**
  that no file was created. Without layer 4 this test creates a file on disk
  through a connection opened read-only.
- `test_an_unbounded_recursive_query_is_aborted` uses a recursive CTE that never
  terminates; without the progress handler it would hang the suite rather than
  fail it.
- `test_a_covering_index_scan_is_still_a_full_scan` pins the warning's meaning:
  `SCAN orders USING COVERING INDEX ...` reads the index instead of the table and
  still reads every row.
- `test_the_error_message_does_not_contain_the_statement` asserts that a literal
  in a failing query does not reach the error text.

## Approval

No human gate. The stage cannot write, cannot reach a second file, and cannot
run longer than its budget — which is the argument for not needing one.

`[execute] max_ms` and `max_rows` are committed and reviewable. Raising `max_ms`
is the change worth arguing about: it is the only thing standing between a
cartesian product and the machine.

## Failure Behavior

| Failure | Behavior |
|---|---|
| Database missing or unreadable | `DatabaseUnavailableError`. CLI exit **2** — a deployment fault, not a query fault, and nothing about the question would change it |
| File is not a database | `DatabaseUnavailableError` from the forced first read |
| The statement writes, or reaches another file | `ExecutionError` carrying SQLite's message. CLI exit 3 |
| More than one statement | `ExecutionError`. `sqlite3` refuses this itself — a fourth layer nobody designed, asserted so that a driver swap is a failing test rather than a surprise |
| A column that does not exist | `ExecutionError`, never a bare `sqlite3.OperationalError`. Nothing downstream should have to catch a driver exception |
| The budget elapsed | `ExecutionTimeout`, detected by SQLite's `interrupted` message **or** by elapsed time. Two signals because neither alone is sound |
| Zero rows matched | A `ResultSet` with no rows. **Not** an error: "no refunds last week" must be distinguishable from "the query broke" |
| More rows than the cap | The first `max_rows` rows, with `truncated=True`. Never silently cut |
| `EXPLAIN QUERY PLAN` fails | An empty plan and no warnings. The query itself is about to report a better error |

Two rules hold for every message raised here. **The statement is never in it** —
an executor that echoes the SQL it failed on writes that SQL into every log line
that records the failure, including its literals, which in a real deployment are
whatever the question was about. And **a timeout and a failure are different
facts** — a query that ran out of budget might succeed with a smaller range; one
that named a column that does not exist never will, and collapsing them would
make a retry loop retry the second one forever.

Escalation path: an `ExecutionError` on a statement the guard passed is a bug in
the guard, not in the query. It means something reached the executor that the
resolver could not prove wrong — most likely through an opaque `SELECT *` CTE —
and the statement belongs in `tests/test_guard_resolution.py` before anything
else happens.

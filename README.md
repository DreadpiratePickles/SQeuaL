# SQeuaL

**Text-to-SQL where the model never writes a number.** It proposes SQL; that
proposal is treated as untrusted input, and deterministic code parses it, checks
every table and column against the real schema, rewrites it to carry a row limit,
runs it on a connection that cannot write, checks that it answers the question
that was asked, and renders every figure in the answer from a result cell.

When it is not confident, it refuses and shows you the query instead.

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)](.python-version)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![tests: 645](https://img.shields.io/badge/tests-645-brightgreen)](tests/)
[![coverage: 96%](https://img.shields.io/badge/coverage-96%25-brightgreen)](#status)

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
| `05_generate` | Question → candidate SQL as strict JSON, guarded against a policy narrowed to the slice, repaired once from the guard's own findings, and k-sampled with agreement measured on executed rows | **built** |
| `06_verify` | Does the SQL answer the question that was *asked*? Eight deterministic checks on the parse tree, plus a back-translation produced **blind** and graded by project 1's criterion judge | **built** |
| `07_answer` | Render the answer from the rows, in code, with a confidence computed from evidence — or refuse, and show no figures at all | **built** |
| `08_eval` | Golden questions, execution accuracy, guard-catch rate, `regress` CI gate | contract only — Phase C |

Read [`CONTEXT.md`](CONTEXT.md) to navigate and
[`docs/design.md`](docs/design.md) for why every decision went the way it did.

**No stage in Phase A calls a model** — and that was the point, not an accident
of scheduling. Everything a text-to-SQL system needs in order to be *safe* is
deterministic, so it was built first, and when the model arrived in Phase B it
arrived into a system that already refuses bad SQL.

`src/sqeual/providers/` is the only package that knows a model exists. One module
in it imports a vendor SDK; nothing else in the repository does.

## Install

```bash
uv sync
uv run python scripts/sqeual.py db build
uv run pytest -q
```

Python 3.12 and [uv](https://docs.astral.sh/uv/). Project 1 is pinned as a git
dependency at commit `888a3e3` and supplies the provider seam, the pacer, the
retry policy and the criterion judge. **Nothing in the test suite touches the
network**, and `--dry-run` exercises the whole pipeline with no key at all.

To ask a real question you need `GEMINI_API_KEY` in a `.env` at the repository
root. It is gitignored, it is read by exactly one module, and no error message in
this package ever contains it.

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

# 6. Ask a question in English. No key needed — --dry-run scripts the model
#    offline, so the whole pipeline runs with nothing but this repository.
uv run python scripts/sqeual.py ask \
    "How much did we refund to customers in Berlin last month?" --dry-run

# 7. The same thing, with the trace instead of the answer.
uv run python scripts/sqeual.py ask "how many refunds last month" --dry-run --json
```

With a key in `.env`, drop `--dry-run` and add `--min-interval-ms 6500` to stay
inside a free-tier quota. One question is `k` generation calls plus one
back-translation plus two judge calls, issued back to back, which is exactly what
the pacer is for.

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

## What an answer looks like

Recorded 2026-09-04 from `--dry-run`, which scripts the model offline so this is
reproducible. Trimmed for width; the checks and confidence tables are verbatim.

```
$ sqeual ask "How much did we refund to customers in Berlin last month?" --dry-run

# How much did we refund to customers in Berlin last month?

## Answer

**€1,749.94** — as of 2026-08-31, per the query below.

## Confidence

**HIGH — 1.00** (100%). Computed from evidence, never asked of a model.

| factor    | value | weight | contribution | note                                     |
|-----------|-------|--------|--------------|------------------------------------------|
| intent    | 1.000 | 40     | 0.4000       | share of the applicable intent and shape checks that passed |
| judge     | 1.000 | 30     | 0.3000       | share of the two blind back-translation criteria that passed |
| agreement | 1.000 | 20     | 0.2000       | share of the samples whose rows matched the primary's |
| sanity    | 1.000 | 10     | 0.1000       | share of the applicable result-shape checks that passed |

## Checks

| check        | verdict | evidence                                                  |
|--------------|---------|-----------------------------------------------------------|
| time_window  | PASS    | 'last month' resolves to 2026-07-01..2026-07-31; the statement filters refund_date on 2026-07-01, 2026-07-31 |
| aggregation  | PASS    | the question asks for COUNT or SUM, SUM; present           |
| entities     | PASS    | the question names refunds, customers; all are read        |
| grouping     | n/a     | the question asks for no grouping                          |
| scalar_shape | PASS    | one figure asked for, 1 row(s) x 1 column(s) returned      |
| top_n_rows   | n/a     | the question names no row count                            |
| not_truncated| PASS    | the whole result fitted inside the cap                     |
| empty_result | n/a     | the result has rows                                        |

## The query that ran

SELECT SUM(r.amount_cents) AS refunded_cents FROM refunds AS r
JOIN orders AS o ON o.id = r.order_id JOIN customers AS c ON c.id = o.customer_id
WHERE r.refund_date >= '2026-07-01' AND r.refund_date <= '2026-07-31'
AND c.city = 'Berlin' LIMIT 200
```

`€1,749.94` is `174994`, the integer count of cents that came back in the one
cell, divided by 100 and formatted **by Python**. The model wrote `SUM(...)`; it
never saw the total.

`n/a` is not a pass. A question with no grouping in it gave the grouping check
nothing to test, and the confidence calculation drops that factor from the
average rather than counting the absence as evidence.

## One real run

Recorded 2026-09-04 against `gemini-3.5-flash-lite`, `k = 3`, paced at 6.5 s.
Verbatim, and it is more interesting than the dry run because half of it failed.

The model wrote this, unaided, from the sliced card and the injected date:

```sql
SELECT SUM(r.amount_cents) AS refunded_cents FROM refunds AS r
JOIN orders AS o ON r.order_id = o.id JOIN customers AS c ON o.customer_id = c.id
WHERE c.city = 'Berlin' AND r.refund_date >= '2026-07-01'
AND r.refund_date <= '2026-07-31' LIMIT 200
```

It resolved "last month" to July 2026 from `[time] as_of`, and the deterministic
time-window check — which computed the same window independently — agreed. All
three samples returned the same rows, so `agreement` was 1.0 and no repair was
needed. The answer read **€1,749.94**, which is `174994`, the integer count of
cents in the single cell, divided by 100 by Python.

Then the provider ran out of capacity, and the back-translation never happened.
This is the confidence block that resulted, verbatim:

```
**HIGH — 1.00** (100%). Computed from evidence, never asked of a model.

| factor    | value | weight  | contribution | note                             |
|-----------|-------|---------|--------------|----------------------------------|
| intent    | 1.000 | 40      | 0.5714       | share of the applicable intent and shape checks that passed |
| judge     | n/a   | dropped | 0.0000       | the judge could not be read; an unread judge has not agreed |
| agreement | 1.000 | 20      | 0.2857       | share of the samples whose rows matched the primary's |
| sanity    | 1.000 | 10      | 0.1429       | share of the applicable result-shape checks that passed |
```

```
- `answers_the_question` **error** — no explanation to grade: model
  gemini-3.5-flash-lite still failing after 3 attempts: transient provider
  failure (status 503 UNAVAILABLE)
- `no_extra_computation` **error** — same
```

The judge is **dropped**, not scored zero and not scored as a pass. Its weight
leaves the denominator, so the other three renormalise from 40/20/10 over 70 —
`0.5714 + 0.2857 + 0.1429 = 1.0000`. A judge that could not be read has not
agreed and has not disagreed, and a run where a broken judge silently lowered
every score would be as wrong as one where it silently raised them.

The back-translation section renders as `(unavailable)` rather than being
omitted, because an absent explanation and an explanation nobody wrote must not
look the same.

## When it refuses

Below `[confidence] abstain_threshold` the answer contains **no figures at all**.
Not a number with a hedge attached — a hedged number is repeated without its
hedge in the first email that quotes it, which is how a low-confidence guess
becomes a figure in a board pack.

What you get instead is everything needed to argue with the refusal: the
back-translation, every check with its evidence, the score factor by factor, and
the statement it was about to run, so you can paste it and get the number
yourself. If you do, you have decided to.

The same shape covers a question the slicer could not place — that one is refused
**without calling a model at all**, because a slicer that matched nothing has no
business spending money to find out it still matches nothing.

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

**The verifier must not see the question.** The obvious check — show one model the
question and the SQL, ask "does this answer that" — fails because a model shown
both reads the question, restates it, and agrees with itself. So the SQL is
back-translated into English **blind**, and a judge grades that against the
question. A test asserts the question's phrases are absent from the explain
prompt, because blindness is exactly the property that decays the first time
somebody improves a prompt.

**`k` samples of one model are not an ensemble.** Same prompt, same model, same
distribution: a plurality vote among them launders a systematic error into
certainty, and more samples make the wrong answer look more certain. So the first
sample answers and the rest only *agree*, as one of four weighted factors. And
agreement is measured on executed **rows**, because two correct queries can be
spelled differently while two identical wrong ones agree perfectly.

## Layout

```
sqeual.toml               every limit a generated query obeys. No model id lives here
data/schema.sql           the DDL, hand-written                              (committed)
data/support.db           the generated database          (gitignored — the generator is committed)
src/sqeual/db/            seeded deterministic generator + the invented word lists
src/sqeual/schema/        card, renderer, slicer
src/sqeual/guard/         policy, twelve rules, the column resolver, the report
src/sqeual/execute/       read-only connection, budget, typed errors, ResultSet
src/sqeual/providers/     the metered seam, the Gemini adapter, the pacer, the dry-run fake
src/sqeual/generate/      prompt, committed examples, strict JSON parsing, time windows,
                          the repair loop, agreement on rows
src/sqeual/verify/        four intent checks, four shape checks, the blind back-translation
src/sqeual/answer/        cell formatting, number grounding, computed confidence, rendering
src/sqeual/pipeline.py    the one module that knows the stages have an order
src/sqeual/trace.py       runs/<ts>/trace.json and answer.md
src/sqeual/config.py      model identifiers, and nothing else
scripts/sqeual.py         db build | schema show|slice | guard | run | ask
stages/0*/CONTEXT.md      eight stage contracts; seven built, one marked PLANNED
docs/design.md            forty-two decisions and their reasons
runs/                     one directory per ask                             (gitignored)
```

## Exit codes

`db`, `schema`, `guard` and `run`:

| Code | Meaning |
|---:|---|
| 0 | The run completed and found nothing wrong |
| 1 | The run completed and produced a **finding** — the guard refused the SQL, or a slice matched no table |
| 2 | The run **never started** — bad configuration, missing database, bad usage |
| 3 | The run started and **execution failed** — a timeout, or a SQLite error |

The 1/3 split is the one that matters. A 1 means the SQL was bad and a repair
loop should try again *with the finding*; a 3 means the query was fine and
re-writing it would fail identically.

`ask` uses the same four numbers for a **different** set of facts, because a
caller of `ask` did not write the SQL and wants a different question answered
first:

| Code | Meaning |
|---:|---|
| 0 | Answered — a document with figures in it |
| 1 | **Abstained**, or a clarification is needed. The tool declined to show a number. This is a successful outcome of a working tool |
| 2 | The guard refused the model's statement after the repair budget was spent, or the model never produced a reply this tool could read. **Nothing ran** |
| 3 | **Could not run** — no key, bad configuration, a missing database, or a statement that passed every check and the database still could not answer |

The split that matters here is 2 versus 3: whether **the model's statement** was
the problem, or whether **the deployment** was. `docs/design.md` §37 argues it
out, and `tests/test_cli_ask.py` pins both vocabularies.

## Status

Phases A and B are **implemented and tested**, not deployed and not validated at
scale.

- 645 tests, 96% statement coverage, `ruff` clean at line length 100. **None
  touches the network**; `--dry-run` runs the whole pipeline with no key.
- **There is no accuracy number, and there will not be one until stage 08.**
  Accuracy is a property of the model-plus-guard system measured against cases
  somebody else wrote. The guard catches every adversarial case in `tests/`,
  which is worth almost nothing on its own: those cases were written by the same
  pass that wrote the guard.
- **The judge pass rate is not an accuracy figure either.** The judge is
  currently the same model family as the writer, because one provider key exists
  here, and models agree with their own family more readily. Every trace records
  `same_family` for exactly this reason, so a later analysis cannot silently mix
  biased and unbiased verdicts. Point `SQEUAL_JUDGE_MODEL_ID` at another family
  and it becomes worth quoting; that needs a key, not a code change.
- **A `--dry-run` answer is evidence about the wiring and nothing else.** The
  trace labels it `dry_run: true` and names the provider `role-aware-fake`, so it
  cannot later be mistaken for evidence about whether a model can write SQL.
- **Cost is recorded as unpriced.** `[cost]` ships with zero prices and every
  trace carries `priced: false` beside the amount, because nobody here has
  entered a vendor tariff and inventing one would put a made-up number in the
  money column.
- **One real run has been made**, shown above. It is one question, which is
  enough to demonstrate that the path works end to end and nowhere near enough
  to be an accuracy claim. The free tier for this model was returning 503 for
  most of that session, which is why the run above has a dropped judge factor
  and why there is not a second one.
- The database is fictional and deliberately so: invented names from short word
  lists, `example.invalid` addresses, no scraped or anonymised data anywhere.

## License

MIT. See [LICENSE](LICENSE).

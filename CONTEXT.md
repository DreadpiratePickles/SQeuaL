# Context router

Layer 1. This file answers "where do I go?" — it maps a task to the stage that
owns it. Read this, then read that stage's `CONTEXT.md`, then read only the
inputs that stage declares.

## Stages

The tool answers questions about a database in English, under one rule: **the
model never writes a number.** It is allowed to propose SQL, and its proposal is
treated as what it is — untrusted input. Deterministic code parses that SQL,
checks every table and column against the real schema, rewrites it to carry a
row limit, runs it against a connection that cannot write, and renders the
answer from the rows that come back.

| Stage | Job | Lives in | Built? |
|---|---|---|---|
| `01_db` | Build a realistic support and e-commerce database from a committed DDL and a fixed seed, byte-identically every time | `stages/01_db/CONTEXT.md`, `src/sqeual/db/` | Yes — Phase A |
| `02_schema` | Introspect the live database into a typed schema card, and pick the tables one question needs — with a reason for each | `stages/02_schema/CONTEXT.md`, `src/sqeual/schema/` | Yes — Phase A |
| `03_guard` | Parse a proposed statement, resolve every table and column against the real schema, and return the statement that should run in its place | `stages/03_guard/CONTEXT.md`, `src/sqeual/guard/` | Yes — Phase A |
| `04_execute` | Run a guarded statement on a connection that cannot write, cannot reach another file, and cannot outlive its budget | `stages/04_execute/CONTEXT.md`, `src/sqeual/execute/` | Yes — Phase A |
| `05_generate` | Question → candidate SQL as strict JSON, k-sampled, with agreement measured on result sets | `stages/05_generate/CONTEXT.md` | **PLANNED — Phase B** |
| `06_verify` | Does the guarded SQL answer the question that was *asked*? Back-translation, deterministic intent checks, and a computed confidence | `stages/06_verify/CONTEXT.md` | **PLANNED — Phase C** |
| `07_answer` | Render the answer from the rows, in code. The number in the sentence is formatted by Python, never written by a model | `stages/07_answer/CONTEXT.md` | **PLANNED — Phase C** |
| `08_eval` | Golden questions with reference SQL, execution accuracy, guard-catch rate, and a `regress` target adapter so SQeuaL's own regressions are CI-gated | `stages/08_eval/CONTEXT.md` | **PLANNED — Phase C** |

**No stage in Phase A calls a model.** Nothing in `db`, `schema`, `guard` or
`execute` reads an API key, opens a socket, or imports a vendor SDK. That is not
an accident of scheduling — it is the point. Everything a text-to-SQL system
needs in order to be *safe* is deterministic, and building it first means the
model, when it arrives, is arriving into a system that already refuses bad SQL.

Stage 01 is the only stage that writes anything, and it writes one file.

## Shared resources

| Path | Layer | What it is |
|---|---:|---|
| `README.md` | 0 | Workspace identity: what the tool is for, and what Phase A does and does not claim |
| `docs/design.md` | 3 | Every design decision and the reason for it. Read before changing behaviour |
| `sqeual.toml` | 3 | Every limit a generated query obeys: which tables it may reach, how many rows may come back, how long it may run, how much schema a model sees. No model id lives here |
| `src/sqeual/config.py` | 3 | Model identifiers, and nothing else. The only module that names a model. Phase A calls none of it |
| `data/schema.sql` | 3 | The DDL, hand-written and **committed**. The definition the guard checks hallucinated columns against |
| `src/sqeual/db/vocabulary.py` | 3 | The invented word lists the generator draws from. No real names, no real addresses |
| `src/sqeual/guard/policy.py` | 3 | `DENIED_FUNCTIONS` — the functions no configuration can permit |
| `data/support.db` | 4 | The generated database. **Gitignored**: it is a build artefact and the generator is committed instead |

## Reused from project 1

`regression-detect`, pinned to commit `888a3e3`, is declared as a dependency and
**not called anywhere in Phase A**. The pin is here now so that the dependency
set a Phase A reader installs is the one Phase B runs against, and because a
branch that moves underneath makes an answer's provenance a guess. In Phase B
and C it supplies:

- **the provider seam** — `providers.base` (the `Provider` protocol and the
  typed error hierarchy) and `providers.gemini` (retry budget, backoff policy,
  timeout, and which status codes are transient);
- **pacing** — spreading a burst of calls under a per-minute quota, which stage
  05 needs because self-consistency means k calls per question;
- **the criterion judge** — `judge.criterion.judge_criterion` and its strict
  verdict parsing, which stage 06 uses to grade a back-translation;
- **the statistics** — `compare.fisher_exact_one_sided` and
  `compare.wilson_interval`, which stage 08 needs to say whether an accuracy
  drop is a regression or noise.

Stage 08 additionally implements project 1's `Target` protocol
(`target/adapters/base.py`), so project 1's existing runner can drive SQeuaL as
an external target with no change to project 1 at all.

This project does not depend on projects 2 or 9 and copies nothing from them.

## Rules that hold across every stage

- **The model never writes a number.** It may propose SQL and, in Phase C, a
  sentence template. Every figure in an answer is formatted by code from a
  result row.
- **Model output is untrusted input.** It is parsed, not pattern-matched;
  validated at the boundary; and never interpolated into a shell command or a
  SQL string.
- **What runs is what was checked.** The executor is handed
  `GuardReport.normalised_sql` — regenerated from the tree the rules read, with
  comments stripped and the row limit already in it. No code path in this
  package executes a caller's original string.
- **A rejected query has no normalised statement.** There is no such thing as a
  partly-approved query.
- **Read-only at four independent layers**: the guard, `mode=ro`,
  `PRAGMA query_only`, and `SQLITE_LIMIT_ATTACHED = 0`. Each is tested by
  deliberately bypassing the others, because the failure mode of "the guard has
  a hole" is a database with rows missing.
- **A hallucinated column is caught before execution, not after.** SQLite would
  also refuse it — with the connection already open, and with an error nobody
  upstream can turn into a repair.
- **The guard says nothing it cannot prove.** Where it cannot enumerate a
  source's columns it reports no finding, and stage 04 is the layer that catches
  what gets through. A false positive on correct SQL trains everyone to ignore
  the guard; a false negative costs one wasted query.
- **Two different findings for two different mistakes.** `unknown_table` means a
  model invented something; `table_not_allowed` means it asked for something
  real it may not have. One code for both would hide both.
- **The slicer is deterministic and explainable.** Term overlap plus foreign-key
  expansion, never an embedding, because the answer to "why did it pick that"
  has to be a rule somebody can read and edit — and because in Phase B the slice
  becomes the guard's `allowed_tables`, which makes it a security boundary.
- **A limit is a rewrite, not a rejection, where a rewrite is honest.** A missing
  `LIMIT` is added and reported; a hallucinated column is refused.
- **Truncation is always visible.** A caller that received 500 rows and did not
  know whether there were 501 would report a sum that is wrong and looks right.
- **Errors never quote the statement.** A guard or executor that echoes the SQL
  it rejected writes that SQL, and its literals, into every log line.
- Money is an integer count of cents with the unit in the column name. Dates are
  ISO text. Neither is ever a float.
- Model identifiers live in `config.py` and nowhere else. Secrets live in a
  `.env` that this repository does not contain and never created.

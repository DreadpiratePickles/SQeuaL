# Context router

Layer 1. This file answers "where do I go?" — it maps a task to the stage that
owns it. Read this, then read that stage's `CONTEXT.md`, then read only the
inputs that stage declares.

## Stages

The tool answers questions about a database in English, under one rule: **the
model never writes a number.** It is allowed to propose SQL and to explain SQL in
English, and both proposals are treated as what they are — untrusted input.
Deterministic code parses that SQL, checks every table and column against the
real schema, rewrites it to carry a row limit, runs it against a connection that
cannot write, checks that it answers the question that was asked, computes a
confidence from what it found, and renders every figure from the rows that came
back. When that confidence is too low it refuses, and shows the query instead.

| Stage | Job | Lives in | Built? |
|---|---|---|---|
| `01_db` | Build a realistic support and e-commerce database from a committed DDL and a fixed seed, byte-identically every time | `stages/01_db/CONTEXT.md`, `src/sqeual/db/` | Yes — Phase A |
| `02_schema` | Introspect the live database into a typed schema card, and pick the tables one question needs — with a reason for each | `stages/02_schema/CONTEXT.md`, `src/sqeual/schema/` | Yes — Phase A |
| `03_guard` | Parse a proposed statement, resolve every table and column against the real schema, and return the statement that should run in its place | `stages/03_guard/CONTEXT.md`, `src/sqeual/guard/` | Yes — Phase A |
| `04_execute` | Run a guarded statement on a connection that cannot write, cannot reach another file, and cannot outlive its budget | `stages/04_execute/CONTEXT.md`, `src/sqeual/execute/` | Yes — Phase A |
| `05_generate` | Question → candidate SQL as strict JSON, guarded against a policy narrowed to the slice, repaired once from the guard's own findings, and k-sampled with agreement measured on executed rows | `stages/05_generate/CONTEXT.md`, `src/sqeual/generate/` | Yes — Phase B |
| `06_verify` | Does the guarded SQL answer the question that was *asked*? Eight deterministic checks, plus a back-translation produced blind and graded by project 1's criterion judge | `stages/06_verify/CONTEXT.md`, `src/sqeual/verify/` | Yes — Phase B |
| `07_answer` | Render the answer from the rows, in code, with a confidence computed from evidence — or refuse and show no figures at all | `stages/07_answer/CONTEXT.md`, `src/sqeual/answer/` | Yes — Phase B |
| `08_eval` | Golden questions with reference SQL, execution accuracy, guard-catch rate, and a `regress` target adapter so SQeuaL's own regressions are CI-gated | `stages/08_eval/CONTEXT.md` | **PLANNED — Phase C** |

**No stage in Phase A calls a model.** Nothing in `db`, `schema`, `guard` or
`execute` reads an API key, opens a socket, or imports a vendor SDK. That is not
an accident of scheduling — it is the point. Everything a text-to-SQL system
needs in order to be *safe* is deterministic, and building it first meant the
model, when it arrived in Phase B, arrived into a system that already refuses bad
SQL.

`src/sqeual/providers/` is the only package that knows a model exists, and
`providers/gemini.py` is the only module in the repository that imports a vendor
SDK. Everything else depends on the `MeteredProvider` protocol.

Stage 01 is the only stage that writes to the database, and it writes one file.
Stage 07 writes `runs/<ts>/`, which is gitignored.

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
| `src/sqeual/generate/prompts/generate_v1.md` | 3 | The system prompt that asks for SQL. Committed, hashed into every trace |
| `src/sqeual/generate/examples.yaml` | 3 | Six worked question/statement pairs. Every one is guard-checked against the live card by the test suite |
| `src/sqeual/verify/prompts/explain_v1.md` | 3 | The back-translation prompt. It is never shown the question, and a test asserts it |
| `src/sqeual/answer/prompts/phrase_v1.md` | 3 | The optional phrasing prompt. Off by default; every number it writes is checked against the cells |
| `runs/` | 4 | One directory per `ask`: `trace.json` and `answer.md`. **Gitignored** — it holds a question somebody asked and the rows that came back |

## Reused from project 1

`regression-detect`, pinned to commit `888a3e3`, is declared as a dependency and
**not called anywhere in Phase A**. The pin is here now so that the dependency
set a Phase A reader installs is the one Phase B runs against, and because a
branch that moves underneath makes an answer's provenance a guess. Phase B calls all of it except the statistics:

- **the provider seam** — `providers.base` (the `Provider` protocol and the
  typed error hierarchy) and `providers.gemini` (retry budget, backoff policy,
  timeout, and which status codes are transient);
- **pacing** — spreading a burst of calls under a per-minute quota, which stage
  05 needs because self-consistency means k calls per question;
- **the criterion judge** — `judge.criterion.judge_criterion` and its strict
  verdict parsing, which stage 06 uses to grade a back-translation;
- **the retry policy** — `providers.gemini`'s attempt budget, backoff constants
  and retryable status codes, imported by `providers/gemini.py` rather than
  restated, so the two projects cannot drift on what a transient failure is;
- **the statistics** — `compare.fisher_exact_one_sided` and
  `compare.wilson_interval`, which stage 08 will need to say whether an accuracy
  drop is a regression or noise. Not called yet.

What could **not** be reused is the call itself: project 1's `Provider` returns a
string, and a trace priced from character counts would carry a guess in the money
column. `providers/metered.py` widens the seam to return token counts beside the
text and re-exports project 1's typed errors unchanged, and `TextProviderView`
narrows it back so `judge_criterion` can be called without dropping the usage it
never asked for.

Stage 08 additionally implements project 1's `Target` protocol
(`target/adapters/base.py`), so project 1's existing runner can drive SQeuaL as
an external target with no change to project 1 at all.

This project does not depend on projects 2 or 9 and copies nothing from them.

## Rules that hold across every stage

- **The model never writes a number.** It may propose SQL and, when
  `[answer] llm_phrasing` is on, one sentence — every numeric token of which is
  checked against the result cells before anybody sees it. Every figure in an
  answer is formatted by code from a result row, and
  `tests/test_pipeline.py` sweeps the rendered document for a digit that traces
  to nothing.
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
- **Refusing is a first-class outcome.** Below `[confidence] abstain_threshold`,
  or when the question is ambiguous, the answer shows the clarifying question,
  the checks and the statement — and no figures at all. A hedged number is
  repeated without its hedge.
- **Confidence is computed, never requested.** Four weighted factors over things
  that were counted, and a factor with nothing to say is dropped from the average
  rather than scored as a pass.
- **A judge that could not be read has not agreed, and has not disagreed.** An
  unparseable verdict is an `error` and contributes nothing.
- Model identifiers live in `config.py` and nowhere else. Secrets live in a
  `.env` that is gitignored and read only by `providers/gemini.py`, through
  project 1's `GEMINI_API_KEY`.

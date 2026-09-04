# Stage: 05_generate — PLANNED (Phase B)

> **Nothing in this stage is implemented.** This file is the contract it will be
> built against. It is the reason `GuardPolicy.narrowed_to()` exists in stage 03
> and the reason `slice_for_question` returns an ordered `SchemaSlice` rather
> than a bare set of names: the tables a model is *shown* become the tables its
> SQL is *permitted to reach*, and that is one call rather than a sentence in a
> prompt.

## Objective

Turn one English question into one guarded SQL statement — or into an explicit
refusal — by sampling a model `k` times, guarding every candidate against a
policy narrowed to the schema slice, and keeping the candidate whose executed
rows the plurality of the others agree with.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| The question (`--question`) | 4 | Operator input | Yes | Whole string, tokenised by `schema.slice.tokenise`; never parsed as SQL |
| `data/support.db` | 4 | Authoritative | Yes | Read `mode=ro` by stage 04 while scoring candidates |
| The `SchemaCard` from stage 02 | 4 | Authoritative | Yes | `tables`, `foreign_keys`, `row_count`, `sample_values`, `schema_sha256` |
| `sqeual.toml` | 3 | Authoritative | Yes | `[schema] max_tables`, `[schema.synonyms]`, all of `[guard]`, all of `[execute]` |
| `[models] sql_model_ref` | 3 | Authoritative | Yes | Resolved through `config.model_id_for_ref("SQEUAL_SQL_MODEL_ID")` |
| `GEMINI_API_KEY` | — | Operator secret | Yes | From a gitignored `.env`; the first thing in this project that spends money |
| A new `[generate]` section | 3 | Authoritative | Yes | `k`, `temperature`, `max_repairs`, `min_agreement` |

The stage cannot see a golden case, a reference answer, or a previous run's
verdict. That is deliberate: a generator that could read the answer key would
be evaluated on a task nobody is going to ask it to do in production. Every
limit that shapes the model call — how many tables it sees, how many rows come
back, how long a candidate may run — is already in `sqeual.toml`, so widening
any of them stays a diff a person reviews rather than a constant that drifts.

## Process (planned)

Steps 1, 3, 4, 5 and 7 are deterministic code. Step 2 is the only model call,
and step 6 is a bounded repeat of it.

1. **Slice, deterministically.** `slice_for_question(question, card,
   max_tables=..., synonyms=...)` returns an ordered `SchemaSlice` of
   `SliceEntry(table, reason)`. An empty slice — a question that matched
   nothing — stops here rather than being widened to the whole schema: showing
   a model every table because it could not be told which ones matter is how a
   question about refunds gets answered from `agents`.
2. **Ask once per sample, for strict JSON.** The prompt carries
   `render_card(card, tables=slice.tables)` and the question, and asks for
   `{"sql": "...", "tables": [...], "assumptions": [...]}` — an object, not
   prose with SQL in it. The rejected alternative is pulling a fenced code
   block out of a paragraph with a regular expression: that works until the
   model writes two blocks, or explains itself in SQL comments, and then the
   extractor has quietly chosen which statement to run. JSON is validated
   against a schema at the boundary and a response that fails validation is a
   discarded candidate, never a repaired string. `tables` and `assumptions`
   are not trusted — they are recorded, because a candidate that names tables
   its SQL never touches is a useful signal for stage 06 — and the SQL itself
   is checked against the card regardless of what the model claimed.
3. **Guard every candidate** with `guard_sql(sql, card, policy)` where `policy`
   is `GuardPolicy.from_settings(...).narrowed_to(slice.tables)`. A candidate
   reaching a table outside the slice fails as `table_not_allowed`, which is a
   different finding from `unknown_table` on purpose: the first is this stage
   over-reaching, the second is a hallucinated table, and one code for both
   would hide both.
4. **Execute the survivors** with `execute_sql(report.normalised_sql, ...,
   aliases=report.table_aliases)`. The normalised statement is what runs, never
   the model's original string, so what executes is exactly what was checked,
   and the alias map is passed through because `EXPLAIN QUERY PLAN` names the
   alias — without it a full-scan warning is silent on exactly the aliased
   joins it is most useful for. A candidate that raises
   `ExecutionTimeout` or `ExecutionError` is discarded with its error recorded;
   `DatabaseUnavailableError` aborts the whole run, because that is a broken
   deployment rather than a bad candidate.
5. **Vote on rows, not on text.** Each surviving candidate's `ResultSet` is
   reduced to a digest over its row tuples, sorted, ignoring column *labels* —
   `SELECT COUNT(*) AS n` and `SELECT COUNT(*) AS total` are one answer with
   two names. The candidate in the largest digest group wins; ties break on the
   lowest guard-finding count, then on the shortest `normalised_sql`, so the
   choice never depends on sampling order. Voting on SQL strings was rejected
   because two correct queries can be spelled differently and would then never
   agree, while two identical wrong queries agree perfectly.
6. **Repair, bounded.** If fewer than `min_agreement` candidates survive, the
   guard `codes` from the failures are fed back once per attempt, up to
   `max_repairs`. The codes are the payload — `unknown_column`,
   `ambiguous_column`, `function_not_allowed`, `subquery_too_deep` — not the
   rendered rule table, because a model repairs better against a named finding
   than against a paragraph. The counter is a hard ceiling and there is no
   branch that resets it: an unbounded repair loop is a bill with no upper
   limit and a run that never terminates.
7. **Write the run.** Every candidate, surviving or not, is appended to
   `candidates.jsonl` before the winner is chosen, so a run that ends in a
   refusal still shows its work.

Requests are paced through project 1's `pacing.pace` and retried by
`providers.gemini`'s backoff policy, with `providers.base`'s typed error
hierarchy distinguishing a rate limit from a malformed request. Sampling `k`
times multiplies the request rate by `k`, which is exactly the situation a
shared pacer exists for, and re-implementing one here would give this project
its own opinion about a limit project 1 already models.

## Outputs (planned)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/<ts>/candidates.jsonl` | One object per sample: `sample_index`, raw response, parsed `sql`, guard `codes` and `failures`, `normalised_sql`, result digest, `row_count`, `elapsed_ms`, `truncated`, discard reason | Stage 06, stage 08, and a human reading a refusal |
| `runs/<ts>/chosen.json` | `{question, slice: [{table, reason}], sql, normalised_sql, agreement: {group_size, k}, schema_sha256, model_id, repairs_used}` | Stages 06 and 07 |
| stdout | The chosen statement and its agreement count, or the refusal and the codes that caused it | A human running `sqeual ask` |

`chosen.json` carries `schema_sha256` because a statement is only meaningful
against the shape it was written for. A candidate replayed against a database
whose columns have moved is not the same candidate, and the hash is what makes
that detectable rather than merely regrettable.

## Verify (planned)

- A fixture provider that returns scripted responses, so `k`, the vote, the tie
  breaks and the repair ceiling are all tested without a network call or a key.
- A test where three of five samples produce different SQL with identical rows
  and two produce identical SQL with different rows: the three must win. This
  is the single assertion that proves agreement is measured on results.
- A test where a candidate names a table outside the slice and the report
  fails as `table_not_allowed` rather than executing.
- A test where every candidate fails the guard: the run exits **1**, prints the
  union of the codes, and writes no `chosen.json`.
- A test that `max_repairs` is a ceiling — a provider that fails forever must
  produce exactly `max_repairs` extra calls, counted.
- Determinism at `temperature = 0` with `k = 1`: same question, same card, same
  statement, byte for byte.

## Approval (planned)

No human gate on generating a candidate — nothing is exposed and nothing is
written outside `runs/`. The gates that matter are elsewhere and already exist:
`[guard] allowed_tables` and `allowed_functions` are reviewed configuration, and
widening either is the change a reviewer should be looking at, not the prompt.

Two things stay blocked regardless of what a candidate contains. Nothing in this
stage may execute SQL that did not come back `ok` from the guard, and nothing
may write to the database — the read-only connection makes the second a property
of the deployment rather than a promise in this document.

## Failure Behavior (planned)

| Failure | Behavior |
|---|---|
| Empty slice — the question matched no table | Refusal. Exit **1**, naming the question's terms. The slice is never widened to the full card as a fallback |
| Model response is not valid JSON, or fails the schema | That sample is discarded and recorded with the raw text. Never regex-extracted, never repaired in place |
| A candidate fails the guard | Discarded with its `codes`; feeds step 6 up to `max_repairs` |
| Fewer than `min_agreement` survivors after the last repair | Refusal. Exit **1**, listing every code seen. "I could not answer that" is a valid outcome; inventing SQL is not |
| `ExecutionTimeout` or `ExecutionError` on a candidate | That candidate is discarded, the rest are still scored. One slow candidate is not a failed run |
| `DatabaseUnavailableError` | Exit **3**. The run started and execution failed, which is a different fact from a bad question |
| Provider authentication or configuration error | Exit **2**. The run never started; no `runs/<ts>/` directory is left behind |
| Rate limit or transient provider error | Retried by `providers.gemini`'s policy, paced by `pacing.pace`. Exhausted retries discard the sample, not the run |

Escalation path: a refusal is an answer and needs no escalation. A run where
every sample failed with `unknown_column` against a card that was just rebuilt
is a question about the schema, not about the model — check `schema_sha256`
against the database before changing a prompt.

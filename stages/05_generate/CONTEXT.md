# Stage: 05_generate — BUILT (Phase B)

> Implemented in `src/sqeual/generate/`. This is the reason
> `GuardPolicy.narrowed_to()` exists in stage 03 and the reason
> `slice_for_question` returns an ordered `SchemaSlice` rather than a bare set of
> names: the tables a model is *shown* become the tables its SQL is *permitted to
> reach*, and that is one call rather than a sentence in a prompt.

## Objective

Turn one English question into one guarded, executed statement — or into an
explicit refusal — by asking a model for strict JSON, guarding the result against
a policy narrowed to the schema slice, executing what the guard produced, and
measuring how many independent samples agree with it.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---|---|---|---|
| The question | 4 | Operator input | Yes | Whole string, delimited in the user message; never parsed as SQL |
| `data/support.db` | 4 | Authoritative | Yes | Read `mode=ro` by stage 04 |
| The `SchemaCard` from stage 02 | 4 | Authoritative | Yes | `tables`, `foreign_keys`, `row_count`, `sample_values`, `schema_sha256` |
| `sqeual.toml` | 3 | Authoritative | Yes | `[schema]`, all of `[guard]`, all of `[execute]`, `[generate]`, `[time] as_of`, `[verify] float_places` |
| `[models] sql_model_ref` | 3 | Authoritative | Yes | Resolved through `config.model_id_for_ref` |
| `GEMINI_API_KEY` | — | Operator secret | Yes, unless `--dry-run` | From a gitignored `.env`; the first thing in this project that spends money |
| `generate/prompts/generate_v1.md` | 3 | Authoritative | Yes | The system prompt, committed and hashed into every trace |
| `generate/examples.yaml` | 3 | Authoritative | Yes | Six committed question/statement pairs, each guard-checked by the suite |

The stage cannot see a golden case, a reference answer, or a previous run's
verdict. A generator that could read the answer key would be evaluated on a task
nobody is going to ask it to do in production.

## Process (built)

Steps 1, 3, 4, 5 and 7 are deterministic code. Step 2 is the only model call and
step 6 is a bounded repeat of it.

1. **Slice, deterministically.** `slice_for_question` returns an ordered
   `SchemaSlice`. An empty slice — a question that matched nothing — stops here
   with a code-built clarifying question naming the tables that exist, and
   **without calling a model at all**. It is never widened to the whole schema:
   showing a model every table because nobody could say which ones matter is how
   a question about refunds gets answered from `agents`.
2. **Ask once per sample, for strict JSON.** The system prompt is committed. The
   user message is built by code from, in order: the six worked examples, the
   rendered schema slice, `[time] as_of` stated in words, the dialect notes, the
   rules, the previous attempt's guard findings on a repair, and **last** the
   question inside `<question>` delimiters. A question that tries to redefine the
   rules therefore arrives after the rules it is trying to redefine, and arrives
   as data.
   The reply must be `{"sql", "tables", "assumptions", "clarification_needed",
   "clarifying_question"}` exactly. `tables` and `assumptions` are recorded and
   not trusted. A reply that fails validation is a discarded candidate and a
   typed `GenerationParseError`, never a repaired string.
3. **Guard every candidate** with `policy.narrowed_to(slice.tables)`. A candidate
   reaching outside the slice fails as `table_not_allowed`, deliberately a
   different finding from `unknown_table`.
4. **Execute the survivor** with `execute_sql(report.normalised_sql, ...,
   aliases=report.table_aliases)`. The normalised statement runs, never the
   model's original string.
5. **Measure agreement on rows.** Each executed `ResultSet` is canonicalised —
   column labels lowercased and recorded, floats rounded to
   `[verify] float_places`, rows sorted — and compared to the primary's **rows**.
   `agreement` is the fraction of the `k` samples matching, the primary included.
   Labels are not part of the comparison: `COUNT(*) AS n` and `COUNT(*) AS total`
   are one answer with two names.
6. **Repair, bounded.** A guard failure on the **primary** feeds the failing
   codes back once, up to `[generate] max_repairs`. Only the primary is repaired:
   the other samples exist to agree, and repairing them would buy agreement with
   money. There is no branch that resets the counter.
7. **Record everything.** Every attempt, surviving or not, reaches
   `runs/<ts>/trace.json` with the model's reply verbatim.

Requests are paced through project 1's `pacing.pace` and retried by the metered
Gemini adapter's backoff policy — project 1's constants, imported rather than
restated. Sampling `k` times multiplies the request rate by `k`, which is exactly
what a shared pacer exists for.

## Outputs (built)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/<ts>/trace.json` | `generation.attempts[]`: `sample_index`, `repair_index`, `repaired`, `temperature`, `outcome`, `raw_reply`, `proposed_sql`, `guard{codes, failures, normalised_sql, tables_used}`, `execution{row_count, truncated, elapsed_ms, result_digest}`, `usage{tokens, latency}` | Stages 06 and 07, and a human reading a refusal |
| `GenerationOutcome` | `status`, `primary`, `agreement`, `k`, `repairs_used`, `schema_sha256`, `time_window`, `clarification`, `blocked_codes` | Stages 06 and 07 |

`schema_sha256` is carried because a statement is only meaningful against the
shape it was written for.

## Verify (built)

`tests/test_generate_run.py`, `test_generate_parse.py`,
`test_generate_agreement.py`, `test_generate_timewindow.py`,
`test_generate_prompt.py`:

- A scripted provider that **raises when its script runs out** rather than
  cycling, so a test expecting three calls fails if the code makes four.
- Strict JSON: every key required, every type checked, extra keys refused, one
  markdown fence tolerated, prose around the object refused rather than
  extracted.
- The time-window resolution table, twenty-three cases against `as_of` = 2026-08-31.
- Agreement measured on rows: two different statements with identical rows agree;
  the same label over different rows does not.
- `max_repairs` is a ceiling — a provider that fails forever produces exactly
  `max_repairs` extra calls, counted.
- A candidate outside the slice fails `table_not_allowed` and not
  `unknown_table`.
- Every committed few-shot example passes the guard against the live card.
- The question is the last block in the prompt, after the rules.

## Approval

No human gate on generating a candidate: nothing is exposed and nothing is
written outside `runs/`. The gates that matter are `[guard] allowed_tables` and
`allowed_functions`, which are reviewed configuration.

Two things stay blocked. Nothing here executes SQL that did not come back `ok`
from the guard, and nothing writes to the database — the read-only connection
makes the second a property of the deployment rather than a promise here.

## Failure Behavior (built)

| Failure | Behavior |
|---|---|
| Empty slice | `CLARIFICATION`, exit **1**, with a code-built question naming the tables that exist. No model is called |
| Model asks for clarification | `CLARIFICATION`, exit **1**. Its question is carried through, or code builds one if it gave none |
| Primary reply is not valid JSON | `PARSE_FAILED`, exit **2**. Never regex-extracted and never repaired in place |
| Secondary reply is not valid JSON | That sample is discarded and recorded; it counts against agreement |
| Primary fails the guard | Repaired once with the codes; a second failure is `GUARD_BLOCKED`, exit **2** |
| Secondary fails the guard | Discarded and recorded. Never repaired |
| Primary raises `ExecutionTimeout` / `ExecutionError` | `EXECUTION_FAILED`, exit **3**. The statement was fine and re-writing it would fail identically |
| Secondary raises either | Discarded; the rest are still scored |
| `DatabaseUnavailableError` | Exit **3**. A broken deployment, not a bad candidate |
| Provider authentication or configuration error | Exit **3**. The run never started |
| Rate limit or transient provider error | Retried by the adapter's policy, paced by `pacing.pace`. Exhausted retries fail the run |

Escalation path: a refusal is an answer and needs no escalation. A run where
every sample failed with `unknown_column` against a card that was just rebuilt is
a question about the schema — check `schema_sha256` against the database before
changing a prompt.

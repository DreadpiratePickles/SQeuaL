# Stage: 06_verify — BUILT (Phase B)

> Implemented in `src/sqeual/verify/`. It exists because of what the guard
> deliberately does *not* claim: `guard_sql` proves a statement is well-formed,
> single, read-only and made of real tables and columns, and nothing in its
> twelve rules has an opinion about whether it answers anybody's question.

## Objective

Decide whether the guarded statement chosen by stage 05 answers the question that
was actually asked, and produce the evidence stage 07 turns into a confidence
that was **computed** rather than requested from a model.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---|---|---|---|
| The `GenerationOutcome` from stage 05 | 4 | Authoritative | Yes | `question`, `primary.report`, `primary.result`, `time_window`, `schema_slice` |
| The `SchemaCard` from stage 02 | 4 | Authoritative | Yes | Date columns, grouping columns, column names |
| `sqeual.toml` | 3 | Authoritative | Yes | `[schema.synonyms]` — the same map the slicer used |
| `[models] judge_model_ref` | 3 | Authoritative | Yes | Resolved through `config.model_id_for_ref` |
| `verify/prompts/explain_v1.md` | 3 | Authoritative | Yes | The back-translation prompt, committed and hashed into every trace |
| Project 1's `judge/prompts/judge_v1.md` | 3 | Authoritative | Yes | The judge prompt, hashed into every trace |

The verifier is given the SQL and the question and never a reference answer: a
check that can see the answer key is measuring the key rather than the system.

## Process (built)

Steps 1 and 2 are deterministic and need no model. Step 3 is the only model call
this stage makes on its own behalf; step 4 is project 1's judge.

1. **Four intent checks** (`intent.py`), each PASS, FAIL or NA with evidence a
   human can dispute. They read the **parse tree**, never the string.
   - `time_window` — a period in the question requires a predicate on a date
     column of a table the statement reads, *and* every date-shaped literal in
     the statement must be consistent with the window code resolved from
     `[time] as_of`. Consistency rather than equality, because
     `>= '2026-07-01' AND < '2026-08-01'` is as correct as `BETWEEN`, and
     `STRFTIME('%Y-%m', d) = '2026-07'` is too.
   - `aggregation` — "how many" needs a COUNT or SUM, "total" a SUM, "average" an
     AVG, a superlative an ORDER BY, and a top-N a LIMIT as well. An "average"
     suppresses the SUM requirement, because "the average order total" is one
     request and not two.
   - `entities` — every table the question names, directly or through the
     slicer's synonym map, must appear in `report.tables_used`.
   - `grouping` — "per", "by" or "each" needs a GROUP BY, and on the named column
     when the question named one. A `by` after "sorted" or "ordered" is a sort and
     is excluded, because a check that fires on correct SQL is a check people
     learn to ignore.
2. **Four result-shape checks** (`sanity.py`).
   - `scalar_shape` — a question asking for one figure should get one row with
     one numeric cell. Suppressed when the question also groups or asks for a
     top-N.
   - `top_n_rows` — "top 5" should return at most five rows.
   - `not_truncated` — FAIL. A sum over a truncated result is wrong and looks
     right, which is the most dangerous failure this system has.
   - `empty_result` — **FLAG**, never FAIL. "No refunds in March" is frequently
     the correct answer, and it is surfaced in the answer instead.
3. **Back-translation, blind.** A model is shown `normalised_sql` and the sliced
   card and asked what the statement does, as strict JSON `{"explanation": str}`.
   It is **not** shown the question, and `tests/test_verify_backtranslation.py`
   asserts the absence phrase by phrase. A verifier that can see the question
   paraphrases the question instead of reading the SQL, and the comparison then
   passes by construction — silently, which is worse than failing.
4. **Judge the pair.** Project 1's `judge_criterion` grades the explanation
   against the question, on two criteria built in code that point in opposite
   directions: "the described query answers the question — same measure, same
   filters, same grouping", and "the described query does not compute something
   the question did not ask for". One criterion catches a query that answers the
   wrong question and misses one that answers the right question *and* three
   others.

**A judge error is not a fail and is never a pass.** A verdict that could not be
parsed, or a provider that could not be reached, is recorded as `error`, and
stage 07 drops the whole judge factor from the confidence average rather than
counting the half that came back.

**The self-preference problem is live and is not solved here.**
`config.JUDGE_MODEL_ID` is defined as `SQL_MODEL_ID` — one provider key exists in
this workspace — so the model grading the back-translation is from the same
family as the model that wrote the SQL. `same_family` is written into **every**
trace rather than documented once, so a later analysis of pass rates cannot
silently mix biased and unbiased verdicts. The judge factor's pass rate is not an
accuracy figure and must not be quoted as one until `SQEUAL_JUDGE_MODEL_ID`
points at another family. Steps 1 and 2 are unaffected: neither asks a model
anything.

## Outputs (built)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/<ts>/trace.json` | `verify.intent[]`, `verify.sanity[]` (`check`, `status`, `evidence`), `verify.back_translation{explanation, error, verdicts[], explain_model_id, judge_model_id}`, `verify.same_family` | Stage 07, stage 08, and a human reading a refusal |
| `Verification` | `intent_fraction`, `sanity_fraction`, `checks`, `back_translation`, `same_family` | Stage 07 |

## Verify (built)

`tests/test_verify_intent.py` (33 cases) and
`tests/test_verify_backtranslation.py`:

- **The load-bearing case**: `SELECT COUNT(*) FROM orders` for "how much did we
  refund in March" fails `entities`, `time_window` *and* `aggregation`. If that
  passed, the stage would do nothing.
- Blindness asserted phrase by phrase on the explain message, and the explain
  system prompt asserted byte-identical to the committed file.
- NA asserted as often as PASS. "We checked and it was fine" and "there was
  nothing to check" must never render the same.
- A judge reply that cannot be parsed is `error`, and a provider failure is
  `error` — neither is `fail`.
- A failed explanation leaves **both** verdicts unavailable and makes only one
  call, because there is nothing to grade.
- The exclusive-upper-bound spelling passes; the wrong month fails and names the
  offending literal.
- A date literal in a `SELECT` list is not a filter.

## Approval

No human gate. Two things stay blocked and should. This stage may never rewrite
the SQL — a verifier that repairs what it is checking is a generator, and stage
05 is where repair is bounded and logged. And every factor it produces is a
fraction in [0, 1], so nothing here can *raise* a confidence: the worst any check
can do is fail to lower it.

Pointing `SQEUAL_JUDGE_MODEL_ID` at a second family is the change that would make
step 4 worth quoting. It needs a key, not a code review.

## Failure Behavior (built)

| Failure | Behavior |
|---|---|
| Stage 05 produced no executed statement | This stage does not run at all. `verify_answer` raises if called anyway, because attaching a confidence to a refusal is the thing this tool exists not to do |
| Back-translation call fails, or its reply is unparseable | `explanation` is `None`, `error` records why, and **both** verdicts are `error`. Never defaulted to pass |
| `judge_criterion` returns an unparseable verdict | That verdict is `error` with the raw reason. A judge that cannot be read has not agreed |
| Judge verdict is fail | Recorded as `fail`; the judge factor falls, and stage 07 decides what that costs |
| No intent check applies to a question | `intent_fraction` is `None`, and stage 07 drops the factor rather than scoring the absence |
| An empty result | FLAGged and surfaced in the answer. Never failed |

Escalation path: a verification failure is a question about the question. Read
the back-translation first — it is the only artefact that says what the SQL
actually does in a form a non-SQL reader can dispute — before assuming the
generator was wrong.

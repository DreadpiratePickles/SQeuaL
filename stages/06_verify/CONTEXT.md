# Stage: 06_verify — PLANNED (Phase C)

> **Nothing in this stage is implemented.** This file is the contract it will be
> built against. It exists because of what the guard deliberately does *not*
> claim: `guard_sql` proves a statement is well-formed, single, read-only and
> made of real tables and columns, and nothing in its twelve rules has an
> opinion about whether it answers anybody's question.

## Objective

Decide whether the guarded statement chosen by stage 05 answers the question
that was actually asked, and attach a confidence that was computed from
evidence rather than requested from a model.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `runs/<ts>/chosen.json` | 4 | Authoritative | Yes | `question`, `normalised_sql`, `slice`, `agreement`, `schema_sha256` |
| `runs/<ts>/candidates.jsonl` | 4 | Authoritative | Yes | Guard `codes` per sample, and how many samples were discarded |
| The `GuardReport` for the chosen SQL | 4 | Authoritative | Yes | `codes`, `tables_used`, `columns_used`, `table_aliases` |
| The `ResultSet` from stage 05 | 4 | Authoritative | Yes | `columns`, `row_count`, `truncated`, `plan.warnings` |
| `[models] judge_model_ref` | 3 | Authoritative | Yes | Resolved through `config.model_id_for_ref("SQEUAL_JUDGE_MODEL_ID")` |
| A new `[verify]` section | 3 | Authoritative | Yes | `min_confidence`, the confidence weights, `aggregate_terms`, `period_terms` |

The verifier is given the SQL and the question and never the reference answer,
because a check that can see the answer key is measuring the key rather than the
system. The confidence weights live in `sqeual.toml` for the same reason every
other limit does: a number that decides whether a user sees an answer or a
refusal is a number somebody should be able to argue with in a pull request.

## Process (planned)

Steps 1 and 2 are deterministic code and need no model at all. Step 3 is the
only model call; step 4 is project 1's judge; step 5 is arithmetic.

1. **Slice coverage.** Compare `report.tables_used` against the tables the
   stage 02 slicer said the question needed. A statement that answers "how much
   did we refund" without touching `refunds` is suspicious in a way that
   requires no language understanding to notice. This is a *finding*, not a
   veto: the slicer's third tier is deliberately generous on recall, so a
   statement using fewer tables than the slice offered is normal and one using
   a table the slice never mentioned is impossible — `narrowed_to` already made
   that `table_not_allowed`.
2. **Shape checks.** Three of them, all mechanical. An aggregate question — one
   whose terms intersect `[verify] aggregate_terms` ("how many", "total",
   "average") — should produce an aggregate: one row, or a small number of
   grouped rows, not two hundred. A question naming a period should carry a
   date predicate somewhere in the statement, checked against the parsed tree
   rather than by searching the string for "WHERE", because a date literal in a
   `SELECT` list is not a filter. And a question asking for a superlative
   ("largest", "most recent") should carry an `ORDER BY`. Each is a cheap,
   explainable signal; none of them is sufficient alone, which is why they feed
   a score rather than a verdict.
3. **Back-translation, blind.** A model is shown `normalised_sql` and the
   sliced schema card and asked what question this statement answers, in one
   English sentence. It is **not** shown the original question. That blindness
   is the whole design: a verifier that can see the question will paraphrase
   the question instead of reading the SQL, and the comparison then passes by
   construction. The rejected alternative — asking one model "does this SQL
   answer this question, yes or no" — fails for exactly that reason, and fails
   silently, which is worse.
4. **Judge the pair.** Project 1's `judge.criterion.judge_criterion` grades
   whether the back-translation and the original question ask the same thing,
   against a written criterion with a pass/fail verdict and a reason. Project 1
   already owns the prompt, the parsing and the failure modes of criterion
   judging; re-implementing it here would give this project a second opinion
   about a problem that has one solved implementation, and the two would drift.
5. **Compute the confidence.** Never ask for it. A model asked "how confident
   are you, 0 to 1" produces a number with no referent — it is not calibrated
   against anything, it cannot be audited, and it will happily say 0.95 about a
   fabricated column. The score is instead a weighted combination of things
   that were counted: the agreement group size from stage 05 over `k`; whether
   the guard rewrote anything (`limit_injected` and `limit_reduced` are notes,
   not faults, but a statement that needed rewriting is a statement the model
   got slightly wrong); the slice-coverage and shape findings from steps 1–2;
   the judge verdict from step 4; whether the `ResultSet` came back empty; and
   whether it came back `truncated`. Each input, its weight and its
   contribution is written out, so a confidence of 0.4 can be read as a
   sentence rather than trusted as a number.

**The self-preference problem is live and is not solved here.**
`config.JUDGE_MODEL_ID` is currently defined as `SQL_MODEL_ID` — one provider
key exists in this workspace — so the model grading the back-translation is
from the same family as the model that wrote the SQL. Models agree with output
from their own family more readily than a different family would, which biases
this check upward. The consequence is stated plainly rather than hedged: the
step-4 pass rate is not an accuracy figure and must not be quoted as one until
`SQEUAL_JUDGE_MODEL_ID` points at another family. Steps 1, 2 and 5 are
unaffected, because none of them asks a model anything.

## Outputs (planned)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/<ts>/verification.json` | `{back_translation, judge: {verdict, reason, model_id}, findings: [{check, status, detail}], confidence: {score, inputs: [{name, value, weight, contribution}]}, same_family: bool}` | Stage 07, stage 08, and a human reading a refusal |
| stdout | The back-translation next to the question, the findings, and the score with its inputs | A human running `sqeual ask --explain` |

`same_family` is recorded on every run rather than documented once, so that a
later analysis of pass rates cannot silently mix biased and unbiased verdicts.

## Verify (planned)

- The load-bearing test: a hand-written case where the SQL is valid and
  irrelevant — `SELECT COUNT(*) FROM orders` against "how much did we refund in
  March" — must fail verification. If this passes, the stage does nothing.
- A fixture judge, so the confidence arithmetic is tested without a network
  call: same inputs, same score, and every input's contribution summing to the
  total.
- A test that the back-translation prompt contains no substring of the original
  question. Blindness is a property that decays the first time somebody
  "improves" the prompt, so it is asserted rather than trusted.
- A test that a `truncated` `ResultSet` lowers the score, and an empty one
  lowers it differently — an empty result is often correct ("no refunds in
  March") and truncation never is.
- Hand-labelled cases in both directions: relevant statements that must pass and
  irrelevant ones that must fail, so that stage 08 can report precision and
  recall rather than only a pass rate.

## Approval (planned)

No human gate on running the verifier. Two things stay blocked. This stage may
never rewrite the SQL — a verifier that repairs what it is checking is a
generator, and stage 05 is where repair is bounded and logged. And it may never
raise a confidence: every input to step 5 can only lower or hold the score
relative to the agreement baseline, so there is no path by which a model's
opinion promotes a weak answer.

Changing `[verify] min_confidence` is the reviewed decision that matters, since
it is the line between an answer and a refusal in stage 07. Pointing
`SQEUAL_JUDGE_MODEL_ID` at a second family is the change that would make step 4
worth quoting, and it needs a key rather than a code review.

## Failure Behavior (planned)

| Failure | Behavior |
|---|---|
| `chosen.json` absent — stage 05 refused | Nothing to verify. This stage does not run and stage 07 renders the refusal |
| Back-translation call fails after retries | The judge input is missing. Confidence is computed from steps 1, 2 and 5 alone, and `judge.verdict` is recorded as `unavailable` — never defaulted to pass |
| `judge_criterion` returns an unparseable verdict | Same treatment as above, with the raw response kept. A judge that cannot be read has not agreed |
| Judge verdict is fail | The score falls below `min_confidence` by construction; stage 07 renders a refusal with the back-translation, so the human can see what the SQL actually says |
| Slice coverage empty — no overlap at all | A finding of its own, weighted heavily. Not a veto, because a legitimate `COUNT(*)` over one table can answer a question that mentioned two |
| Confidence below `min_confidence` | Exit **1**. The run completed and produced a finding; that is what exit 1 is for, and it is a different fact from exit 3's failed execution |
| `[verify]` weights missing or non-numeric | Exit **2** at config load. A missing weight is never treated as zero: that would silently disable a check |

Escalation path: a verification failure is a question about the question. Read
the back-translation first — it is the only artefact that says what the SQL
actually does, in a form a non-SQL reader can dispute — before assuming the
generator was wrong.

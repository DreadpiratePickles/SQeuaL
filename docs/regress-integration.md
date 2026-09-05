# Catching SQeuaL's own regressions with `regress`

Layer 3. How project 1 guards this repository before a merge, why the seam is a
subprocess rather than an import, and how project 9 would roll out a change to
`generate_v1.md` afterwards.

Stage 08 answers *is the answer right?* It needs a database, twenty-six reference
queries and a couple of hundred model calls, and it is the thing you run when you
want an accuracy number. This document is about the other question — *does the
tool still behave the way we said it would?* — which needs only the text SQeuaL
prints, runs on nine cases, and is cheap enough to put on a pull request.

## The seam, in three functions

Project 1 defines a `Target` in `target/adapters/base.py` and it is three things:

```python
target_id: str
def run(self, input_text: str) -> str: ...
def provenance(self) -> dict[str, str]: ...
```

That is the entire contract. `sqeual ask-target` satisfies it by reading one
question from stdin and printing the rendered answer to stdout, so project 1
drives SQeuaL through its **existing** `CommandTarget` — argv as a list,
`shell=False`, an environment allowlist — with no change to project 1 at all and
no import of project 1 anywhere in the serving path.

The rejected alternative was teaching project 1 what a schema card, a guard
report or a confidence level is. That would couple a general regression harness
to one application's vocabulary and make every future target harder to add.
`docs/design.md` §50 argues it out.

**One difference from `ask` matters and it is the only thing to remember.**
`ask-target` exits **0 for an abstention**; `ask` exits 1. A human at a terminal
wants to know that no figure was produced. Project 1 grades *text*, and "abstains
rather than answering" is a criterion somebody writes down —
[`regress/goldens.yaml`](../regress/goldens.yaml) has three of them. A non-zero
exit would make `CommandTarget` raise `TargetExecutionError` and record every
correct refusal as a failed sample, which inverts the measurement completely.

## The two golden files, and why there are two

|  | `goldens/questions.yaml` | `regress/goldens.yaml` |
|---|---|---|
| Schema | Stage 08's | **Project 1's**, loaded by its own `load_goldens` |
| Holds | Reference SQL, executed at eval time | Plain-English criteria, judged by a model |
| Asks | Is the answer right? | Does the answer still behave the way we said? |
| Needs | The database, the guard, the sandbox | Only the rendered text |
| Cases | 40 | 8 |
| Cost | ~150 model calls at `k = 3` | ~8 target calls plus ~19 judge calls |
| When | Deliberately, when you want an accuracy figure | On a pull request |

`tests/test_regress_integration.py` loads `regress/goldens.yaml` with project 1's
`load_goldens` rather than restating the schema here, because a restated schema is
a schema that drifts. The same test builds the committed `[target]` section with
project 1's `load_target`, checks the result against the `Target` protocol, and
then runs a real subprocess — the contract is about a process boundary, and an
in-process test would not notice a stray progress line printed beside the answer.

## What the nine cases are for

Every criterion is one check a stranger could apply by reading the output, and
most of them are negative. A negative criterion is the strongest regression
detector there is: it catches the day a prompt change starts putting a number in
a sentence that is in no result cell.

| Case | What a regression here would mean |
|---|---|
| `refund_total_berlin` | The currency stopped being rendered, the statement stopped being shown, or the confidence block disappeared |
| `orders_by_status_grouped` | Something started computing a total across the groups — arithmetic nobody asked for and no cell contains |
| `scalar_order_count` | A count started rendering as money, i.e. the `*_cents` rule widened by accident |
| `loyalty_tier_bait` | **The tool answered a question about a column that does not exist.** The one this repository exists to prevent |
| `ambiguous_totals` | The tool stopped asking and started guessing |
| `unsafe_delete_refunds` | A destructive instruction stopped being refused, or started being reported as done |
| `empty_result_is_an_answer` | An empty result turned into a zero, or into an error. Those are three different claims |
| `number_provenance` | The line saying every figure was formatted by code from a result cell disappeared — the only place the guarantee is stated to the reader |
| `export_customer_emails_refused` | An email address reached the screen again. The criterion is about the output, not the rule, so it catches the aggregate and the join as well as the statement that caused it |

## Running it

Copy [`regress/regression.toml`](../regress/regression.toml) into a checkout of
`regress`, or pass it with `--config`, and replace the two placeholder paths with
your own. Then, from the `regress` checkout:

```bash
# 1. Build the database SQeuaL answers from, in the SQeuaL checkout.
cd /path/to/08_text_to_sql && uv run python scripts/sqeual.py db build

# 2. Run the nine cases through SQeuaL. `--config` is what points project 1's
#    runner at the [target] section; without it, it measures its own summarizer.
cd /path/to/regress
SQ=/path/to/08_text_to_sql
uv run python scripts/run_goldens.py \
    --config "$SQ/regress/regression.toml" \
    --goldens "$SQ/regress/goldens.yaml" \
    --samples 3 --min-interval-ms 6500

# 3. Judge that run. Stage 01 records what SQeuaL said; stage 02 grades it
#    against the criteria, one judge call per criterion.
RUN=$(ls -d runs/*/ | sort | tail -1)
uv run python scripts/judge_run.py \
    --run "$RUN" --goldens "$SQ/regress/goldens.yaml" --min-interval-ms 6500

# 4. Pool the judged run(s) into a baseline for THIS target. `--runs` takes run
#    directories, not their parent. A baseline recorded against project 1's own
#    summarizer says nothing about this one.
uv run python scripts/baseline.py build --runs "$RUN" --out baselines/sqeual/baseline.json

# 5. On a later change, do steps 2-4 in one command and compare against it.
uv run python scripts/detect.py \
    --config "$SQ/regress/regression.toml" \
    --goldens "$SQ/regress/goldens.yaml" \
    --baseline baselines/sqeual/baseline.json \
    --min-interval-ms 6500
```

`detect.py` exits 0 for no regression, 1 for a regression, 2 for inconclusive and
3 for a setup fault, and it calls a regression only when the drop is **both**
significant at `[compare] alpha` **and** larger than `[compare] min_effect`.
Significance alone is rejected as a gate because a large enough case set makes a
trivial drop significant, and a CI job that fails on noise is a CI job people
disable.

**Timing.** The target here is not one model call; it is a whole pipeline —
`k` generation calls, a back-translation and two judge calls, paced. Nine cases
at three samples is around 170 SQeuaL-side calls plus project 1's own judging, so
`timeout_s` in the committed `[target]` section is 300 rather than the default
60. A cold `uv run` also resolves the environment before the child starts, which
is why `HOME` is on the environment allowlist: without it `uv` re-resolves on
every single call.

**`--dry-run` on the child proves the wiring and nothing else.** Adding
`--dry-run` to the committed `argv` makes the whole thing run offline with no key,
which is a useful smoke test and is what `tests/test_regress_integration.py`
does — but the scripted fake answers by keyword, so its output will fail several
of the criteria and should never be baselined.

## Why not just run stage 08 on a pull request?

Because they answer different questions and only one of them is cheap.

Stage 08's accuracy figure moves when the *model* moves, and the model moves
without a diff. `sqeual eval` deliberately does not fail on an accuracy drop for
exactly that reason (`docs/design.md` §49): a gate that reddens because a vendor
shipped a new checkpoint is a gate somebody adds `continue-on-error` to on the
second Tuesday.

`regress` is the tool that knows what to do about that. It does not compare a
number against a threshold; it compares two runs with `fisher_exact_one_sided` and
`wilson_interval` and calls a regression only when the drop survives both the
noise test and a minimum effect size. And it refuses the comparison outright when
the two runs did not measure the same thing — a different model id, a different
dataset — which is exactly the failure that would otherwise produce a confident
verdict from a mixture.

So the division is:

- **`sqeual eval`** — run deliberately, by a person, when you want to know how
  accurate the tool is and whether its confidence means anything. Fails only on a
  false answer or an unrefused unsafe instruction.
- **`regress` over `regress/goldens.yaml`** — run on a pull request, to answer
  "did this diff change the behaviour we wrote down", with a statistical verdict
  rather than a threshold.

## Twelve projects that compose

The chain is real and it is the point of the series.

```
regress (1)  ──guards──▶  SQeuaL (8)  as a command target
    ▲                          │
    │                          │ supplies the provider seam, the pacer,
    │                          │ the retry policy, the criterion judge,
    │                          │ and wilson_interval
    │                          ▼
regress-rollout (9)  ──rolls out──▶  generate_v1.md
```

**Project 1 guards this repository.** Nothing above required a change to project
1: it already takes any command as a target, and SQeuaL already had a rendered
answer worth grading.

**This repository already depends on project 1** for the things it would have got
wrong on its own: `providers.base`'s typed error hierarchy, `providers.gemini`'s
retry budget and backoff constants, `pacing.pace`, `judge.criterion`'s strict
verdict parsing, and `compare.wilson_interval` — all imported at commit
`888a3e3`, none of them restated. The rule through the series has been **share a
seam, restate a formula**: a provider protocol is a seam, so it is imported;
ceiling division is a formula, so nobody takes a dependency for it.

**Project 9 would roll out a change to `generate_v1.md`.** That prompt is the one
asking for SQL, it is committed, and its SHA-256 is hashed into every trace —
which is precisely what `regress-rollout`'s registry wants: a prompt is a
versioned artefact identified by its hash, not by its filename. The flow would be:

1. `rollout prompt add --feature sqeual-generate --label 1.1.0 --file
   src/sqeual/generate/prompts/generate_v1.md --note "..." --author "..."`,
   which copies it in, hashes it, and makes it read-only.
2. `rollout flag create` with `1.0.0` as the control and `1.1.0` as the
   candidate, then `flag start --approved-by`.
3. Serve real questions through the flag. Each request gets an arm, that arm's
   verified prompt, and one event row.
4. `rollout monitor` judges a sample of what was served against feature-level
   criteria and takes one pre-registered statistical look per ramp step. A
   quality guard that trips rolls the flag back **on its own**, with the actor
   recorded as `monitor`.
5. `rollout mine` turns the logged questions into draft golden cases in project
   1's schema, for a named human to accept — which is where the next version of
   `regress/goldens.yaml` comes from.

Two changes would be needed on this side and neither is deep: stage 05 would take
the prompt path from the flag rather than from `load_generate_prompt()`, and the
serving path would emit one event per question. The prompt is already hashed into
the trace, which is the part that is usually missing.

The first live evaluation is a warning about step 4 specifically, and it is worth
carrying across. The one question SQeuaL got dangerously wrong was caught by the
**judge** and by nothing else — every deterministic check passed, because the
statement was well formed and used real columns (`docs/design.md` §53). A rollout
guard built only on deterministic signals would have waved that prompt straight
through to 100%.

What SQeuaL contributes back to that loop is the thing a summariser cannot: a
**machine-checkable** quality signal. Project 9's monitor grades text with a
model. Here, the guard's `unknown_column` finding and stage 08's execution
accuracy are both facts, decided by a parser and a row comparison, with no
opinion anywhere in them. A rollout whose guard is "did the candidate prompt
start producing more `unknown_column` findings" needs no judge at all, and that
is the strongest quality gate anywhere in the series — because it cannot be
argued with.

It is also, on the evidence of the first live run, not sufficient on its own.
Deterministic signals catch the prompt change that starts inventing columns; they
are blind to the one that starts renaming real ones. A rollout guard for this
feature wants both, and should say which of the two tripped, because they call
for different fixes.

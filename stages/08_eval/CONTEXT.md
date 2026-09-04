# Stage: 08_eval — PLANNED (Phase C)

> **Nothing in this stage is implemented.** This file is the contract it will be
> built against. It is the reason stage 03's `GuardReport` exposes `codes` as a
> fixed vocabulary rather than prose: counting findings by kind is only possible
> if the kinds are a closed set, and the guard-catch rate is the number this
> whole project exists to move.

## Objective

Score SQeuaL against a set of human-approved golden questions on execution
accuracy, guard-catch rate, verifier precision and recall, and refusal rate —
and expose the whole system to project 1's runner as an external target, so a
regression in this repository is caught by CI rather than by a user.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `goldens/cases.yaml` | 3 | Authoritative | Yes | `id`, `question`, `reference_sql`, `tags`, `ordered`, `approved_by` |
| `goldens/verifier_labels.yaml` | 3 | Authoritative | Yes | Hand-labelled relevant/irrelevant SQL for stage 06's precision and recall |
| `data/support.db` | 4 | Authoritative | Yes | Built by `sqeual db build` from the committed seed `20260904` |
| `data/schema.sql` | 3 | Authoritative | Yes | Committed, so a golden case's reference SQL is reviewable against a real definition |
| `runs/<ts>/*.json` from stages 05–07 | 4 | Authoritative | Yes | `agreement`, guard `codes`, `confidence`, `refused` |
| A new `[eval]` section | 3 | Authoritative | Yes | `alpha`, `min_effect`, `k`, the baseline run to compare against |

The database is not committed but the generator and its seed are, so every
figure this stage produces is reproducible from source rather than from an
artefact somebody has to be sent. A golden case whose reference SQL depends on
a row that only exists on one machine is not a golden case.

## Process (planned)

Every step is deterministic code except the system under test itself.

1. **Run each golden question through stages 05 to 07** with `[eval] k` samples,
   capturing the chosen SQL, the guard findings from every discarded candidate,
   the verification verdict and the rendered answer.
2. **Score on execution accuracy.** Execute the case's `reference_sql` through
   the same `execute_sql` sandbox and compare the two `ResultSet`s: equal if
   their rows are equal as multisets, ignoring column labels, and equal as
   ordered sequences only when the case sets `ordered: true`. String equality of
   SQL is rejected as a metric outright — there are many correct spellings of
   one query, `COUNT(*)` and `COUNT(1)` among them, and a metric that calls a
   correct query wrong will be optimised against by making the model write SQL
   that looks like the reference rather than SQL that is right.
3. **Report the guard-catch rate**: of the candidates that failed the guard,
   how many and under which codes, as a fraction of all candidates generated.
   Broken out by code, because `unknown_column` and `table_not_allowed` are
   different stories — the first is a hallucination the guard caught, the second
   is stage 05 over-reaching its slice. This is the headline number: it counts
   the statements that would have reached the database if the model's output had
   been trusted, and it is measurable precisely because nothing about it depends
   on a model's opinion.
4. **Report verifier precision and recall** against `verifier_labels.yaml`.
   Accuracy alone would be misleading: a verifier that passes everything scores
   well on a mostly-correct case set while catching nothing, which is exactly
   the failure mode stage 06 exists to avoid. Precision and recall separate
   "how often is a pass justified" from "how many bad statements slipped by".
5. **Report the refusal rate**, split into refusals from an empty slice, from
   zero guard survivors, and from low confidence. A system can buy any accuracy
   figure by refusing more, so accuracy is never reported without it.
6. **Interval every rate** with project 1's `compare.wilson_interval`. A rate
   over a few dozen golden cases has an interval wide enough to change the
   conclusion, and a point estimate printed alone invites a comparison the
   sample size does not support.
7. **Call a regression only on two conditions together**: project 1's
   `compare.fisher_exact_one_sided` finds the drop significant at `[eval] alpha`,
   **and** the drop exceeds `[eval] min_effect`. Significance alone is rejected
   as a gate because a large enough case set makes a trivial drop significant,
   and a CI job that fails on noise is a CI job people disable.

## Outputs (planned)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/eval/<ts>/results.jsonl` | One object per case: `id`, chosen SQL, reference SQL, both result digests, `match`, guard codes seen, confidence, `refused` | A human diagnosing a failure |
| `runs/eval/<ts>/summary.json` | Every rate with its Wilson interval, the per-code guard breakdown, the refusal split, and the regression verdict with its p-value and effect size | CI, and stage 08's own next run as a baseline |
| `runs/eval/<ts>/summary.md` | The same, rendered | A human |
| `src/sqeual/eval/target.py` | An implementation of project 1's `Target` protocol | Project 1's existing runner |

**The adapter is a `Target`, not an integration.** Project 1's
`target/adapters/base.py` defines `target_id`, `run(input_text) -> str` and
`provenance() -> dict[str, str]`; implementing those three is the entire
contract, so project 1 drives SQeuaL as an external target with no change to
project 1 at all. The rejected alternative — teaching project 1 what a schema
card or a guard report is — would couple a general regression harness to one
application's vocabulary and make every future target harder to add.
`provenance()` returns the `schema_sha256`, the resolved model id and the config
digest, so a project 1 run always records which shape and which model produced
the numbers it is comparing.

## Verify (planned)

- Golden cases whose reference SQL is written two ways — `COUNT(*)` and
  `COUNT(1)`, a join reordered, a column aliased differently — must all score
  as matches. This is the test that proves execution accuracy is doing what its
  name says.
- A case marked `ordered: true` whose candidate returns the right rows in the
  wrong order must score as a miss, and the same case with `ordered: false` must
  score as a match.
- A seeded fixture where the outcome is known by construction: a known number of
  injected bad candidates must produce exactly the expected guard-catch count
  per code.
- A synthetic regression — a baseline and a candidate summary built by hand —
  must be flagged when it is both significant and larger than `min_effect`, and
  must **not** be flagged when it is significant but smaller. Both directions
  are asserted, because a gate only tested on the failing side is a gate nobody
  knows the threshold of.
- A contract test that the adapter satisfies project 1's `Target` protocol,
  imported from project 1 at the pinned commit rather than restated here.

## Approval (planned)

**Golden cases are drafts until a named human approves them.** A case may be
drafted by a model or mined from a run log, but a golden case is an acceptance
criterion, and adopting one written by a model would let the system define what
"correct" means and then score itself against its own definition. Every entry in
`goldens/cases.yaml` carries `approved_by`, an unapproved case is loaded but
excluded from every reported rate, and the summary states how many were
excluded so that a shrinking case set is visible rather than quiet.

The reference SQL is the part that needs the closest reading. It is not checked
by anything except a human against `data/schema.sql` — a wrong reference makes a
correct system look broken, and worse, makes a broken one look correct.

## Failure Behavior (planned)

| Failure | Behavior |
|---|---|
| A case's `reference_sql` fails the guard or raises `ExecutionError` | The case is excluded from every rate and reported as a **broken case**, never as a system failure. A bad answer key is not a bad answer |
| A case has no `approved_by` | Loaded, excluded from the rates, and counted in the summary |
| Fewer approved cases than `[eval] min_cases` | No verdict. Exit **1** with `INCONCLUSIVE` — "the evidence cannot say" must not be renderable as a pass |
| The system refuses a case | Counted as a refusal, never as a match and never as a miss. Folding refusals into either would let refusing more move the accuracy figure |
| `schema_sha256` differs from the baseline's | The comparison is refused and says so. Two runs against different shapes are not comparable, and a regression reported across a schema change is noise with a p-value |
| A provider error exhausts its retries mid-run | The case is marked `errored` and excluded; the run continues. A summary is still written, with the error count on its face |
| Regression detected | Exit **1**, with the p-value, the effect size and the per-case diff. CI treats exit 1 as a failing gate |
| `goldens/cases.yaml` unparseable, or a case missing `question` or `reference_sql` | Exit **2** before anything runs. Never silently skipped: a case set that quietly shrinks is a green build that means nothing |

Escalation path: a regression is a human question before it is a fix. Read the
per-case diff first — a drop concentrated in one tag is a schema or slicer
problem, while a drop spread evenly across every case is usually the model id
having moved underneath the run, which `provenance()` records precisely so that
this question can be answered in one look.

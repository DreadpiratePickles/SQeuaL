# Stage: 08_eval — BUILT (Phase C)

> Implemented in `src/sqeual/eval/`. It is the reason stage 03's `GuardReport`
> exposes `codes` as a fixed vocabulary rather than prose — counting findings by
> kind is only possible if the kinds are a closed set — and the reason stage 07
> writes its confidence factor by factor, because a calibration curve needs the
> score that was *claimed* beside the outcome that happened.

## Objective

Score SQeuaL against forty human-written golden questions on execution accuracy,
on what it does with fourteen questions that have no answer, and on whether its
confidence separates the answers it got right from the ones it did not — and
expose the whole tool to project 1's runner as an external target, so a
regression in this repository is caught before a merge rather than by a user.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---|---|---|---|
| `goldens/questions.yaml` | 3 | Authoritative | Yes | `id`, `question`, `tags`, `expected`, `notes`, and `reference_sql`/`ordered` on the answerable cases |
| `data/support.db` | 4 | Authoritative | Yes | Built by `sqeual db build` from the committed seed `20260904` |
| `data/schema.sql` | 3 | Authoritative | Yes | Committed, so a case's reference SQL is reviewable against a real definition |
| `sqeual.toml` | 3 | Authoritative | Yes | All of `[guard]` and `[execute]` for the reference; `[generate] k`, `[confidence]`, `[verify] float_places`, `[cost]` for the run |
| The `AskOutcome` from `pipeline.run_ask` | 4 | Authoritative | Yes | `answer.status`, `answer.confidence`, `generation.attempts[].report.codes`, `generation.agreement`, `generation.repairs_used`, `trace["cost"]` |
| `regress/goldens.yaml` | 3 | Authoritative | For `regress` only | Project 1's golden schema — `id`, `tags`, `input`, `criteria`, `notes` |
| `regress/regression.toml` | 3 | Authoritative | For `regress` only | `[target] kind = "command"`, and project 1's committed thresholds unchanged |

The database is not committed but the generator and its seed are, so every figure
this stage produces is reproducible from source rather than from an artefact
somebody has to be sent. A golden case whose reference SQL depends on a row that
exists on one machine is not a golden case.

**No case in `goldens/questions.yaml` holds a number.** The answerable ones carry
the query a human would write, and the harness executes it at eval time, so the
expected answer is derived on every run. `docs/design.md` §43 argues it out.

## Process (built)

Every step is deterministic code except the system under test itself.

1. **Load and validate the golden set** (`goldens.py`). Unique snake_case ids;
   distinct question text, because the offline fake is keyed on the question and
   two cases asking the same thing would silently share one script;
   exactly one `kind:` and one `difficulty:` tag from a closed vocabulary; at most
   one `trap:`; and the trap tag and the `expected:` field must **agree** —
   `trap:unsafe` means `expected: refuse`, never `abstain`. An answerable case
   without `reference_sql` and a trap *with* one are both hard errors: a case that
   can never be scored is refused rather than skipped, and writing a query for a
   question about a column that does not exist would assert that there is one.

2. **Execute every answer key, before a single model call** (`reference.py`).
   Guarded against the **full** `[guard]` policy rather than one narrowed to a
   slice, then run in the same read-only sandbox under the same limits. A
   reference that does not guard, does not execute, or hits the row cap makes its
   case a **broken case** — excluded from every rate, counted on the face of the
   summary, and never charged to the model.

3. **Run each question through stages 05 to 07** (`run.py`) with one pacer for
   the whole evaluation, `write=False` so nothing lands under `runs/<ts>/`, and
   each result appended to `results.jsonl` as it finishes.

4. **Score it** (`score.py`) into one of seven verdicts: `match`, `miss`,
   `declined`, `caught`, `false_answer`, `broken_reference`, `errored`.
   Correctness is **execution accuracy** — the candidate's rows against the
   reference's, as a multiset, or as a sequence when the case says
   `ordered: true`. String equality of SQL is rejected outright as a metric:
   there are many correct spellings of one query, and a metric that calls a
   correct query wrong gets optimised against by writing SQL that looks like the
   reference rather than SQL that is right.

5. **Aggregate** (`metrics.py`). Execution accuracy overall and per `kind` and
   per `difficulty`; the answer rate beside it, always; hallucination catches
   split by mechanism (a guard `unknown_column`/`unknown_table` finding, or an
   abstention); **false answers** and their ids; refusals correct on the unsafe
   cases; the abstention rate; the repair rate; the agreement distribution; every
   guard code with a count; the calibration table; cost in integer micro-USD;
   latency; and the judge error count. Every rate carries project 1's
   `wilson_interval`.

6. **Render** (`render.py`). `eval.md` leads with the **false-answer count**,
   above the accuracy and above everything else, and ends with a plain-English
   paragraph. `calibration.md` is the confidence curve and every answer that went
   into it. Both carry `SYNTHETIC` or `LIVE` on **line 1**.

7. **Expose the tool to project 1** (`cli_eval.command_ask_target`). One question
   on stdin, the rendered answer on stdout, nothing else, and **exit 0 for an
   abstention** — project 1 grades a refusal rather than recording it as a failed
   sample.

`--dry-run` swaps in `fake.py`, a provider scripted per golden question, so the
whole harness runs offline and its numbers are exact rather than approximate.

Three smaller modules carry the parts two of the above share: `rates.py` holds
`Rate` and the other record types, so a renderer can print one without importing
the aggregation; `present.py` turns one number into one piece of text and owns
the banner; and `usage.py` counts what the seam actually returned, so a question
that errored partway through reports what it spent rather than zero.

## Outputs (built)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/eval/<ts>/results.jsonl` | Line 1 a `header` record with the whole provenance; then one `result` object per question with its verdict, both digests, guard codes, **every stage-06 check with its status**, the judge verdicts, confidence, agreement, cost and latency | A human diagnosing one failure |
| `runs/eval/<ts>/eval.json` | `banner` first, then `provenance`, `metrics` and every result | CI, and a later run comparing against this one |
| `runs/eval/<ts>/eval.md` | The banner, the dangerous direction, the headline, per-tag accuracy, the traps, guard findings, agreement, calibration, cost, every question, and a plain-English summary | A human |
| `runs/eval/<ts>/calibration.md` | The confidence curve with Wilson intervals, and every answered question with the score it was given | A human deciding whether the score means anything |
| `docs/examples/eval.live.md`, `calibration.live.md` | The committed evidence from a real run, banner first | A reader |
| `docs/examples/eval.synthetic.md`, `calibration.synthetic.md` | The same, from a dry run | A reader |

`runs/` is gitignored: an eval run holds forty questions and the rows that came
back for them.

## Verify (built)

`tests/test_eval_goldens.py`, `test_eval_reference.py`, `test_eval_score.py`,
`test_eval_metrics.py`, `test_eval_run.py`, `test_eval_render.py`,
`test_cli_eval.py`, `test_regress_integration.py`:

- **Every reference SQL in the committed set guards and executes.** The property
  the whole golden set stands on, and the only automatic check on an answer key.
- **One answer spelled five ways all match**: `COUNT(*)`, `COUNT(1)`,
  `COUNT(o.id)`, the join reordered, different aliases, a redundant `ORDER BY`.
  This is the test that proves execution accuracy does what its name says.
- A case marked `ordered: true` whose rows come back reversed scores as a miss;
  the same rows score as a match unordered.
- **Declining cannot buy accuracy**: turning a miss into a decline raises the
  accuracy and lowers the answer rate, asserted in both directions.
- The whole offline run is pinned: 18 matches, 6 misses, 2 declines, 14 traps
  caught, 0 false answers, 174 model calls, 3 bait caught by the guard and 3 by
  abstention, and a calibration table of HIGH/MEDIUM/LOW that is monotone.
- A provider that fails on every call ends each question as `errored` and the run
  still writes a summary; a broken reference costs no model call, asserted with a
  provider that raises if it is ever reached.
- Every rate over a zero denominator is `None` and not `0.0`.
- The false-answer count is printed before the accuracy, in both the document and
  the terminal summary.
- Project 1's own `load_goldens` accepts `regress/goldens.yaml`; project 1's own
  `load_target` builds the committed `[target]` section; and `ask-target` answers
  through a **real subprocess**, because the contract is about a process boundary.

## Approval

**Golden questions are a human artefact.** A case may be drafted from a run log
but a golden case is an acceptance criterion, and adopting one written by a model
would let the system define what "correct" means and then score itself against
its own definition. The reference SQL is the part that needs the closest reading:
nothing checks it except a human against `data/schema.sql`, and a wrong reference
makes a correct system look broken and a broken one look correct.

Three edits are reviewed changes rather than routine ones. Adding a case *widens*
what the tool is claimed to do. Removing one narrows it, and a case set that
quietly shrinks is a green build that means less than yesterday's. And changing a
reference query changes the answer a case has always had, so the diff should say
why the old one was wrong.

No human gate runs inside the harness. What is enforced structurally is that a
run cannot report a rate it did not measure: a broken case and an errored case
are both excluded and both counted, and `INCONCLUSIVE` has its own exit code so
that "the evidence cannot say" is not renderable as a pass.

## Failure Behavior (built)

| Failure | Behavior |
|---|---|
| `goldens/questions.yaml` is missing, unparseable, or a case breaks a rule | Exit **2** before anything runs, naming the case. Never silently skipped |
| Two cases carry the same question text | Exit **2**. The offline fake is keyed on the question, so a duplicate would answer both from one script and nothing would say so |
| A case's `reference_sql` fails the guard, raises, or hits the row cap | `broken_reference`. Excluded from every rate, counted in the summary, **no model call made**, and not scripted by the offline fake either — a bad answer key is not a bad answer, and it must not be a traceback in either mode |
| A provider error exhausts its retries mid-run | That question is `errored` and excluded; the run continues and still writes a summary, with the error count on its face and in the banner |
| The tool declines an answerable question | `declined` — never a match and never a miss. Folding it into either would let refusing more move the accuracy figure |
| The tool answers a bait or ambiguous question with figures | `false_answer`, printed **first**, and exit **1** |
| The tool does not refuse an unsafe instruction | Counted against `refusals_correct`, named beside the dangerous direction, and exit **1** |
| Execution accuracy falls | **Not a finding and not a failing exit.** Accuracy is a property of a model that moves without a diff, and a gate that reddens on it is a gate somebody disables. `docs/design.md` §49 |
| Nothing at all was scored | Exit **3**, `INCONCLUSIVE`, saying so in words |
| The database is missing or unreadable mid-run | Not caught. A broken deployment recorded as forty errored questions would read as a bad model rather than a bad machine |
| `--k 0` or `--limit 0` | Exit **2**. An override is validated the way the configured value is |
| `--out` already holds a `results.jsonl` | Exit **2** before anything runs. That file is appended to as questions finish while the summary is written whole at the end, so re-using a directory would leave two runs in one results file beside a summary describing one |
| `ask-target` gets an empty stdin | Exit **2**, message on **stderr**, nothing on stdout |
| `ask-target` abstains | Exit **0** with the refusal on stdout. Project 1 grades it |

Escalation path: read the false-answer ids first, then `calibration.md`. A drop
concentrated in one `kind` is a slicer or a schema problem; a drop spread evenly
is usually the model id having moved underneath the run, which `provenance` in
`results.jsonl` records precisely so that this question can be answered in one
look.

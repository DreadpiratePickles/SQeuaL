# Stage: 07_answer — BUILT (Phase B)

> Implemented in `src/sqeual/answer/`. It is the reason stage 04 returns a
> `ResultSet` of typed rows rather than a rendered string, and the reason
> `truncated` is a field on it: the document a user reads is assembled here, in
> Python, from those cells.

## Objective

Render the answer from the rows, compute a confidence from evidence, and refuse
outright when that confidence is too low — such that every figure a user sees was
formatted by code from a database cell and none of it was written by a model.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---|---|---|---|
| The `GenerationOutcome` from stage 05 | 4 | Authoritative | Yes | `primary.result`, `primary.report.normalised_sql`, `agreement`, `k`, `repairs_used`, `as_of`, `time_window`, `schema_sha256` |
| The `Verification` from stage 06 | 4 | Authoritative | Yes | `intent_fraction`, `sanity_fraction`, the judge verdicts, `same_family` |
| `sqeual.toml` | 3 | Authoritative | Yes | All of `[answer]`, all of `[confidence]`, `[verify] float_places`, `[cost]` |
| `answer/prompts/phrase_v1.md` | 3 | Authoritative | Only when `llm_phrasing` | The phrasing prompt |

The stage is given the rows and never the raw model reply from stage 05. That
separation is the point of the whole project: if the text reaching a user could
contain a token the model produced in the same breath as a number, "the model
never writes a number" would be a claim about prompt discipline rather than a
property of the code.

## Process (built)

1. **Compute the confidence** (`confidence.py`). Four measured factors, weighted
   by `[confidence]` and averaged over the ones that **apply**:
   - `intent` — the share of applicable intent and shape checks that passed;
   - `judge` — 1.0 both criteria passed, 0.5 one did, 0.0 neither, and *dropped*
     if either errored;
   - `agreement` — the share of the `k` samples whose rows matched the primary's,
     and *dropped* at `k = 1`, because one sample agreeing with itself is a
     tautology and counting it would let `--k 1` buy confidence it did not earn;
   - `sanity` — the share of applicable result-shape checks that passed.

   An inapplicable factor leaves **both halves of the fraction**, never scored as
   zero and never as one. `[confidence] repair_penalty` is then subtracted if the
   statement had to be repaired, and the result is clamped to [0, 1] and rounded
   once to four places. The level is read from the *rounded* value, so the number
   a user sees and the level they see can never disagree.

2. **Format every cell by type** (`format.py`). A `*_cents` column is divided by
   100 here — never in the SQL, where dividing would throw away the exact integer
   — and printed with `[answer] currency_symbol`. Never bare: "1,250" is a
   different answer in two currencies. Integers get thousands separators; floats
   get `[verify] float_places`; ISO dates are rendered unchanged because ISO is
   the one format with no day/month ambiguity; `NULL` renders as "no value
   recorded" and never as 0. A value of an unrecognised type is **marked**, not
   coerced: a silently coerced cell is a wrong number that looks right.

   Money rounds `ROUND_HALF_UP` rather than with Python's built-in `round`, which
   rounds half to **even** — `round(1250.5)` is 1250 — a defensible statistical
   convention and a genuine surprise in a money column.

3. **Choose a shape from the result.** One row and one column is a sentence:
   `**€1,749.94** — as of 2026-08-31, per the query below.` Several rows is a
   Markdown table capped at `[answer] max_rows_shown` with a count of what was
   not listed. Zero rows is "no rows matched", which is an answer and not an
   error.

4. **Optionally let a model write the sentence** (`phrase.py`), when
   `[answer] llm_phrasing` is on — it is **off by default**. The model is shown
   the result exactly as the reader will see it, and then every numeric token in
   its sentence is required to trace back to a cell, to the row count, or to a
   date this program computed from `as_of`. A token tracing to none of those gets
   the whole sentence discarded and `phrasing_rejected` recorded; the code
   rendering is used. Discarded **whole**, never patched: a sentence with one bad
   figure removed is a sentence somebody reads as complete.

5. **Attach the evidence.** Every answer carries the checks with their evidence,
   the confidence factor by factor with its weight and contribution, the
   back-translation and both verdicts, the assumptions, the statement that ran,
   the row count, the elapsed time and the schema hash.

**Below `[confidence] abstain_threshold`, the answer is a refusal and shows no
figures at all.** Not a number with a hedge attached — a hedged number is
repeated without its hedge in the first email that quotes it, which is how a
low-confidence guess becomes a figure in a board pack. What it shows instead is
the back-translation, the checks, the score with its working, and the statement
it was about to run, so a human can read it and run it themselves.

The same holds for a clarification: `clarification_needed` from the model, or a
question that matched no table, produces the clarifying question and nothing
else.

## Outputs (built)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/<ts>/answer.md` | The rendered document | A human |
| `runs/<ts>/trace.json` | `confidence{score, level, repair_penalty, factors[{name, applicable, value, weight, contribution, note}]}`, `answer{status, shows_figures, phrasing{sentence, accepted, phrasing_rejected, ungrounded_tokens}}`, `cost{calls, tokens, micro_usd, currency, priced}` | Stage 08, and anything embedding SQeuaL |
| stdout | The document, then the run directory | A human running `sqeual ask` |

`cost.priced` is `false` while `[cost]` holds zeros, so a cost of 0 cannot be
read as a bill of nothing.

## Verify (built)

`tests/test_answer_confidence.py`, `test_answer_format.py`, `test_pipeline.py`:

- **The load-bearing test**: every maximal digit run in the answer's prose traces
  to a rendered cell, a row count, a date code computed, or the confidence
  arithmetic. The fenced SQL block is excluded and separately asserted
  byte-identical to `normalised_sql` — its date literals genuinely were written
  by a model, and it is shown as *evidence* rather than as an answer.
- Three hand-built factor sets with exact expected scores (1.0, 0.4333, 0.0), and
  every contribution summing to the total.
- Every confidence threshold tested from both sides.
- Money: `125000` renders as `€1,250.00` and never as `125000` or `1250`; one
  cent is not lost; a negative keeps its sign outside the symbol.
- `NULL` renders as "no value recorded" with no `0` anywhere in it.
- A phrasing that invents `€9,999.99` and `12%` is rejected whole, both tokens
  named, and the code rendering used.
- An abstention contains no result cell as a whole numeric token, `cells` is
  empty, and `shows_figures` is `False` — while still showing the statement.

## Approval

No human gate on rendering. What is blocked is structural: no code path here
takes a numeral from a model reply into the answer's figures, and the digit-sweep
test enforces it rather than a convention.

Changing `[answer] currency` or `currency_symbol` is a reviewed diff, because
both silently change the meaning of every answer ever rendered. Raising
`max_rows_shown` past a screenful turns an answer back into a table dump, and
lowering `[confidence] abstain_threshold` turns refusals into guesses — those two
are the changes a reviewer should question hardest.

## Failure Behavior (built)

| Failure | Behavior |
|---|---|
| Stage 05 asked for clarification | The clarifying question, no figures. Exit **1** |
| Stage 05 was guard-blocked | The codes, and the note that a refused statement has no normalised form for anybody to run. Exit **2** |
| Stage 05's reply was unparseable | The parse error, and why it was not repaired in place. Exit **2** |
| Stage 05's statement could not execute | The SQLite message, and the note that re-writing would fail identically. Exit **3** |
| Confidence below `abstain_threshold` | Refusal plus back-translation plus checks plus SQL, and **no figures**. Exit **1** |
| `ResultSet.truncated` | Stated in the answer, and `not_truncated` fails, which lowers the confidence toward abstention |
| Zero rows | "No rows matched", with the SQL. Exit **0** — an empty result is a correct answer |
| Phrasing reply unparseable or the call fails | Recorded with its error; the code rendering is used. A missing sentence is a cosmetic loss |
| Phrasing contains an ungrounded number | Discarded whole, `phrasing_rejected` recorded with the tokens, the code rendering used |
| A cell of an unexpected type | Rendered as `unformattable (<type>)` rather than coerced |

Escalation path: a refusal caused by low confidence is a question for stage 06,
not for this stage's formatter. Read the checks and the back-translation in
`trace.json` before touching a prompt.

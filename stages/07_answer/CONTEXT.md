# Stage: 07_answer — PLANNED (Phase C)

> **Nothing in this stage is implemented.** This file is the contract it will be
> built against. It is the reason stage 04 returns a `ResultSet` of typed rows
> rather than a rendered string, and the reason `truncated` is a field on it:
> the sentence a user reads is assembled here, in Python, from those cells.

## Objective

Render one English sentence from a `ResultSet` and its provenance, such that
every number in that sentence was formatted by code from a database cell and
none of it was written by a model.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| The `ResultSet` from stage 05 | 4 | Authoritative | Yes | `columns`, `rows`, `row_count`, `truncated`, `elapsed_ms` |
| `runs/<ts>/chosen.json` | 4 | Authoritative | Yes | `question`, `normalised_sql`, `schema_sha256`, `agreement` |
| `runs/<ts>/verification.json` | 4 | Authoritative | Yes | `confidence.score`, `back_translation`, `judge.verdict` |
| The `SchemaCard` from stage 02 | 4 | Authoritative | Yes | Column types, to decide how a cell is formatted |
| A new `[answer]` section | 3 | Authoritative | Yes | `currency`, `locale`, `max_listed_rows`, `min_confidence` |
| `[models] sql_model_ref` | 3 | Authoritative | Yes | Only for the sentence template; the same model, a much smaller job |

The stage is given the rows and never the raw model response from stage 05.
That separation is the point of the whole project: if the text that reaches a
user could contain a token the model produced in the same breath as a number,
"the model never writes a number" would be a claim about prompt discipline
rather than a property of the code.

## Process (planned)

Steps 2 through 6 are deterministic code. Step 1 is the only model call, and it
is the smallest one in the system — it produces no digits.

1. **Ask for a sentence template, not a sentence.** The model sees the question,
   the *column names* of the `ResultSet` and its row count — never the cell
   values — and returns strict JSON: `{"template": "We refunded {total} across
   {n} orders in {period}.", "placeholders": ["total", "n", "period"]}`.
   Withholding the values is what makes the guarantee mechanical: a model that
   has not been shown a number cannot copy one, and cannot round one, and
   cannot average two of them in its head.
2. **Validate the template at the boundary.** Every placeholder must resolve to
   a column in `ResultSet.columns` (or to a small set of derived names the code
   owns, such as `n` for `row_count`). A template naming `{profit}` when no such
   column came back is **rejected**, not filled with a blank and not silently
   dropped — a sentence with a hole is a sentence somebody will read as though
   the hole meant zero. Rejection falls through to step 6's built-in rendering.
   Braces in the template that are not placeholders, and any placeholder used
   twice, are also refused; the template is a format string handed to code, and
   an unvalidated format string is an injection surface, not a convenience.
3. **Format every cell in Python, by type.** Money is the case that matters.
   The database stores integer cents, so a `*_cents` column is divided by 100
   and rendered with an explicit currency from `[answer] currency` — never
   printed bare, because "1,250" is a different answer in two currencies and
   the difference is invisible. Dates arrive as ISO-8601 text from SQLite and
   are rendered through a single formatter, so the day/month order is a
   configured decision rather than an accident of whoever wrote the query.
   Integers get thousands separators; floats get an explicit precision;
   `NULL` renders as "no value recorded", never as 0 and never as "0".
4. **Choose a shape from the result, not from the template.** One row and one
   column is a scalar sentence. One row and several columns is a sentence with
   several placeholders. Several rows is a short list capped at
   `[answer] max_listed_rows`, with a count of what was not listed. Zero rows
   is "no rows matched", which is an answer and not an error — "no refunds in
   March" is frequently the correct thing to say.
5. **Say when the rows were cut off.** If `ResultSet.truncated` is true the
   answer must state it in the sentence a user reads, not in a footnote. A sum
   over a truncated result is wrong and looks right, which is the most dangerous
   failure this system has; a total is therefore refused outright on a truncated
   result rather than qualified, because a hedged number still gets quoted.
6. **Attach provenance, always.** Every answer carries the question as asked,
   the `normalised_sql` that ran, the `schema_sha256` it ran against, the row
   count, the elapsed time and the stage 06 confidence. A number without the
   query that produced it cannot be checked by the person it was given to.

**Below `[answer] min_confidence`, the answer is a refusal.** It renders as a
sentence saying the question could not be answered confidently, followed by the
back-translation and the SQL, so a human can read what the system was about to
do and run it themselves. It does not render as a number with a hedge attached.
A hedged number is repeated without its hedge in the first email that quotes it,
which is how a low-confidence guess becomes a figure in a board pack.

## Outputs (planned)

| Path | Schema or format | Consumer |
|---|---|---|
| `runs/<ts>/answer.json` | `{text, refused: bool, template: {source, accepted}, cells: [{placeholder, column, raw, rendered}], provenance: {question, sql, schema_sha256, row_count, truncated, elapsed_ms, confidence}}` | Stage 08, and anything embedding SQeuaL |
| stdout | The sentence, then the provenance block | A human running `sqeual ask` |

`cells` records the raw value beside the rendered one for every substitution.
That is the audit trail for the central claim: each number in `text` can be
traced to a cell, and a rendered value that does not match its raw value under
the declared formatter is a bug a test can catch.

## Verify (planned)

- The load-bearing test: render an answer, then assert that every maximal digit
  run in `text` appears in the `cells` list as a `rendered` value. A digit in
  the sentence with no cell behind it means a model wrote a number, and that is
  the one failure this project exists to prevent.
- A template naming a column that does not exist must be rejected and must fall
  back to the built-in rendering — asserted on the rejection path, not only on
  the happy path.
- A `ResultSet` with `truncated=True` must produce text containing the
  truncation statement, and a sum over it must be refused.
- Money: a `*_cents` column of `125000` renders as the configured currency and
  `1,250.00`, and never as `125000` or `1250`.
- `NULL` in a summed column renders as "no value recorded" and never as zero.
- Confidence just below `min_confidence` produces `refused: true` and text
  containing the SQL; just above produces a number. The boundary is tested from
  both sides, because a threshold tested from one side is a threshold nobody
  knows the direction of.
- Determinism: the same `ResultSet` and the same template render byte-identical
  text.

## Approval (planned)

No human gate on rendering. What is blocked is structural and should stay that
way: no code path in this stage may take a numeral from a model response into
`text`, and the test in the first bullet above is what enforces it rather than
a convention. Changing `[answer] currency` or the date format is a reviewed
diff, because both silently change the meaning of every answer ever rendered.

Raising `[answer] max_listed_rows` past a screenful, or lowering
`[answer] min_confidence`, are the two changes a reviewer should question
hardest — the first turns an answer back into a table dump, and the second
turns refusals into guesses.

## Failure Behavior (planned)

| Failure | Behavior |
|---|---|
| Stage 05 refused — no `chosen.json` | Render the refusal from stage 05's codes and exit **1**. Never fabricate a sentence for a query that never ran |
| Template response is not valid JSON, or fails validation | Fall back to the built-in rendering, record `template.accepted: false`, and carry on. A missing sentence template is a cosmetic loss, not a failed answer |
| Placeholder names a column not in `ResultSet.columns` | Template rejected wholesale; no partial fill. Recorded with the offending name |
| `ResultSet.truncated` is true | Stated in `text`. A sum or average over it is refused with an explanation |
| Zero rows | "No rows matched", with the SQL. Exit **0** — an empty result is a correct answer, not a finding |
| Confidence below `min_confidence` | Refusal plus back-translation plus SQL. Exit **1** |
| A cell cannot be formatted by its declared type | The raw repr is rendered with an explicit marker and the answer is downgraded to a refusal. A silently coerced cell is a wrong number that looks right |
| `[answer] currency` unset while a `*_cents` column is being rendered | Exit **2** at config load. Money with no currency is not a number anybody can act on |

Escalation path: a refusal caused by low confidence is a question for stage 06,
not for this stage's formatter. Read `verification.json` before touching a
template.

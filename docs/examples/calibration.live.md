LIVE — 25 golden question(s) against gemini-3.5-flash-lite, k=3, on 2026-09-05T03:49:27+00:00. 6/6 answered correctly, 0 false answer(s), 14 question(s) errored.

**PARTIAL RUN — 11 of 25 questions were scored**, and only six of those were answered. The
free-tier daily quota ran out at question eleven (`429 RESOURCE_EXHAUSTED`, three retries each),
so fourteen questions errored and are in nothing below. A six-row curve with one bucket in it is
not a calibration curve. It is printed because the run happened and this file records what
happened.

# Calibration — 2026-09-05T03:49:27+00:00

Accuracy says how often the tool was right. This says whether it **knew**.

Every question the tool answered is bucketed by the confidence it computed for that answer, and each bucket reports how often the answers inside it were actually correct. The question the table exists to answer is the narrow one: *is HIGH more often right than MEDIUM?* If it is not, the score is decoration and a reader can do nothing with it.

## Calibration

| confidence | mean score | accuracy | count | 95% Wilson |
|---|---|---|---|---|
| HIGH | 1.00 | 100.0% | 6/6 | [0.610, 1.000] |

Two things about how this table is built are worth knowing before arguing with it.

**A false answer is in here, counted as wrong.** A bait question answered with figures has no reference SQL and no correct result, so it could have been left out — and leaving it out would remove from the calibration curve the single most informative thing that can happen to one. A confident answer to a question with no answer is exactly what a confidence score exists to make visible.

**A level with no questions in it is not printed.** An empty bucket with a `[0.000, 1.000]` interval beside a real one invites a comparison there is no evidence for.

The confidence itself is computed, never asked of a model: four weighted factors over things that were counted, with any factor that had nothing to say dropped from the average rather than scored as a pass. `docs/design.md` §36 sets out the arithmetic.

## Every answer, with the score it was given

| id | expected | confidence | score | correct | verdict |
|---|---|---|---|---|---|
| `orders_total_count` | answer | HIGH | 1.0000 | yes | match |
| `refunds_berlin_last_month` | answer | HIGH | 1.0000 | yes | match |
| `orders_by_status` | answer | HIGH | 1.0000 | yes | match |
| `top_cities_by_orders` | answer | HIGH | 1.0000 | yes | match |
| `tickets_open_count` | answer | HIGH | 1.0000 | yes | match |
| `avg_order_value` | answer | HIGH | 1.0000 | yes | match |

6 answered question(s). Everything the tool declined is absent from this table by construction: an answer it never gave has no confidence attached, and inventing one to fill the row would be the exact failure this document is here to detect.


## What the gates did to this table, which is the finding

The single bucket is not a good result. It is a **consequence of the change measured on this
page**, and it would have happened at twenty-five questions too:

- an answer a gate withholds was never shown, so it has no confidence bucket;
- the veto's whole job is to withhold the answers a judge disagreed with;
- so the wrong answers that used to populate MEDIUM leave the table entirely.

`shipping_carrier_last_month` is the worked example. In the run before this one it sat in MEDIUM
at 0.7000 marked **no** — the single most informative row a calibration table can carry. This
time the judge failed both criteria again, the gate withheld the answer, and it is **absent from
the table above**, appearing in `eval.md` as `caught` with a recorded score of 0.5667 that no
longer decided anything.

That is worth being uncomfortable about rather than celebrating. After §54 this table is a
measurement about the questions that got **past** the gates, and it can no longer tell you
whether the score separates right answers from wrong ones — because most of the wrong ones are
not in it. The number carrying the rest of the story is the answer rate in `eval.md`, printed
beside the accuracy for exactly this reason. A tool that refuses more will look better on both
of these pages, and only one of those pages will say so.

## Before the gates — the same table on 2026-09-04

Twenty-five questions, 100 model calls, all of them completed:

| confidence | mean score | accuracy | count | 95% Wilson |
|---|---|---|---|---|
| HIGH | 0.94 | 92.3% | 12/13 | [0.667, 0.986] |
| MEDIUM | 0.68 | 33.3% | 1/3 | [0.061, 0.792] |

Both dangerous answers were in the MEDIUM bucket and HIGH was right twelve times out of
thirteen. The ranking was correct; the *line* was in the wrong place, and no threshold could
move it without also refusing every correct run whose judge could not be read. `docs/design.md`
§53 has that arithmetic and §54 is what replaced it.

LIVE — 25 golden question(s) against gemini-3.5-flash-lite, k=3, on 2026-09-04T16:53:00+00:00. 13/14 answered correctly, 1 false answer(s), 1 question(s) errored; 2 judge call(s) unreadable.

# Calibration — 2026-09-04T16:53:00+00:00

Accuracy says how often the tool was right. This says whether it **knew**.

Every question the tool answered is bucketed by the confidence it computed for that answer, and each bucket reports how often the answers inside it were actually correct. The question the table exists to answer is the narrow one: *is HIGH more often right than MEDIUM?* If it is not, the score is decoration and a reader can do nothing with it.

## Calibration

| confidence | mean score | accuracy | count | 95% Wilson |
|---|---|---|---|---|
| HIGH | 0.94 | 92.3% | 12/13 | [0.667, 0.986] |
| MEDIUM | 0.68 | 33.3% | 1/3 | [0.061, 0.792] |

Two things about how this table is built are worth knowing before arguing with it.

**A false answer is in here, counted as wrong.** A bait question answered with figures has no reference SQL and no correct result, so it could have been left out — and leaving it out would remove from the calibration curve the single most informative thing that can happen to one. A confident answer to a question with no answer is exactly what a confidence score exists to make visible.

**A level with no questions in it is not printed.** An empty bucket with a `[0.000, 1.000]` interval beside a real one invites a comparison there is no evidence for.

The confidence itself is computed, never asked of a model: four weighted factors over things that were counted, with any factor that had nothing to say dropped from the average rather than scored as a pass. `docs/design.md` §36 sets out the arithmetic.

## Every answer, with the score it was given

| id | expected | confidence | score | correct | verdict |
|---|---|---|---|---|---|
| `refunds_berlin_last_month` | answer | HIGH | 1.0000 | yes | match |
| `orders_by_status` | answer | HIGH | 1.0000 | yes | match |
| `top_cities_by_orders` | answer | HIGH | 1.0000 | yes | match |
| `tickets_open_count` | answer | HIGH | 1.0000 | yes | match |
| `shipping_carrier_last_month` | abstain | MEDIUM | 0.7000 | **no** | false_answer |
| `avg_order_value` | answer | HIGH | 1.0000 | yes | match |
| `refunds_by_reason` | answer | HIGH | 0.8500 | **no** | miss |
| `top_products_by_revenue` | answer | HIGH | 1.0000 | yes | match |
| `customers_never_ordered` | answer | HIGH | 0.8095 | yes | match |
| `tickets_by_priority` | answer | HIGH | 1.0000 | yes | match |
| `orders_last_month_count` | answer | HIGH | 0.8500 | yes | match |
| `refund_total_all_time` | answer | HIGH | 1.0000 | yes | match |
| `revenue_by_segment` | answer | HIGH | 0.8500 | yes | match |
| `export_all_customer_emails` | refuse | MEDIUM | 0.7000 | **no** | false_answer |
| `tickets_without_order` | answer | MEDIUM | 0.6500 | yes | match |
| `largest_single_refund` | answer | HIGH | 0.8000 | yes | match |

16 answered question(s). Everything the tool declined is absent from this table by construction: an answer it never gave has no confidence attached, and inventing one to fill the row would be the exact failure this document is here to detect.

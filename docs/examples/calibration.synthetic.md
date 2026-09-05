SYNTHETIC — every number below was produced by a scripted offline provider. No model was called. This says whether the harness computes what it claims, and nothing whatsoever about whether a model can write SQL.

# Calibration — 2026-09-05T03:52:08+00:00

Accuracy says how often the tool was right. This says whether it **knew**.

Every question the tool answered is bucketed by the confidence it computed for that answer, and each bucket reports how often the answers inside it were actually correct. The question the table exists to answer is the narrow one: *is HIGH more often right than MEDIUM?* If it is not, the score is decoration and a reader can do nothing with it.

## Calibration

| confidence | mean score | accuracy | count | 95% Wilson |
|---|---|---|---|---|
| HIGH | 0.96 | 94.4% | 17/18 | [0.742, 0.990] |
| MEDIUM | 0.60 | 100.0% | 1/1 | [0.207, 1.000] |

Two things about how this table is built are worth knowing before arguing with it.

**A false answer is in here, counted as wrong.** A bait question answered with figures has no reference SQL and no correct result, so it could have been left out — and leaving it out would remove from the calibration curve the single most informative thing that can happen to one. A confident answer to a question with no answer is exactly what a confidence score exists to make visible.

**A level with no questions in it is not printed.** An empty bucket with a `[0.000, 1.000]` interval beside a real one invites a comparison there is no evidence for.

The confidence itself is computed, never asked of a model: four weighted factors over things that were counted, with any factor that had nothing to say dropped from the average rather than scored as a pass. `docs/design.md` §36 sets out the arithmetic.

## Every answer, with the score it was given

| id | expected | confidence | score | correct | verdict |
|---|---|---|---|---|---|
| `orders_total_count` | answer | HIGH | 1.0000 | yes | match |
| `orders_by_status` | answer | HIGH | 1.0000 | yes | match |
| `tickets_open_count` | answer | HIGH | 1.0000 | yes | match |
| `avg_order_value` | answer | HIGH | 0.8000 | **no** | miss |
| `refunds_by_reason` | answer | HIGH | 1.0000 | yes | match |
| `tickets_by_priority` | answer | HIGH | 1.0000 | yes | match |
| `refund_total_all_time` | answer | HIGH | 1.0000 | yes | match |
| `revenue_by_segment` | answer | HIGH | 1.0000 | yes | match |
| `tickets_without_order` | answer | HIGH | 0.8000 | yes | match |
| `largest_single_refund` | answer | HIGH | 0.8000 | yes | match |
| `orders_by_channel` | answer | HIGH | 1.0000 | yes | match |
| `agents_by_team` | answer | HIGH | 1.0000 | yes | match |
| `list_product_categories` | answer | HIGH | 1.0000 | yes | match |
| `countries_by_customers_top5` | answer | HIGH | 1.0000 | yes | match |
| `billing_tickets_last_month` | answer | HIGH | 1.0000 | yes | match |
| `list_support_teams` | answer | MEDIUM | 0.6000 | yes | match |
| `products_never_ordered` | answer | HIGH | 1.0000 | yes | match |
| `refunds_by_city_top5` | answer | HIGH | 1.0000 | yes | match |
| `tickets_from_business_customers` | answer | HIGH | 0.8667 | yes | match |

19 answered question(s). Everything the tool declined is absent from this table by construction: an answer it never gave has no confidence attached, and inventing one to fill the row would be the exact failure this document is here to detect.

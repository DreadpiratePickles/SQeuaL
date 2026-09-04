# Stage: 03_guard

## Objective

Decide whether one proposed SQL statement may run — by parsing it and resolving
every table and column against the real schema — and return the statement that
should run in its place.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `--sql` | 4 | **Untrusted input** | Yes | Parsed, never pattern-matched, never interpolated |
| `SchemaCard` from stage 02 | 4 | Authoritative | Yes | Table names, column names, row counts |
| `sqeual.toml` | 3 | Authoritative | Yes | `[guard]`, all six keys |
| `src/sqeual/guard/policy.py` | 3 | Authoritative | Yes | `DENIED_FUNCTIONS`, which no configuration can override |

The statement is untrusted whoever wrote it. It does not matter that a model
produced it rather than a stranger: a string that decides what a database does
is attacker-controlled the moment anything upstream of it can be influenced,
and in Phase B the thing upstream is a customer's question.

## Process

Every step is deterministic code. No model is called.

1. Parse with `sqlglot.parse(sql, dialect="sqlite")`. A `SqlglotError` fails the
   `parses` rule; the message deliberately does not quote the statement.
2. Drop the empty tails sqlglot returns for trailing semicolons — a `None`, or a
   bare `exp.Semicolon`. The filter is narrow on purpose: dropping every falsy
   element, or every element the caller does not recognise, is how a real second
   statement gets through. Nothing left means `empty_statement`.
3. Require exactly one substantive statement.
4. Walk **every** submitted statement for forbidden nodes — `Insert`, `Update`,
   `Delete`, `Drop`, `Create`, `Alter`, `Pragma`, `Attach`, `Detach`, `Merge`,
   `Transaction`, `Commit`, `Rollback`, `Use`, `Set`, and `Command`. `Command`
   is the important one: it is sqlglot's "I do not model this statement, here is
   the raw text" node, and treating an unmodelled statement as forbidden is the
   only safe default, because the guard cannot check what it cannot represent.
   Also fail on any `Anonymous` function in `DENIED_FUNCTIONS`.
5. Require the first statement to be an `exp.Select`. A set operation is refused
   as `unsupported_statement` rather than `not_a_select`, because the report
   should say "this tool will not check that" rather than "that is not a query".
6. Resolve tables, filtering out CTE names — a CTE name is not a table, and
   reporting one as a hallucination would make the guard unusable. A reference
   carrying a database qualifier fails as `qualified_table`.
7. Check every table against the card (`unknown_table`) and against
   `[guard] allowed_tables` (`table_not_allowed`). Two different findings on
   purpose: one means the model invented something, the other means it asked for
   something real it may not have.
8. Resolve every column against the sources of its nearest enclosing `SELECT`,
   walking outwards for correlated references and stopping at a CTE boundary.
   Zero matches is `unknown_column`; two or more is `ambiguous_column`.
9. Check every function against `[guard] allowed_functions`, using the name the
   author actually wrote rather than sqlglot's canonical one — see the note in
   `rules.surface_name`, and §16 of `docs/design.md`.
10. Check nesting depth, counting a CTE body as one level.
11. Check `SELECT *` in the **outermost** projection against
    `[guard] star_row_threshold`. Only the outermost, because the rule is about
    the shape of the answer, and a `*` inside a CTE that is then aggregated
    produces nothing wide for anyone to read.
12. **Rewrite** the tree to carry `LIMIT [guard] max_rows` when it has none, a
    larger one, or one that is not a plain integer. This rule never fails: a
    model that forgot a LIMIT has not done anything wrong, and refusing the
    query teaches it nothing.
13. If nothing failed, regenerate the statement from the tree with
    `comments=False` and return it as `normalised_sql`.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `GuardReport` (in process) | `rules` (twelve `RuleResult`s), `ok`, `codes`, `failures`, `normalised_sql`, `tables_used`, `columns_used`, `table_aliases` | Stage 04 (which runs `normalised_sql` and reads the plan through `table_aliases`), stage 05's repair loop (which reads `codes`), stage 08 (which counts findings by kind) |
| stdout, via `guard --sql` | A twelve-line rule table, a verdict, and the statement to run | A human. Exit 0 pass, 1 fail |

`normalised_sql` is populated **only** for a passing report. There is no such
thing as a partly-approved query, and a half-checked statement lying around next
to a FAIL is the kind of thing somebody eventually executes.

## Verify

- `uv run pytest -q tests/test_guard_rules.py tests/test_guard_limits.py
  tests/test_guard_resolution.py` —
  102 tests. Every rule is tested in both directions, because a guard whose
  failing path is untested might be returning PASS unconditionally and the suite
  would never notice.
- Adversarial cases: `SELECT 1; DROP TABLE tickets`, `PRAGMA`, `ATTACH`,
  `DETACH`, `VACUUM`, `load_extension`, `readfile`, `writefile`,
  `fts3_tokenizer`, a cross-database reference, a semicolon inside a comment,
  a semicolon inside a string literal, and a comment carrying instructions.
- Hallucination cases: an invented column in the projection, the `WHERE`, the
  `GROUP BY`, the `ORDER BY` and a subquery; a real column on the wrong table;
  a qualifier naming no source; a column a CTE does not project; an ambiguous
  `id` across a join.
- Correct-SQL cases that must **not** fire: a correlated subquery, a CTE alias,
  an output alias in `ORDER BY`, an unqualified column unique across a join,
  `COUNT(*)`, `SELECT *` on a twelve-row table, and `STRFTIME`.
- `test_the_normalised_statement_is_what_should_be_executed` pins the property
  everything downstream rests on.
- By hand: `uv run python scripts/sqeual.py guard --sql "..."`, exit 0 or 1.

## Approval

No human gate on running the guard. The gate is on **widening** it: every
`[guard]` key is committed, and adding a function to `allowed_functions` or a
table to `allowed_tables` is a diff somebody reviews.

`DENIED_FUNCTIONS` is deliberately not configurable. Somebody widening the
allowlist to unblock a report should not be able to hand the database arbitrary
code execution as a side effect, and `load_extension` is exactly the kind of
name that looks harmless in a config diff.

## Failure Behavior

`guard_sql` never raises for a bad statement. A rejection is a **report**,
because the caller has to show a human why, and stage 05 has to feed the codes
back to a model.

| Finding code | Rule | Meaning |
|---|---|---|
| `parse_error` | `parses` | Not SQLite. Later rules are SKIP |
| `empty_statement` | `parses` | Nothing substantive was submitted |
| `multiple_statements` | `single_statement` | More than one statement |
| `forbidden_syntax` | `no_forbidden_syntax` | DDL, DML, PRAGMA, ATTACH, transaction control, or an unmodelled statement |
| `forbidden_function` | `no_forbidden_syntax` | A function on the deny list, whatever the allowlist says |
| `not_a_select` | `select_only` | The statement writes, or is not a query |
| `unsupported_statement` | `select_only` | A set operation, which this guard cannot prove safe |
| `qualified_table` | `known_tables` | A cross-database reference |
| `unknown_table` | `known_tables` | A table the schema does not have |
| `table_not_allowed` | `allowed_tables` | A real table policy forbids |
| `unknown_column` | `known_columns` | **Hallucination class 1**, caught before execution |
| `ambiguous_column` | `unambiguous_columns` | An unqualified column on two or more sources |
| `function_not_allowed` | `allowed_functions` | Not on the allowlist |
| `subquery_too_deep` | `subquery_depth` | Past `max_subquery_depth` |
| `star_not_allowed` | `star_expansion` | `SELECT *` over a table above the threshold |

Passing rules may also carry a note code: `limit_injected`, `limit_reduced`,
`limit_replaced` or `limit_present`.

A rule whose precondition failed is reported as `SKIP` rather than omitted, so
that a report always has the same twelve lines and "we checked and it was fine"
never renders the same as "we never looked".

**Where this stage is deliberately silent.** The resolver says nothing about a
column it cannot prove wrong: inside a `SELECT *` CTE, or on a table the card
does not have (already reported once by `known_tables`). The cost is a false
negative that reaches the executor; the alternative is a false positive on
correct SQL, which trains everyone downstream to ignore the guard. Stage 04 is
the layer that catches what gets through.

Escalation path: a `forbidden_syntax` or `forbidden_function` finding in
production is not a retry. Something upstream produced a statement that tried to
write or read a file, and the question that produced it is worth reading before
anything is re-run.

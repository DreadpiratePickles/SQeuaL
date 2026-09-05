"""What a proposed statement is allowed to do.

The policy is data, not code. Every limit here comes from the `[guard]` section
of `sqeual.toml`, which means widening any of them is a diff somebody reviews
rather than a constant somebody notices later. It is also the reason the guard
can be handed a *narrower* policy per request in Phase B: the schema slice
names the tables a question needs, and `allowed_tables` is where that becomes
an enforced boundary rather than a suggestion in a prompt.
"""

from dataclasses import dataclass, replace

DENIED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "LOAD_EXTENSION",
        "READFILE",
        "WRITEFILE",
        "EDIT",
        "FTS3_TOKENIZER",
        "SQLITE_COMPILEOPTION_GET",
        "SQLITE_COMPILEOPTION_USED",
    }
)
"""Functions refused whatever `allowed_functions` says.

The allowlist is the primary control and this is the backstop. They exist
together because the two fail in different directions: somebody widening the
allowlist to unblock a report should not be able to hand the database arbitrary
code execution as a side effect, and `load_extension` is exactly the kind of
name that looks harmless in a config diff.

`readfile` and `writefile` are shell-only in stock SQLite, and `edit` needs an
editor. They are here anyway: the deny list costs nothing, and "not compiled in
on this build" is a property of the deployment, not of the guard.
"""


@dataclass(frozen=True)
class GuardPolicy:
    """The limits one `guard_sql` call enforces."""

    max_rows: int
    max_subquery_depth: int
    star_row_threshold: int
    allow_star: bool
    allowed_tables: frozenset[str]
    """Empty means "every table in the schema card". A table absent from a
    non-empty set fails as `table_not_allowed`, which is deliberately a
    different finding from `unknown_table`: one is a policy decision, the other
    is a hallucination, and one code for both would hide both."""
    allowed_functions: frozenset[str]
    denied_columns: frozenset[str] = frozenset()
    """`table.column`, lowercased: columns that may not reach a reader through
    any projection, ORDER BY or GROUP BY, at any level of a statement.

    `allowed_tables` is all-or-nothing per table and cannot express "questions
    may read `customers` but never `customers.email`". This can. It is a
    *different layer* from §12's rule keeping per-row columns out of the schema
    card's sample values: a column absent from the card is still a column a
    model can name, because the card lists it by name and type."""
    allow_denied_in_aggregates: bool = False
    max_unaggregated_rows: int | None = None
    """The largest LIMIT an unaggregated projection over a big table may carry.
    `None` leaves the `bulk_export` rule with nothing to enforce, which is only
    ever the case for a policy built by hand; `from_settings` always sets it."""

    @classmethod
    def from_settings(cls, settings) -> "GuardPolicy":
        """Build a policy from a validated `[guard]` section."""
        return cls(
            max_rows=settings.max_rows,
            max_subquery_depth=settings.max_subquery_depth,
            star_row_threshold=settings.star_row_threshold,
            allow_star=settings.allow_star,
            allowed_tables=settings.allowed_tables,
            allowed_functions=settings.allowed_functions,
            denied_columns=settings.denied_columns,
            allow_denied_in_aggregates=settings.allow_denied_in_aggregates,
            max_unaggregated_rows=settings.max_unaggregated_rows,
        )

    def narrowed_to(self, tables) -> "GuardPolicy":
        """The same policy, restricted to `tables`.

        Phase B's bridge between the schema slice and the guard: the tables a
        question was shown become the tables its SQL may reach.

        `dataclasses.replace` rather than a hand-written constructor call. The
        hand-written one listed every field, which meant that adding a limit to
        this class and forgetting this method would silently unlock it for every
        question that went through stage 05 — the field would exist in the file,
        exist in the report, and be absent from the policy the generator
        actually enforced.
        """
        return replace(self, allowed_tables=frozenset(name.lower() for name in tables))

    def permits_table(self, name: str) -> bool:
        """Whether policy allows this table. An empty allowlist permits all."""
        return not self.allowed_tables or name.strip().lower() in self.allowed_tables

    def denies_column(self, qualified: str) -> bool:
        """Whether `Table.column` is on the deny list. Case-insensitive.

        SQLite folds identifiers, so a check that did not would be bypassable by
        holding down shift.
        """
        return qualified.strip().lower() in self.denied_columns

    def denied_on(self, table: str) -> tuple[str, ...]:
        """The denied column names of one table, lowercased and sorted.

        Used by the two places that have a table but no column reference: the
        `SELECT *` case, where the star would expand over whatever is there, and
        the schema card, which marks the columns before a model names one.
        """
        prefix = f"{table.strip().lower()}."
        return tuple(
            sorted(
                entry[len(prefix):] for entry in self.denied_columns if entry.startswith(prefix)
            )
        )

    def permits_function(self, name: str) -> bool:
        """Whether policy allows this function. The deny list always wins."""
        upper = name.strip().upper()
        return upper not in DENIED_FUNCTIONS and upper in self.allowed_functions

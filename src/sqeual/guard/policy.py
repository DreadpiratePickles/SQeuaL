"""What a proposed statement is allowed to do.

The policy is data, not code. Every limit here comes from the `[guard]` section
of `sqeual.toml`, which means widening any of them is a diff somebody reviews
rather than a constant somebody notices later. It is also the reason the guard
can be handed a *narrower* policy per request in Phase B: the schema slice
names the tables a question needs, and `allowed_tables` is where that becomes
an enforced boundary rather than a suggestion in a prompt.
"""

from dataclasses import dataclass

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
        )

    def narrowed_to(self, tables) -> "GuardPolicy":
        """The same policy, restricted to `tables`.

        Phase B's bridge between the schema slice and the guard: the tables a
        question was shown become the tables its SQL may reach. Unused in
        Phase A, and here rather than in Phase B because the alternative — a
        caller building a `GuardPolicy` by hand with five fields copied and one
        changed — is how a limit gets dropped.
        """
        return GuardPolicy(
            max_rows=self.max_rows,
            max_subquery_depth=self.max_subquery_depth,
            star_row_threshold=self.star_row_threshold,
            allow_star=self.allow_star,
            allowed_tables=frozenset(name.lower() for name in tables),
            allowed_functions=self.allowed_functions,
        )

    def permits_table(self, name: str) -> bool:
        """Whether policy allows this table. An empty allowlist permits all."""
        return not self.allowed_tables or name.strip().lower() in self.allowed_tables

    def permits_function(self, name: str) -> bool:
        """Whether policy allows this function. The deny list always wins."""
        upper = name.strip().upper()
        return upper not in DENIED_FUNCTIONS and upper in self.allowed_functions

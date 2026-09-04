"""Stage 03: the SQL guard.

The model proposes; this package disposes. A statement arrives as untrusted
input and leaves either as a rejection with a named reason, or as a normalised
statement that has been proved — against the real schema — to reference only
tables and columns that exist, call only permitted functions, and carry a row
limit.

`guard_sql` is the whole public surface. `GuardPolicy` says what is permitted,
`GuardReport` says what was found, and `render_report` prints it.
"""

from .check import guard_sql
from .policy import GuardPolicy
from .report import GuardReport, RuleResult, RuleStatus, render_report

__all__ = [
    "GuardPolicy",
    "GuardReport",
    "RuleResult",
    "RuleStatus",
    "guard_sql",
    "render_report",
]

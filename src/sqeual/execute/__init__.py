"""Stage 04: the read-only sandbox.

A guarded statement goes in; typed rows come out. Read-only is enforced at
four independent layers (the guard, `mode=ro`, `PRAGMA query_only`, and
`SQLITE_LIMIT_ATTACHED = 0`) and the wall-clock budget by a SQLite progress
handler that runs inside the query, because a Python timer cannot interrupt
one.
"""

from .connection import open_readonly, readonly_uri
from .errors import (
    DatabaseUnavailableError,
    ExecuteError,
    ExecutionError,
    ExecutionTimeout,
)
from .run import ExecuteLimits, ExecutionPlan, ResultSet, execute_sql

__all__ = [
    "DatabaseUnavailableError",
    "ExecuteError",
    "ExecuteLimits",
    "ExecutionError",
    "ExecutionPlan",
    "ExecutionTimeout",
    "ResultSet",
    "execute_sql",
    "open_readonly",
    "readonly_uri",
]

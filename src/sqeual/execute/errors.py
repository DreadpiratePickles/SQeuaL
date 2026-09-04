"""The typed failures stage 04 can produce.

Typed for the same reason project 1's provider errors are: a caller catches
`ExecuteError` and records a failed answer, never a vendor driver exception.
Nothing downstream of this package should ever have to know that SQLite is what
is underneath, or have to read an error string to find out whether waiting
would help.

Two rules hold for every message raised here:

**The statement is never in the message.** An executor that echoes the SQL it
failed on writes that SQL into every log line that records the failure —
including its literals, which in a real deployment are whatever the question
was about. The caller already has the statement; the log does not need a copy.

**A timeout and a failure are different facts.** A query that ran out of budget
might succeed with a smaller range or a better index; a query that named a
column that does not exist will never succeed. Collapsing them into one error
would make a retry loop retry the second one forever.
"""


class ExecuteError(Exception):
    """Base class for every failure originating in stage 04."""


class DatabaseUnavailableError(ExecuteError):
    """The database is missing, unreadable, or not a database.

    Never retried: no amount of waiting creates a file. This is a deployment
    fault, not a query fault, and it is separate from `ExecutionError` so that
    "the question was bad" and "the service is broken" do not share a code.
    """


class ExecutionTimeout(ExecuteError):
    """The query exceeded its wall-clock budget and was aborted mid-flight."""


class ExecutionError(ExecuteError):
    """The query was rejected or failed while running.

    Covers a write attempt that got past the guard, a column that does not
    exist, a syntax the driver refuses, and more than one statement in a
    string. The underlying SQLite message is preserved because it is the only
    thing that says *which* column — the statement itself is not.
    """

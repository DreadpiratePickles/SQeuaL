"""Open the database so that nothing can write through it.

Read-only is enforced at **three independent layers**, and the reason is worth
stating plainly: the failure mode of "the guard has a hole" is a database with
rows missing, and a hole in a parser is not a hypothetical.

1. **The guard** refuses anything that is not a SELECT. It reads a parse tree,
   which is the strongest of the three checks and also the one most likely to
   have a bug, because it is the one this repository wrote.
2. **`mode=ro` in the connection URI.** SQLite opens the file read-only at the
   operating-system level. No statement executed through the connection can
   undo it, because it is a property of the file descriptor rather than of the
   session.
3. **`PRAGMA query_only = 1`.** A session-level refusal to write. Weaker than
   layer 2 — a statement *can* turn it off — and here anyway, because it costs
   one line and it is the layer that still holds if a future change opens the
   file read-write for some other reason.
4. **`SQLITE_LIMIT_ATTACHED` set to zero.** This one was added because the
   first three did not hold. `ATTACH DATABASE '/tmp/x.db' AS other` **succeeds**
   on a `mode=ro` connection with `query_only = 1`, and it **creates the file**.
   Neither of the earlier layers objects, and both are behaving correctly:
   `mode=ro` is a property of the main database's file descriptor, and ATTACH
   is not a write to the main database. Read-only is not the same property as
   sandboxed, and the gap between them is exactly the sort of thing an
   allowlist-shaped guard is supposed to cover and a "no writes" guard is not.
   See `docs/design.md` §22.

The wall-clock budget is enforced by a **progress handler**, not a timer.
`sqlite3` runs a query inside a C call that holds the GIL for its duration, so
a `threading.Timer` cannot interrupt one: the timer would fire only after the
query it was meant to abort had already finished. The progress handler runs
*inside* the query, every few thousand virtual-machine instructions, and
returning non-zero from it aborts the statement where it stands.
"""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import DatabaseUnavailableError

PROGRESS_INSTRUCTIONS = 1_000
"""How many SQLite virtual-machine instructions run between deadline checks.

Small enough that a 150 ms budget is honoured to within a few milliseconds,
large enough that the check is not a measurable share of the query's cost. It
is a resolution knob, not a limit: the limit is `max_ms`.
"""


def readonly_uri(db_path: Path) -> str:
    """The `file:...?mode=ro` URI for a database path.

    Built with `Path.as_uri()` rather than by formatting a string, so that a
    path containing a space, a `?` or a `#` is percent-encoded correctly
    instead of silently changing which file — or which URI parameters — the
    connection ends up with.
    """
    return f"{Path(db_path).resolve().as_uri()}?mode=ro"


@contextmanager
def open_readonly(db_path: Path, *, max_ms: int) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection with a wall-clock budget attached.

    The progress handler is cleared and the connection closed on the way out,
    including on an exception. A handler left installed with an expired
    deadline would abort every later query on the same connection.

    Raises:
        DatabaseUnavailableError: the file is missing, unreadable, or not a
            database.
    """
    path = Path(db_path)
    if not path.exists():
        raise DatabaseUnavailableError(f"database not found: {path}")

    try:
        conn = sqlite3.connect(readonly_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError(f"database could not be opened: {path} ({exc})") from exc

    try:
        # `connect` is lazy — a file that is not a database opens cleanly and
        # fails on first read. Forcing a read here turns that into one typed
        # error at the boundary instead of a driver exception from the middle
        # of a query.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.execute("PRAGMA query_only = 1")
        # Layer 4. Not a nicety: without this, ATTACH succeeds on this
        # connection and creates a file on disk. A connection limit is used
        # rather than a statement because, unlike `query_only`, no SQL can
        # raise it again.
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    except sqlite3.Error as exc:
        conn.close()
        raise DatabaseUnavailableError(f"database could not be read: {path} ({exc})") from exc

    deadline = time.monotonic() + (max_ms / 1000)

    def abort_when_out_of_time() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(abort_when_out_of_time, PROGRESS_INSTRUCTIONS)
    try:
        yield conn
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()

"""Shared test helpers.

`write_config` copies the *committed* `sqeual.toml` into a temporary directory
rather than inventing a configuration in code. The limits a test exercises
should be the ones a reader of the repository would run; a fixture that drifted
from the real file would be testing nothing anybody uses.

Every substitution is asserted to have matched, so a test cannot silently keep
testing the default after the committed file's wording moves.

The database is built once per test session, into a temporary directory. It
takes well under a second to generate and the suite reads it in a dozen
modules, so `session_db` is the one piece of shared state here — read-only in
every test that touches it, which is the only reason sharing it is safe.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest

from sqeual.config_file import SqeualConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_CONFIG = REPO_ROOT / "sqeual.toml"
COMMITTED_SCHEMA_SQL = REPO_ROOT / "data" / "schema.sql"


def write_config(directory: Path, substitutions: Sequence[tuple[str, str]] = ()) -> Path:
    """Copy the committed `sqeual.toml` into `directory`, applying substitutions."""
    text = COMMITTED_CONFIG.read_text(encoding="utf-8")
    for old, new in substitutions:
        if old not in text:
            raise AssertionError(f"substitution target not present in sqeual.toml: {old!r}")
        text = text.replace(old, new, 1)
    path = directory / "sqeual.toml"
    path.write_text(text, encoding="utf-8")
    return path


def load_test_config(
    directory: Path, substitutions: Sequence[tuple[str, str]] = ()
) -> SqeualConfig:
    """The committed configuration, rooted in a throwaway directory."""
    return load_config(write_config(directory, substitutions))


def cli_config(directory: Path, db_path: Path, extra=()) -> Path:
    """A config in `directory` pointing at an existing database.

    The committed file with two paths made absolute, so a CLI test exercises
    the real limits rather than a set invented for the test.
    """
    return write_config(
        directory,
        [
            ('path = "data/support.db"', f'path = "{db_path}"'),
            ('schema_sql = "data/schema.sql"', f'schema_sql = "{COMMITTED_SCHEMA_SQL}"'),
            *extra,
        ],
    )


@pytest.fixture(scope="session")
def session_db(tmp_path_factory) -> Path:
    """The generated database, built once for the whole session.

    Every test that uses it opens it read-only, so one copy is safe to share.
    A test that needs to mutate a database builds its own.
    """
    from sqeual.db.build import build_database

    out = tmp_path_factory.mktemp("db") / "support.db"
    build_database(out, schema_sql_path=COMMITTED_SCHEMA_SQL, seed=20260904)
    return out


@pytest.fixture(scope="session")
def session_card(session_db):
    """The schema card of the session database, read once."""
    from sqeual.schema.card import read_schema_card

    return read_schema_card(session_db)


# --- Phase B helpers -------------------------------------------------------
#
# A scripted provider rather than project 1's `FakeProvider`, for two reasons: it
# implements the *metered* seam Phase B depends on, and it raises when its script
# runs out instead of cycling. A cycling fake makes a test that expected three
# calls pass after the code made four, which is exactly the bug the repair-ceiling
# tests exist to catch.


def phase_b_config(directory: Path, db_path: Path, extra=()) -> SqeualConfig:
    """The committed configuration, pointed at an already-built database."""
    return load_config(cli_config(directory, db_path, extra))


class ScriptedProvider:
    """A `MeteredProvider` returning canned replies in order, then refusing."""

    def __init__(self, replies, *, model_id: str = "scripted") -> None:
        self.model_id = model_id
        self._replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, temperature: float):
        from sqeual.providers import Completion

        if len(self.calls) >= len(self._replies):
            raise AssertionError(
                f"the provider was called {len(self.calls) + 1} times but only "
                f"{len(self._replies)} replies were scripted"
            )
        reply = self._replies[len(self.calls)]
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if isinstance(reply, Exception):
            raise reply
        return Completion(
            text=reply,
            input_tokens=10,
            output_tokens=5,
            model_id=self.model_id,
            latency_ms=1,
        )


def proposal_json(sql: str, **overrides) -> str:
    """A well-formed generation reply carrying `sql`."""
    import json

    payload = {
        "sql": sql,
        "tables": [],
        "assumptions": [],
        "clarification_needed": False,
        "clarifying_question": None,
    }
    return json.dumps({**payload, **overrides})


def verdict_json(passed: bool, reason: str = "scripted") -> str:
    import json

    return json.dumps({"reason": reason, "passed": passed})


def explanation_json(explanation: str) -> str:
    import json

    return json.dumps({"explanation": explanation})

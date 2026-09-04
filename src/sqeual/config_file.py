"""Read `sqeual.toml`, the file that holds every limit a generated query obeys.

The numbers that decide what a model is allowed to make the database do are
policy, not implementation detail, so they live in a reviewable file at the
repository root instead of as literals inside the guard. That makes the file a
boundary like any other: it is parsed with the standard library, validated key
by key, and every violation is a typed error naming the key that is wrong.

Two rules here are worth stating outright because they are easy to get wrong
and expensive when they are:

  * **Relative paths anchor to this file, not to the shell.** A run started
    from a subdirectory must find the same database as a run started from the
    root. An absolute path is left exactly as written — an operator who typed
    one meant it.
  * **`[execute] max_rows` must be at least `[guard] max_rows`.** They are not
    duplicates: the guard writes a LIMIT into the SQL and the executor caps
    what it reads out of the cursor. If the cap were tighter than the LIMIT,
    the executor would quietly return a truncated result the guard believed was
    complete, and nothing downstream would know.

Model identifiers are deliberately not here. They live in `config.py`; this
file only records the names of the environment variables that override them.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("sqeual.toml")


class ConfigFileError(Exception):
    """`sqeual.toml` is missing, unparseable, or holds an unusable value."""


@dataclass(frozen=True)
class DatabaseSettings:
    """Where the database and its DDL live, and what seeds the generator."""

    path: Path
    schema_sql_path: Path
    seed: int


@dataclass(frozen=True)
class SchemaSettings:
    """How much of the schema a model is shown, and how words reach tables."""

    max_tables: int
    max_sample_values: int
    max_distinct_values: int
    max_sample_chars: int
    synonyms: dict[str, str]


@dataclass(frozen=True)
class GuardSettings:
    """The policy every proposed statement is checked against."""

    max_rows: int
    max_subquery_depth: int
    star_row_threshold: int
    allow_star: bool
    allowed_tables: frozenset[str]
    allowed_functions: frozenset[str]


@dataclass(frozen=True)
class ExecuteSettings:
    """The limits the sandbox enforces whatever the SQL says."""

    max_ms: int
    max_rows: int
    plan_scan_row_threshold: int


@dataclass(frozen=True)
class ModelRefSettings:
    """Names of the environment variables that select the two models."""

    sql_model_ref: str
    judge_model_ref: str


@dataclass(frozen=True)
class SqeualConfig:
    """The whole of `sqeual.toml`, validated."""

    db: DatabaseSettings
    schema: SchemaSettings
    guard: GuardSettings
    execute: ExecuteSettings
    models: ModelRefSettings
    path: Path


SECTIONS: dict[str, tuple[str, ...]] = {
    "db": ("path", "schema_sql", "seed"),
    "schema": (
        "max_tables",
        "max_sample_values",
        "max_distinct_values",
        "max_sample_chars",
        "synonyms",
    ),
    "guard": (
        "max_rows",
        "max_subquery_depth",
        "star_row_threshold",
        "allow_star",
        "allowed_tables",
        "allowed_functions",
    ),
    "execute": ("max_ms", "max_rows", "plan_scan_row_threshold"),
    "models": ("sql_model_ref", "judge_model_ref"),
}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError as exc:
        raise ConfigFileError(
            f"Configuration file not found: {path}. The repository root must hold a "
            "sqeual.toml; copy the committed one rather than inventing limits."
        ) from exc
    except OSError as exc:
        raise ConfigFileError(f"Configuration file could not be read: {path}") from exc

    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"Configuration file is not valid TOML: {path} ({exc})") from exc


def _section(document: dict[str, Any], name: str, *, path: Path) -> dict[str, Any]:
    if name not in document:
        raise ConfigFileError(f"{path}: missing required section [{name}]")
    value = document[name]
    if not isinstance(value, dict):
        raise ConfigFileError(f"{path}: [{name}] must be a table, got {type(value).__name__}")
    unknown = sorted(set(value) - set(SECTIONS[name]))
    if unknown:
        raise ConfigFileError(f"{path}: [{name}] has unknown key(s): {', '.join(unknown)}")
    missing = [key for key in SECTIONS[name] if key not in value]
    if missing:
        raise ConfigFileError(f"{path}: [{name}] is missing key(s): {', '.join(missing)}")
    return value


def _positive_int(section: dict[str, Any], key: str, *, path: Path, minimum: int = 1) -> int:
    value = section[key]
    # `True` passes `isinstance(x, int)`. A configuration that says `max_rows =
    # true` is a mistake, not a request for one row.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigFileError(f"{path}: '{key}' must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ConfigFileError(f"{path}: '{key}' must be at least {minimum}, got {value}")
    return value


def _boolean(section: dict[str, Any], key: str, *, path: Path) -> bool:
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigFileError(f"{path}: '{key}' must be true or false, got {type(value).__name__}")
    return value


def _non_empty_str(section: dict[str, Any], key: str, *, path: Path) -> str:
    value = section[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigFileError(f"{path}: '{key}' must be a non-empty string")
    return value.strip()


def _string_list(
    section: dict[str, Any], key: str, *, path: Path, allow_empty: bool
) -> tuple[str, ...]:
    value = section[key]
    if not isinstance(value, list):
        raise ConfigFileError(f"{path}: '{key}' must be a list, got {type(value).__name__}")
    if not value and not allow_empty:
        raise ConfigFileError(f"{path}: '{key}' must name at least one entry")
    entries: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigFileError(
                f"{path}: '{key}'[{index}] must be a non-empty string, got {item!r}"
            )
        entries.append(item.strip())
    return tuple(entries)


def _synonyms(section: dict[str, Any], *, path: Path) -> dict[str, str]:
    value = section["synonyms"]
    if not isinstance(value, dict):
        raise ConfigFileError(
            f"{path}: [schema.synonyms] must be a table, got {type(value).__name__}"
        )
    mapping: dict[str, str] = {}
    for term, table in value.items():
        if not isinstance(table, str) or not table.strip():
            raise ConfigFileError(
                f"{path}: [schema.synonyms] {term!r} must map to a table name, got {table!r}"
            )
        mapping[term.strip().lower()] = table.strip().lower()
    return mapping


def _resolve(base: Path, raw: str) -> Path:
    """Anchor a relative configured path to the config file's own directory.

    A run started from `scripts/` must find the same database as a run started
    from the repository root, and neither may depend on where the shell happens
    to be. An absolute path is returned untouched: somebody who typed one meant
    it.
    """
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else (base / candidate)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> SqeualConfig:
    """Read and validate `sqeual.toml`.

    Raises:
        ConfigFileError: if the file is missing, is not TOML, omits a section or
            key, carries an unknown one, or holds a value outside its range.
    """
    path = Path(path)
    document = _read_toml(path)

    unknown = sorted(set(document) - set(SECTIONS))
    if unknown:
        raise ConfigFileError(f"{path}: unknown section(s): {', '.join(unknown)}")

    base = path.resolve().parent
    db = _section(document, "db", path=path)
    schema = _section(document, "schema", path=path)
    guard = _section(document, "guard", path=path)
    execute = _section(document, "execute", path=path)
    models = _section(document, "models", path=path)

    guard_max_rows = _positive_int(guard, "max_rows", path=path)
    execute_max_rows = _positive_int(execute, "max_rows", path=path)
    if execute_max_rows < guard_max_rows:
        raise ConfigFileError(
            f"{path}: [execute] max_rows ({execute_max_rows}) must be at least [guard] max_rows "
            f"({guard_max_rows}). The fetch cap is a backstop for SQL that was never guarded; a "
            "tighter one would silently truncate a result the guard believed was complete."
        )

    return SqeualConfig(
        db=DatabaseSettings(
            path=_resolve(base, _non_empty_str(db, "path", path=path)),
            schema_sql_path=_resolve(base, _non_empty_str(db, "schema_sql", path=path)),
            seed=_positive_int(db, "seed", path=path),
        ),
        schema=SchemaSettings(
            max_tables=_positive_int(schema, "max_tables", path=path),
            max_sample_values=_positive_int(schema, "max_sample_values", path=path),
            max_distinct_values=_positive_int(schema, "max_distinct_values", path=path),
            max_sample_chars=_positive_int(schema, "max_sample_chars", path=path),
            synonyms=_synonyms(schema, path=path),
        ),
        guard=GuardSettings(
            max_rows=guard_max_rows,
            max_subquery_depth=_positive_int(guard, "max_subquery_depth", path=path, minimum=0),
            star_row_threshold=_positive_int(guard, "star_row_threshold", path=path, minimum=0),
            allow_star=_boolean(guard, "allow_star", path=path),
            allowed_tables=frozenset(
                name.lower()
                for name in _string_list(guard, "allowed_tables", path=path, allow_empty=True)
            ),
            allowed_functions=frozenset(
                name.upper()
                for name in _string_list(guard, "allowed_functions", path=path, allow_empty=False)
            ),
        ),
        execute=ExecuteSettings(
            max_ms=_positive_int(execute, "max_ms", path=path),
            max_rows=execute_max_rows,
            plan_scan_row_threshold=_positive_int(
                execute, "plan_scan_row_threshold", path=path, minimum=0
            ),
        ),
        models=ModelRefSettings(
            sql_model_ref=_non_empty_str(models, "sql_model_ref", path=path),
            judge_model_ref=_non_empty_str(models, "judge_model_ref", path=path),
        ),
        path=path,
    )

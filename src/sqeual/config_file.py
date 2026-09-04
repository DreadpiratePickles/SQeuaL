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

from .config_pipeline import (
    PIPELINE_SECTIONS,
    AnswerSettings,
    ConfidenceSettings,
    CostSettings,
    GenerateSettings,
    TimeSettings,
    VerifySettings,
    load_answer,
    load_confidence,
    load_cost,
    load_generate,
    load_time,
    load_verify,
)
from .config_values import (
    ConfigFileError,
    boolean,
    non_empty_str,
    positive_int,
    section_of,
    string_list,
)

DEFAULT_CONFIG_PATH = Path("sqeual.toml")

__all__ = ["DEFAULT_CONFIG_PATH", "ConfigFileError", "SqeualConfig", "load_config"]


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
    time: TimeSettings
    generate: GenerateSettings
    verify: VerifySettings
    answer: AnswerSettings
    confidence: ConfidenceSettings
    cost: CostSettings
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
    **PIPELINE_SECTIONS,
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
    db = section_of(document, "db", SECTIONS["db"], path=path)
    schema = section_of(document, "schema", SECTIONS["schema"], path=path)
    guard = section_of(document, "guard", SECTIONS["guard"], path=path)
    execute = section_of(document, "execute", SECTIONS["execute"], path=path)
    models = section_of(document, "models", SECTIONS["models"], path=path)

    guard_max_rows = positive_int(guard, "max_rows", path=path)
    execute_max_rows = positive_int(execute, "max_rows", path=path)
    if execute_max_rows < guard_max_rows:
        raise ConfigFileError(
            f"{path}: [execute] max_rows ({execute_max_rows}) must be at least [guard] max_rows "
            f"({guard_max_rows}). The fetch cap is a backstop for SQL that was never guarded; a "
            "tighter one would silently truncate a result the guard believed was complete."
        )

    return SqeualConfig(
        db=DatabaseSettings(
            path=_resolve(base, non_empty_str(db, "path", path=path)),
            schema_sql_path=_resolve(base, non_empty_str(db, "schema_sql", path=path)),
            seed=positive_int(db, "seed", path=path),
        ),
        schema=SchemaSettings(
            max_tables=positive_int(schema, "max_tables", path=path),
            max_sample_values=positive_int(schema, "max_sample_values", path=path),
            max_distinct_values=positive_int(schema, "max_distinct_values", path=path),
            max_sample_chars=positive_int(schema, "max_sample_chars", path=path),
            synonyms=_synonyms(schema, path=path),
        ),
        guard=GuardSettings(
            max_rows=guard_max_rows,
            max_subquery_depth=positive_int(guard, "max_subquery_depth", path=path, minimum=0),
            star_row_threshold=positive_int(guard, "star_row_threshold", path=path, minimum=0),
            allow_star=boolean(guard, "allow_star", path=path),
            allowed_tables=frozenset(
                name.lower()
                for name in string_list(guard, "allowed_tables", path=path, allow_empty=True)
            ),
            allowed_functions=frozenset(
                name.upper()
                for name in string_list(guard, "allowed_functions", path=path, allow_empty=False)
            ),
        ),
        execute=ExecuteSettings(
            max_ms=positive_int(execute, "max_ms", path=path),
            max_rows=execute_max_rows,
            plan_scan_row_threshold=positive_int(
                execute, "plan_scan_row_threshold", path=path, minimum=0
            ),
        ),
        models=ModelRefSettings(
            sql_model_ref=non_empty_str(models, "sql_model_ref", path=path),
            judge_model_ref=non_empty_str(models, "judge_model_ref", path=path),
        ),
        time=load_time(document, path=path),
        generate=load_generate(document, path=path),
        verify=load_verify(document, path=path),
        answer=load_answer(document, path=path),
        confidence=load_confidence(document, path=path),
        cost=load_cost(document, path=path),
        path=path,
    )

"""`sqeual.toml` is a boundary, so it is validated like one.

Every number in that file is a limit on what a generated query may do. A typo
that turns `max_rows` into a string, or a missing `[guard]` section, must stop
the run with a message naming the key — never be read as a default, because the
default a reader would then be running is not the one they reviewed.
"""

import pytest

from conftest import load_test_config, write_config
from sqeual.config_file import ConfigFileError, load_config


def test_committed_config_loads(tmp_path):
    config = load_test_config(tmp_path)
    assert config.guard.max_rows == 200
    assert config.guard.max_subquery_depth == 2
    assert config.guard.star_row_threshold == 100
    assert config.guard.allow_star is False
    assert config.guard.allowed_tables == frozenset()
    assert "COUNT" in config.guard.allowed_functions
    assert config.execute.max_ms == 5000
    assert config.execute.max_rows == 500
    assert config.schema.max_tables == 6
    assert config.db.seed == 20260904


def test_paths_resolve_relative_to_the_config_file_not_the_shell(tmp_path):
    """A relative path in the config means "next to the config"."""
    config = load_test_config(tmp_path)
    assert config.db.path == tmp_path / "data" / "support.db"
    assert config.db.schema_sql_path == tmp_path / "data" / "schema.sql"
    assert config.db.path.is_absolute()


def test_synonyms_are_lowercased_and_singularised(tmp_path):
    config = load_test_config(tmp_path)
    assert config.schema.synonyms["refund"] == "refunds"
    assert config.schema.synonyms["client"] == "customers"
    assert config.schema.synonyms["complaint"] == "tickets"


def test_allowed_functions_are_uppercased_into_a_frozenset(tmp_path):
    config = load_test_config(
        tmp_path, [('    "COUNT", "SUM",', '    "count", "SUM",')]
    )
    assert "COUNT" in config.guard.allowed_functions
    assert isinstance(config.guard.allowed_functions, frozenset)


def test_missing_file_is_named(tmp_path):
    with pytest.raises(ConfigFileError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_unparseable_toml_is_named(tmp_path):
    path = tmp_path / "sqeual.toml"
    path.write_text("[guard\nmax_rows = ", encoding="utf-8")
    with pytest.raises(ConfigFileError, match="not valid TOML"):
        load_config(path)


def test_missing_section_is_named(tmp_path):
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace("[execute]", "[exec_typo]")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigFileError, match=r"unknown section"):
        load_config(path)


def test_unknown_key_in_a_section_is_refused(tmp_path):
    with pytest.raises(ConfigFileError, match="unknown key"):
        load_test_config(tmp_path, [("max_rows = 200", "max_rows = 200\nmax_colums = 3")])


def test_missing_key_in_a_section_is_refused(tmp_path):
    with pytest.raises(ConfigFileError, match="missing key"):
        load_test_config(tmp_path, [("max_ms = 5000", "")])


def test_non_integer_limit_is_refused(tmp_path):
    with pytest.raises(ConfigFileError, match="max_rows"):
        load_test_config(tmp_path, [("max_rows = 200", 'max_rows = "two hundred"')])


def test_boolean_is_not_an_integer(tmp_path):
    """`True` is an `int` in Python. A config that says so is still wrong."""
    with pytest.raises(ConfigFileError, match="max_rows"):
        load_test_config(tmp_path, [("max_rows = 200", "max_rows = true")])


def test_zero_max_rows_is_refused(tmp_path):
    with pytest.raises(ConfigFileError, match="at least 1"):
        load_test_config(tmp_path, [("max_rows = 200", "max_rows = 0")])


def test_execute_max_rows_below_guard_max_rows_is_refused(tmp_path):
    """The fetch cap is a backstop, so it may never be tighter than the LIMIT
    the guard writes: the guard would ask for 200 rows and the executor would
    silently truncate to 50 and call the result complete."""
    with pytest.raises(ConfigFileError, match="at least"):
        load_test_config(tmp_path, [("max_rows = 500", "max_rows = 50")])


def test_allow_star_must_be_a_boolean(tmp_path):
    with pytest.raises(ConfigFileError, match="allow_star"):
        load_test_config(tmp_path, [("allow_star = false", 'allow_star = "no"')])


def test_allowed_tables_must_be_a_list_of_strings(tmp_path):
    with pytest.raises(ConfigFileError, match="allowed_tables"):
        load_test_config(tmp_path, [("allowed_tables = []", "allowed_tables = [1, 2]")])


def test_allowed_functions_may_not_be_empty(tmp_path):
    """An empty allowlist would refuse COUNT(*), which is most of the point."""
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    start = text.index("allowed_functions = [")
    end = text.index("]", start) + 1
    path.write_text(text[:start] + "allowed_functions = []" + text[end:], encoding="utf-8")
    with pytest.raises(ConfigFileError, match="allowed_functions"):
        load_config(path)


def test_synonym_values_must_be_strings(tmp_path):
    with pytest.raises(ConfigFileError, match="synonyms"):
        load_test_config(tmp_path, [('refund = "refunds"', "refund = 3")])


def test_model_refs_must_be_non_empty(tmp_path):
    with pytest.raises(ConfigFileError, match="sql_model_ref"):
        load_test_config(
            tmp_path, [('sql_model_ref = "SQEUAL_SQL_MODEL_ID"', 'sql_model_ref = "  "')]
        )


def test_absolute_db_path_is_kept_as_given(tmp_path):
    """An operator who writes an absolute path meant it. Only *relative* paths
    are anchored to the config file."""
    config = load_test_config(
        tmp_path, [('path = "data/support.db"', 'path = "/var/data/support.db"')]
    )
    assert str(config.db.path) == "/var/data/support.db"

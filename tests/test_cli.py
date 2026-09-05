"""The command line: four commands and four exit codes.

The exit codes are the part worth testing hardest, because they are the only
part a script can read. The vocabulary is fixed and documented:

    0  the run completed and found nothing wrong
    1  the run completed and produced a finding — the guard refused the SQL
    2  the run never started — bad configuration, missing database, bad usage
    3  the run started and the execution failed — a timeout or a SQLite error

The distinction between 1 and 3 is the one that matters in a pipeline: a 1
means the model wrote a bad query and Phase B should try again with the
finding; a 3 means the query was fine and the database could not answer it, and
trying again with the same question would produce the same failure.

`main()` returns the code rather than calling `exit`, so every test here runs
in-process with no subprocess and no shell.
"""

import pytest

from conftest import COMMITTED_SCHEMA_SQL, cli_config, write_config
from sqeual.cli import EXIT_BAD_CONFIG, EXIT_EXECUTION_FAILED, EXIT_FINDING, EXIT_OK, main


class Recorder:
    """Collects the lines a command printed."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, *parts) -> None:
        self.lines.append(" ".join(str(part) for part in parts))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def echo():
    return Recorder()


@pytest.fixture
def config(tmp_path, session_db):
    return cli_config(tmp_path, session_db)


# --- db build ---------------------------------------------------------------


def test_db_build_creates_a_database(tmp_path, echo):
    out = tmp_path / "built.db"
    path = write_config(
        tmp_path,
        [
            ('path = "data/support.db"', f'path = "{out}"'),
            ('schema_sql = "data/schema.sql"', f'schema_sql = "{COMMITTED_SCHEMA_SQL}"'),
        ],
    )
    assert main(["db", "build", "--config", str(path)], echo=echo) == EXIT_OK
    assert out.exists()
    assert "orders" in echo.text


def test_db_build_reports_the_row_digest(tmp_path, echo):
    out = tmp_path / "built.db"
    path = write_config(
        tmp_path,
        [
            ('path = "data/support.db"', f'path = "{out}"'),
            ('schema_sql = "data/schema.sql"', f'schema_sql = "{COMMITTED_SCHEMA_SQL}"'),
        ],
    )
    main(["db", "build", "--config", str(path)], echo=echo)
    assert "digest" in echo.text.lower()


def test_db_build_refuses_to_overwrite_without_force(tmp_path, echo, session_db):
    path = cli_config(tmp_path, session_db)
    assert main(["db", "build", "--config", str(path)], echo=echo) == EXIT_BAD_CONFIG
    assert "already exists" in echo.text


def test_db_build_overwrites_with_force(tmp_path, echo):
    out = tmp_path / "built.db"
    path = write_config(
        tmp_path,
        [
            ('path = "data/support.db"', f'path = "{out}"'),
            ('schema_sql = "data/schema.sql"', f'schema_sql = "{COMMITTED_SCHEMA_SQL}"'),
        ],
    )
    main(["db", "build", "--config", str(path)], echo=echo)
    assert main(["db", "build", "--config", str(path), "--force"], echo=echo) == EXIT_OK


def test_db_build_honours_an_explicit_out_path(tmp_path, echo, session_db):
    path = cli_config(tmp_path, session_db)
    other = tmp_path / "elsewhere.db"
    assert main(
        ["db", "build", "--config", str(path), "--out", str(other)], echo=echo
    ) == EXIT_OK
    assert other.exists()


# --- schema -----------------------------------------------------------------


def test_schema_show_prints_the_card(config, echo):
    assert main(["schema", "show", "--config", str(config)], echo=echo) == EXIT_OK
    assert "## orders" in echo.text
    assert "refunds.order_id -> orders.id" in echo.text


def test_schema_show_can_be_narrowed_to_named_tables(config, echo):
    assert main(
        ["schema", "show", "--config", str(config), "--tables", "orders,customers"], echo=echo
    ) == EXIT_OK
    assert "## orders" in echo.text
    assert "## tickets" not in echo.text


def test_schema_slice_prints_the_tables_and_the_reasons(config, echo):
    code = main(
        [
            "schema",
            "slice",
            "--config",
            str(config),
            "--question",
            "how much did we refund to customers in Berlin last month",
        ],
        echo=echo,
    )
    assert code == EXIT_OK
    assert "refunds" in echo.text
    assert "orders" in echo.text
    assert "customers" in echo.text
    assert "joins customers to refunds" in echo.text


def test_schema_slice_can_print_the_card_it_would_send(config, echo):
    main(
        [
            "schema",
            "slice",
            "--config",
            str(config),
            "--question",
            "how many tickets are open",
            "--card",
        ],
        echo=echo,
    )
    assert "## tickets" in echo.text


def test_schema_slice_says_so_when_nothing_matched(config, echo):
    code = main(
        ["schema", "slice", "--config", str(config), "--question", "what is the weather"],
        echo=echo,
    )
    assert code == EXIT_FINDING
    assert "no table" in echo.text.lower()


def test_schema_show_on_a_missing_database_never_started(tmp_path, echo):
    path = cli_config(tmp_path, tmp_path / "absent.db")
    assert main(["schema", "show", "--config", str(path)], echo=echo) == EXIT_BAD_CONFIG


# --- guard ------------------------------------------------------------------


def test_guard_passes_a_good_query(config, echo):
    code = main(
        ["guard", "--config", str(config), "--sql", "SELECT COUNT(*) FROM orders"], echo=echo
    )
    assert code == EXIT_OK
    assert "PASS" in echo.text


def test_guard_prints_the_whole_rule_table(config, echo):
    main(["guard", "--config", str(config), "--sql", "SELECT COUNT(*) FROM orders"], echo=echo)
    for rule in ("parses", "known_tables", "known_columns", "row_limit"):
        assert rule in echo.text


def test_guard_prints_the_statement_it_would_run(config, echo):
    main(["guard", "--config", str(config), "--sql", "SELECT id FROM orders"], echo=echo)
    assert "LIMIT 200" in echo.text


def test_guard_fails_a_hallucinated_column_with_exit_one(config, echo):
    code = main(
        ["guard", "--config", str(config), "--sql", "SELECT revenue FROM orders"], echo=echo
    )
    assert code == EXIT_FINDING
    assert "FAIL" in echo.text
    assert "unknown_column" in echo.text


def test_guard_fails_a_stacked_statement_with_exit_one(config, echo):
    code = main(
        ["guard", "--config", str(config), "--sql", "SELECT 1; DROP TABLE tickets"], echo=echo
    )
    assert code == EXIT_FINDING
    assert "multiple_statements" in echo.text


def test_guard_never_prints_a_statement_it_refused(config, echo):
    main(["guard", "--config", str(config), "--sql", "SELECT revenue FROM orders"], echo=echo)
    assert "sql to run" not in echo.text


# --- run --------------------------------------------------------------------


def test_run_prints_a_table_of_results(config, echo):
    code = main(
        [
            "run",
            "--config",
            str(config),
            "--sql",
            "SELECT status, COUNT(*) FROM orders GROUP BY status",
        ],
        echo=echo,
    )
    assert code == EXIT_OK
    assert "status" in echo.text
    assert "refunded" in echo.text


def test_run_reports_the_row_count_and_the_time(config, echo):
    main(["run", "--config", str(config), "--sql", "SELECT COUNT(*) FROM orders"], echo=echo)
    assert "1 row" in echo.text
    assert "ms" in echo.text


def test_run_refuses_a_guard_failure_before_touching_the_database(config, echo):
    code = main(["run", "--config", str(config), "--sql", "SELECT revenue FROM orders"], echo=echo)
    assert code == EXIT_FINDING
    assert "unknown_column" in echo.text


def test_run_executes_the_normalised_statement_not_the_input(config, echo):
    """The LIMIT the guard injected has to be the one that ran, or the whole
    chain is theatre.

    Grouped rather than a bare projection: 250 customers have orders, so the
    injected `LIMIT 200` is visible in the row count. A bare `SELECT id FROM
    orders` would now be refused by `bulk_export` before it could demonstrate
    anything about the rewrite."""
    main(
        [
            "run", "--config", str(config), "--sql",
            "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id",
        ],
        echo=echo,
    )
    assert "200 rows" in echo.text


def test_run_says_when_the_result_was_truncated(config, echo):
    main(
        [
            "run", "--config", str(config), "--sql",
            "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id LIMIT 200",
        ],
        echo=echo,
    )
    assert "200 rows" in echo.text


def test_run_shows_the_plan_when_asked(config, echo):
    main(
        ["run", "--config", str(config), "--sql", "SELECT COUNT(*) FROM orders", "--plan"],
        echo=echo,
    )
    assert "SCAN orders" in echo.text


def test_run_surfaces_a_plan_warning(config, echo):
    main(
        ["run", "--config", str(config), "--sql", "SELECT COUNT(*) FROM orders", "--plan"],
        echo=echo,
    )
    assert "full scan" in echo.text


def test_run_reports_an_execution_failure_as_exit_three(tmp_path, session_db, echo):
    """Exit 3, not 1: the guard passed this query, so nothing about rewriting
    it would help. That is the whole reason 1 and 3 are different codes.

    The query is a three-way cross join — 2,000 x 5,013 x 600 rows — rather
    than a normal query with an absurdly small budget. A test that asserts "this
    join takes longer than 1 ms" is asserting something about the machine it
    runs on, and it passes on a loaded laptop and fails on a fast one. Six
    billion rows do not finish in 200 ms anywhere.
    """
    path = cli_config(tmp_path, session_db, [("max_ms = 5000", "max_ms = 200")])
    code = main(
        [
            "run",
            "--config",
            str(path),
            "--sql",
            "SELECT COUNT(*) FROM orders o "
            "CROSS JOIN order_items i CROSS JOIN tickets t",
        ],
        echo=echo,
    )
    assert code == EXIT_EXECUTION_FAILED
    assert "budget" in echo.text


def test_run_on_a_missing_database_never_started(tmp_path, echo):
    path = cli_config(tmp_path, tmp_path / "absent.db")
    code = main(["run", "--config", str(path), "--sql", "SELECT 1"], echo=echo)
    assert code == EXIT_BAD_CONFIG


# --- usage ------------------------------------------------------------------


def test_no_command_prints_usage_and_never_started(echo):
    assert main([], echo=echo) == EXIT_BAD_CONFIG
    assert "usage" in echo.text.lower()


def test_a_broken_config_never_started(tmp_path, echo):
    path = tmp_path / "sqeual.toml"
    path.write_text("[guard\n", encoding="utf-8")
    assert main(["schema", "show", "--config", str(path)], echo=echo) == EXIT_BAD_CONFIG
    assert "TOML" in echo.text


def test_a_missing_config_never_started(tmp_path, echo):
    argv = ["guard", "--config", str(tmp_path / "nope.toml"), "--sql", "SELECT 1"]
    assert main(argv, echo=echo) == EXIT_BAD_CONFIG


def test_the_config_defaults_to_the_repository_file(echo, monkeypatch, tmp_path, session_db):
    """`--config` is optional. Omitting it must find `./sqeual.toml`, not a
    path relative to wherever the package happens to be installed."""
    cli_config(tmp_path, session_db)
    monkeypatch.chdir(tmp_path)
    assert main(["guard", "--sql", "SELECT COUNT(*) FROM orders"], echo=echo) == EXIT_OK

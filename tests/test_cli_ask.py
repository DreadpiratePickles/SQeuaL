"""`sqeual ask` at the command line: four exit codes, each tested for.

The codes are the interface. A pipeline reads them and nothing else, so each one
is pinned to the fact it reports — and the fact that `ask` renumbers 2 and 3
relative to the Phase A commands is pinned too, because a caller that assumed one
vocabulary held everywhere would retry the wrong thing.
"""

import json

import pytest

from conftest import cli_config
from sqeual.cli import main


@pytest.fixture
def config_path(tmp_path, session_db):
    return cli_config(tmp_path, session_db)


def run(config_path, tmp_path, *args):
    lines: list[str] = []
    code = main(
        ["ask", *args, "--config", str(config_path), "--runs", str(tmp_path / "runs")],
        echo=lines.append,
    )
    return code, "\n".join(lines)


class TestExitCodes:
    def test_an_answered_question_exits_zero(self, config_path, tmp_path):
        code, output = run(
            config_path,
            tmp_path,
            "How much did we refund to customers in Berlin last month?",
            "--dry-run",
        )
        assert code == 0
        assert "€" in output

    def test_a_question_matching_no_table_exits_one(self, config_path, tmp_path):
        code, output = run(config_path, tmp_path, "what is the weather like", "--dry-run")
        assert code == 1
        assert "I need one more thing" in output

    def test_a_missing_database_exits_three_not_two(self, tmp_path, session_db):
        """`ask` renumbers: 2 means the guard refused, so a broken deployment is 3."""
        path = cli_config(tmp_path, tmp_path / "absent.db")
        code, output = run(path, tmp_path, "how many refunds", "--dry-run")
        assert code == 3
        assert "cannot start" in output

    def test_a_database_that_vanishes_mid_run_also_exits_three(self, tmp_path, session_db):
        """`DatabaseUnavailableError` is a *sibling* of `ExecutionError`, not a subclass.

        It is raised fresh by every `execute_sql`, and stage 05 deliberately does
        not catch it, so it can arrive after `load_card` already succeeded — and
        it must not be reported as "the guard refused the model's statement",
        which would send a caller off to rewrite a query that was fine.
        """
        import shutil

        from sqeual.execute import DatabaseUnavailableError

        copy = tmp_path / "copy.db"
        shutil.copy(session_db, copy)
        path = cli_config(tmp_path, copy)

        import sqeual.generate.sample as sample

        original = sample.execute_sql

        def vanishing(*args, **kwargs):
            raise DatabaseUnavailableError("database not found: copy.db")

        sample.execute_sql = vanishing
        try:
            code, output = run(path, tmp_path, "how many refunds", "--dry-run")
        finally:
            sample.execute_sql = original
        assert code == 3
        assert "cannot start" in output

    def test_a_bad_configuration_exits_three(self, tmp_path):
        lines: list[str] = []
        code = main(["ask", "x", "--config", str(tmp_path / "nope.toml")], echo=lines.append)
        assert code == 3
        assert "configuration error" in "\n".join(lines)

    def test_the_phase_a_commands_keep_their_own_vocabulary(self, tmp_path):
        """A missing config is still 2 for `guard`. Only `ask` renumbers."""
        lines: list[str] = []
        code = main(
            ["guard", "--sql", "SELECT 1", "--config", str(tmp_path / "nope.toml")],
            echo=lines.append,
        )
        assert code == 2


class TestFlags:
    def test_json_prints_the_trace_and_not_the_answer(self, config_path, tmp_path):
        code, output = run(
            config_path, tmp_path, "how many refunds last month", "--dry-run", "--json"
        )
        assert code == 0
        trace = json.loads(output)
        assert trace["provenance"]["dry_run"] is True
        assert "# how many refunds" not in output

    def test_k_reduces_the_number_of_generation_calls(self, config_path, tmp_path):
        code, output = run(
            config_path, tmp_path, "how many refunds last month", "--dry-run", "--json",
            "--k", "1",
        )
        assert code == 0
        trace = json.loads(output)
        assert trace["generation"]["k"] == 1
        assert len(trace["generation"]["attempts"]) == 1

    def test_a_single_sample_drops_the_agreement_factor_rather_than_scoring_it(
        self, config_path, tmp_path
    ):
        """One sample agreeing with itself is a tautology, not evidence."""
        _code, output = run(
            config_path, tmp_path, "how many refunds last month", "--dry-run", "--json",
            "--k", "1",
        )
        trace = json.loads(output)
        agreement = next(
            factor for factor in trace["confidence"]["factors"] if factor["name"] == "agreement"
        )
        assert agreement["applicable"] is False

    def test_a_k_below_one_is_refused_at_the_boundary(self, config_path, tmp_path):
        """`--k` overrides a validated limit, so it is validated the same way.

        Unvalidated, `--k 0` does not crash — the primary runs outside the
        sampling loop — it writes a trace saying `k: 0` beside one attempt and a
        real agreement fraction, which is a record that contradicts itself.
        """
        code, output = run(
            config_path, tmp_path, "how many refunds", "--dry-run", "--k", "0"
        )
        assert code == 3
        assert "--k must be at least 1" in output

    def test_no_phrasing_forces_the_switch_off(self, tmp_path, session_db):
        path = cli_config(
            tmp_path, session_db, [("llm_phrasing = false", "llm_phrasing = true")]
        )
        _code, output = run(
            path, tmp_path, "how many refunds last month", "--dry-run", "--json",
            "--no-phrasing",
        )
        assert json.loads(output)["answer"]["phrasing"] is None

    def test_without_no_phrasing_the_switch_is_honoured(self, tmp_path, session_db):
        path = cli_config(
            tmp_path, session_db, [("llm_phrasing = false", "llm_phrasing = true")]
        )
        _code, output = run(path, tmp_path, "how many refunds last month", "--dry-run", "--json")
        phrasing = json.loads(output)["answer"]["phrasing"]
        assert phrasing is not None
        assert phrasing["accepted"] is True

    def test_min_interval_is_recorded_even_when_it_never_had_to_wait(
        self, config_path, tmp_path
    ):
        _code, output = run(
            config_path, tmp_path, "how many refunds last month", "--dry-run", "--json",
            "--min-interval-ms", "0",
        )
        assert json.loads(output)["pacing"] == {"min_interval_ms": 0, "waits": 0}

    def test_the_run_directory_is_named_in_the_human_output(self, config_path, tmp_path):
        _code, output = run(config_path, tmp_path, "how many refunds last month", "--dry-run")
        assert f"run: {tmp_path / 'runs'}" in output


class TestNoKeyIsNeededOffline:
    def test_the_dry_run_never_imports_the_vendor_sdk_path(
        self, config_path, tmp_path, monkeypatch
    ):
        """A dry run must work with no key set, not merely with one unused."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        code, _output = run(config_path, tmp_path, "how many refunds last month", "--dry-run")
        assert code == 0


class TestHelp:
    def test_ask_is_listed_when_no_command_is_given(self, tmp_path, session_db):
        lines: list[str] = []
        code = main(["--config", str(cli_config(tmp_path, session_db))], echo=lines.append)
        assert code == 2
        assert "ask" in "\n".join(lines)

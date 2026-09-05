"""`sqeual eval` and `sqeual ask-target` through `main()`, including the exit codes.

The exit codes are the contract CI depends on, and the two that matter are the
ones that are *not* an accuracy drop: a false answer and an unrefused unsafe
instruction. Accuracy moves when a model moves, and a gate that goes red on that
is a gate somebody disables.
"""

import io
import json

import pytest

from conftest import COMMITTED_GOLDENS, cli_config
from sqeual.cli import main
from sqeual.eval.goldens import Expectation, GoldenQuestion
from sqeual.eval.metrics import compute_metrics
from sqeual.eval.run import EvalRun
from sqeual.eval.score import QuestionResult, Verdict


def run(argv, capture=None):
    lines: list[str] = [] if capture is None else capture
    code = main(argv, echo=lambda *parts: lines.append(" ".join(str(part) for part in parts)))
    return code, "\n".join(lines)


def test_a_limited_dry_run_is_clean_and_writes_four_files(tmp_path, session_db):
    config = cli_config(tmp_path, session_db)
    out = tmp_path / "eval-out"
    code, output = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--dry-run",
            "--limit", "10",
            "--out", str(out),
        ]
    )
    assert code == 0, output
    assert "SYNTHETIC —" in output
    for name in ("results.jsonl", "eval.json", "eval.md", "calibration.md"):
        assert (out / name).exists(), name
    assert (out / "eval.md").read_text(encoding="utf-8").startswith("SYNTHETIC —")
    lines = (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 11


def test_the_limited_run_scores_exactly_what_it_should(tmp_path, session_db):
    """The ten-question prefix CI runs. Its numbers are pinned here."""
    config = cli_config(tmp_path, session_db)
    out = tmp_path / "eval-out"
    code, _ = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--dry-run",
            "--limit", "10",
            "--out", str(out),
        ]
    )
    assert code == 0
    payload = json.loads((out / "eval.json").read_text(encoding="utf-8"))
    metrics = payload["metrics"]
    assert metrics["counts"] == {
        "total": 10,
        "scored": 10,
        "broken_reference": 0,
        "errored": 0,
    }
    assert metrics["false_answers"]["count"] == 0
    # 3 of 4, not 3 of 5: since §54 one of the five answerable questions in this
    # prefix is withheld by a gate rather than answered wrongly, and a withheld
    # answer is in no accuracy denominator. The answer rate below is where that
    # shows up as a cost.
    assert metrics["execution_accuracy"]["passes"] == 3
    assert metrics["execution_accuracy"]["n"] == 4
    assert metrics["hallucination_catches"]["n"] == 2
    assert metrics["refusals_correct"] == {
        **metrics["refusals_correct"],
        "passes": 1,
        "n": 1,
    }


def test_the_summary_prints_the_dangerous_direction_first(tmp_path, session_db):
    config = cli_config(tmp_path, session_db)
    code, output = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--dry-run",
            "--limit", "6",
            "--out", str(tmp_path / "out"),
        ]
    )
    assert code == 0
    assert output.index("false answers") < output.index("execution accuracy")


def test_the_default_output_directory_is_under_runs_eval(tmp_path, session_db, monkeypatch):
    config = cli_config(tmp_path, session_db)
    monkeypatch.chdir(tmp_path)
    code, output = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--dry-run",
            "--limit", "2",
        ]
    )
    assert code == 0
    (created,) = list((tmp_path / "runs" / "eval").iterdir())
    assert created.name.endswith("Z")
    assert f"runs/eval/{created.name}" in output


@pytest.mark.parametrize("bad", (["--k", "0"], ["--limit", "0"]))
def test_a_nonsense_flag_cannot_run(tmp_path, session_db, bad):
    config = cli_config(tmp_path, session_db)
    code, output = run(
        ["eval", "--config", str(config), "--goldens", str(COMMITTED_GOLDENS), *bad]
    )
    assert code == 2
    assert "cannot run" in output


def test_an_unparseable_golden_file_stops_before_anything_runs(tmp_path, session_db):
    config = cli_config(tmp_path, session_db)
    broken = tmp_path / "questions.yaml"
    broken.write_text("- id: Bad\n", encoding="utf-8")
    code, output = run(
        ["eval", "--config", str(config), "--goldens", str(broken), "--dry-run"]
    )
    assert code == 2
    assert "snake_case" in output
    assert not (tmp_path / "runs").exists()


def test_a_missing_golden_file_stops_before_anything_runs(tmp_path, session_db):
    config = cli_config(tmp_path, session_db)
    code, output = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(tmp_path / "nope.yaml"),
            "--dry-run",
        ]
    )
    assert code == 2
    assert "not found" in output


# --- the exit codes, driven directly -----------------------------------------
#
# The offline fake can never produce a false answer — it is scripted to decline
# every trap — which is a good property of the fake and a problem for testing the
# gate. So the two failing verdicts are exercised by handing `command_eval` a
# finished run, which is the decision under test and nothing else.


def _case(case_id, *, expected="answer", trap=None):
    tags = ["kind:scalar", "difficulty:easy"]
    if trap:
        tags.append(f"trap:{trap}")
    return GoldenQuestion(
        id=case_id,
        question=f"question {case_id}",
        tags=tuple(tags),
        expected=Expectation(expected),
        reference_sql="SELECT 1 AS n" if expected == "answer" else None,
        ordered=False,
        notes="fixture",
    )


def _result(question, verdict):
    return QuestionResult(
        question=question,
        verdict=verdict,
        reference=None,
        answer_status="answered",
        shows_figures=verdict in (Verdict.MATCH, Verdict.MISS, Verdict.FALSE_ANSWER),
        confidence_level="HIGH" if verdict is not Verdict.ERRORED else None,
        confidence_value=0.9 if verdict is not Verdict.ERRORED else None,
        agreement=1.0,
        k=3,
        attempts=1,
        repairs_used=0,
        guard_codes=(),
        candidate_sql="SELECT 1 AS n",
        candidate_digest="abc",
        candidate_row_count=1,
        judge_statuses=("pass", "pass"),
        checks=(),
        clarification=None,
        calls=6,
        input_tokens=1,
        output_tokens=1,
        micro_usd=0,
        latency_ms=1,
    )


PROVENANCE = {
    "dry_run": False,
    "started_utc": "2026-09-04T12:00:00+00:00",
    "questions": 2,
    "goldens": "goldens/questions.yaml",
    "goldens_sha256": "a" * 64,
    "schema_sha256": "b" * 64,
    "config": "sqeual.toml",
    "model_id": "gemini-3.5-flash-lite",
    "judge_model_id": "gemini-3.5-flash-lite",
    "same_family": True,
    "k": 3,
    "min_interval_ms": 0,
    "as_of": "2026-08-31",
}


def _fixed_run(results, out_dir):
    return EvalRun(
        results=tuple(results),
        metrics=compute_metrics(results, priced=False),
        provenance=PROVENANCE,
        out_dir=out_dir,
    )


def _drive(monkeypatch, results, tmp_path, session_db):
    from sqeual import cli_eval

    out = tmp_path / "driven"
    out.mkdir()
    monkeypatch.setattr(
        cli_eval, "run_eval", lambda **kwargs: _fixed_run(results, out)
    )
    config = cli_config(tmp_path, session_db)
    return run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--dry-run",
            "--limit", "2",
            "--out", str(out),
        ]
    )


def test_a_false_answer_is_a_finding(monkeypatch, tmp_path, session_db):
    results = [
        _result(_case("a"), Verdict.MATCH),
        _result(
            _case("bait", expected="abstain", trap="hallucination_bait"),
            Verdict.FALSE_ANSWER,
        ),
    ]
    code, output = _drive(monkeypatch, results, tmp_path, session_db)
    assert code == 1
    assert "FINDING" in output
    assert "1 false answer(s)" in output


def test_an_unrefused_unsafe_instruction_is_a_finding(monkeypatch, tmp_path, session_db):
    results = [
        _result(_case("a"), Verdict.MATCH),
        _result(_case("u", expected="refuse", trap="unsafe"), Verdict.FALSE_ANSWER),
    ]
    code, output = _drive(monkeypatch, results, tmp_path, session_db)
    assert code == 1
    assert "unsafe instruction(s) not refused" in output


def test_a_miss_is_not_a_finding(monkeypatch, tmp_path, session_db):
    """An accuracy drop does not fail this command. Models move; gates should not."""
    results = [_result(_case("a"), Verdict.MISS), _result(_case("b"), Verdict.MISS)]
    code, output = _drive(monkeypatch, results, tmp_path, session_db)
    assert code == 0
    assert "FINDING" not in output


def test_a_run_that_scored_nothing_is_inconclusive(monkeypatch, tmp_path, session_db):
    results = [_result(_case("a"), Verdict.ERRORED), _result(_case("b"), Verdict.ERRORED)]
    code, output = _drive(monkeypatch, results, tmp_path, session_db)
    assert code == 3
    assert "INCONCLUSIVE" in output


# --- ask-target ---------------------------------------------------------------


def test_ask_target_prints_only_the_answer(tmp_path, session_db, monkeypatch):
    config = cli_config(tmp_path, session_db)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("How much did we refund to customers in Berlin last month?")
    )
    code, output = run(["ask-target", "--config", str(config), "--dry-run"])
    assert code == 0
    assert output.startswith("# How much did we refund")
    assert "run: " not in output


def test_ask_target_writes_no_run_directory(tmp_path, session_db, monkeypatch):
    config = cli_config(tmp_path, session_db)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("how many tickets are open"))
    code, _ = run(["ask-target", "--config", str(config), "--dry-run"])
    assert code == 0
    assert not (tmp_path / "runs").exists()


def test_ask_target_refuses_an_empty_question(tmp_path, session_db, monkeypatch):
    config = cli_config(tmp_path, session_db)
    monkeypatch.setattr("sys.stdin", io.StringIO("  \n "))
    code, output = run(["ask-target", "--config", str(config), "--dry-run"])
    assert code == 2
    assert output == ""


def test_ask_target_cannot_run_without_a_key(tmp_path, session_db, monkeypatch):
    config = cli_config(tmp_path, session_db)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr("sys.stdin", io.StringIO("how many orders are there"))
    code, _ = run(["ask-target", "--config", str(config)])
    assert code == 2


def test_the_usage_line_lists_the_new_commands():
    code, output = run([])
    assert code == 2
    assert "eval" in output
    assert "ask-target" in output


def test_a_missing_database_cannot_run(tmp_path):
    from conftest import COMMITTED_SCHEMA_SQL, write_config

    config = write_config(
        tmp_path,
        [
            ('path = "data/support.db"', f'path = "{tmp_path / "absent.db"}"'),
            ('schema_sql = "data/schema.sql"', f'schema_sql = "{COMMITTED_SCHEMA_SQL}"'),
        ],
    )
    code, output = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--dry-run",
            "--limit", "1",
        ]
    )
    assert code == 2
    assert "cannot start" in output


def test_a_provider_that_cannot_be_built_cannot_run(monkeypatch, tmp_path, session_db):
    """No key, a rejected key, or an unreachable provider. Nothing about the
    golden set would change any of them, so it is exit 2 and not a finding."""
    from sqeual import cli_eval
    from sqeual.providers import ProviderConfigError

    def refuse(*_args, **_kwargs):
        raise ProviderConfigError("GEMINI_API_KEY is not set")

    monkeypatch.setattr(cli_eval, "build_provider", refuse)
    config = cli_config(tmp_path, session_db)
    code, output = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--limit", "1",
            "--out", str(tmp_path / "out"),
        ]
    )
    assert code == 2
    assert "cannot run: GEMINI_API_KEY is not set" in output


def test_an_output_directory_that_already_holds_a_run_is_refused(tmp_path, session_db):
    """`results.jsonl` is appended to as questions finish and the summary is
    written whole at the end, so re-using a directory would leave two runs in one
    results file beside a summary describing one of them."""
    config = cli_config(tmp_path, session_db)
    out = tmp_path / "eval-out"
    argv = [
        "eval",
        "--config", str(config),
        "--goldens", str(COMMITTED_GOLDENS),
        "--dry-run",
        "--limit", "3",
        "--out", str(out),
    ]
    first, _ = run(argv)
    assert first == 0
    before = (out / "results.jsonl").read_text(encoding="utf-8")

    second, output = run(argv)
    assert second == 2
    assert "already holds a results.jsonl" in output
    assert (out / "results.jsonl").read_text(encoding="utf-8") == before


def test_an_output_path_that_is_a_file_is_refused(tmp_path, session_db):
    """An evaluation writes four files and needs somewhere to put them."""
    config = cli_config(tmp_path, session_db)
    not_a_directory = tmp_path / "somefile"
    not_a_directory.write_text("", encoding="utf-8")
    code, output = run(
        [
            "eval",
            "--config", str(config),
            "--goldens", str(COMMITTED_GOLDENS),
            "--dry-run",
            "--limit", "2",
            "--out", str(not_a_directory),
        ]
    )
    assert code == 2
    assert "is not a directory" in output

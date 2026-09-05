"""The `regress` seam: project 1 drives SQeuaL as a `[target] kind = "command"`.

Everything here is checked by calling **project 1's own code** on this
repository's files rather than by restating what project 1 expects. A restated
schema is a schema that drifts, and the whole claim of this integration is that
neither repository had to learn the other's vocabulary.
"""

import subprocess
import sys
import tomllib

import pytest
from regression_detect.goldens import GoldenDatasetError, load_goldens
from regression_detect.target.adapters.base import Target
from regression_detect.target.adapters.factory import load_target

from conftest import COMMITTED_SCHEMA_SQL, cli_config
from conftest import REPO_ROOT as ROOT

REGRESS_GOLDENS = ROOT / "regress" / "goldens.yaml"
REGRESS_CONFIG = ROOT / "regress" / "regression.toml"
SCRIPT = ROOT / "scripts" / "sqeual.py"

# The number of cases in `regress/goldens.yaml`, in the one place anything
# that needs it reads it from. The CI job imports this constant rather than
# restating the number, because restating it is how the workflow came to
# assert eight cases against a file that had nine.
EXPECTED_CASES = 9


def test_project_ones_loader_accepts_our_golden_file():
    """The claim, checked by the only thing that can check it."""
    cases = load_goldens(REGRESS_GOLDENS)
    assert len(cases) == EXPECTED_CASES
    assert all(case.criteria for case in cases)
    assert all(case.notes for case in cases)


def test_every_case_has_at_least_one_negative_criterion():
    """A negative criterion is what catches a hallucination coming back."""
    negatives = [
        case for case in load_goldens(REGRESS_GOLDENS)
        if any(criterion.lower().startswith("does not") for criterion in case.criteria)
    ]
    assert len(negatives) >= 6


def test_the_dangerous_cases_are_present_and_tagged():
    tags = {case.id: set(case.tags) for case in load_goldens(REGRESS_GOLDENS)}
    assert "abstain" in tags["loyalty_tier_bait"]
    assert "refuse" in tags["unsafe_delete_refunds"]
    assert "abstain" in tags["ambiguous_totals"]
    assert "refuse" in tags["export_customer_emails_refused"]


def test_a_malformed_case_would_be_caught(tmp_path):
    """`load_goldens` is a real validator, not a YAML read."""
    path = tmp_path / "goldens.yaml"
    path.write_text("- id: Bad Id\n  tags: []\n  input: x\n  criteria: [y]\n", encoding="utf-8")
    with pytest.raises(GoldenDatasetError):
        load_goldens(path)


def test_the_target_section_builds_project_ones_command_adapter(tmp_path):
    document = tomllib.loads(REGRESS_CONFIG.read_text(encoding="utf-8"))
    section = dict(document["target"])
    assert section["kind"] == "command"
    assert isinstance(section["argv"], list)
    # The committed paths are placeholders a reader replaces. `cwd` must exist
    # for the adapter to accept it, so this points it at the real checkout.
    section["cwd"] = str(ROOT)
    target = load_target(
        section, provider_factory=lambda: pytest.fail("a command target needs no provider")
    )
    assert isinstance(target, Target)
    assert target.target_id.startswith("command:")
    provenance = target.provenance()
    assert provenance["kind"] == "command"
    assert "GEMINI_API_KEY" in provenance["env_allowlist"]


def test_the_committed_argv_is_a_list_and_never_a_shell_string():
    document = tomllib.loads(REGRESS_CONFIG.read_text(encoding="utf-8"))
    argv = document["target"]["argv"]
    assert isinstance(argv, list)
    assert all(isinstance(item, str) for item in argv)
    assert "ask-target" in argv


def test_the_thresholds_are_project_ones_committed_defaults():
    """Copied, not tuned: what counts as a regression must not follow the target."""
    from regression_detect.compare import DEFAULT_ALPHA, DEFAULT_MIN_EFFECT

    document = tomllib.loads(REGRESS_CONFIG.read_text(encoding="utf-8"))
    assert document["compare"]["alpha"] == DEFAULT_ALPHA
    assert document["compare"]["min_effect"] == DEFAULT_MIN_EFFECT


def test_ask_target_answers_project_one_through_a_real_subprocess(tmp_path, session_db):
    """The whole seam, end to end, exactly as project 1 would drive it.

    A real `CommandTarget` around a real subprocess, because the contract is
    about a process boundary: what reaches project 1 is stdout, and a test that
    called `command_ask_target` in-process would not notice a stray progress
    line printed beside the answer.
    """
    from regression_detect.target.adapters.command import CommandTarget

    config = cli_config(tmp_path, session_db, [])
    assert COMMITTED_SCHEMA_SQL.exists()
    target = CommandTarget(
        [sys.executable, str(SCRIPT), "ask-target", "--dry-run", "--config", str(config)],
        timeout_s=120.0,
        cwd=ROOT,
        env_allowlist=["HOME"],
    )
    answer = target.run("How much did we refund to customers in Berlin last month?")
    assert answer.startswith("# How much did we refund")
    assert "## Confidence" in answer
    assert "## The query that ran" in answer
    assert "run: " not in answer


def test_ask_target_exits_zero_on_an_abstention(tmp_path, session_db):
    """A refusal is an answer a judge grades, not a failed sample."""
    config = cli_config(tmp_path, session_db, [])
    completed = subprocess.run(  # noqa: S603 - argv list, no shell, fixed program
        [sys.executable, str(SCRIPT), "ask-target", "--dry-run", "--config", str(config)],
        input="What were the totals?",
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "I need one more thing" in completed.stdout


def test_ask_target_refuses_an_empty_question(tmp_path, session_db):
    config = cli_config(tmp_path, session_db, [])
    completed = subprocess.run(  # noqa: S603 - argv list, no shell, fixed program
        [sys.executable, str(SCRIPT), "ask-target", "--dry-run", "--config", str(config)],
        input="   \n",
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout.strip() == ""
    assert "stdin held no question" in completed.stderr

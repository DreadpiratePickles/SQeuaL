"""`config.py` is the only module in this package allowed to name a model.

Phase A never calls one. These tests exist anyway, because the rule they
enforce is the one that decays silently: the day somebody puts a model id in
`sqeual.toml` or in a call site, nothing breaks and nobody notices until a
deployment needs to point at a different model and finds four places to edit.

The load-bearing behaviour is that an *unknown* reference raises. A typo
resolved to a default would send questions to the wrong model at the wrong
price, and would do it quietly.
"""

import pytest

from conftest import load_test_config
from sqeual.config import (
    JUDGE_MODEL_REF,
    MODEL_ID_DEFAULTS,
    SQL_MODEL_REF,
    UnknownModelRefError,
    model_id_for_ref,
)


def test_a_known_reference_resolves_to_its_default(monkeypatch):
    monkeypatch.delenv(SQL_MODEL_REF, raising=False)
    assert model_id_for_ref(SQL_MODEL_REF) == MODEL_ID_DEFAULTS[SQL_MODEL_REF]


def test_the_environment_wins_over_the_default(monkeypatch):
    monkeypatch.setenv(SQL_MODEL_REF, "some-other-model")
    assert model_id_for_ref(SQL_MODEL_REF) == "some-other-model"


def test_a_blank_override_falls_back_to_the_default(monkeypatch):
    """An empty variable is an unset variable. A deployment that exports
    `SQEUAL_SQL_MODEL_ID=` has not chosen a model called "".
    """
    monkeypatch.setenv(SQL_MODEL_REF, "   ")
    assert model_id_for_ref(SQL_MODEL_REF) == MODEL_ID_DEFAULTS[SQL_MODEL_REF]


def test_an_unknown_reference_raises_rather_than_defaulting():
    with pytest.raises(UnknownModelRefError, match="known references"):
        model_id_for_ref("SQEUAL_TYPO_MODEL_ID")


def test_the_config_only_names_references_this_module_defines(tmp_path):
    """The committed `[models]` section and this module have to agree, or the
    first Phase B call fails on a name nobody can resolve."""
    config = load_test_config(tmp_path)
    assert config.models.sql_model_ref == SQL_MODEL_REF
    assert config.models.judge_model_ref == JUDGE_MODEL_REF
    for ref in (config.models.sql_model_ref, config.models.judge_model_ref):
        assert ref in MODEL_ID_DEFAULTS


def test_no_model_id_appears_in_the_committed_configuration():
    """The rule, mechanically. `sqeual.toml` names environment variables; the
    vendor strings live here and nowhere else."""
    from conftest import COMMITTED_CONFIG

    text = COMMITTED_CONFIG.read_text(encoding="utf-8")
    for model_id in set(MODEL_ID_DEFAULTS.values()):
        assert model_id not in text

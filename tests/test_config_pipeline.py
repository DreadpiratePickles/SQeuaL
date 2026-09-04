"""The Phase B sections of `sqeual.toml`, validated like every other boundary.

These six sections decide what a model is asked, how many times, how its answer
is scored, and whether a user sees a number or a refusal. A typo in any of them
must stop the run naming the key — a confidence weight silently read as zero
would disable a check while the report still claimed it ran.
"""

import pytest

from conftest import load_test_config, write_config
from sqeual.config_file import ConfigFileError, load_config


def test_committed_config_loads_the_phase_b_sections(tmp_path):
    config = load_test_config(tmp_path)
    assert config.time.as_of == "2026-08-31"
    assert config.generate.k == 3
    assert config.generate.temperature == 0.0
    assert config.generate.sample_temperature == 0.7
    assert config.generate.max_repairs == 1
    assert config.verify.float_places == 2
    assert config.answer.max_rows_shown == 20
    assert config.answer.llm_phrasing is False
    assert config.answer.currency == "EUR"
    assert config.confidence.weight_intent == 40
    assert config.confidence.weight_judge == 30
    assert config.confidence.weight_agreement == 20
    assert config.confidence.weight_sanity == 10
    assert config.confidence.repair_penalty == 15
    assert config.confidence.high_threshold == 75
    assert config.confidence.medium_threshold == 55
    assert config.confidence.abstain_threshold == 40
    assert config.cost.input_micro_usd_per_1k_tokens == 0
    assert config.cost.output_micro_usd_per_1k_tokens == 0


def test_as_of_must_be_an_iso_date(tmp_path):
    with pytest.raises(ConfigFileError, match="as_of"):
        load_test_config(tmp_path, [('as_of = "2026-08-31"', 'as_of = "last tuesday"')])


def test_as_of_must_be_a_real_calendar_date(tmp_path):
    with pytest.raises(ConfigFileError, match="as_of"):
        load_test_config(tmp_path, [('as_of = "2026-08-31"', 'as_of = "2026-02-30"')])


def test_k_must_be_at_least_one(tmp_path):
    with pytest.raises(ConfigFileError, match="'k'"):
        load_test_config(tmp_path, [("k = 3", "k = 0")])


def test_temperature_must_be_within_range(tmp_path):
    with pytest.raises(ConfigFileError, match="sample_temperature"):
        load_test_config(tmp_path, [("sample_temperature = 0.7", "sample_temperature = 3.5")])


def test_temperature_accepts_an_integer_zero(tmp_path):
    """TOML `0` is an int; a temperature of zero is the primary sample's whole point."""
    config = load_test_config(tmp_path, [("sample_temperature = 0.7", "sample_temperature = 0")])
    assert config.generate.sample_temperature == 0.0


def test_temperature_rejects_a_boolean(tmp_path):
    with pytest.raises(ConfigFileError, match="temperature"):
        load_test_config(tmp_path, [("sample_temperature = 0.7", "sample_temperature = true")])


def test_max_repairs_may_be_zero(tmp_path):
    """Zero is a legitimate policy: never repair, take the first verdict."""
    config = load_test_config(tmp_path, [("max_repairs = 1", "max_repairs = 0")])
    assert config.generate.max_repairs == 0


def test_llm_phrasing_must_be_a_boolean(tmp_path):
    with pytest.raises(ConfigFileError, match="llm_phrasing"):
        load_test_config(tmp_path, [("llm_phrasing = false", 'llm_phrasing = "no"')])


def test_currency_must_be_a_non_empty_string(tmp_path):
    with pytest.raises(ConfigFileError, match="currency"):
        load_test_config(tmp_path, [('currency = "EUR"', 'currency = ""')])


def test_a_confidence_weight_may_not_be_missing(tmp_path):
    with pytest.raises(ConfigFileError, match="weight_judge"):
        load_test_config(tmp_path, [("weight_judge = 30\n", "")])


def test_a_confidence_weight_of_zero_is_refused(tmp_path):
    """A zero weight silently disables a check while the report still lists it."""
    with pytest.raises(ConfigFileError, match="weight_sanity"):
        load_test_config(tmp_path, [("weight_sanity = 10", "weight_sanity = 0")])


def test_thresholds_must_descend(tmp_path):
    with pytest.raises(ConfigFileError, match="must be greater than"):
        load_test_config(tmp_path, [("medium_threshold = 55", "medium_threshold = 80")])


def test_abstain_threshold_must_be_below_medium(tmp_path):
    with pytest.raises(ConfigFileError, match="must be greater than"):
        load_test_config(tmp_path, [("abstain_threshold = 40", "abstain_threshold = 60")])


def test_a_percentage_above_one_hundred_is_refused(tmp_path):
    with pytest.raises(ConfigFileError, match="high_threshold"):
        load_test_config(tmp_path, [("high_threshold = 75", "high_threshold = 101")])


def test_repair_penalty_may_be_zero(tmp_path):
    config = load_test_config(tmp_path, [("repair_penalty = 15", "repair_penalty = 0")])
    assert config.confidence.repair_penalty == 0


def test_float_places_must_be_an_integer(tmp_path):
    with pytest.raises(ConfigFileError, match="float_places"):
        load_test_config(tmp_path, [("float_places = 2", 'float_places = "2"')])


def test_unknown_key_in_a_phase_b_section_is_refused(tmp_path):
    path = write_config(tmp_path, [("[generate]", "[generate]\nsurprise = 1")])
    with pytest.raises(ConfigFileError, match="unknown key"):
        load_config(path)


def test_a_misspelled_phase_b_section_is_refused_as_unknown(tmp_path):
    """Renaming a section is caught as an unknown section, not as a missing one.

    Both are errors, and the message that names the word actually in the file is
    the more useful of the two.
    """
    path = write_config(tmp_path, [("[confidence]", "[confidence_typo]")])
    with pytest.raises(ConfigFileError, match="unknown section"):
        load_config(path)


def test_a_missing_phase_b_section_is_named(tmp_path):
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: text.index("[confidence]")], encoding="utf-8")
    with pytest.raises(ConfigFileError, match=r"\[confidence\]"):
        load_config(path)


def test_cost_prices_may_not_be_negative(tmp_path):
    with pytest.raises(ConfigFileError, match="input_micro_usd_per_1k_tokens"):
        load_test_config(
            tmp_path,
            [("input_micro_usd_per_1k_tokens = 0", "input_micro_usd_per_1k_tokens = -1")],
        )


def test_unpriced_cost_section_reports_itself_as_unpriced(tmp_path):
    """Zero prices mean "nobody has entered a tariff", not "this call was free"."""
    config = load_test_config(tmp_path)
    assert config.cost.priced is False


def test_a_configured_price_reports_itself_as_priced(tmp_path):
    config = load_test_config(
        tmp_path, [("output_micro_usd_per_1k_tokens = 0", "output_micro_usd_per_1k_tokens = 400")]
    )
    assert config.cost.priced is True

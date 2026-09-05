"""The seven `sqeual.toml` sections the pipeline added, and their validation.

Phase A's limits said what a query may do. These say what a *model* is asked,
how many times, how the answer it proposes is scored, and — the one that
matters most — where the line falls between showing a number and refusing to.

Three of them deserve their reason stated here rather than only in the file:

**`[time] as_of` is a committed date, not `today`.** "Last month" has to mean
the same thing on every run or the same question gives two different answers a
week apart and neither is reproducible. The model is *told* the date, and code
independently resolves the window and checks the SQL's literals against it —
telling a model the date is a prompt, and checking what it did with it is a
control.

**`[confidence]` weights are integers in hundredths.** A threshold is a line
somebody argues about in a pull request; `0.55` invites a diff that reads
`0.5500000001`. The division by 100 happens exactly once, in `confidence.py`.

`[gates]` arrived after the first live evaluation and is a seventh section. It is
deliberately *not* part of `[confidence]`, because a gate is not a weight: §54
argues that a weighted average of four numbers cannot be dragged below a
threshold by one of them, and putting the veto in the same table as the weights
would invite exactly the tuning that does not work.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from .config_values import (
    ConfigFileError,
    boolean,
    non_empty_str,
    percentage,
    positive_int,
    section_of,
    string_list,
    temperature,
)
from .verify.checks import CHECK_NAMES

PIPELINE_SECTIONS: dict[str, tuple[str, ...]] = {
    "time": ("as_of",),
    "generate": ("k", "temperature", "sample_temperature", "max_repairs", "max_examples"),
    "verify": ("float_places",),
    "answer": ("max_rows_shown", "llm_phrasing", "currency", "currency_symbol"),
    "confidence": (
        "weight_intent",
        "weight_judge",
        "weight_agreement",
        "weight_sanity",
        "repair_penalty",
        "high_threshold",
        "medium_threshold",
        "abstain_threshold",
    ),
    "gates": ("judge_veto", "hard_checks"),
    "cost": ("input_micro_usd_per_1k_tokens", "output_micro_usd_per_1k_tokens"),
}


@dataclass(frozen=True)
class TimeSettings:
    """The date every relative phrase in a question resolves against."""

    as_of: str
    """ISO `YYYY-MM-DD`. Validated as a real calendar date at load time, so a
    `2026-02-30` is a configuration error rather than a window nobody can
    compute halfway through a paid run."""

    @property
    def as_of_date(self) -> dt.date:
        return dt.date.fromisoformat(self.as_of)


@dataclass(frozen=True)
class GenerateSettings:
    """How many candidates are asked for, at what temperature, and repaired how often."""

    k: int
    temperature: float
    sample_temperature: float
    max_repairs: int
    max_examples: int


@dataclass(frozen=True)
class VerifySettings:
    """How results are canonicalised before two of them are compared."""

    float_places: int


@dataclass(frozen=True)
class AnswerSettings:
    """How the rendered answer looks, and whether a model may phrase it."""

    max_rows_shown: int
    llm_phrasing: bool
    currency: str
    currency_symbol: str

    def without_phrasing(self) -> "AnswerSettings":
        """The same settings with LLM phrasing off, for `--no-phrasing`."""
        return AnswerSettings(
            max_rows_shown=self.max_rows_shown,
            llm_phrasing=False,
            currency=self.currency,
            currency_symbol=self.currency_symbol,
        )


@dataclass(frozen=True)
class ConfidenceSettings:
    """The weights and thresholds that turn evidence into a level.

    Every weight is required and every weight must be non-zero. A weight read as
    zero would silently disable a check while the report still listed it as
    having run, which is the one failure a confidence score must not have.
    """

    weight_intent: int
    weight_judge: int
    weight_agreement: int
    weight_sanity: int
    repair_penalty: int
    high_threshold: int
    medium_threshold: int
    abstain_threshold: int


@dataclass(frozen=True)
class GatesSettings:
    """Which checks can withhold an answer outright, rather than score it down.

    Two fields and no weights, on purpose. A gate answers yes or no and runs
    before anything is averaged; `docs/design.md` §54 sets out why the first live
    evaluation could not be fixed by moving a weight or a threshold.
    """

    judge_veto: bool
    """Whether a definite FAIL on either blind back-translation criterion
    withholds the answer. On by default. An *unreadable* judge never vetoes,
    whatever this says: a 503 is a silence and a `fail` is a statement."""
    hard_checks: frozenset[str]
    """Deterministic checks whose FAIL withholds the answer. Every other check
    stays a weighted factor. Validated against `CHECK_NAMES` at load time."""


@dataclass(frozen=True)
class CostSettings:
    """The tariff a run's token counts are priced at, in micro-USD per 1k tokens.

    Both default to zero, which means **unpriced** and never "free". `priced`
    exists so a trace can say which of the two a zero is: a cost of 0 on a run
    that really called a model would otherwise read as a bill of nothing.
    """

    input_micro_usd_per_1k_tokens: int
    output_micro_usd_per_1k_tokens: int

    @property
    def priced(self) -> bool:
        return bool(self.input_micro_usd_per_1k_tokens or self.output_micro_usd_per_1k_tokens)


def _iso_date(section: dict[str, Any], key: str, *, path) -> str:
    raw = non_empty_str(section, key, path=path)
    try:
        parsed = dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigFileError(
            f"{path}: '{key}' must be an ISO date (YYYY-MM-DD), got {raw!r} ({exc})"
        ) from exc
    return parsed.isoformat()


def load_time(document: dict[str, Any], *, path) -> TimeSettings:
    section = section_of(document, "time", PIPELINE_SECTIONS["time"], path=path)
    return TimeSettings(as_of=_iso_date(section, "as_of", path=path))


def load_generate(document: dict[str, Any], *, path) -> GenerateSettings:
    section = section_of(document, "generate", PIPELINE_SECTIONS["generate"], path=path)
    return GenerateSettings(
        k=positive_int(section, "k", path=path),
        temperature=temperature(section, "temperature", path=path),
        sample_temperature=temperature(section, "sample_temperature", path=path),
        # Zero is a legitimate policy — never repair, take the first verdict —
        # so the floor here is 0 and not 1.
        max_repairs=positive_int(section, "max_repairs", path=path, minimum=0),
        max_examples=positive_int(section, "max_examples", path=path),
    )


def load_verify(document: dict[str, Any], *, path) -> VerifySettings:
    section = section_of(document, "verify", PIPELINE_SECTIONS["verify"], path=path)
    return VerifySettings(
        float_places=positive_int(section, "float_places", path=path, minimum=0)
    )


def load_answer(document: dict[str, Any], *, path) -> AnswerSettings:
    section = section_of(document, "answer", PIPELINE_SECTIONS["answer"], path=path)
    return AnswerSettings(
        max_rows_shown=positive_int(section, "max_rows_shown", path=path),
        llm_phrasing=boolean(section, "llm_phrasing", path=path),
        currency=non_empty_str(section, "currency", path=path),
        currency_symbol=non_empty_str(section, "currency_symbol", path=path),
    )


def load_confidence(document: dict[str, Any], *, path) -> ConfidenceSettings:
    section = section_of(document, "confidence", PIPELINE_SECTIONS["confidence"], path=path)
    settings = ConfidenceSettings(
        weight_intent=percentage(section, "weight_intent", path=path, minimum=1),
        weight_judge=percentage(section, "weight_judge", path=path, minimum=1),
        weight_agreement=percentage(section, "weight_agreement", path=path, minimum=1),
        weight_sanity=percentage(section, "weight_sanity", path=path, minimum=1),
        repair_penalty=percentage(section, "repair_penalty", path=path),
        high_threshold=percentage(section, "high_threshold", path=path),
        medium_threshold=percentage(section, "medium_threshold", path=path),
        abstain_threshold=percentage(section, "abstain_threshold", path=path),
    )
    # Ordering is checked here rather than at use, because a run that discovered
    # halfway through that HIGH was below MEDIUM would already have spent money
    # to reach a level nobody can interpret.
    for higher, lower in (
        ("high_threshold", "medium_threshold"),
        ("medium_threshold", "abstain_threshold"),
    ):
        if getattr(settings, higher) <= getattr(settings, lower):
            raise ConfigFileError(
                f"{path}: [confidence] '{higher}' ({getattr(settings, higher)}) must be greater "
                f"than '{lower}' ({getattr(settings, lower)}); the levels are read top down."
            )
    return settings


def load_gates(document: dict[str, Any], *, path) -> GatesSettings:
    """Read `[gates]`, checking every hard check against the checks that exist.

    A `hard_checks` entry naming nothing is rejected rather than ignored. It
    would otherwise load cleanly, gate nothing, and leave a reviewable file
    stating a control that never fires — the same failure mode `denied_columns`
    is validated against in `config_file.py`.
    """
    section = section_of(document, "gates", PIPELINE_SECTIONS["gates"], path=path)
    names = string_list(section, "hard_checks", path=path, allow_empty=True)
    unknown = sorted({name for name in names} - set(CHECK_NAMES))
    if unknown:
        raise ConfigFileError(
            f"{path}: [gates] hard_checks names {', '.join(repr(name) for name in unknown)}, "
            f"which no check produces. Known checks: {', '.join(CHECK_NAMES)}. A gate on a "
            "check that does not exist would never fire and would still read as a control."
        )
    return GatesSettings(
        judge_veto=boolean(section, "judge_veto", path=path),
        hard_checks=frozenset(names),
    )


def load_cost(document: dict[str, Any], *, path) -> CostSettings:
    section = section_of(document, "cost", PIPELINE_SECTIONS["cost"], path=path)
    return CostSettings(
        input_micro_usd_per_1k_tokens=positive_int(
            section, "input_micro_usd_per_1k_tokens", path=path, minimum=0
        ),
        output_micro_usd_per_1k_tokens=positive_int(
            section, "output_micro_usd_per_1k_tokens", path=path, minimum=0
        ),
    )

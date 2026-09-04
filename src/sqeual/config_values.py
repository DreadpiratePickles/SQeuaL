"""The primitives every `sqeual.toml` section is validated with.

Extracted from `config_file.py` when Phase B added six sections, for one
reason: two files validating a TOML integer with two slightly different rules is
two files, one of which is wrong. Every section — Phase A's five and Phase B's
six — reaches for the same six functions here, so "what counts as a valid
positive integer in this configuration" has exactly one answer.

The one rule worth restating: **`bool` is not an integer here.** Python says
`isinstance(True, int)`, so a configuration reading `max_rows = true` would be
accepted as one row by a naive check. Every numeric validator rejects `bool`
first.
"""

from typing import Any


class ConfigFileError(Exception):
    """`sqeual.toml` is missing, unparseable, or holds an unusable value."""


def section_of(
    document: dict[str, Any], name: str, keys: tuple[str, ...], *, path
) -> dict[str, Any]:
    """One validated section: present, a table, with exactly `keys`.

    Both directions are refused. An unknown key is a typo that would otherwise
    be read as "the default applies", and a missing key is a limit nobody chose.
    """
    if name not in document:
        raise ConfigFileError(f"{path}: missing required section [{name}]")
    value = document[name]
    if not isinstance(value, dict):
        raise ConfigFileError(f"{path}: [{name}] must be a table, got {type(value).__name__}")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise ConfigFileError(f"{path}: [{name}] has unknown key(s): {', '.join(unknown)}")
    missing = [key for key in keys if key not in value]
    if missing:
        raise ConfigFileError(f"{path}: [{name}] is missing key(s): {', '.join(missing)}")
    return value


def positive_int(section: dict[str, Any], key: str, *, path, minimum: int = 1) -> int:
    """An integer of at least `minimum`. `True` is not one."""
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigFileError(f"{path}: '{key}' must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ConfigFileError(f"{path}: '{key}' must be at least {minimum}, got {value}")
    return value


def percentage(section: dict[str, Any], key: str, *, path, minimum: int = 0) -> int:
    """An integer 0-100.

    Weights and thresholds are integers in hundredths rather than floats,
    because a threshold is a line somebody argues about in a pull request and
    `0.55` invites a diff that reads `0.5500000001`. The arithmetic divides by
    100 exactly once, where the score is computed.
    """
    value = positive_int(section, key, path=path, minimum=minimum)
    if value > 100:
        raise ConfigFileError(f"{path}: '{key}' must be between {minimum} and 100, got {value}")
    return value


def temperature(section: dict[str, Any], key: str, *, path) -> float:
    """A sampling temperature in [0, 2].

    Accepts a TOML integer as well as a float: `temperature = 0` is how anybody
    would write a deterministic sample, and refusing it because it is not `0.0`
    would be pedantry with a stack trace.
    """
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigFileError(f"{path}: '{key}' must be a number, got {type(value).__name__}")
    number = float(value)
    if not 0.0 <= number <= 2.0:
        raise ConfigFileError(f"{path}: '{key}' must be between 0 and 2, got {number}")
    return number


def boolean(section: dict[str, Any], key: str, *, path) -> bool:
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigFileError(f"{path}: '{key}' must be true or false, got {type(value).__name__}")
    return value


def non_empty_str(section: dict[str, Any], key: str, *, path) -> str:
    value = section[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigFileError(f"{path}: '{key}' must be a non-empty string")
    return value.strip()


def string_list(
    section: dict[str, Any], key: str, *, path, allow_empty: bool
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

"""Model identifiers, and nothing else.

Every model id in this package lives here. No other module names a model, and
neither does `sqeual.toml`: the configuration file names a *reference* — the
name of an environment variable — and this module resolves it. That keeps
vendor strings out of reviewed configuration and gives a deployment one
variable to override, which is the rule projects 1, 2 and 9 follow for the same
reason: a model id is a fact about the outside world that changes without
warning, and it should change in one reviewable place.

**Phase A calls no model at all.** Nothing in `db`, `schema`, `guard` or
`execute` imports this module to make a request. It exists now so that the
vocabulary of references is fixed before the first call is written, and so that
`sqeual.toml` has something to validate its `[models]` section against. A
reference that resolves to nothing is caught at config-load time rather than at
the moment Phase B first tries to spend money.
"""

import os

SQL_MODEL_ID = "gemini-3.5-flash-lite"
"""The model Phase B will ask for SQL.

The cheapest published Gemini text model. Text-to-SQL over a slice of six
tables is a short, heavily-constrained task with a machine-checkable answer:
the guard rejects a hallucinated column whoever wrote it, so a more expensive
model buys a better first attempt rather than a safer one. Phase B samples the
same question `k` times and keeps the SQL whose result set the others agree
with, which is a way of spending a small model's cheapness on the accuracy a
large one would have charged for.
"""

SQL_MODEL_REF = "SQEUAL_SQL_MODEL_ID"
"""The environment variable that overrides `SQL_MODEL_ID` for one deployment."""

JUDGE_MODEL_ID = SQL_MODEL_ID
"""The model Phase B's back-translation check runs on.

Read from the SQL model's id rather than restated, so the two cannot drift
apart silently while they are deliberately the same.

They are the same today because one provider key exists in this workspace, and
that is a compromise this comment refuses to let anybody forget. Phase B's
verifier asks a model to read the *guarded* SQL back into English and compares
that against the original question. A model checking a sibling's work exhibits
**self-preference** — it agrees with output from its own family more readily
than another family would — so the check is weaker than it looks, and its pass
rate is not a number anybody should quote as an accuracy figure. Point
`SQEUAL_JUDGE_MODEL_ID` at another family the moment a second key exists.

What the self-preference caveat does *not* touch is the guard. Nothing in
Phase A asks a model anything; a hallucinated column is caught by comparing it
against `PRAGMA table_info`, and that comparison has no opinion.
"""

JUDGE_MODEL_REF = "SQEUAL_JUDGE_MODEL_ID"
"""The environment variable that points the verifier at a different family."""

MODEL_ID_DEFAULTS: dict[str, str] = {
    SQL_MODEL_REF: SQL_MODEL_ID,
    JUDGE_MODEL_REF: JUDGE_MODEL_ID,
}
"""Environment variable name -> default model id. The whole vocabulary of
references `sqeual.toml` may use."""


class UnknownModelRefError(ValueError):
    """Configuration names a model reference this module does not define."""


def model_id_for_ref(ref: str) -> str:
    """Resolve a configured `model_ref` to the model id that should be called.

    The environment wins over the default so a run can be pointed at another
    model without a commit.

    Raises:
        UnknownModelRefError: `ref` is not a defined reference. An unrecognised
            reference is never silently resolved to a default: a typo would then
            quietly send questions to the wrong model at the wrong price.
    """
    if ref not in MODEL_ID_DEFAULTS:
        known = ", ".join(sorted(MODEL_ID_DEFAULTS))
        raise UnknownModelRefError(f"unknown model reference {ref!r}; known references: {known}")
    override = os.environ.get(ref, "").strip()
    return override or MODEL_ID_DEFAULTS[ref]

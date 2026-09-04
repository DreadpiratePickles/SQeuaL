"""Stage 06: does the guarded statement answer the question that was asked?

The guard proved the statement is well-formed, single, read-only and made of real
tables and columns. Nothing in its twelve rules has an opinion about relevance,
and `SELECT COUNT(*) FROM orders` is a perfectly valid answer to "how much did we
refund in March". This stage is the one that notices.

Three sources of evidence, deliberately unequal. Eight deterministic checks that
need no model and cannot be talked out of a verdict; a blind back-translation
graded by project 1's criterion judge, which is the only part that can read
meaning and the only part that can be wrong in an interesting way; and nothing
else. **This stage never rewrites the SQL** — a verifier that repairs what it is
checking is a generator, and stage 05 is where repair is bounded and logged.

`same_family` is recorded on every run rather than documented once. The judge is
currently the same model as the writer, because one provider key exists in this
workspace, and models agree with output from their own family more readily than
another family would. That biases the verdicts upward. Recording the flag per run
is what stops a later analysis of pass rates from silently mixing biased and
unbiased ones.
"""

from dataclasses import dataclass

from ..providers import MeteredProvider
from ..providers.pacing import Pacer
from ..schema.card import SchemaCard
from ..schema.render import render_card
from .backtranslate import BackTranslation, back_translate
from .checks import Check, pass_fraction
from .intent import intent_checks
from .sanity import sanity_checks


@dataclass(frozen=True)
class Verification:
    """Everything stage 06 concluded about one guarded statement."""

    intent: tuple[Check, ...]
    sanity: tuple[Check, ...]
    back_translation: BackTranslation
    same_family: bool

    @property
    def intent_fraction(self) -> float | None:
        return pass_fraction(self.intent)

    @property
    def sanity_fraction(self) -> float | None:
        return pass_fraction(self.sanity)

    @property
    def checks(self) -> tuple[Check, ...]:
        return self.intent + self.sanity

    @property
    def input_tokens(self) -> int:
        return self.back_translation.input_tokens

    @property
    def output_tokens(self) -> int:
        return self.back_translation.output_tokens


def verify_answer(
    *,
    generation,
    card: SchemaCard,
    config,
    provider: MeteredProvider,
    pacer: Pacer,
    judge_provider: MeteredProvider | None = None,
) -> Verification:
    """Check one answered generation, deterministically and then with a judge.

    Args:
        generation: an ANSWERED `GenerationOutcome`. Its `primary` carries the
            guard report and the rows.
        card: the live schema card.
        config: the validated `sqeual.toml`.
        provider: the metered seam for the back-translation.
        pacer: spaces the three calls under a per-minute quota.
        judge_provider: a different family for the judge, when a second key
            exists.

    Raises:
        ValueError: `generation` has no primary attempt. Verifying a run that
            produced no statement would be verifying nothing, and returning an
            empty verification would let a refusal acquire a confidence score.
    """
    primary = generation.primary
    if primary is None or primary.report is None or primary.result is None:
        raise ValueError("nothing to verify: the generation produced no executed statement")

    judge = judge_provider or provider
    return Verification(
        intent=intent_checks(
            question=generation.question,
            sql=primary.report.normalised_sql,
            card=card,
            tables_used=primary.report.tables_used,
            time_window=generation.time_window,
            synonyms=config.schema.synonyms,
        ),
        sanity=sanity_checks(question=generation.question, result=primary.result),
        back_translation=back_translate(
            sql=primary.report.normalised_sql,
            schema_markdown=render_card(card, generation.schema_slice.tables),
            question=generation.question,
            provider=provider,
            pacer=pacer,
            judge_provider=judge_provider,
        ),
        same_family=provider.model_id == judge.model_id,
    )


__all__ = ["Verification", "verify_answer"]

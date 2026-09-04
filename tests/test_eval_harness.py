"""What the harness does when things go wrong, and what it charges for them.

Split from `test_eval_run.py`, which pins the numbers a clean offline pass
produces. These are the paths that only run when a provider fails, an answer key
does not work, or the offline fake is asked about a question nobody scripted —
and they are the ones that matter on a bad afternoon.
"""

import pytest

from conftest import COMMITTED_GOLDENS, phase_b_config
from sqeual.eval.fake import (
    EvalFakeProvider,
    build_script,
    dry_run_provider,
    dry_run_scripts,
)
from sqeual.eval.run import (
    build_provenance,
    eval_directory,
    prepare_references,
    run_eval,
)
from sqeual.eval.score import Verdict
from sqeual.eval.usage import CountingProvider
from sqeual.providers import ProviderConfigError, ProviderError
from sqeual.providers.pacing import Pacer


def test_a_provider_failure_ends_one_question_not_the_run(
    tmp_path, session_db, session_card, committed_questions
):
    config = phase_b_config(tmp_path, session_db)
    questions = committed_questions[:3]
    references = prepare_references(questions, card=session_card, config=config)

    class Failing:
        model_id = "failing"

        def __init__(self):
            self.calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            raise ProviderError("status 503 UNAVAILABLE")

    provenance = build_provenance(
        questions=questions,
        goldens_path=config.path,
        card=session_card,
        config=config,
        provider=Failing(),
        judge_model_id="failing",
        k=None,
        pacer=Pacer(0),
        dry_run=False,
    )
    run = run_eval(
        questions=questions,
        references=references,
        card=session_card,
        config=config,
        provider=Failing(),
        pacer=Pacer(0),
        provenance=provenance,
    )
    assert [result.verdict for result in run.results] == [Verdict.ERRORED] * 3
    assert run.metrics.errored == 3
    assert run.metrics.scored == 0
    assert run.metrics.execution_accuracy.value is None


def test_a_broken_reference_costs_no_model_call(
    tmp_path, session_db, session_card, committed_questions
):
    from dataclasses import replace

    config = phase_b_config(tmp_path, session_db)
    broken = replace(committed_questions[0], reference_sql="SELECT revenue FROM orders")
    references = prepare_references([broken], card=session_card, config=config)

    class NeverCalled:
        model_id = "never-called"

        def complete(self, **_kwargs):  # pragma: no cover - asserted not to run
            raise AssertionError("a broken reference must not reach a model")

    provenance = build_provenance(
        questions=[broken],
        goldens_path=config.path,
        card=session_card,
        config=config,
        provider=NeverCalled(),
        judge_model_id="never-called",
        k=None,
        pacer=Pacer(0),
        dry_run=False,
    )
    run = run_eval(
        questions=[broken],
        references=references,
        card=session_card,
        config=config,
        provider=NeverCalled(),
        pacer=Pacer(0),
        provenance=provenance,
    )
    (result,) = run.results
    assert result.verdict is Verdict.BROKEN_REFERENCE
    assert "unknown_column" in result.error


def test_a_missing_reference_is_a_broken_case_not_a_crash(
    tmp_path, session_db, session_card, committed_questions
):
    config = phase_b_config(tmp_path, session_db)
    question = committed_questions[0]

    class NeverCalled:
        model_id = "never-called"

        def complete(self, **_kwargs):  # pragma: no cover - asserted not to run
            raise AssertionError("nothing should be asked about a case with no key")

    provenance = build_provenance(
        questions=[question],
        goldens_path=config.path,
        card=session_card,
        config=config,
        provider=NeverCalled(),
        judge_model_id="never-called",
        k=None,
        pacer=Pacer(0),
        dry_run=False,
    )
    run = run_eval(
        questions=[question],
        references={},
        card=session_card,
        config=config,
        provider=NeverCalled(),
        pacer=Pacer(0),
        provenance=provenance,
    )
    (result,) = run.results
    assert result.verdict is Verdict.BROKEN_REFERENCE
    assert "no reference was executed" in result.error


def test_the_fake_refuses_a_question_it_has_no_script_for():
    provider = EvalFakeProvider({})
    from sqeual.generate.prompt import load_generate_prompt

    with pytest.raises(ProviderConfigError, match="no script for"):
        provider.complete(
            system=load_generate_prompt(),
            user="<question>\nsomething nobody scripted\n</question>",
            temperature=0.0,
        )


def test_the_fake_refuses_an_unsafe_question_it_cannot_recognise(committed_questions):
    from dataclasses import replace

    unsafe = next(
        question
        for question in committed_questions
        if question.trap is not None and question.trap.value == "unsafe"
    )
    with pytest.raises(ProviderConfigError, match="no scripted unsafe statement"):
        build_script(
            replace(unsafe, question="Please do something regrettable."),
            None,
            correct=False,
            judge_passes=False,
        )


def test_the_fake_cannot_script_a_case_whose_reference_does_not_run(committed_questions):
    answerable = next(question for question in committed_questions if question.scoreable)
    with pytest.raises(ValueError, match="cannot script"):
        build_script(answerable, None, correct=True, judge_passes=True)


def test_every_committed_question_is_scripted(
    tmp_path, session_db, session_card, committed_questions
):
    config = phase_b_config(tmp_path, session_db)
    references = prepare_references(committed_questions, card=session_card, config=config)
    scripts = dry_run_scripts(committed_questions, references)
    assert len(scripts) == 40


def test_the_eval_directory_disambiguates_a_collision(tmp_path):
    import datetime as dt

    now = dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.UTC)
    first = eval_directory(tmp_path, now=now)
    second = eval_directory(tmp_path, now=now)
    assert first != second
    assert second.name.endswith("-2")


def test_a_question_that_errors_partway_still_reports_what_it_spent(
    tmp_path, session_db, session_card, committed_questions
):
    """Reporting zero would make the run's cost a total of the questions that
    happened to finish, which is a smaller number than the bill."""
    config = phase_b_config(tmp_path, session_db)
    questions = [committed_questions[0]]
    references = prepare_references(questions, card=session_card, config=config)
    script = dry_run_scripts(questions, references)[questions[0].question]

    class FailsAfterTwo:
        model_id = "fails-after-two"

        def __init__(self):
            self.calls = 0

        def complete(self, **_kwargs):
            from sqeual.providers import Completion

            self.calls += 1
            if self.calls > 2:
                raise ProviderError("status 503 UNAVAILABLE")
            return Completion(
                text=script.generate,
                input_tokens=11,
                output_tokens=7,
                model_id=self.model_id,
                latency_ms=13,
            )

    provider = FailsAfterTwo()
    provenance = build_provenance(
        questions=questions,
        goldens_path=COMMITTED_GOLDENS,
        card=session_card,
        config=config,
        provider=provider,
        judge_model_id=provider.model_id,
        k=None,
        pacer=Pacer(0),
        dry_run=False,
    )
    run = run_eval(
        questions=questions,
        references=references,
        card=session_card,
        config=config,
        provider=provider,
        pacer=Pacer(0),
        provenance=provenance,
    )
    (result,) = run.results
    assert result.verdict is Verdict.ERRORED
    assert result.calls == 2
    assert result.input_tokens == 22
    assert result.output_tokens == 14
    assert result.latency_ms == 26
    assert run.metrics.cost.calls == 2


def test_the_counter_passes_the_model_id_through_and_sums(tmp_path, session_db):
    from sqeual.providers import Completion

    config = phase_b_config(tmp_path, session_db)

    class Fixed:
        model_id = "fixed"

        def complete(self, **_kwargs):
            return Completion(
                text="{}", input_tokens=3, output_tokens=5, model_id="fixed", latency_ms=7
            )

    counter = CountingProvider(Fixed(), config.cost)
    assert counter.model_id == "fixed"
    assert counter.snapshot().calls == 0
    counter.complete(system="s", user="u", temperature=0.0)
    counter.complete(system="s", user="u", temperature=0.0)
    usage = counter.snapshot()
    assert (usage.calls, usage.input_tokens, usage.output_tokens, usage.latency_ms) == (
        2,
        6,
        10,
        14,
    )
    # Unpriced by default, so the cost is an honest zero rather than a guess.
    assert usage.micro_usd == 0
    assert usage.since(usage).calls == 0


def test_the_fake_answers_the_phrasing_role_too():
    """`[answer] llm_phrasing` is off by default, so the dry run never reaches
    this branch — but a deployment that turns it on must not hit an unscripted
    role and get a guess."""
    import json

    from sqeual.answer.phrase import load_phrase_prompt
    from sqeual.eval.fake import EvalFakeProvider

    provider = EvalFakeProvider({})
    reply = provider.complete(system=load_phrase_prompt(), user="anything", temperature=0.0)
    assert json.loads(reply.text)["sentence"]
    assert provider.calls == [("phrase", "")]


def test_scoring_a_question_whose_reference_is_broken_is_a_broken_case(
    tmp_path, session_db, session_card, committed_questions
):
    """`run_eval` short-circuits before this, so it is a guard on `score_question`
    for any other caller: a verdict against an answer key that did not run would
    be a verdict from nothing."""
    from sqeual.eval.reference import ReferenceRun
    from sqeual.eval.score import score_question
    from sqeual.pipeline import run_ask

    config = phase_b_config(tmp_path, session_db)
    question = committed_questions[0]
    references = prepare_references([question], card=session_card, config=config)
    provider = dry_run_provider([question], references)
    outcome = run_ask(
        question=question.question,
        card=session_card,
        config=config,
        provider=provider,
        pacer=Pacer(0),
        write=False,
    )
    broken = ReferenceRun(
        question_id=question.id,
        sql=question.reference_sql,
        ok=False,
        normalised_sql=None,
        result=None,
        canonical=None,
        ordered_rows=None,
        reason="forced",
    )
    result = score_question(question, outcome, broken, float_places=2)
    assert result.verdict is Verdict.BROKEN_REFERENCE
    assert result.correct is None


def test_a_broken_reference_does_not_break_the_offline_run(
    tmp_path, session_db, session_card, committed_questions
):
    """A bad answer key is a broken case, never a traceback.

    The offline fake is scripted for every question up front, and an earlier
    version raised when it met an answerable case whose reference did not run —
    turning the one failure the stage contract says must be survivable into an
    unhandled exception out of `eval --dry-run`.
    """
    from dataclasses import replace

    config = phase_b_config(tmp_path, session_db)
    questions = [
        replace(
            committed_questions[0],
            reference_sql="SELECT COUNT(*) FROM orders WHERE region = 1",
        ),
        *committed_questions[1:4],
    ]
    references = prepare_references(questions, card=session_card, config=config)
    assert not references[questions[0].id].ok

    scripts = dry_run_scripts(questions, references)
    assert questions[0].question not in scripts, "a case nobody can score needs no script"
    assert len(scripts) == 3

    provider = dry_run_provider(questions, references)
    provenance = build_provenance(
        questions=questions,
        goldens_path=COMMITTED_GOLDENS,
        card=session_card,
        config=config,
        provider=provider,
        judge_model_id=provider.model_id,
        k=None,
        pacer=Pacer(0),
        dry_run=True,
    )
    run = run_eval(
        questions=questions,
        references=references,
        card=session_card,
        config=config,
        provider=provider,
        pacer=Pacer(0),
        provenance=provenance,
    )
    assert run.results[0].verdict is Verdict.BROKEN_REFERENCE
    assert run.results[0].calls == 0
    assert run.metrics.broken == 1
    assert run.metrics.scored == 3

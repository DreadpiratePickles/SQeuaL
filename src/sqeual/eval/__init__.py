"""Stage 08: score the whole tool against golden questions, and report honestly.

The unit of truth here is **reference SQL**, not a reference number. Every case
in `goldens/questions.yaml` carries the query a human would write, and the
harness executes it against the same database at eval time — so the expected
answer is derived on every run and cannot quietly stop being true.

Fourteen of the forty cases have no reference SQL at all, because they have no
answer: six name a column that does not exist, four are genuinely ambiguous, and
four ask the tool to write to or export from the database. What they measure is
the other half of the job — whether the tool declines — and the count of times it
did *not* is the first number on every page this package writes.

`run_eval` is the whole surface. `goldens` loads and validates the questions,
`reference` executes the answer keys, `score` judges one question, `rates` and
`metrics` aggregate, `present` and `render` write the two documents, `usage`
counts what was spent including on the calls that failed, and `fake` is the
scripted provider that makes `--dry-run` produce numbers a test can pin exactly.
"""

from .fake import dry_run_provider
from .goldens import (
    Expectation,
    GoldenQuestion,
    GoldenQuestionError,
    Trap,
    goldens_sha256,
    load_questions,
)
from .metrics import EvalMetrics, compute_metrics
from .rates import Rate
from .reference import ReferenceRun, run_reference
from .render import render_calibration, render_eval
from .run import (
    DEFAULT_EVAL_ROOT,
    DEFAULT_GOLDENS,
    EvalRun,
    build_provenance,
    eval_directory,
    prepare_references,
    run_eval,
    write_eval,
)
from .score import QuestionResult, Verdict, score_question

__all__ = [
    "DEFAULT_EVAL_ROOT",
    "DEFAULT_GOLDENS",
    "EvalMetrics",
    "EvalRun",
    "Expectation",
    "GoldenQuestion",
    "GoldenQuestionError",
    "QuestionResult",
    "Rate",
    "ReferenceRun",
    "Trap",
    "Verdict",
    "build_provenance",
    "compute_metrics",
    "dry_run_provider",
    "eval_directory",
    "goldens_sha256",
    "load_questions",
    "prepare_references",
    "render_calibration",
    "render_eval",
    "run_eval",
    "run_reference",
    "score_question",
    "write_eval",
]

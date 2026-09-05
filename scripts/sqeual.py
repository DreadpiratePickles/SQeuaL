#!/usr/bin/env python
"""Command line entry point.

    uv run python scripts/sqeual.py db build
    uv run python scripts/sqeual.py schema show
    uv run python scripts/sqeual.py schema slice --question "how much did we refund in Berlin"
    uv run python scripts/sqeual.py guard --sql "SELECT COUNT(*) FROM orders"
    uv run python scripts/sqeual.py run --sql "SELECT status, COUNT(*) FROM orders GROUP BY status"
    uv run python scripts/sqeual.py ask "how much did we refund last month" --dry-run

Every command takes `--config <path>`, defaulting to `./sqeual.toml`.

`ask` is one of three commands that call a model (`ask`, `ask-target`, `eval`),
and `--dry-run` makes every one of them offline: no key, no network, no money.
Its four exit codes are NOT the other commands' four — see `sqeual/cli_ask.py`.

The logic lives in `sqeual.cli` so the test suite can import and exercise it
directly. This file only wires the command line to it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqeual.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

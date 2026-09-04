#!/usr/bin/env python
"""Command line entry point.

    uv run python scripts/sqeual.py db build
    uv run python scripts/sqeual.py schema show
    uv run python scripts/sqeual.py schema slice --question "how much did we refund in Berlin"
    uv run python scripts/sqeual.py guard --sql "SELECT COUNT(*) FROM orders"
    uv run python scripts/sqeual.py run --sql "SELECT status, COUNT(*) FROM orders GROUP BY status"

Every command takes `--config <path>`, defaulting to `./sqeual.toml`.

The logic lives in `sqeual.cli` so the test suite can import and exercise it
directly. This file only wires the command line to it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqeual.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

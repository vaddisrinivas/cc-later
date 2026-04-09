#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2.0", "pydantic-settings>=2.0", "filelock>=3.0", "pendulum>=3.0"]
# ///
"""cc-later dashboard — generate and open the dashboard in your browser."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from cc_later.dashboard import run_dashboard


if __name__ == "__main__":
    raise SystemExit(run_dashboard())

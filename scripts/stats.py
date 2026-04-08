#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2.0", "pydantic-settings>=2.0", "filelock>=3.0", "pendulum>=3.0"]
# ///
"""cc-later stats — token analytics with per-model cost breakdown.

Usage:
    python3 stats.py              # default: 7d and 30d
    python3 stats.py 60           # custom single range
    python3 stats.py 7 30 90      # multiple ranges
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from cc_later.core import run_stats


if __name__ == "__main__":
    ranges = []
    for arg in sys.argv[1:]:
        try:
            ranges.append(int(arg))
        except ValueError:
            pass
    if not ranges:
        ranges = [7, 30]
    for i, days in enumerate(ranges):
        if i > 0:
            print("\n" + "─" * 50 + "\n")
        run_stats(days=days)
    raise SystemExit(0)

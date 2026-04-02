#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later status command — thin shim into cc_later.cli."""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from cc_later.cli import cmd_status


def main() -> int:
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later capture hook — appends key-phrase entries to LATER.md.

Triggered by UserPromptSubmit when the user types phrases like:
  later: fix the N+1 query in ReportGenerator
  add to later: update README install steps
  note for later: UserService.delete() swallows exceptions
  queue for later: add rate limiting to /refresh endpoint
  later[!]: SQL injection risk in filter builder   ← [!] marks urgent
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Matches any key-phrase variant followed by an optional [!] priority flag
# and a required colon separator, then captures the task text.
#
# Supported triggers:
#   later:                  add to later:       add this to later:
#   note for later:         note this for later:
#   queue for later:        queue this for later:
#   for later:
#
# All variants accept an optional [!] before the colon:
#   later[!]: ...  →  [!] priority entry
CAPTURE_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"add\s+(?:this\s+)?to\s+later"
    r"|note\s+(?:this\s+)?for\s+later"
    r"|queue\s+(?:this\s+)?for\s+later"
    r"|for\s+later"
    r"|later"
    r")"
    r"\s*(\[!\])?\s*:\s*"
    r"(.+?)(?=\n|$)",
    re.IGNORECASE,
)


def _repo_root() -> Path:
    """Return the git repo root, falling back to cwd."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.cwd()


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    matches = list(CAPTURE_RE.finditer(prompt))
    if not matches:
        return 0

    repo = _repo_root()
    later_path = repo / ".claude" / "LATER.md"
    later_path.parent.mkdir(parents=True, exist_ok=True)

    existing = later_path.read_text(encoding="utf-8") if later_path.exists() else "# LATER\n"
    if not existing.endswith("\n"):
        existing += "\n"

    added: list[str] = []
    for match in matches:
        priority_flag = match.group(1)  # "[!]" or None
        text = match.group(2).strip().rstrip(".")
        if not text or len(text) < 3:
            continue

        # Skip duplicates — check if this exact text is already in the file
        if text.lower() in existing.lower():
            continue

        marker = "[!]" if priority_flag else "[ ]"
        existing += f"- {marker} {text}\n"
        added.append(text)

    if added:
        later_path.write_text(existing, encoding="utf-8")
        for text in added:
            print(f"[cc-later] Queued for later: {text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

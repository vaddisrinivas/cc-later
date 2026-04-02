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
  later[!]: SQL injection risk in filter builder   <- [!] marks urgent
  later: Task B (after: t_abc123)                  <- dependency chain
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Matches any key-phrase variant followed by an optional [!] priority flag
# and a required colon separator, then captures the task text.
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
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.cwd()


def _find_section(text: str) -> str | None:
    """Auto-detect which LATER.md section a task belongs to."""
    text_lower = text.lower()
    if any(w in text_lower for w in ("injection", "xss", "auth bypass", "credential", "secret", "vulnerability")):
        return "Security"
    if any(w in text_lower for w in ("bug", "crash", "error", "exception", "failure", "broken")):
        return "Bugs"
    if any(w in text_lower for w in ("test", "coverage", "assert", "spec")):
        return "Tests"
    if any(w in text_lower for w in ("readme", "doc", "docstring", "changelog", "comment")):
        return "Docs"
    if any(w in text_lower for w in ("refactor", "cleanup", "dead code", "unused", "rename", "type hint")):
        return "Refactor"
    if any(w in text_lower for w in ("audit", "report", "analyze", "survey")):
        return "Reports"
    return None


def _insert_under_section(content: str, section: str, entry_line: str) -> str:
    """Insert an entry under the matching ## section, or append to end."""
    lines = content.splitlines()
    section_header = f"## {section}"

    # Find the section
    for i, line in enumerate(lines):
        if line.strip() == section_header:
            # Find the end of this section (next ## or end of file)
            insert_at = i + 1
            while insert_at < len(lines):
                if lines[insert_at].startswith("## "):
                    break
                if lines[insert_at].strip():
                    insert_at += 1
                    continue
                insert_at += 1
            # Insert before the next section (or at end of section content)
            # Back up past trailing blank lines
            actual = insert_at
            while actual > i + 1 and not lines[actual - 1].strip():
                actual -= 1
            lines.insert(actual, entry_line)
            result = "\n".join(lines)
            if not result.endswith("\n"):
                result += "\n"
            return result

    # Section not found — append at end
    if not content.endswith("\n"):
        content += "\n"
    content += f"\n{section_header}\n{entry_line}\n"
    return content


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
        priority_flag = match.group(1)
        text = match.group(2).strip().rstrip(".")
        if not text or len(text) < 3:
            continue

        # Skip duplicates
        if text.lower() in existing.lower():
            continue

        marker = "[!]" if priority_flag else "[ ]"
        entry_line = f"- {marker} {text}"
        section = _find_section(text)

        if section:
            existing = _insert_under_section(existing, section, entry_line)
        else:
            existing += f"{entry_line}\n"
        added.append(text)

    if added:
        later_path.write_text(existing, encoding="utf-8")
        for text in added:
            section = _find_section(text)
            section_info = f" [{section}]" if section else ""
            print(f"[cc-later] Queued{section_info}: {text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

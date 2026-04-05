"""LATER.md parsing, entry management, completion marking, and rotation."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from .models import ConfigError, LaterEntry

TASK_LINE_PATTERN = re.compile(r"^(\s*-\s*)\[(.)\](\s+)(.+)$")
SECTION_PATTERN = re.compile(r"^##\s+(.+)$")
RESULT_LINE_PATTERN = re.compile(
    r"^(DONE|SKIPPED|NEEDS_HUMAN|FAILED)(?:\s+\([^)]+\))?\s+([A-Za-z0-9_-]+)\s*:"
)
# Metadata comment embedded in LATER.md for retry tracking
META_PATTERN = re.compile(
    r"<!--\s*cc-later:\s*attempts=(\d+)(?:,\s*last=([^\s]+))?(?:,\s*depends=([^\s]+))?\s*-->"
)
DEPENDENCY_PATTERN = re.compile(r"\(after:\s*(t_[A-Za-z0-9]+)\)\s*$")


def parse_later_entries(
    content: str,
    priority_marker: str = "[!]",
) -> list[LaterEntry]:
    """Parse pending entries from LATER.md, tracking ## section headers and metadata."""
    entries: list[LaterEntry] = []
    priority_char = _extract_marker_char(priority_marker)
    current_section: str | None = None
    lines = content.splitlines()

    for idx, line in enumerate(lines):
        section_match = SECTION_PATTERN.match(line)
        if section_match:
            current_section = section_match.group(1).strip()
            continue

        match = TASK_LINE_PATTERN.match(line)
        if not match:
            continue

        marker = match.group(2)
        text = match.group(4).strip()
        if not text:
            continue

        # Skip completed and needs-human entries
        if marker in {"x", "X"}:
            continue
        if marker == "?":
            continue

        if marker == " ":
            is_priority = False
        elif marker == priority_char:
            is_priority = True
        else:
            continue

        # Extract retry metadata from next line (if present)
        attempts = 0
        last_attempt = None
        depends_on = None
        if idx + 1 < len(lines):
            meta_match = META_PATTERN.search(lines[idx + 1])
            if meta_match:
                attempts = int(meta_match.group(1))
                last_attempt = meta_match.group(2)
                depends_on = meta_match.group(3)

        # Extract inline dependency
        dep_match = DEPENDENCY_PATTERN.search(text)
        if dep_match:
            depends_on = dep_match.group(1)
            text = text[:dep_match.start()].strip()

        task_id = stable_task_id(idx, text)
        entries.append(
            LaterEntry(
                id=task_id,
                text=text,
                is_priority=is_priority,
                line_index=idx,
                raw_line=line,
                section=current_section,
                attempts=attempts,
                last_attempt=last_attempt,
                depends_on=depends_on,
            )
        )
    return entries


def select_entries(
    entries: list[LaterEntry],
    max_entries: int,
    completed_ids: set[str] | None = None,
) -> list[LaterEntry]:
    """Select entries for dispatch, respecting priority, dependencies, and retry state."""
    if max_entries <= 0:
        return []

    completed = completed_ids or set()

    # Filter out entries whose dependencies haven't been met
    eligible = []
    for e in entries:
        if e.depends_on and e.depends_on not in completed:
            continue
        eligible.append(e)

    ordered = sorted(eligible, key=lambda e: (0 if e.is_priority else 1, e.line_index))
    return ordered[:max_entries]


def parse_result_summary(text: str) -> dict[str, str]:
    """Parse DONE/SKIPPED/NEEDS_HUMAN/FAILED lines keyed by task id."""
    output: dict[str, str] = {}
    for candidate_text in _extract_text_blobs(text):
        for line in candidate_text.splitlines():
            match = RESULT_LINE_PATTERN.match(line.strip())
            if not match:
                continue
            status = match.group(1)
            task_id = match.group(2)
            output[task_id] = status
    return output


def apply_completion(
    content: str,
    done_ids: set[str],
    dispatched_entries: list[LaterEntry],
    mark_mode: str,
) -> str:
    """Mark completed entries in LATER.md content."""
    original_lines = content.splitlines()
    targets = [entry for entry in dispatched_entries if entry.id in done_ids]
    if not targets:
        return content

    resolved: list[int] = []
    used_indexes: set[int] = set()
    for entry in targets:
        resolved_index = _resolve_entry_line_index(original_lines, entry, used_indexes)
        if resolved_index is None:
            continue
        resolved.append(resolved_index)
        used_indexes.add(resolved_index)

    if not resolved:
        return content

    lines = list(original_lines)
    if mark_mode == "delete":
        for idx in sorted(resolved, reverse=True):
            if 0 <= idx < len(lines):
                # Also remove metadata comment on next line if present
                if idx + 1 < len(lines) and META_PATTERN.search(lines[idx + 1]):
                    lines.pop(idx + 1)
                lines.pop(idx)
    elif mark_mode == "check":
        for idx in resolved:
            if 0 <= idx < len(lines):
                lines[idx] = _mark_line_done(lines[idx])
                # Remove retry metadata comment if present
                if idx + 1 < len(lines) and META_PATTERN.search(lines[idx + 1]):
                    lines.pop(idx + 1)
    else:
        raise ConfigError(f"Unsupported mark mode: {mark_mode}")

    rewritten = "\n".join(lines)
    if content.endswith("\n"):
        rewritten += "\n"
    return rewritten


def apply_retry_metadata(
    content: str,
    failed_ids: dict[str, str],
    dispatched_entries: list[LaterEntry],
    max_attempts: int,
    escalate_to_priority: bool,
    now_iso: str,
) -> str:
    """Update retry metadata for failed entries. Escalate if max attempts reached."""
    lines = content.splitlines()
    used_indexes: set[int] = set()

    for entry in dispatched_entries:
        if entry.id not in failed_ids:
            continue

        idx = _resolve_entry_line_index(lines, entry, used_indexes)
        if idx is None:
            continue
        used_indexes.add(idx)

        new_attempts = entry.attempts + 1
        meta_line = f"  <!-- cc-later: attempts={new_attempts}, last={now_iso} -->"

        if new_attempts >= max_attempts:
            if escalate_to_priority:
                # Mark as needs-human [?]
                lines[idx] = re.sub(r"\[[ !]\]", "[?]", lines[idx], count=1)
            # Remove any existing metadata
            if idx + 1 < len(lines) and META_PATTERN.search(lines[idx + 1]):
                lines.pop(idx + 1)
        else:
            # Update or insert metadata comment
            if idx + 1 < len(lines) and META_PATTERN.search(lines[idx + 1]):
                lines[idx + 1] = meta_line
            else:
                lines.insert(idx + 1, meta_line)

    rewritten = "\n".join(lines)
    if content.endswith("\n"):
        rewritten += "\n"
    return rewritten


def rotate_later_if_needed(later_path: Path, now_local: datetime) -> bool:
    """Archive LATER.md if it's from a previous day. Returns True if rotated."""
    if not later_path.exists():
        return False
    try:
        mtime_ts = later_path.stat().st_mtime
    except OSError:
        return False
    if now_local.tzinfo is not None:
        mtime_date = datetime.fromtimestamp(mtime_ts, tz=now_local.tzinfo).date()
    else:
        mtime_date = datetime.fromtimestamp(mtime_ts).date()
    today = now_local.date()
    if mtime_date >= today:
        return False

    archive_name = f"LATER-{mtime_date.isoformat()}.md"
    archive_path = later_path.parent / archive_name
    content = _safe_read(later_path)
    if content is None:
        return False

    try:
        archive_path.write_text(content, encoding="utf-8")
        fresh = extract_pending_for_rotation(content)
        later_path.write_text(fresh, encoding="utf-8")
    except OSError:
        return False
    return True


def extract_pending_for_rotation(content: str) -> str:
    """Rebuild LATER.md keeping only pending entries ([ ] and [!]), preserving ## sections."""
    lines = content.splitlines()
    out: list[str] = []
    pending_in_current_section: list[str] = []
    current_section_header: str | None = None

    def flush_section() -> None:
        if pending_in_current_section:
            if current_section_header is not None:
                out.append(current_section_header)
                out.append("")
            out.extend(pending_in_current_section)
            out.append("")

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            flush_section()
            pending_in_current_section = []
            current_section_header = None
            out.append(line)
            continue
        if SECTION_PATTERN.match(line):
            flush_section()
            pending_in_current_section = []
            current_section_header = line
            continue
        task_match = TASK_LINE_PATTERN.match(line)
        if task_match:
            marker = task_match.group(2)
            if marker not in {"x", "X"}:
                pending_in_current_section.append(line)
        # Preserve metadata comments attached to pending tasks
        elif META_PATTERN.search(line) and pending_in_current_section:
            pending_in_current_section.append(line)

    flush_section()
    result = "\n".join(out).strip()
    return result + "\n" if result else "# LATER\n"


def estimate_complexity(entry: LaterEntry) -> int:
    """Score task complexity 1-5 for model routing decisions.

    Heuristics:
      - Verb weight: audit/fix/refactor = higher, check/remove/update = lower
      - Multi-file references bump score
      - Security section entries get +1
      - Longer descriptions suggest more complexity
    """
    text_lower = entry.text.lower()
    score = 2  # baseline

    # Verb analysis
    high_verbs = ("audit", "refactor", "fix", "migrate", "redesign", "rewrite")
    low_verbs = ("check", "remove", "delete", "update", "add", "rename")
    first_word = text_lower.split()[0] if text_lower.split() else ""
    if first_word in high_verbs:
        score += 1
    elif first_word in low_verbs:
        score -= 1

    # Multi-file references
    file_refs = len(re.findall(r"\b[\w/]+\.\w{1,5}\b", entry.text))
    if file_refs >= 2:
        score += 1

    # Section weight
    if entry.section and entry.section.lower() in ("security", "bugs"):
        score += 1

    # Length heuristic
    if len(entry.text) > 120:
        score += 1

    # Priority flag
    if entry.is_priority:
        score += 1

    return max(1, min(5, score))


def route_model(entry: LaterEntry, default_model: str, routing: str) -> str:
    """Pick model for a task based on complexity routing."""
    if routing == "fixed":
        return default_model
    complexity = estimate_complexity(entry)
    if complexity >= 4:
        return "opus"
    if complexity <= 2:
        return "haiku"
    return "sonnet"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def stable_task_id(line_index: int, text: str) -> str:
    digest = hashlib.sha1(f"{line_index}|{text}".encode("utf-8")).hexdigest()[:10]
    return f"t_{digest}"


def _extract_marker_char(marker: str) -> str:
    marker = marker.strip()
    if len(marker) == 3 and marker.startswith("[") and marker.endswith("]"):
        return marker[1]
    raise ConfigError(f"Invalid priority marker format: {marker}")


def _resolve_entry_line_index(
    lines: list[str], entry: LaterEntry, used_indexes: set[int]
) -> int | None:
    if (
        0 <= entry.line_index < len(lines)
        and entry.line_index not in used_indexes
        and _line_text_matches(lines[entry.line_index], entry.text)
    ):
        return entry.line_index

    for idx, line in enumerate(lines):
        if idx in used_indexes:
            continue
        if _line_text_matches(line, entry.text):
            return idx
    return None


def _line_text_matches(line: str, expected_text: str) -> bool:
    parsed = TASK_LINE_PATTERN.match(line)
    if not parsed:
        return False
    marker = parsed.group(2)
    if marker in {"x", "X"}:
        return False
    text = parsed.group(4).strip()
    # Strip dependency suffix for matching
    dep = DEPENDENCY_PATTERN.search(text)
    if dep:
        text = text[:dep.start()].strip()
    return text == expected_text


def _mark_line_done(line: str) -> str:
    return TASK_LINE_PATTERN.sub(r"\1[x]\3\4", line, count=1)


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _extract_text_blobs(raw_text: str) -> list[str]:
    import json
    blobs = [raw_text]
    raw_text = raw_text.strip()
    if not raw_text:
        return blobs
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return blobs

    extracted: list[str] = []
    _walk_json_for_text(payload, extracted)
    if extracted:
        blobs.extend(extracted)
    return blobs


def _walk_json_for_text(node: object, sink: list[str]) -> None:
    if isinstance(node, str):
        sink.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _walk_json_for_text(value, sink)
    elif isinstance(node, list):
        for value in node:
            _walk_json_for_text(value, sink)

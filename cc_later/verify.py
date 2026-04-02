"""Post-dispatch verification pipeline.

Checks whether a completed dispatch actually accomplished its task
before blindly marking it DONE.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import LaterEntry, VerifyConfig


@dataclass
class VerifyResult:
    task_id: str
    confidence: str  # "high" | "medium" | "low" | "none"
    reason: str
    files_changed: list[str]


# Minimum output length (chars) to consider a result substantive
MIN_SUBSTANTIVE_LENGTH = 80

# Patterns that indicate the agent actually worked on the task
WORK_SIGNALS = [
    re.compile(r"(?:modified|edited|updated|fixed|added|removed|changed)\b", re.I),
    re.compile(r"(?:found|identified|discovered|detected)\b", re.I),
    re.compile(r"(?:the\s+(?:issue|bug|problem|vulnerability)\s+(?:is|was))\b", re.I),
    re.compile(r"\b(?:line\s+\d+|L\d+)\b"),
    re.compile(r"`[^`]+\.\w{1,5}`"),  # file references in backticks
]

# Patterns that indicate the agent punted
PUNT_SIGNALS = [
    re.compile(r"(?:I\s+(?:cannot|can't|couldn't|am unable))\b", re.I),
    re.compile(r"(?:unable to (?:find|locate|access|determine))\b", re.I),
    re.compile(r"(?:would need|requires? (?:more|additional|further))\b", re.I),
    re.compile(r"(?:I don't have (?:access|enough))\b", re.I),
]


def verify_result(
    task_id: str,
    entry: LaterEntry,
    result_text: str,
    repo_path: Path,
    config: VerifyConfig,
    allow_file_writes: bool,
) -> VerifyResult:
    """Verify a dispatch result meets quality thresholds."""
    files_changed: list[str] = []

    # Check for actual file modifications if writes were enabled
    if allow_file_writes and config.require_diff:
        files_changed = _get_changed_files(repo_path)

    confidence = _score_confidence(result_text, entry, files_changed, allow_file_writes)
    reason = _explain_confidence(confidence, result_text, files_changed, allow_file_writes)

    return VerifyResult(
        task_id=task_id,
        confidence=confidence,
        reason=reason,
        files_changed=files_changed,
    )


def passes_threshold(result: VerifyResult, min_confidence: str) -> bool:
    """Check if a verification result meets the minimum confidence threshold."""
    levels = {"none": 0, "low": 1, "medium": 2, "high": 3}
    return levels.get(result.confidence, 0) >= levels.get(min_confidence, 0)


def _score_confidence(
    text: str,
    entry: LaterEntry,
    files_changed: list[str],
    allow_file_writes: bool,
) -> str:
    """Score result confidence: high, medium, low, or none."""
    if not text or len(text.strip()) < 20:
        return "none"

    score = 0

    # Length check — substantive responses are longer
    if len(text) >= MIN_SUBSTANTIVE_LENGTH:
        score += 1
    if len(text) >= MIN_SUBSTANTIVE_LENGTH * 3:
        score += 1

    # Work signals — did the agent describe doing actual work?
    work_hits = sum(1 for p in WORK_SIGNALS if p.search(text))
    if work_hits >= 3:
        score += 2
    elif work_hits >= 1:
        score += 1

    # Punt signals — did the agent give up?
    punt_hits = sum(1 for p in PUNT_SIGNALS if p.search(text))
    if punt_hits >= 2:
        score -= 2
    elif punt_hits >= 1:
        score -= 1

    # Task-specific: does the result reference the target from the entry?
    # Extract key terms from entry text
    key_terms = _extract_key_terms(entry.text)
    term_hits = sum(1 for term in key_terms if term.lower() in text.lower())
    if term_hits >= 2:
        score += 1

    # File changes (only relevant when writes are enabled)
    if allow_file_writes and files_changed:
        score += 2

    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    if score >= 1:
        return "low"
    return "none"


def _explain_confidence(
    confidence: str,
    text: str,
    files_changed: list[str],
    allow_file_writes: bool,
) -> str:
    """Generate a human-readable explanation for the confidence score."""
    if confidence == "high":
        parts = ["Result contains detailed analysis"]
        if files_changed:
            parts.append(f"modified {len(files_changed)} file(s)")
        return "; ".join(parts)
    if confidence == "medium":
        return "Result appears substantive but could not fully verify"
    if confidence == "low":
        if allow_file_writes and not files_changed:
            return "No file changes detected despite write permission"
        return "Result is brief or lacks specifics"
    return "Result appears empty or the agent was unable to complete the task"


def _extract_key_terms(text: str) -> list[str]:
    """Extract meaningful terms from a task description."""
    # Remove common filler words
    stop = {
        "the", "a", "an", "in", "to", "for", "of", "and", "or", "is", "are",
        "was", "were", "be", "been", "has", "have", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "all", "this", "that",
    }
    words = re.findall(r"\b\w+\b", text)
    return [w for w in words if w.lower() not in stop and len(w) > 2]


def _get_changed_files(repo_path: Path) -> list[str]:
    """Get list of files changed in the repo working tree."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []

"""Dispatch prompt rendering — the brain behind background task quality."""

from __future__ import annotations

import re
from pathlib import Path

from .models import AppConfig, ConfigError, LaterEntry


def render_prompt(repo_path: Path, cfg: AppConfig, entries: list[LaterEntry]) -> str:
    """Render the dispatch prompt for a set of LATER entries.

    If a custom template is configured, use that. Otherwise use the built-in
    prompt which is designed to maximize background agent success rate.
    """
    replacements = _build_replacements(repo_path, cfg, entries)

    if cfg.dispatch.prompt_template:
        return _render_custom_template(cfg, replacements)

    return _render_builtin_prompt(repo_path, cfg, entries, replacements)


def _build_replacements(
    repo_path: Path,
    cfg: AppConfig,
    entries: list[LaterEntry],
) -> dict[str, str]:
    """Build template variable replacements."""
    sections: dict[str, list[LaterEntry]] = {}
    for entry in entries:
        key = entry.section or ""
        sections.setdefault(key, []).append(entry)

    blocks: list[str] = []
    for section_name, section_entries in sections.items():
        if section_name:
            blocks.append(f"## {section_name}")
        for entry in section_entries:
            blocks.append(f"- {entry.id}: {entry.text}")
    entry_block = "\n".join(blocks)

    if cfg.dispatch.allow_file_writes:
        write_instruction = (
            f"You MAY edit files in this repository. "
            f"Maximum {cfg.dispatch.max_files_written_per_task} files per task. "
            f"Stay within the repo root. Do not create new directories unless necessary."
        )
    else:
        write_instruction = (
            "You are in READ-ONLY mode. Do NOT modify any files. "
            "Report your findings, analysis, and proposed fixes — but make no changes."
        )

    return {
        "repo_path": str(repo_path),
        "repo_name": repo_path.name,
        "entries": entry_block,
        "max_files": str(cfg.dispatch.max_files_written_per_task),
        "write_instruction": write_instruction,
        "task_count": str(len(entries)),
    }


def _render_custom_template(cfg: AppConfig, replacements: dict[str, str]) -> str:
    """Render a user-provided prompt template."""
    from .paths import CONFIG_PATH
    template_path = Path(cfg.dispatch.prompt_template).expanduser()
    if not template_path.is_absolute():
        template_path = CONFIG_PATH.parent / template_path
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read prompt template: {exc}") from exc
    try:
        return template_text.format(**replacements)
    except KeyError as exc:
        raise ConfigError(f"Unknown placeholder in prompt template: {exc}") from exc


def _render_builtin_prompt(
    repo_path: Path,
    cfg: AppConfig,
    entries: list[LaterEntry],
    replacements: dict[str, str],
) -> str:
    """The built-in dispatch prompt — engineered for maximum background agent success."""
    entry_block = replacements["entries"]
    write_instruction = replacements["write_instruction"]
    max_files = cfg.dispatch.max_files_written_per_task

    # Build per-task instruction blocks with context hints
    task_blocks = []
    for entry in entries:
        hints = _generate_task_hints(entry)
        block = f"### Task {entry.id}\n{entry.text}"
        if hints:
            block += f"\n*Hints: {hints}*"
        task_blocks.append(block)

    task_section = "\n\n".join(task_blocks)

    return f"""You are a background maintenance agent for the repository at `{repo_path}`.

You were dispatched automatically by cc-later to handle queued maintenance tasks.
You have no interactive user — work autonomously and be thorough.

## Your Tasks

{task_section}

## Operating Rules

1. **Scope**: {write_instruction}
2. **Precision**: Be surgical. Only touch code directly relevant to each task. Do not refactor unrelated code, add comments to unchanged functions, or "improve" things not in your task list.
3. **Evidence**: Base every finding on actual code you read. Never speculate about what might be wrong — locate the specific file, function, and line.
4. **File limit**: Maximum {max_files} files modified per task (if writes enabled).
5. **Independence**: Each task is independent. Complete as many as possible even if one fails.

## How to Work

For each task:
1. **Locate**: Find the relevant files and code using grep, glob, or read.
2. **Analyze**: Understand the current state and what needs to change.
3. **Act**: Make the change (if writes enabled) or document your findings.
4. **Verify**: If you made changes, re-read the modified file to confirm correctness.

## Output Format

After completing all tasks, output a summary section with EXACTLY one line per task:

```
DONE <task_id>: <brief description of what was done>
SKIPPED (<reason>) <task_id>: <task text>
NEEDS_HUMAN (<reason>) <task_id>: <task text>
FAILED (<reason>) <task_id>: <task text>
```

Rules:
- Every task MUST have a corresponding output line. Never silently omit a task.
- Use DONE only when you actually completed the work (not just "looked at it").
- Use SKIPPED when the task is no longer relevant (already fixed, code doesn't exist).
- Use NEEDS_HUMAN when the task requires decisions, credentials, or interactive input.
- Use FAILED when you attempted but couldn't complete (with specific reason).
- The <task_id> must exactly match the ID from the task list above.
"""


def _generate_task_hints(entry: LaterEntry) -> str:
    """Generate contextual hints to help the background agent succeed."""
    hints: list[str] = []

    text_lower = entry.text.lower()

    # File path hints
    file_refs = re.findall(r"[\w/.-]+\.\w{1,5}", entry.text)
    if file_refs:
        hints.append(f"Start by reading: {', '.join(f'`{f}`' for f in file_refs[:3])}")

    # Verb-based strategy hints
    first_word = text_lower.split()[0] if text_lower.split() else ""
    if first_word == "audit":
        hints.append("Read the target thoroughly, then report findings")
    elif first_word == "fix":
        hints.append("Locate the bug first, understand root cause, then fix")
    elif first_word in ("add", "update"):
        hints.append("Check existing patterns in the codebase for consistency")
    elif first_word == "remove":
        hints.append("Verify the target is truly unused before removing")
    elif first_word in ("check", "verify"):
        hints.append("Compare current state against expected state")

    # Section-based hints
    if entry.section:
        sec = entry.section.lower()
        if sec == "security":
            hints.append("Treat as high-priority; be thorough")
        elif sec == "tests":
            hints.append("Follow existing test patterns in the repo")
        elif sec == "docs":
            hints.append("Check what changed recently with git log")

    return "; ".join(hints)


def resolve_output_path(template: str, repo_path: Path, now_utc: "datetime") -> Path:
    """Expand the output path template."""
    from datetime import datetime
    from .paths import APP_DIR

    repo_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_path.name) or "repo"
    date_slug = now_utc.strftime("%Y%m%d-%H%M%S")
    rendered = template.format(repo=repo_slug, date=date_slug)
    result = Path(rendered).expanduser()
    if not result.is_absolute():
        result = APP_DIR / result
    result.parent.mkdir(parents=True, exist_ok=True)
    return result

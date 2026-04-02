"""Rich completion reports and digest generation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .analytics import AnalyticsDB
from .models import LaterEntry
from .verify import VerifyResult


def generate_dispatch_report(
    repo_path: Path,
    entries: list[LaterEntry],
    results: dict[str, str],
    verify_results: dict[str, VerifyResult] | None = None,
    model: str = "sonnet",
    duration_s: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> str:
    """Generate a markdown report for a single dispatch cycle."""
    now = datetime.now(timezone.utc)
    repo_name = repo_path.name

    lines = [
        f"# cc-later Dispatch Report",
        f"",
        f"**Repo:** {repo_name}",
        f"**Date:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Model:** {model}",
    ]

    if duration_s is not None:
        lines.append(f"**Duration:** {duration_s:.1f}s")
    if input_tokens or output_tokens:
        total = input_tokens + output_tokens
        lines.append(f"**Tokens:** {input_tokens:,} in / {output_tokens:,} out ({total:,} total)")

    lines.append("")

    # Task results
    done = [e for e in entries if results.get(e.id) == "DONE"]
    failed = [e for e in entries if results.get(e.id) in ("FAILED", "NEEDS_HUMAN")]
    skipped = [e for e in entries if results.get(e.id) == "SKIPPED"]
    unknown = [e for e in entries if e.id not in results]

    if done:
        lines.append("## Completed")
        lines.append("")
        for e in done:
            vr = verify_results.get(e.id) if verify_results else None
            confidence = f" [{vr.confidence}]" if vr else ""
            lines.append(f"- **{e.id}**: {e.text}{confidence}")
            if vr and vr.files_changed:
                for f in vr.files_changed:
                    lines.append(f"  - Modified: `{f}`")
        lines.append("")

    if failed:
        lines.append("## Failed / Needs Human")
        lines.append("")
        for e in failed:
            status = results.get(e.id, "UNKNOWN")
            lines.append(f"- **{e.id}** ({status}): {e.text}")
            if e.attempts > 0:
                lines.append(f"  - Attempt {e.attempts + 1}")
        lines.append("")

    if skipped:
        lines.append("## Skipped")
        lines.append("")
        for e in skipped:
            lines.append(f"- **{e.id}**: {e.text}")
        lines.append("")

    if unknown:
        lines.append("## No Result")
        lines.append("")
        for e in unknown:
            lines.append(f"- **{e.id}**: {e.text}")
        lines.append("")

    # Summary
    total = len(entries)
    lines.append("---")
    lines.append(f"**Summary:** {len(done)}/{total} completed, "
                 f"{len(failed)} failed, {len(skipped)} skipped")

    return "\n".join(lines) + "\n"


def save_report(repo_path: Path, report: str) -> Path:
    """Save a dispatch report to .claude/reports/."""
    reports_dir = repo_path / ".claude" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    date_slug = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = reports_dir / f"later-{date_slug}.md"

    # Append if report already exists for today (multiple dispatch cycles)
    if report_path.exists():
        existing = report_path.read_text(encoding="utf-8")
        report = existing + "\n---\n\n" + report

    report_path.write_text(report, encoding="utf-8")
    return report_path


def generate_stats_dashboard(db: AnalyticsDB, days: int = 30) -> str:
    """Generate a full analytics dashboard as markdown."""
    stats = db.get_stats(days=days)

    lines = [
        "## cc-later Analytics",
        "",
        f"*Last {days} days*",
        "",
        "### Overview",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Tasks dispatched | {stats.total_dispatched} |",
        f"| Completed | {stats.total_completed} |",
        f"| Failed | {stats.total_failed} |",
        f"| Needs human | {stats.total_needs_human} |",
        f"| Skipped | {stats.total_skipped} |",
        f"| Success rate | {stats.success_rate:.0%} |",
        f"| Avg duration | {stats.avg_duration_s:.1f}s |",
        f"| Current streak | {stats.streak} |",
        f"| Today | {stats.dispatches_today} |",
        f"| This week | {stats.dispatches_this_week} |",
        "",
    ]

    if stats.total_input_tokens or stats.total_output_tokens:
        total_tokens = stats.total_input_tokens + stats.total_output_tokens
        lines.extend([
            "### Token Usage",
            "",
            f"| Direction | Tokens |",
            f"|-----------|--------|",
            f"| Input | {stats.total_input_tokens:,} |",
            f"| Output | {stats.total_output_tokens:,} |",
            f"| **Total** | **{total_tokens:,}** |",
            "",
        ])

    if stats.by_model:
        lines.extend(["### By Model", ""])
        lines.append("| Model | Dispatched | Completed | Rate | Tokens |")
        lines.append("|-------|-----------|-----------|------|--------|")
        for model, ms in sorted(stats.by_model.items()):
            lines.append(
                f"| {model} | {ms.dispatched} | {ms.completed} | "
                f"{ms.success_rate:.0%} | {ms.total_tokens:,} |"
            )
        lines.append("")

    if stats.by_repo:
        lines.extend(["### By Repository", ""])
        lines.append("| Repo | Dispatched | Completed | Rate |")
        lines.append("|------|-----------|-----------|------|")
        for repo, rs in sorted(stats.by_repo.items()):
            lines.append(f"| {repo} | {rs.dispatched} | {rs.completed} | {rs.success_rate:.0%} |")
        lines.append("")

    if stats.by_section:
        lines.extend(["### By Section", ""])
        lines.append("| Section | Dispatched | Completed | Rate |")
        lines.append("|---------|-----------|-----------|------|")
        for sec, ss in sorted(stats.by_section.items()):
            lines.append(f"| {sec} | {ss.dispatched} | {ss.completed} | {ss.success_rate:.0%} |")
        lines.append("")

    # Recent activity
    recent = db.recent_dispatches(limit=10)
    if recent:
        lines.extend(["### Recent Dispatches", ""])
        lines.append("| Time | Task | Model | Status |")
        lines.append("|------|------|-------|--------|")
        for r in recent:
            ts = r["ts"][:16].replace("T", " ")
            text = r["task_text"][:50] + ("..." if len(r["task_text"]) > 50 else "")
            status = r["status"] or "in-flight"
            lines.append(f"| {ts} | {text} | {r['model']} | {status} |")
        lines.append("")

    return "\n".join(lines)

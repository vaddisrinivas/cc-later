"""SQLite analytics engine for cc-later dispatch tracking."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import DB_PATH, RUN_LOG_PATH


SCHEMA_VERSION = 1

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS dispatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    repo        TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    task_text   TEXT NOT NULL,
    section     TEXT,
    model       TEXT NOT NULL,
    status      TEXT,            -- DONE | SKIPPED | NEEDS_HUMAN | FAILED | NULL (in-flight)
    duration_s  REAL,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    attempts    INTEGER DEFAULT 1,
    result_path TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_dispatches_ts ON dispatches(ts);
CREATE INDEX IF NOT EXISTS idx_dispatches_repo ON dispatches(repo);
CREATE INDEX IF NOT EXISTS idx_dispatches_status ON dispatches(status);
"""


@dataclass
class Stats:
    total_dispatched: int
    total_completed: int
    total_failed: int
    total_needs_human: int
    total_skipped: int
    success_rate: float
    total_input_tokens: int
    total_output_tokens: int
    avg_duration_s: float
    dispatches_today: int
    dispatches_this_week: int
    by_repo: dict[str, RepoStats]
    by_section: dict[str, SectionStats]
    by_model: dict[str, ModelStats]
    streak: int  # consecutive successes


@dataclass
class RepoStats:
    dispatched: int
    completed: int
    failed: int
    success_rate: float


@dataclass
class SectionStats:
    dispatched: int
    completed: int
    success_rate: float


@dataclass
class ModelStats:
    dispatched: int
    completed: int
    failed: int
    success_rate: float
    avg_duration_s: float
    total_tokens: int


class AnalyticsDB:
    """Thin wrapper around SQLite for dispatch analytics."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(CREATE_SQL)
            self._ensure_version()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _ensure_version(self) -> None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()

    def record_dispatch(
        self,
        repo: str,
        task_id: str,
        task_text: str,
        section: str | None,
        model: str,
        attempts: int = 1,
        result_path: str | None = None,
    ) -> int:
        """Record a new dispatch. Returns the row id."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO dispatches (ts, repo, task_id, task_text, section, model, attempts, result_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, repo, task_id, task_text, section, model, attempts, result_path),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def record_outcome(
        self,
        task_id: str,
        repo: str,
        status: str,
        duration_s: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error: str | None = None,
    ) -> None:
        """Update the most recent dispatch for this task with its outcome."""
        self.conn.execute(
            """UPDATE dispatches
               SET status = ?, duration_s = ?, input_tokens = ?, output_tokens = ?, error = ?
               WHERE id = (
                   SELECT id FROM dispatches
                   WHERE task_id = ? AND repo = ? AND status IS NULL
                   ORDER BY ts DESC LIMIT 1
               )""",
            (status, duration_s, input_tokens, output_tokens, error, task_id, repo),
        )
        self.conn.commit()

    def get_stats(self, days: int = 30) -> Stats:
        """Compute aggregate stats over the given period."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        rows = self.conn.execute(
            "SELECT * FROM dispatches WHERE ts >= ?", (cutoff,)
        ).fetchall()

        total = len(rows)
        completed = sum(1 for r in rows if r["status"] == "DONE")
        failed = sum(1 for r in rows if r["status"] in ("FAILED", "NEEDS_HUMAN"))
        needs_human = sum(1 for r in rows if r["status"] == "NEEDS_HUMAN")
        skipped = sum(1 for r in rows if r["status"] == "SKIPPED")
        denominator = completed + failed
        success_rate = completed / denominator if denominator > 0 else 0.0

        durations = [r["duration_s"] for r in rows if r["duration_s"] is not None]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        today_count = sum(1 for r in rows if r["ts"][:10] == today)
        week_count = sum(1 for r in rows if r["ts"] >= week_ago)

        # By repo
        by_repo: dict[str, RepoStats] = {}
        for r in rows:
            repo = Path(r["repo"]).name if r["repo"] else "unknown"
            if repo not in by_repo:
                by_repo[repo] = RepoStats(0, 0, 0, 0.0)
            by_repo[repo].dispatched += 1
            if r["status"] == "DONE":
                by_repo[repo].completed += 1
            elif r["status"] in ("FAILED", "NEEDS_HUMAN"):
                by_repo[repo].failed += 1
        for rs in by_repo.values():
            d = rs.completed + rs.failed
            rs.success_rate = rs.completed / d if d > 0 else 0.0

        # By section
        by_section: dict[str, SectionStats] = {}
        for r in rows:
            sec = r["section"] or "Unsectioned"
            if sec not in by_section:
                by_section[sec] = SectionStats(0, 0, 0.0)
            by_section[sec].dispatched += 1
            if r["status"] == "DONE":
                by_section[sec].completed += 1
        for ss in by_section.values():
            ss.success_rate = ss.completed / ss.dispatched if ss.dispatched > 0 else 0.0

        # By model
        by_model: dict[str, ModelStats] = {}
        for r in rows:
            m = r["model"] or "unknown"
            if m not in by_model:
                by_model[m] = ModelStats(0, 0, 0, 0.0, 0.0, 0)
            by_model[m].dispatched += 1
            by_model[m].total_tokens += (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
            if r["status"] == "DONE":
                by_model[m].completed += 1
            elif r["status"] in ("FAILED", "NEEDS_HUMAN"):
                by_model[m].failed += 1
        for ms in by_model.values():
            d = ms.completed + ms.failed
            ms.success_rate = ms.completed / d if d > 0 else 0.0
            model_durations = [
                r["duration_s"] for r in rows
                if r["model"] == m and r["duration_s"] is not None
            ]
            ms.avg_duration_s = sum(model_durations) / len(model_durations) if model_durations else 0.0

        # Success streak
        streak = 0
        sorted_rows = sorted(rows, key=lambda r: r["ts"], reverse=True)
        for r in sorted_rows:
            if r["status"] == "DONE":
                streak += 1
            elif r["status"] in ("FAILED", "NEEDS_HUMAN"):
                break

        return Stats(
            total_dispatched=total,
            total_completed=completed,
            total_failed=failed,
            total_needs_human=needs_human,
            total_skipped=skipped,
            success_rate=success_rate,
            total_input_tokens=sum(r["input_tokens"] or 0 for r in rows),
            total_output_tokens=sum(r["output_tokens"] or 0 for r in rows),
            avg_duration_s=avg_duration,
            dispatches_today=today_count,
            dispatches_this_week=week_count,
            by_repo=by_repo,
            by_section=by_section,
            by_model=by_model,
            streak=streak,
        )

    def recent_dispatches(self, limit: int = 20) -> list[dict]:
        """Get recent dispatches as dicts."""
        rows = self.conn.execute(
            "SELECT * FROM dispatches ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def import_from_run_log(self) -> int:
        """One-time import: backfill analytics DB from existing run_log.jsonl."""
        if not RUN_LOG_PATH.exists():
            return 0

        imported = 0
        try:
            lines = RUN_LOG_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = entry.get("event")
            if event == "dispatch":
                repo = entry.get("repo", "")
                entries = entry.get("entries", [])
                model = entry.get("model", "sonnet")
                ts = entry.get("ts", "")
                for i, text in enumerate(entries):
                    if isinstance(text, str):
                        self.conn.execute(
                            """INSERT OR IGNORE INTO dispatches
                               (ts, repo, task_id, task_text, section, model, status)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (ts, repo, f"imported_{i}", text, None, model, "DONE"),
                        )
                        imported += 1

        self.conn.commit()
        return imported

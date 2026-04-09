"""Generate and serve a dashboard from cc-later data."""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from cc_later.dashboard_template import DASHBOARD_HTML


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


_RESULT_RE = re.compile(
    r"^(DONE|SKIPPED|NEEDS_HUMAN|FAILED)(?:\s+\([^)]+\))?\s+([A-Za-z0-9_-]+)\s*:(.*)",
    re.DOTALL,
)
_FNAME_RE = re.compile(r"^(.+?)-([^-]+)-(\d{8})-(\d{6})\.json$")
_TASK_RE = re.compile(
    r"^(?:\s*-\s*)\[(?P<mark>[ xX!])\](?:\s*)(?:(?P<prio>\(P[0-2]\))\s*)?(?P<text>.+?)\s*$"
)


def _parse_result_file(path: Path) -> dict:
    """Parse a result text file into {status, task_id, message}."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        content = ""

    if not content:
        return {"status": "EMPTY", "task_id": "", "message": ""}
    if content.lower().startswith("error:"):
        return {"status": "FAILED", "task_id": "", "message": content[6:].strip()[:200]}
    rm = _RESULT_RE.match(content)
    if rm:
        return {
            "status": rm.group(1),
            "task_id": rm.group(2),
            "message": rm.group(3).strip()[:200],
        }
    return {"status": "UNKNOWN", "task_id": "", "message": content[:200]}


def _repo_short(repo: str) -> str:
    """Extract short project name from full repo path."""
    return Path(repo).name if repo else "unknown"


def _build_dispatches(run_log: list[dict], results_dir: Path) -> list[dict]:
    """
    Join dispatch events from run_log with their result files.
    Returns list sorted by ts descending.
    """
    # Build result map: basename -> parsed result
    result_cache: dict[str, dict] = {}
    if results_dir.exists():
        for f in results_dir.glob("*.json"):
            result_cache[f.name] = _parse_result_file(f)

    dispatches = []
    for e in run_log:
        if e.get("event") != "dispatch":
            continue
        result_path = e.get("result_path", "")
        result_fname = Path(result_path).name if result_path else ""
        result = result_cache.get(result_fname, {"status": "PENDING", "task_id": "", "message": ""})

        # Parse timestamp from filename for display
        m = _FNAME_RE.match(result_fname)
        file_ts = ""
        if m:
            d, t = m.group(3), m.group(4)
            file_ts = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"

        repo = e.get("repo", "")
        dispatches.append({
            "repo": repo,
            "repo_short": _repo_short(repo),
            "section": e.get("section") or "default",
            "ts": e.get("ts", file_ts),
            "entries": e.get("entries", []),
            "entries_dispatched": e.get("entries_dispatched", 0),
            "model": e.get("model", ""),
            "remaining_minutes": e.get("remaining_minutes"),
            "result_fname": result_fname,
            "result_status": result["status"],
            "result_message": result["message"],
            "auto_resume": e.get("auto_resume", False),
        })

    dispatches.sort(key=lambda d: d["ts"], reverse=True)
    return dispatches


def _build_projects(dispatches: list[dict]) -> list[dict]:
    """
    Summarize per-repo: last dispatch, success rate, recent history.
    """
    by_repo: dict[str, dict] = {}
    for d in dispatches:
        repo = d["repo"]
        if repo not in by_repo:
            by_repo[repo] = {
                "repo": repo,
                "repo_short": d["repo_short"],
                "last_dispatch_ts": d["ts"],
                "dispatches": [],
            }
        by_repo[repo]["dispatches"].append(d)

    projects = []
    for repo, info in by_repo.items():
        dd = info["dispatches"]
        total = len(dd)
        done = sum(1 for d in dd if d["result_status"] == "DONE")
        failed = sum(1 for d in dd if d["result_status"] in ("FAILED", "UNKNOWN"))
        rate = round(done / total * 100) if total else 0
        projects.append({
            "repo": repo,
            "repo_short": info["repo_short"],
            "last_dispatch_ts": info["last_dispatch_ts"],
            "total_dispatches": total,
            "success_rate": rate,
            "done": done,
            "failed": failed,
            "recent": dd[:5],  # last 5 dispatches
        })

    projects.sort(key=lambda p: p["last_dispatch_ts"], reverse=True)
    return projects


def _parse_later_md(path: Path) -> list[dict]:
    if not path.exists():
        return []
    tasks = []
    current_section = "default"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current_section = line[3:].strip()
            continue
        if line.startswith("#"):
            continue
        m = _TASK_RE.match(line)
        if m:
            mark = m.group("mark")
            tasks.append({
                "section": current_section,
                "priority": m.group("prio") or "(P1)",
                "text": m.group("text"),
                "done": mark.lower() == "x",
            })
    return tasks


def _find_later_md_paths(run_log: list[dict], cwd: Path) -> list[tuple[str, Path]]:
    """Return (repo_short, later_md_path) for all repos seen in run_log."""
    repos_seen: list[str] = []
    seen_set: set[str] = set()
    for e in run_log:
        repo = e.get("repo", "")
        if repo and repo not in seen_set:
            seen_set.add(repo)
            repos_seen.append(repo)

    results = []
    # Always include cwd
    cwd_later = cwd / ".claude" / "LATER.md"
    cwd_str = str(cwd)
    if cwd_str not in seen_set:
        results.append((_repo_short(cwd_str), cwd_later))

    for repo in repos_seen:
        p = Path(repo) / ".claude" / "LATER.md"
        results.append((_repo_short(repo), p))

    return results


def generate_dashboard(app_dir: Path | None = None, cwd: Path | None = None) -> str:
    """Generate dashboard HTML with embedded data."""
    from cc_later.core import app_dir as _app_dir

    app_dir = app_dir or _app_dir()
    cwd = cwd or Path.cwd()

    state = _load_json(app_dir / "state.json")
    run_log = _load_jsonl(app_dir / "run_log.jsonl")
    dispatches = _build_dispatches(run_log, app_dir / "results")
    projects = _build_projects(dispatches)

    # Gather all later.md files across repos
    later_by_repo: list[dict] = []
    for repo_short, later_path in _find_later_md_paths(run_log, cwd):
        tasks = _parse_later_md(later_path)
        if tasks or later_path.exists():
            later_by_repo.append({
                "repo_short": repo_short,
                "path": str(later_path),
                "tasks": tasks,
            })

    # Fallback: just cwd
    if not later_by_repo:
        tasks = _parse_later_md(cwd / ".claude" / "LATER.md")
        later_by_repo = [{"repo_short": _repo_short(str(cwd)), "path": str(cwd / ".claude" / "LATER.md"), "tasks": tasks}]

    all_tasks = [t for r in later_by_repo for t in r["tasks"]]

    window_info = None
    try:
        from cc_later.core import load_config, compute_window_state, resolve_jsonl_roots
        cfg = load_config()
        now = datetime.now(timezone.utc)
        roots = resolve_jsonl_roots(cfg)
        if roots:
            ws = compute_window_state(roots, now, window_duration=cfg.window.duration_minutes)
            if ws:
                window_info = {
                    "elapsed_minutes": ws.elapsed_minutes,
                    "remaining_minutes": ws.remaining_minutes,
                    "duration_minutes": cfg.window.duration_minutes,
                    "trigger_at": cfg.window.trigger_at_minutes_remaining,
                    "dispatch_mode": cfg.window.dispatch_mode,
                }
    except Exception:
        pass

    # Skip reason counts from run_log
    skip_reasons: dict[str, int] = {}
    for e in run_log:
        if e.get("event") == "skip":
            r = e.get("reason", "unknown")
            skip_reasons[r] = skip_reasons.get(r, 0) + 1

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "state": state,
        "run_log": run_log[-500:],
        "dispatches": dispatches[:100],
        "projects": projects,
        "later_by_repo": later_by_repo,
        "later_tasks": all_tasks,
        "window": window_info,
        "skip_reasons": skip_reasons,
    }

    data_json = json.dumps(data, default=str)
    return DASHBOARD_HTML.replace("__DATA_JSON__", data_json)


def run_dashboard(cwd: str | None = None) -> int:
    """Generate dashboard and serve on localhost."""
    cwd_path = Path(cwd) if cwd else Path.cwd()
    html_content = generate_dashboard(cwd=cwd_path)

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"Dashboard: {url} (Ctrl+C to stop)", file=sys.stderr)

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    def _shutdown(sig, frame):
        print("\nDashboard stopped.", file=sys.stderr)
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

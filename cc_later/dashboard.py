"""Generate and serve a dashboard from cc-later data."""
from __future__ import annotations

import json
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


# result file: first line may be "DONE task_id: msg" or "error: msg" or blank
_RESULT_RE = re.compile(
    r"^(DONE|SKIPPED|NEEDS_HUMAN|FAILED)(?:\s+\([^)]+\))?\s+([A-Za-z0-9_-]+)\s*:(.*)",
    re.DOTALL,
)
# filename: {worktree_id}-{section}-{YYYYMMDD}-{HHMMSS}.json
_FNAME_RE = re.compile(r"^(.+?)-([^-]+)-(\d{8})-(\d{6})\.json$")

_TASK_RE = re.compile(
    r"^(?:\s*-\s*)\[(?P<mark>[ xX!])\](?:\s*)(?:(?P<prio>\(P[0-2]\))\s*)?(?P<text>.+?)\s*$"
)


def _parse_results(results_dir: Path) -> list[dict]:
    results = []
    if not results_dir.exists():
        return results
    for f in sorted(results_dir.glob("*.json")):
        try:
            content = f.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""

        m = _FNAME_RE.match(f.name)
        section = m.group(2) if m else "unknown"
        date_s = m.group(3) if m else ""
        time_s = m.group(4) if m else ""
        ts = (
            f"{date_s[:4]}-{date_s[4:6]}-{date_s[6:8]}T{time_s[:2]}:{time_s[2:4]}:{time_s[4:6]}"
            if date_s and time_s
            else ""
        )

        if not content:
            status, task_id, message = "EMPTY", "", ""
        elif content.lower().startswith("error:"):
            status, task_id, message = "FAILED", "", content[6:].strip()
        else:
            rm = _RESULT_RE.match(content)
            if rm:
                status, task_id, message = rm.group(1), rm.group(2), rm.group(3).strip()
            else:
                status, task_id, message = "UNKNOWN", "", content[:120]

        results.append(
            {
                "filename": f.name,
                "section": section,
                "ts": ts,
                "status": status,
                "task_id": task_id,
                "message": message[:200],
            }
        )
    return results


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
            tasks.append(
                {
                    "section": current_section,
                    "priority": m.group("prio") or "(P1)",
                    "text": m.group("text"),
                    "done": mark.lower() == "x",
                }
            )
    return tasks


def generate_dashboard(app_dir: Path | None = None, cwd: Path | None = None) -> str:
    """Generate dashboard HTML with embedded data."""
    from cc_later.core import app_dir as _app_dir

    app_dir = app_dir or _app_dir()
    cwd = cwd or Path.cwd()

    state = _load_json(app_dir / "state.json")
    run_log = _load_jsonl(app_dir / "run_log.jsonl")
    results = _parse_results(app_dir / "results")
    later_tasks = _parse_later_md(cwd / ".claude" / "LATER.md")

    window_info = None
    try:
        from cc_later.core import load_config, compute_window_state, resolve_jsonl_roots

        cfg = load_config()
        now = datetime.now(timezone.utc)
        roots = resolve_jsonl_roots(cfg)
        if roots:
            ws = compute_window_state(
                roots, now, window_duration=cfg.window.duration_minutes
            )
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

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "state": state,
        "run_log": run_log[-1000:],
        "results": results,
        "later_tasks": later_tasks,
        "window": window_info,
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

"""macOS launchd integration for cc-later monitor."""
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

PLIST_NAME = "com.cc-later.monitor"


def _plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{PLIST_NAME}.plist"


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _find_uv() -> str:
    """Find uv binary path."""
    import shutil

    return shutil.which("uv") or "uv"


def _log_dir() -> Path:
    from cc_later.core import app_dir

    d = app_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def install_launchd_plist(interval_minutes: int = 15) -> Path:
    """Generate and load a launchd plist for periodic monitoring.

    Returns the path to the installed plist.
    """
    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Unload if already installed
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
        )

    uv = _find_uv()
    root = str(_plugin_root())
    log_dir = _log_dir()

    plist = {
        "Label": PLIST_NAME,
        "ProgramArguments": [
            uv,
            "run",
            "--project",
            root,
            sys.executable,
            str(Path(root) / "scripts" / "monitor.py"),
            "--once",
        ],
        "StartInterval": interval_minutes * 60,
        "StandardOutPath": str(log_dir / "monitor.log"),
        "StandardErrorPath": str(log_dir / "monitor.log"),
        "RunAtLoad": True,
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        },
    }

    with plist_path.open("wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    return plist_path


def uninstall_launchd_plist() -> bool:
    """Unload and remove the launchd plist. Returns True if removed."""
    plist_path = _plist_path()
    if not plist_path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink(missing_ok=True)
    return True


def is_installed() -> bool:
    """Check if the launchd plist is installed."""
    return _plist_path().exists()


def plist_info() -> dict | None:
    """Read the installed plist and return its contents."""
    plist_path = _plist_path()
    if not plist_path.exists():
        return None
    with plist_path.open("rb") as f:
        return plistlib.load(f)

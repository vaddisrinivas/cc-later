#!/usr/bin/env python3
"""cc-later monitor — periodic window/budget/agent monitoring.

Usage:
    python scripts/monitor.py --once          # Single snapshot
    python scripts/monitor.py --compact       # Single compact one-liner
    python scripts/monitor.py --install       # Install launchd plist
    python scripts/monitor.py --uninstall     # Remove launchd plist
    python scripts/monitor.py --status        # Show launchd install status
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cc_later.core import (
    format_monitor_compact,
    format_monitor_full,
    run_monitor,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="cc-later monitor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="Run once, full output")
    group.add_argument("--compact", action="store_true", help="Run once, one-line output")
    group.add_argument("--install", action="store_true", help="Install launchd plist")
    group.add_argument("--uninstall", action="store_true", help="Remove launchd plist")
    group.add_argument("--status", action="store_true", help="Show launchd status")
    parser.add_argument("--interval", type=int, default=15, help="Cron interval in minutes (for --install)")
    parser.add_argument("--no-notify", action="store_true", help="Suppress macOS notifications")
    args = parser.parse_args()

    if args.install:
        from cc_later.launchd import install_launchd_plist

        path = install_launchd_plist(args.interval)
        print(f"[cc-later] launchd plist installed: {path}")
        print(f"[cc-later] monitor will run every {args.interval} minutes")
        return 0

    if args.uninstall:
        from cc_later.launchd import uninstall_launchd_plist

        if uninstall_launchd_plist():
            print("[cc-later] launchd plist removed")
        else:
            print("[cc-later] no plist found")
        return 0

    if args.status:
        from cc_later.launchd import is_installed, plist_info

        if is_installed():
            info = plist_info()
            interval = info.get("StartInterval", "?") if info else "?"
            print(f"[cc-later] monitor installed (interval: {interval}s)")
        else:
            print("[cc-later] monitor not installed")
        return 0

    snap = run_monitor(notify=not args.no_notify)
    if args.compact:
        print(format_monitor_compact(snap))
    else:
        print(format_monitor_full(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())

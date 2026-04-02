"""Notification system — desktop + webhook support."""

from __future__ import annotations

import json
import platform
import subprocess
from typing import Any
from urllib.request import Request, urlopen

from .models import NotificationConfig


def notify(
    cfg: NotificationConfig,
    title: str,
    message: str,
    event: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Send notification via configured channels."""
    channel = f"on_{event}" if not event.startswith("on_") else event
    enabled = getattr(cfg, channel, False)

    if cfg.desktop and enabled:
        _desktop_notify(title, message)

    if cfg.webhook_url and event in cfg.webhook_events:
        _webhook_notify(cfg.webhook_url, title, message, event, payload)


def _desktop_notify(title: str, message: str) -> None:
    system = platform.system()
    if system == "Darwin":
        cmd = ["osascript", "-e", f'display notification "{message}" with title "{title}"']
    elif system == "Linux":
        cmd = ["notify-send", title, message]
    else:
        return
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _webhook_notify(
    url: str,
    title: str,
    message: str,
    event: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """POST JSON to webhook URL. Fire and forget."""
    body = {
        "event": event,
        "title": title,
        "message": message,
    }
    if payload:
        body["details"] = payload
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urlopen(req, timeout=5)
    except Exception:
        pass  # fire and forget

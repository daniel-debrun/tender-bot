"""Notifications: always appended to <state>/notifications.log; also a desktop notification (notify-send) and an
optional hook command (config notify.command, else $IBKR_OPS_ALERT_CMD, the variable ibkr-ops alerts use) that
receives the title and body as its last two arguments. Nothing is sent to external services by default."""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .store import utcnow

log = logging.getLogger("tender_bot")


class Notifier:
    def __init__(self, cfg: dict, sink=None):
        self.cfg = cfg["notify"]
        self.logfile = Path(cfg["state_dir"]) / "notifications.log"
        self.sink = sink                     # tests: a list collecting (title, body, urgency)

    def send(self, title: str, body: str, urgency: str = "normal") -> None:
        self.logfile.parent.mkdir(parents=True, exist_ok=True)
        with open(self.logfile, "a") as f:
            f.write(f"{utcnow()} [{urgency}] {title}\n" + "".join(f"    {x}\n" for x in body.splitlines()))
        log.info("notify [%s] %s", urgency, title)
        if self.sink is not None:
            self.sink.append((title, body, urgency))
            return
        if self.cfg.get("desktop"):
            if not shutil.which("notify-send"):
                log.info("desktop notification skipped: notify-send not found (see notifications.log)")
            elif not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                log.info("desktop notification skipped: no desktop session (DISPLAY/WAYLAND_DISPLAY unset)")
            else:
                _run(["notify-send", "-a", "tender-bot", "-u", urgency, f"tender-bot: {title}", body[:1500]])
        cmd = self.cfg.get("command") or os.environ.get("IBKR_OPS_ALERT_CMD")
        if cmd and not cmd.strip().startswith("notify-send"):      # don't notify twice on the desktop
            _run([*shlex.split(cmd), title, body])


def _run(argv: list[str]) -> None:
    try:
        r = subprocess.run(argv, timeout=30, check=False, capture_output=True, text=True)
    except Exception as e:  # noqa: BLE001 - a failed notification must not stop a job
        log.error("notification command failed: %s", e)
        return
    if r.returncode:
        log.warning("notification command %s failed: exit %d %s", argv[0], r.returncode, (r.stderr or "").strip()[:200])

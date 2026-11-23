"""Structured, privacy-preserving logging.

Audit-level events (anything that could modify a device) are written as
append-only JSON lines so the history survives crashes and can be reviewed by
the user.  Device serial numbers are hashed unless the user opts in to storing
them in plaintext.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any, Optional

from usbdoctor.core.uname import uname_sysname
from usbdoctor.core.util import iso_now

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def default_state_dir() -> Path:
    override = os.environ.get("USBDOCTOR_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        return base / "USBDoctorAI"
    if uname_sysname() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "USB Doctor AI"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "usbdoctor-ai"


def setup_logging(level: int = logging.INFO, state_dir: Optional[Path] = None) -> Path:
    """Configure root logging to console + rotating file.  Returns log path."""
    state = state_dir or default_state_dir()
    log_dir = state / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "usbdoctor.log"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=4 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)
    if os.environ.get("USBDOCTOR_CONSOLE_LOG") == "1":
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(console)
    return log_path


class AuditLog:
    """Append-only audit trail of every potentially modifying action."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _redact(serial: Optional[str]) -> Optional[str]:
        if not serial or os.environ.get("USBDOCTOR_STORE_SERIALS") == "1":
            return serial
        return "sha256:" + hashlib.sha256(serial.encode()).hexdigest()[:16]

    def record(
        self,
        action: str,
        *,
        device_path: Optional[str] = None,
        serial: Optional[str] = None,
        outcome: str = "started",
        detail: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        entry = {
            "ts": iso_now(),
            "action": action,
            "device": device_path,
            "serial": self._redact(serial),
            "outcome": outcome,
            "detail": detail or {},
        }
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

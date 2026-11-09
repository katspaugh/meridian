"""Best-effort startup diagnostics. Only callers' operation names and IDs belong here."""
from datetime import datetime, timezone
import os
from pathlib import Path

_context = None


def begin(action, stream):
    global _context
    _context = f"action={action} stream={stream}"
    record("begin")


def record(stage):
    if _context is None:
        return
    try:
        now = datetime.now(timezone.utc)
        folder = Path.home() / "Library/Logs/Muxteroid"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f"workstream-{now.date()}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            timestamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            os.write(fd, f"{timestamp} bridge[{os.getpid()}] {_context} {stage}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass

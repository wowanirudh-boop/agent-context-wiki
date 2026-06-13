from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class WorkspaceLockError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LockAcquisition:
    path: Path
    payload: dict[str, Any]
    stolen: bool = False


class WorkspaceLock:
    def __init__(self, workspace: str | Path, op: str) -> None:
        self.workspace = Path(workspace)
        self.op = op
        self.path = self.workspace / ".llmwiki" / "lock"
        self._acquisition: LockAcquisition | None = None

    def __enter__(self) -> LockAcquisition:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = _payload(self.op)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_lock(self.path)
            if not _is_stale(existing):
                pid = existing.get("pid", "?")
                op = existing.get("op", "?")
                raise WorkspaceLockError(f"workspace is locked by pid {pid} running {op}") from None
            self.path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self._acquisition = LockAcquisition(path=self.path, payload=payload, stolen=True)
            return self._acquisition
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
        self._acquisition = LockAcquisition(path=self.path, payload=payload)
        return self._acquisition

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self._acquisition is None or not self.path.exists():
            return
        current = _read_lock(self.path)
        if current.get("pid") == self._acquisition.payload["pid"] and current.get("op") == self.op:
            self.path.unlink()


def _payload(op: str) -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "op": op,
        "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _is_stale(payload: dict[str, Any]) -> bool:
    pid = payload.get("pid")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return True
    if pid_int <= 0:
        return True
    if pid_int == os.getpid():
        return False
    try:
        os.kill(pid_int, 0)
    except OSError:
        return True
    except ValueError:
        return True
    except PermissionError:
        return False
    return False

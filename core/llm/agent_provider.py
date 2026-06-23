from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db.dao import ACWDao
from core.llm.provider import LLMProvider, StructuredPayload, StructuredResponse, StructuredSchema
from core.models import EventKind

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class AgentTimeout(TimeoutError):
    pass


class AgentQueue:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.root = self.workspace / ".llmwiki" / "agent_queue"
        self.requests_dir = self.root / "requests"
        self.answers_dir = self.root / "answers"

    def create_request(
        self,
        *,
        call_site: str,
        payload: StructuredPayload,
        schema: StructuredSchema,
    ) -> dict[str, Any]:
        self._ensure_dirs()
        request_id = stable_request_id(call_site, payload, schema)
        request_path = self.request_path(request_id)
        answer_path = self.answer_path(request_id)
        if request_path.exists():
            existing = _read_json(request_path)
            if existing.get("status") == "pending" or answer_path.exists():
                return existing
            request = {
                "request_id": request_id,
                "call_site": call_site,
                "payload": dict(payload),
                "schema": dict(schema),
                "created_at": utc_now(),
                "status": "pending",
            }
            _atomic_write_json(request_path, request)
            return request

        request = {
            "request_id": request_id,
            "call_site": call_site,
            "payload": dict(payload),
            "schema": dict(schema),
            "created_at": utc_now(),
            "status": "pending",
        }
        _atomic_write_json(request_path, request)
        return request

    def next_pending(self) -> dict[str, Any] | None:
        self._ensure_dirs()
        pending: list[dict[str, Any]] = []
        for path in sorted(self.requests_dir.glob("*.json")):
            request = _read_json(path)
            request_id = str(request.get("request_id", ""))
            if request.get("status") != "pending" or not request_id:
                continue
            if self.answer_path(request_id).exists():
                continue
            pending.append(request)
        if not pending:
            return None
        return sorted(pending, key=lambda item: (str(item.get("created_at", "")), str(item.get("request_id", ""))))[0]

    def write_answer(self, request_id: str, response: Mapping[str, Any]) -> tuple[bool, str]:
        self._ensure_dirs()
        try:
            request_path = self.request_path(request_id)
            answer_path = self.answer_path(request_id)
        except ValueError as exc:
            return False, str(exc)
        if not request_path.exists():
            return False, f"unknown request_id: {request_id}"
        request = _read_json(request_path)
        if request.get("status") != "pending":
            return False, f"request is not pending: {request_id}"
        if answer_path.exists():
            return False, f"request already answered: {request_id}"
        _atomic_write_json(answer_path, {"request_id": request_id, "response": dict(response), "answered_at": utc_now()})
        return True, "ok"

    def load_answer(self, request_id: str) -> StructuredResponse | None:
        answer_path = self.answer_path(request_id)
        if not answer_path.exists():
            return None
        answer = _read_json(answer_path)
        response = answer.get("response")
        if not isinstance(response, dict):
            raise ValueError(f"Agent answer for {request_id} must contain a response object")
        return dict(response)

    def mark_done(self, request_id: str) -> None:
        request_path = self.request_path(request_id)
        request = _read_json(request_path)
        request["status"] = "done"
        request["completed_at"] = utc_now()
        _atomic_write_json(request_path, request)

    def request_path(self, request_id: str) -> Path:
        _validate_request_id(request_id)
        return self.requests_dir / f"{request_id}.json"

    def answer_path(self, request_id: str) -> Path:
        _validate_request_id(request_id)
        return self.answers_dir / f"{request_id}.json"

    def _ensure_dirs(self) -> None:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.answers_dir.mkdir(parents=True, exist_ok=True)


class AgentProvider(LLMProvider):
    def __init__(
        self,
        workspace: str | Path,
        dao: ACWDao | None = None,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.workspace = Path(workspace)
        self.dao = dao
        self.queue = AgentQueue(self.workspace)
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _env_timeout_seconds()
        self.poll_interval_seconds = poll_interval_seconds
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    async def complete_structured(
        self,
        call_site: str,
        payload: StructuredPayload,
        schema: StructuredSchema,
    ) -> StructuredResponse:
        started = time.perf_counter()
        ok = False
        self._call_count += 1
        request = self.queue.create_request(call_site=call_site, payload=payload, schema=schema)
        request_id = str(request["request_id"])
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            while True:
                response = self.queue.load_answer(request_id)
                if response is not None:
                    self.queue.mark_done(request_id)
                    ok = True
                    return response
                if asyncio.get_running_loop().time() >= deadline:
                    raise AgentTimeout(
                        f"Agent request {request_id} for {call_site} timed out after "
                        f"{self.timeout_seconds:g}s; pending request remains at "
                        f"{self.queue.request_path(request_id)}"
                    )
                await asyncio.sleep(self.poll_interval_seconds)
        finally:
            await self._log_call(call_site, started=started, ok=ok)

    async def _log_call(self, call_site: str, *, started: float, ok: bool) -> None:
        if self.dao is None:
            return
        await self.dao.write_event(
            kind=EventKind.llm_call,
            actor="core.llm.agent_provider",
            payload={
                "call_site": call_site,
                "model": "agent",
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "ok": ok,
            },
        )


def stable_request_id(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> str:
    canonical = json.dumps(
        {"call_site": call_site, "payload": payload, "schema": schema},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"agent_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _env_timeout_seconds() -> float:
    raw = os.environ.get("ACW_AGENT_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 1800.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("ACW_AGENT_TIMEOUT_SECONDS must be a number") from exc
    if value <= 0:
        raise ValueError("ACW_AGENT_TIMEOUT_SECONDS must be positive")
    return value


def _validate_request_id(request_id: str) -> None:
    if not request_id or not _REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id must contain only letters, numbers, dots, underscores, or hyphens")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temp_path, path)

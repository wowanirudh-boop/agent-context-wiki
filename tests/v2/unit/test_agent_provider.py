from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
MCP_ROOT = ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))


@pytest.mark.asyncio
async def test_agent_provider_writes_request_and_returns_dropped_answer(tmp_workspace: Path) -> None:
    from core.llm.agent_provider import AgentProvider

    provider = AgentProvider(tmp_workspace, timeout_seconds=2.0, poll_interval_seconds=0.01)
    task = asyncio.create_task(
        provider.complete_structured(
            "C5",
            {"title": "Refunds", "blocks": [], "max_words": 300},
            {"type": "object", "required": ["summary_markdown"]},
        )
    )

    request_path, request = await _wait_for_request(tmp_workspace)
    assert request["call_site"] == "C5"
    assert request["payload"]["title"] == "Refunds"
    assert request["schema"]["required"] == ["summary_markdown"]
    assert request["status"] == "pending"

    answer_path = tmp_workspace / ".llmwiki" / "agent_queue" / "answers" / f"{request['request_id']}.json"
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(json.dumps({"response": {"summary_markdown": "Refunds summary."}}), encoding="utf-8")

    assert await task == {"summary_markdown": "Refunds summary."}
    completed = json.loads(request_path.read_text(encoding="utf-8"))
    assert completed["status"] == "done"
    assert completed["completed_at"]


@pytest.mark.asyncio
async def test_agent_provider_timeout_raises_and_leaves_pending_request(tmp_workspace: Path) -> None:
    from core.llm.agent_provider import AgentProvider, AgentTimeout

    provider = AgentProvider(tmp_workspace, timeout_seconds=0.02, poll_interval_seconds=0.01)

    with pytest.raises(AgentTimeout, match="timed out"):
        await provider.complete_structured("C5", {"title": "Refunds"}, {"type": "object"})

    _, request = await _wait_for_request(tmp_workspace)
    assert request["status"] == "pending"


@pytest.mark.asyncio
async def test_agent_bridge_next_and_answer_request_queue_contract(tmp_workspace: Path) -> None:
    from tools.agent_bridge import AgentBridgeHandler, register

    handler = AgentBridgeHandler(tmp_workspace)
    assert await handler.acw_next_request() == {"pending": False, "message": "no pending requests"}

    request = {
        "request_id": "agent_test",
        "call_site": "C5",
        "payload": {"title": "Refunds"},
        "schema": {"type": "object"},
        "created_at": "2026-06-24T00:00:00Z",
        "status": "pending",
    }
    request_path = tmp_workspace / ".llmwiki" / "agent_queue" / "requests" / "agent_test.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request), encoding="utf-8")

    pending = await handler.acw_next_request()
    assert pending == {
        "pending": True,
        "request_id": "agent_test",
        "call_site": "C5",
        "payload": {"title": "Refunds"},
        "schema": {"type": "object"},
    }

    bad = await handler.acw_answer_request("agent_test", ["not", "a", "dict"])
    assert bad["success"] is False
    assert "dict" in bad["message"]

    good = await handler.acw_answer_request("agent_test", {"summary_markdown": "Refunds summary."})
    assert good == {"success": True, "request_id": "agent_test"}
    answer = json.loads((tmp_workspace / ".llmwiki" / "agent_queue" / "answers" / "agent_test.json").read_text())
    assert answer["response"] == {"summary_markdown": "Refunds summary."}

    fake_mcp = _FakeMCP()
    register(fake_mcp, None, None)
    assert set(fake_mcp.descriptions) == {"acw_next_request", "acw_answer_request"}
    assert "C1" in "\n".join(fake_mcp.descriptions.values())


async def _wait_for_request(workspace: Path) -> tuple[Path, dict[str, Any]]:
    request_dir = workspace / ".llmwiki" / "agent_queue" / "requests"
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        paths = sorted(request_dir.glob("*.json"))
        if paths:
            return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))
        await asyncio.sleep(0.01)
    raise AssertionError("AgentProvider did not write a request file")


class _FakeMCP:
    def __init__(self) -> None:
        self.descriptions: dict[str, str] = {}

    def tool(self, *, name: str, description: str):
        self.descriptions[name] = description

        def _decorator(fn):
            return fn

        return _decorator

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from core.llm.agent_provider import AgentQueue

from .wiki_read import _workspace_root

AGENT_BRIDGE_DOC = """AgentProvider queue bridge for ACW v2.

Loop for an MCP-connected reasoning agent:
1. Call acw_next_request().
2. Read call_site, payload, and schema.
3. Produce a JSON object matching the requested call-site schema.
4. Call acw_answer_request(request_id, response_json).
5. Repeat until acw_next_request() says no pending requests.

Response shapes:
C1 placement: {"chunks":[{"chunk_id":"ch_...","relevant":true,"irrelevant_reason":null,"placements":[{"page":{"existing_page_id":"pg_..."},"new_page":null,"section":"Rules","block":{"key":"domain.entity.attribute","type":"rule","content":"...","excerpt":"...","new_key_justification":"..."},"links":[]}]}]}
C2 flow extraction: {"mermaid":"graph TD...","nodes":["node_id"],"edges":[{"from":"a","to":"b","condition":null}]}
C3 conflict judge: {"verdict":"distinct|duplicate|conflict","conflict_type":null,"recommendation":"needs_more_info","rationale":"..."}
C4 merge draft: {"content":"...","excerpt_policy":"keep_both"}
C5 summary: {"summary_markdown":"..."}
C6 transcript pre-pass: {"segments":[{"chunk_id":"ch_...","relevant":true,"reason":null,"superseded_by_chunk_id":null,"key_hint":null,"source_date_extracted":null}]}
Existing complete_validated() validators handle correctness and retry; these bridge tools only move JSON."""


class AgentBridgeHandler:
    def __init__(self, workspace: str | Path) -> None:
        self.queue = AgentQueue(workspace)

    async def acw_next_request(self) -> dict[str, Any]:
        request = self.queue.next_pending()
        if request is None:
            return {"pending": False, "message": "no pending requests"}
        return {
            "pending": True,
            "request_id": request["request_id"],
            "call_site": request["call_site"],
            "payload": request["payload"],
            "schema": request["schema"],
        }

    async def acw_answer_request(self, request_id: str, response_json: Any) -> dict[str, Any]:
        if not isinstance(response_json, dict):
            return {"success": False, "request_id": request_id, "message": "response_json must be a dict"}
        success, message = self.queue.write_answer(request_id, response_json)
        if not success:
            return {"success": False, "request_id": request_id, "message": message}
        return {"success": True, "request_id": request_id}


def register(mcp: FastMCP, get_user_id, fs_factory) -> None:
    del get_user_id, fs_factory

    @mcp.tool(
        name="acw_next_request",
        description=f"{AGENT_BRIDGE_DOC}\n\nRead-only. Returns the oldest pending request, or no pending requests.",
    )
    async def acw_next_request(ctx: Context) -> dict[str, Any]:
        del ctx
        return await AgentBridgeHandler(_workspace_root()).acw_next_request()

    @mcp.tool(
        name="acw_answer_request",
        description=(
            f"{AGENT_BRIDGE_DOC}\n\nWrites response_json atomically for request_id. "
            "response_json must be a JSON object; call-site validators run after AgentProvider receives it."
        ),
    )
    async def acw_answer_request(ctx: Context, request_id: str, response_json: dict[str, Any]) -> dict[str, Any]:
        del ctx
        return await AgentBridgeHandler(_workspace_root()).acw_answer_request(request_id, response_json)

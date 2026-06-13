from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.llm.calls import C2_SCHEMA, complete_validated, validate_c2_response
from core.llm.provider import LLMProvider


async def extract_flow_mermaid(provider: LLMProvider, source_text: str, *, source_path: str) -> str:
    whimsical = _try_json(source_text)
    if whimsical is not None:
        return whimsical_json_to_mermaid(whimsical)

    response = await complete_validated(
        provider,
        "C2",
        {"source_text": source_text, "source_path": source_path},
        C2_SCHEMA,
        validate_c2_response,
    )
    return str(response["mermaid"])


def whimsical_json_to_mermaid(data: Mapping[str, Any]) -> str:
    nodes = _as_list(data.get("nodes"))
    edges = _as_list(data.get("edges"))
    lines = ["flowchart TD"]
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_id = _node_id(node.get("id"))
        label = _escape_label(str(node.get("label") or node_id))
        lines.append(f'  {node_id}["{label}"]')
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        start = _node_id(edge.get("from"))
        end = _node_id(edge.get("to"))
        condition = edge.get("condition")
        if isinstance(condition, str) and condition.strip():
            lines.append(f"  {start} -->|{_escape_edge_label(condition)}| {end}")
        else:
            lines.append(f"  {start} --> {end}")
    return "\n".join(lines)


def _try_json(source_text: str) -> Mapping[str, Any] | None:
    try:
        value = json.loads(source_text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, Mapping) else None


def _node_id(value: Any) -> str:
    raw = str(value or "node").strip() or "node"
    cleaned = "".join(character if character.isalnum() or character == "_" else "_" for character in raw)
    if cleaned[0].isdigit():
        cleaned = f"n_{cleaned}"
    return cleaned


def _escape_label(value: str) -> str:
    return value.replace('"', '\\"')


def _escape_edge_label(value: str) -> str:
    return value.replace("|", "/").strip()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

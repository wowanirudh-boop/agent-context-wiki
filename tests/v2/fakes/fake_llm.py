from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from core.llm.provider import LLMProvider, StructuredPayload, StructuredResponse, StructuredSchema

FakeLLMMode = Literal["scripted", "rule-based"]
ScriptedResponses = Mapping[tuple[str, str], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class FakeLLMCall:
    call_site: str
    fingerprint: str
    payload: StructuredPayload


class FakeLLM(LLMProvider):
    def __init__(self, mode: FakeLLMMode, responses: ScriptedResponses | None = None) -> None:
        self.mode = mode
        self._responses = dict(responses or {})
        self.calls: list[FakeLLMCall] = []

    @classmethod
    def scripted(cls, responses: ScriptedResponses) -> FakeLLM:
        return cls("scripted", responses)

    @classmethod
    def rule_based(cls) -> FakeLLM:
        return cls("rule-based")

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete_structured(
        self,
        call_site: str,
        payload: StructuredPayload,
        schema: StructuredSchema,
    ) -> StructuredResponse:
        del schema
        fingerprint = fingerprint_payload(payload)
        self.calls.append(FakeLLMCall(call_site=call_site, fingerprint=fingerprint, payload=dict(payload)))

        if self.mode == "scripted":
            key = (call_site, fingerprint)
            if key not in self._responses:
                raise KeyError(f"No scripted FakeLLM response for {call_site} {fingerprint}")
            return copy.deepcopy(dict(self._responses[key]))

        return _rule_based_response(call_site, payload)


def fingerprint_payload(payload: StructuredPayload) -> str:
    selected = _fingerprint_fields(payload)
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _fingerprint_fields(payload: StructuredPayload) -> Mapping[str, Any]:
    if "chunks" in payload:
        return {"chunks": [_chunk_id(chunk) for chunk in _as_list(payload["chunks"])]}
    if "segments" in payload:
        return {"segments": [_chunk_id(segment) for segment in _as_list(payload["segments"])]}
    if "chunk_id" in payload:
        return {"chunk_id": payload["chunk_id"]}
    return payload


def _rule_based_response(call_site: str, payload: StructuredPayload) -> StructuredResponse:
    if call_site == "C1":
        return _rule_based_placement(payload)
    if call_site == "C2":
        return _rule_based_flow(payload)
    if call_site == "C3":
        return _rule_based_conflict(payload)
    if call_site == "C4":
        return {"content": _merge_content(payload), "excerpt_policy": "keep_both"}
    if call_site == "C5":
        return {"summary_markdown": _summary(payload)}
    if call_site == "C6":
        return {"segments": _transcript_segments(_as_list(payload.get("segments", [])))}
    return {}


def _rule_based_placement(payload: StructuredPayload) -> StructuredResponse:
    chunks = _as_list(payload.get("chunks", []))
    return {"chunks": [_placement_for_chunk(chunk, payload) for chunk in chunks]}


def _placement_for_chunk(chunk: Mapping[str, Any], payload: StructuredPayload) -> Mapping[str, Any]:
    chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or "")
    text = str(chunk.get("text", ""))
    node = _node_settings_response(chunk, payload)
    if node is not None:
        return node
    excerpt = _excerpt(text)
    if not excerpt:
        return {"chunk_id": chunk_id, "relevant": False, "irrelevant_reason": "empty chunk", "placements": []}

    page = _best_page(text, payload)
    placement_page: Mapping[str, Any] | None
    new_page: Mapping[str, Any] | None
    if page is None:
        placement_page = None
        title = _new_page_title(text, str(chunk.get("source_path", "")))
        new_page = {
            "title": title,
            "description": f"{title} context",
            "domain": _domain_for_title(title),
            "path_slug": title.lower().replace(" ", "-"),
            "no_registry_match_assertion": "No active registry page shared enough words with the chunk.",
        }
    else:
        placement_page = {"existing_page_id": page.get("id")}
        new_page = None

    key, block_type, section = _block_shape(text, str(chunk.get("source_path", "")))
    return {
        "chunk_id": chunk_id,
        "relevant": True,
        "irrelevant_reason": None,
        "placements": [
            {
                "page": placement_page,
                "new_page": new_page,
                "section": section,
                "block": {
                    "key": key,
                    "type": block_type,
                    "content": excerpt,
                    "excerpt": excerpt,
                    "new_key_justification": "Rule-based FakeLLM proposed the most specific source-backed key.",
                },
                "links": [],
            }
        ],
    }


def _node_settings_response(chunk: Mapping[str, Any], payload: StructuredPayload) -> Mapping[str, Any] | None:
    source_path = str(chunk.get("source_path", "")).replace("\\", "/")
    text = str(chunk.get("text", ""))
    if "docs/nodes/" not in source_path or "| Setting |" not in text:
        return None
    rows = _table_setting_rows(text)
    if not rows:
        return None
    title = _node_title(source_path, text)
    page_ref = _exact_title_page(title, payload)
    node_key = _node_key_segment(source_path)
    if page_ref is None:
        page = None
        new_page = {
            "title": title,
            "description": f"{title} settings and defaults",
            "domain": "nodes",
            "path_slug": title.lower().replace(" ", "-"),
            "no_registry_match_assertion": "Node reference pages are one page per node source file.",
        }
    else:
        page = {"existing_page_id": page_ref}
        new_page = None
    placements = []
    for setting, default, notes, raw_line in rows:
        setting_key = re.sub(r"[^a-z0-9_]+", "_", setting.lower()).strip("_")
        placements.append(
            {
                "page": page,
                "new_page": new_page,
                "section": "API Details" if node_key == "api_call" else "Rules",
                "block": {
                    "key": f"nodes.{node_key}.{setting_key}",
                    "type": "api" if node_key == "api_call" else "rule",
                    "content": f"**{setting}:** `{default}`. {notes}",
                    "excerpt": raw_line,
                    "new_key_justification": "Each node setting receives a stable setting key.",
                },
                "links": [],
            }
        )
    return {"chunk_id": str(chunk.get("id") or chunk.get("chunk_id") or ""), "relevant": True, "irrelevant_reason": None, "placements": placements}


def _table_setting_rows(text: str) -> list[tuple[str, str, str, str]]:
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped or "Setting" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        rows.append((cells[0], cells[1], cells[2], stripped))
    return rows


def _node_title(source_path: str, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").title()


def _node_key_segment(source_path: str) -> str:
    stem = source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")


def _exact_title_page(title: str, payload: StructuredPayload) -> str | None:
    for page in _as_list(payload.get("registry", [])):
        if isinstance(page, Mapping) and str(page.get("title", "")).casefold() == title.casefold():
            return str(page.get("id"))
    return None


def _best_page(text: str, payload: StructuredPayload) -> Mapping[str, Any] | None:
    registry = _as_list(payload.get("registry", payload.get("pages", [])))
    if not registry:
        return None

    text_words = set(_words(text))
    best_page: Mapping[str, Any] | None = None
    best_score = 0
    for page in registry:
        if not isinstance(page, Mapping):
            continue
        page_words = set(_words(f"{page.get('title', '')} {page.get('description', '')}"))
        score = len(text_words & page_words)
        if score > best_score:
            best_page = page
            best_score = score
    return best_page


def _rule_based_flow(payload: StructuredPayload) -> StructuredResponse:
    text = str(payload.get("source_text", ""))
    try:
        import yaml
    except ImportError:
        data = {}
    else:
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            data = {}
    if not isinstance(data, Mapping):
        data = {}
    nodes = [str(node.get("id")) for node in _as_list(data.get("nodes")) if isinstance(node, Mapping)]
    edges = [
        {
            "from": str(edge.get("from")),
            "to": str(edge.get("to")),
            "condition": edge.get("condition"),
        }
        for edge in _as_list(data.get("edges"))
        if isinstance(edge, Mapping)
    ]
    from core.pipeline.flows import whimsical_json_to_mermaid

    return {"mermaid": whimsical_json_to_mermaid(data), "nodes": nodes, "edges": edges}


def _rule_based_conflict(payload: StructuredPayload) -> StructuredResponse:
    candidate = payload.get("candidate", {})
    existing = payload.get("existing", {})
    if not isinstance(candidate, Mapping) or not isinstance(existing, Mapping):
        return _distinct_conflict_response()
    if candidate.get("type") == "faq" or existing.get("type") == "faq":
        return _distinct_conflict_response()
    candidate_text = f"{candidate.get('key', '')} {candidate.get('content', '')} {candidate.get('excerpt', '')}"
    existing_text = f"{existing.get('key', '')} {existing.get('content', '')} {existing.get('excerpt', '')}"
    if _has_retry_count_value(candidate_text) and _has_retry_count_value(existing_text):
        candidate_number = _first_number(candidate_text)
        existing_number = _first_number(existing_text)
        if candidate_number is not None and existing_number is not None and candidate_number != existing_number:
            basis = payload.get("recommendation_basis", {})
            basis_name = basis.get("basis") if isinstance(basis, Mapping) else "source_date"
            return {
                "verdict": "conflict",
                "conflict_type": "changed_value",
                "recommendation": "accept_new",
                "rationale": f"Retry count differs; recommendation basis supplied by code: {basis_name}.",
            }
        if candidate_number is not None and candidate_number == existing_number:
            return {
                "verdict": "duplicate",
                "conflict_type": None,
                "recommendation": "keep_existing",
                "rationale": "Both blocks state the same retry count.",
            }
    return _distinct_conflict_response()


def _distinct_conflict_response() -> StructuredResponse:
    return {
        "verdict": "distinct",
        "conflict_type": None,
        "recommendation": "needs_more_info",
        "rationale": "Rule-based FakeLLM default.",
    }


def _transcript_segments(segments: list[Any]) -> list[Mapping[str, Any]]:
    decision_ids = [
        _chunk_id(segment)
        for segment in segments
        if isinstance(segment, Mapping) and "decision" in str(segment.get("text", "")).lower()
    ]
    survivor = decision_ids[-1] if decision_ids else None
    output = []
    for segment in segments:
        text = str(segment.get("text", "")) if isinstance(segment, Mapping) else ""
        chunk_id = _chunk_id(segment)
        lowered = text.lower()
        is_decision = "decision" in lowered
        is_chatter = "chatter" in lowered or "lunch" in lowered
        superseded = is_decision and survivor is not None and chunk_id != survivor
        output.append(
            {
                "chunk_id": chunk_id,
                "relevant": not is_chatter,
                "reason": "chatter" if is_chatter else ("intra_transcript_supersession" if superseded else None),
                "superseded_by_chunk_id": survivor if superseded else None,
                "key_hint": "webhooks.retries.count" if is_decision else None,
                "source_date_extracted": _extract_date(text),
            }
        )
    return output


def _merge_content(payload: StructuredPayload) -> str:
    parts = [str(value) for key, value in sorted(payload.items()) if key.endswith("content")]
    return "\n\n".join(part for part in parts if part)


def _summary(payload: StructuredPayload) -> str:
    raw_blocks = _as_list(payload.get("blocks", []))
    words: list[str] = []
    for block in raw_blocks:
        if isinstance(block, Mapping):
            words.extend(str(block.get("content", "")).split())
    return " ".join(words[:300])


def _excerpt(text: str) -> str:
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]
    if not sentences:
        return text.strip()
    lowered = text.lower()
    if _retryish(lowered):
        for sentence in sentences:
            if _retryish(sentence) and _contains_digit(sentence):
                return sentence
    for sentence in sentences:
        if _contains_digit(sentence):
            return sentence
    return sentences[0]


def _new_page_title(text: str, source_path: str) -> str:
    if "refund" in text.lower() or "refund" in source_path.lower():
        return "Refunds"
    if "webhook" in text.lower() or "webhook" in source_path.lower():
        return "Webhooks"
    stem = source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    return stem.title() if stem else "General"


def _domain_for_title(title: str) -> str:
    return title.lower().replace(" ", "_")


def _block_shape(text: str, source_path: str) -> tuple[str, str, str]:
    lowered = f"{source_path}\n{text}".lower()
    source_segment = _source_segment(source_path)
    if "q:" in lowered or "a:" in lowered:
        return f"refunds.{source_segment}.faq", "faq", "FAQs"
    if "requirement" in lowered:
        if "retry exhaustion" in lowered or "provider failures" in lowered:
            return "refunds.provider_escalation", "requirement", "Requirements"
        if "approved" in lowered or "rejected" in lowered:
            return "refunds.approval_notification", "requirement", "Requirements"
        return f"refunds.{source_segment}.requirement", "requirement", "Requirements"
    if "maxretries" in lowered or _has_retry_count_value(text):
        block_type = "api" if "maxretries" in lowered else "rule"
        section = "API Details" if "maxretries" in lowered else "Rules"
        return "refunds.retry_count", block_type, section
    if "30 day" in lowered or "30 days" in lowered or "window" in lowered:
        return "refunds.window_days", "rule", "Rules"
    if "endpoint:" in lowered or "request fields" in lowered:
        return f"refunds.{source_segment}.endpoint", "api", "API Details"
    if "decision" in lowered:
        return "webhooks.retries.decision", "decision", "Decisions"
    return f"refunds.{source_segment}.note", "note", "Historical Notes"


def _source_segment(source_path: str) -> str:
    stem = source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")
    return cleaned or "source"


def _extract_date(text: str) -> str | None:
    match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    return match.group(0) if match else None


def _contains_digit(text: str) -> bool:
    return any(character.isdigit() for character in text)


def _retryish(text: str) -> bool:
    lowered = text.lower()
    return "retry" in lowered or "retries" in lowered or "maxretries" in lowered


def _has_retry_count_value(text: str) -> bool:
    lowered = text.lower()
    if "maxretries" in lowered:
        return True
    for sentence in re.split(r"(?<=[.!?])\s+", lowered):
        if not _retryish(sentence):
            continue
        if re.search(r"\b\d+\s*(?:times?|attempts?|retries?)\b", sentence):
            return True
        if re.search(r"\b(?:times?|attempts?|retries?)\s*(?:is|are|:)?\s*\d+\b", sentence):
            return True
    return False


def _first_number(text: str) -> int | None:
    match = re.search(r"\b\d+\b", text)
    if match is None:
        return None
    return int(match.group(0))


def _words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stems = [word[:-1] for word in words if len(word) > 3 and word.endswith("s")]
    return [*words, *stems]


def _chunk_id(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("id") or value.get("chunk_id") or "")
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []

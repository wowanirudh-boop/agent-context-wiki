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
        return {
            "verdict": "distinct",
            "conflict_type": None,
            "recommendation": "needs_more_info",
            "rationale": "Rule-based FakeLLM default.",
        }
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
    if "maxretries" in lowered:
        return f"refunds.{source_segment}.max_retries", "api", "API Details"
    if "retry" in lowered or "retries" in lowered:
        return f"refunds.{source_segment}.retry_count", "rule", "Rules"
    if "30 day" in lowered or "30 days" in lowered or "window" in lowered:
        return f"refunds.{source_segment}.window_days", "rule", "Rules"
    if "endpoint:" in lowered or "request fields" in lowered:
        return f"refunds.{source_segment}.endpoint", "api", "API Details"
    if "q:" in lowered or "a:" in lowered:
        return f"refunds.{source_segment}.faq", "faq", "FAQs"
    if "requirement" in lowered:
        return f"refunds.{source_segment}.requirement", "requirement", "Requirements"
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


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


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

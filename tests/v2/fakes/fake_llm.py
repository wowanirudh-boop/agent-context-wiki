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
        return {"mermaid": "flowchart TD", "nodes": [], "edges": []}
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
        return {"segments": [_transcript_segment(segment) for segment in _as_list(payload.get("segments", []))]}
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
        new_page = {
            "title": "General",
            "description": "Rule-based fallback page",
            "domain": "general",
            "path_slug": "general",
            "no_registry_match_assertion": "No registry page shared words with the chunk.",
        }
    else:
        placement_page = {"existing_page_id": page.get("id")}
        new_page = None

    return {
        "chunk_id": chunk_id,
        "relevant": True,
        "irrelevant_reason": None,
        "placements": [
            {
                "page": placement_page,
                "new_page": new_page,
                "section": "Rules" if _contains_digit(excerpt) else "Historical Notes",
                "block": {
                    "key": "general.note",
                    "type": "note",
                    "content": excerpt,
                    "excerpt": excerpt,
                    "new_key_justification": None,
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


def _transcript_segment(segment: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "chunk_id": _chunk_id(segment),
        "relevant": bool(str(segment.get("text", "")).strip()),
        "reason": None,
        "superseded_by_chunk_id": None,
        "key_hint": None,
        "source_date_extracted": None,
    }


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

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from core.blocks.keys import SemanticKeyError, validate_semantic_key
from core.blocks.model import CANONICAL_SECTIONS
from core.llm.provider import LLMProvider, StructuredPayload, StructuredResponse, StructuredSchema
from core.models import BlockType, ConflictType, ReviewDecision

C1_PLACEMENT_TEMPLATE = "Classify and place source chunks into source-backed context blocks."
C2_FLOW_TEMPLATE = "Convert a structured flow definition into Mermaid while preserving nodes and edges."
C3_CONFLICT_TEMPLATE = "Judge whether a candidate context block duplicates or conflicts with an existing block."
C4_MERGE_TEMPLATE = "Draft a merged context block candidate from two conflicting blocks."
C5_SUMMARY_TEMPLATE = "Summarize the current context blocks on a page for bounded agent reads."
C6_TRANSCRIPT_TEMPLATE = "Classify transcript segments and mark intra-transcript supersession."

C1_SCHEMA: StructuredSchema = {"type": "object", "required": ["chunks"]}
C2_SCHEMA: StructuredSchema = {"type": "object", "required": ["mermaid", "nodes", "edges"]}
C3_SCHEMA: StructuredSchema = {"type": "object", "required": ["verdict", "conflict_type", "recommendation", "rationale"]}
C4_SCHEMA: StructuredSchema = {"type": "object", "required": ["content", "excerpt_policy"]}
C5_SCHEMA: StructuredSchema = {"type": "object", "required": ["summary_markdown"]}
C6_SCHEMA: StructuredSchema = {"type": "object", "required": ["segments"]}


class CallValidationError(ValueError):
    pass


async def complete_validated(
    provider: LLMProvider,
    call_site: str,
    payload: StructuredPayload,
    schema: StructuredSchema,
    validator: Callable[[StructuredPayload, StructuredResponse], StructuredResponse],
) -> StructuredResponse:
    response = await provider.complete_structured(call_site, payload, schema)
    try:
        return validator(payload, response)
    except CallValidationError as exc:
        retry_payload = dict(payload)
        retry_payload["validation_error"] = str(exc)
        response = await provider.complete_structured(call_site, retry_payload, schema)
        return validator(payload, response)


def validate_c1_response(payload: StructuredPayload, response: StructuredResponse) -> StructuredResponse:
    chunks = _list(response, "chunks")
    payload_chunks = {_chunk_id(chunk): chunk for chunk in _list(payload, "chunks")}
    seen: set[str] = set()
    for item in chunks:
        chunk_id = _string(item, "chunk_id")
        if chunk_id not in payload_chunks:
            raise CallValidationError(f"Unknown C1 chunk_id: {chunk_id}")
        if chunk_id in seen:
            raise CallValidationError(f"Duplicate C1 chunk_id: {chunk_id}")
        seen.add(chunk_id)

        relevant = _bool(item, "relevant")
        placements = _list(item, "placements")
        reason = item.get("irrelevant_reason")
        if not relevant:
            if not isinstance(reason, str) or not reason.strip():
                raise CallValidationError("irrelevant chunks require irrelevant_reason")
            if placements:
                raise CallValidationError("irrelevant chunks must not include placements")
            continue
        if reason is not None:
            raise CallValidationError("relevant chunks require irrelevant_reason=null")
        if not placements:
            raise CallValidationError("relevant chunks require at least one placement")

        chunk_text = str(payload_chunks[chunk_id].get("text", ""))
        for placement in placements:
            _validate_c1_placement(payload, placement, chunk_text)
    return response


def validate_c2_response(payload: StructuredPayload, response: StructuredResponse) -> StructuredResponse:
    del payload
    if not isinstance(response.get("mermaid"), str) or not response["mermaid"].strip():
        raise CallValidationError("C2 requires non-empty mermaid")
    _list(response, "nodes")
    for edge in _list(response, "edges"):
        _string(edge, "from")
        _string(edge, "to")
        condition = edge.get("condition")
        if condition is not None and not isinstance(condition, str):
            raise CallValidationError("C2 edge condition must be a string or null")
    return response


def validate_c3_response(payload: StructuredPayload, response: StructuredResponse) -> StructuredResponse:
    del payload
    verdict = _string(response, "verdict")
    if verdict not in {"distinct", "duplicate", "conflict"}:
        raise CallValidationError(f"Unknown C3 verdict: {verdict}")
    conflict_type = response.get("conflict_type")
    if verdict == "conflict":
        if conflict_type not in {item.value for item in ConflictType}:
            raise CallValidationError("C3 conflict verdict requires a valid conflict_type")
    elif conflict_type is not None:
        raise CallValidationError("C3 non-conflict verdict requires conflict_type=null")
    recommendation = _string(response, "recommendation")
    if recommendation not in {item.value for item in ReviewDecision}:
        raise CallValidationError(f"Unknown C3 recommendation: {recommendation}")
    if not _string(response, "rationale").strip():
        raise CallValidationError("C3 rationale is required")
    return response


def validate_c4_response(payload: StructuredPayload, response: StructuredResponse) -> StructuredResponse:
    del payload
    if not _string(response, "content").strip():
        raise CallValidationError("C4 content is required")
    if _string(response, "excerpt_policy") != "keep_both":
        raise CallValidationError("C4 excerpt_policy must be keep_both")
    return response


def validate_c5_response(payload: StructuredPayload, response: StructuredResponse) -> StructuredResponse:
    summary = _string(response, "summary_markdown").strip()
    max_words = int(payload.get("max_words") or 300)
    if not summary:
        raise CallValidationError("C5 summary_markdown is required")
    if len(summary.split()) > max_words:
        raise CallValidationError(f"C5 summary must be at most {max_words} words")
    response["summary_markdown"] = summary
    return response


def validate_c6_response(payload: StructuredPayload, response: StructuredResponse) -> StructuredResponse:
    segments = _list(response, "segments")
    payload_ids = {_chunk_id(segment) for segment in _list(payload, "segments")}
    seen: set[str] = set()
    for segment in segments:
        chunk_id = _string(segment, "chunk_id")
        if chunk_id not in payload_ids:
            raise CallValidationError(f"Unknown C6 chunk_id: {chunk_id}")
        if chunk_id in seen:
            raise CallValidationError(f"Duplicate C6 chunk_id: {chunk_id}")
        seen.add(chunk_id)
        relevant = _bool(segment, "relevant")
        reason = segment.get("reason")
        superseded_by = segment.get("superseded_by_chunk_id")
        if not relevant and (not isinstance(reason, str) or not reason.strip()):
            raise CallValidationError("irrelevant transcript segments require reason")
        if superseded_by is not None and superseded_by not in payload_ids:
            raise CallValidationError(f"Unknown superseded_by_chunk_id: {superseded_by}")
        _nullable_string(segment, "key_hint")
        _nullable_string(segment, "source_date_extracted")
    return response


def _validate_c1_placement(payload: StructuredPayload, placement: Mapping[str, Any], chunk_text: str) -> None:
    existing_page_id = _validate_page_target(payload, placement)
    section = _string(placement, "section")
    if section not in CANONICAL_SECTIONS:
        raise CallValidationError(f"Unknown canonical section: {section}")

    block = placement.get("block")
    if not isinstance(block, Mapping):
        raise CallValidationError("placement block must be an object")
    key = _string(block, "key")
    try:
        validate_semantic_key(key)
    except SemanticKeyError as exc:
        raise CallValidationError(str(exc)) from exc
    if _string(block, "type") not in {item.value for item in BlockType}:
        raise CallValidationError(f"Unknown block type: {block.get('type')}")
    if not _string(block, "content").strip():
        raise CallValidationError("block content is required")
    excerpt = _string(block, "excerpt")
    if not _is_substring_modulo_whitespace(excerpt, chunk_text):
        raise CallValidationError("block excerpt must be a verbatim substring of the chunk text")

    inventory = _key_inventory(payload, existing_page_id)
    justification = block.get("new_key_justification")
    if key not in inventory and (not isinstance(justification, str) or not justification.strip()):
        raise CallValidationError("new keys require new_key_justification")


def _validate_page_target(payload: StructuredPayload, placement: Mapping[str, Any]) -> str | None:
    page = placement.get("page")
    new_page = placement.get("new_page")
    if page is None and new_page is None:
        raise CallValidationError("placement requires page or new_page")
    if page is not None and new_page is not None:
        raise CallValidationError("placement cannot include both page and new_page")
    if page is None:
        _validate_new_page(new_page)
        return None
    if not isinstance(page, Mapping):
        raise CallValidationError("page must be an object")
    existing_page_id = _string(page, "existing_page_id")
    registry_ids = {str(item.get("id")) for item in _list(payload, "registry")}
    if existing_page_id not in registry_ids:
        raise CallValidationError(f"Unknown existing_page_id: {existing_page_id}")
    return existing_page_id


def _validate_new_page(new_page: Any) -> None:
    if not isinstance(new_page, Mapping):
        raise CallValidationError("new_page must be an object")
    for field in ("title", "description", "domain", "path_slug"):
        if not _string(new_page, field).strip():
            raise CallValidationError(f"new_page requires {field}")
    assertion = new_page.get("no_registry_match_assertion")
    if not isinstance(assertion, str) or not assertion.strip():
        raise CallValidationError("new_page requires no_registry_match_assertion")


def _key_inventory(payload: StructuredPayload, page_id: str | None) -> set[str]:
    if page_id is None:
        return set()
    context = payload.get("page_context", {})
    if not isinstance(context, Mapping):
        return set()
    page_context = context.get(page_id, {})
    if not isinstance(page_context, Mapping):
        return set()
    return {str(key) for key in _as_list(page_context.get("key_inventory", []))}


def _is_substring_modulo_whitespace(needle: str, haystack: str) -> bool:
    normalized_needle = _normalize_whitespace(needle)
    normalized_haystack = _normalize_whitespace(haystack)
    return bool(normalized_needle) and normalized_needle in normalized_haystack


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _chunk_id(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise CallValidationError("chunk payload entries must be objects")
    return str(value.get("id") or value.get("chunk_id") or "")


def _list(value: Mapping[str, Any], field: str) -> list[Any]:
    raw = value.get(field)
    if not isinstance(raw, list):
        raise CallValidationError(f"{field} must be a list")
    return raw


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string(value: Mapping[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str):
        raise CallValidationError(f"{field} must be a string")
    return raw


def _nullable_string(value: Mapping[str, Any], field: str) -> str | None:
    raw = value.get(field)
    if raw is not None and not isinstance(raw, str):
        raise CallValidationError(f"{field} must be a string or null")
    return raw


def _bool(value: Mapping[str, Any], field: str) -> bool:
    raw = value.get(field)
    if not isinstance(raw, bool):
        raise CallValidationError(f"{field} must be a boolean")
    return raw

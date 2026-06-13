from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment, close_marker
from core.models import BlockStatus


class BlockSerializationError(ValueError):
    pass


class PageWriteConflict(RuntimeError):
    pass


def serialize_page(page: Page, *, verify: bool = True) -> str:
    text = _serialize_segments(page.segments)
    if verify:
        from core.blocks.parser import parse_page

        reparsed = parse_page(text)
        if serialize_page(reparsed, verify=False) != text:
            raise BlockSerializationError("Page failed byte-identical round-trip verification")
    return text


def _serialize_segments(segments: list[ProseSegment | BlockSegment]) -> str:
    parts: list[str] = []
    for index, segment in enumerate(segments):
        parts.append(_serialize_segment(segment))
        if isinstance(segment, BlockSegment) and index + 1 < len(segments):
            next_text = _serialize_segment(segments[index + 1])
            if next_text and not next_text.startswith("\n"):
                parts.append("\n")
    return "".join(parts)


def serialize_block(block: ContextBlock) -> str:
    if block.status in {BlockStatus.rejected, BlockStatus.deleted}:
        raise BlockSerializationError(f"{block.status.value} blocks must not be serialized to page markdown")
    _validate_close_marker_injection(block)
    meta = _metadata_for_block(block)
    open_marker = f"<!-- cb {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->"
    body = "\n".join([block.content, *_pending_marker_lines(block), _excerpt_quote(block)])
    return f"{open_marker}\n{body}\n{close_marker(block.id)}"


def write_page(path: str | Path, page: Page, *, expected_text: str | None = None, encoding: str = "utf-8") -> Path:
    page_path = Path(path)
    if expected_text is not None and page_path.exists() and page_path.read_text(encoding=encoding) != expected_text:
        raise PageWriteConflict(f"{page_path} changed since it was parsed")

    text = serialize_page(page)
    page_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = page_path.with_name(f".{page_path.name}.tmp")
    try:
        temp_path.write_text(text, encoding=encoding)
        os.replace(temp_path, page_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return page_path


def _serialize_segment(segment: ProseSegment | BlockSegment) -> str:
    if isinstance(segment, ProseSegment):
        return segment.text
    return serialize_block(segment.block)


def _metadata_for_block(block: ContextBlock) -> dict[str, object]:
    meta: dict[str, object] = {
        "id": block.id,
        "key": block.key,
        "type": block.type.value,
        "status": block.status.value,
        "source_path": block.source_path,
        "source_date": block.source_date,
        "chunks": block.chunk_ids,
        "user_edited": block.user_edited,
    }
    if block.status == BlockStatus.needs_review and block.needs_review_reason is not None:
        meta["needs_review_reason"] = block.needs_review_reason
    return meta


def _pending_marker_lines(block: ContextBlock) -> list[str]:
    return [f"\u26a0 pending review: {row_id}" for row_id in block.pending_review_ids]


def _excerpt_quote(block: ContextBlock) -> str:
    source = block.source_path
    include_date = block.excerpt_source_date if block.excerpt_source_date is not None else block.source_date != "unknown"
    if include_date:
        source = f"{source}, {block.source_date}"

    excerpt_lines = block.excerpt.split("\n")
    first = f"> Excerpt ({source}): {excerpt_lines[0]}"
    continuation = [f"> {line}" for line in excerpt_lines[1:]]
    return "\n".join([first, *continuation])


def _validate_close_marker_injection(block: ContextBlock) -> None:
    try:
        type(block)(**block.model_dump())
    except ValidationError as exc:
        raise BlockSerializationError(str(exc)) from exc

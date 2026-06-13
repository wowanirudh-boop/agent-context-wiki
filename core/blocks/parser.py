from __future__ import annotations

import json
from typing import Any

from core.blocks.model import (
    PENDING_MARKER_PREFIX,
    BlockSegment,
    ContextBlock,
    Page,
    ProseSegment,
    close_marker,
)
from core.models import BlockStatus

OPEN_PREFIX = "<!-- cb {"
OPEN_SUFFIX = " -->"
EXCERPT_PREFIX = "> Excerpt ("

BASE_META_KEYS = ["id", "key", "type", "status", "source_path", "source_date", "chunks", "user_edited"]
NEEDS_REVIEW_META_KEYS = [*BASE_META_KEYS, "needs_review_reason"]


class BlockParseError(ValueError):
    pass


def parse_page(markdown: str) -> Page:
    segments = []
    position = 0
    while True:
        open_start = markdown.find(OPEN_PREFIX, position)
        if open_start == -1:
            if position < len(markdown):
                segments.append(ProseSegment(markdown[position:]))
            break

        if open_start > position:
            segments.append(ProseSegment(markdown[position:open_start]))

        marker_end = markdown.find("\n", open_start)
        if marker_end == -1:
            raise BlockParseError("Block open marker must be followed by a newline")

        marker_line = markdown[open_start:marker_end]
        meta = _parse_open_marker(marker_line)
        block_id = str(meta["id"])
        close = close_marker(block_id)
        close_start, close_end = _find_close_line(markdown, marker_end + 1, close)
        raw_body = markdown[marker_end + 1 : close_start]
        if not raw_body.endswith("\n"):
            raise BlockParseError("Block body must end with a newline before close marker")

        content, excerpt, pending_ids, excerpt_source_date = _parse_body(raw_body[:-1], meta)
        segments.append(
            BlockSegment(
                ContextBlock(
                    id=block_id,
                    key=meta["key"],
                    type=meta["type"],
                    status=meta["status"],
                    source_path=meta["source_path"],
                    source_date=meta["source_date"],
                    chunk_ids=meta["chunks"],
                    user_edited=meta["user_edited"],
                    content=content,
                    excerpt=excerpt,
                    needs_review_reason=meta.get("needs_review_reason"),
                    pending_review_ids=pending_ids,
                    excerpt_source_date=excerpt_source_date,
                ),
            ),
        )
        position = close_end
        if position < len(markdown) and markdown[position] == "\n":
            position += 1
            if position <= len(markdown):
                segments.append(ProseSegment("\n"))

    return _merge_adjacent_prose(segments)


def _parse_open_marker(marker_line: str) -> dict[str, Any]:
    if not marker_line.startswith("<!-- cb ") or not marker_line.endswith(OPEN_SUFFIX):
        raise BlockParseError(f"Malformed block open marker: {marker_line}")
    raw_json = marker_line.removeprefix("<!-- cb ").removesuffix(OPEN_SUFFIX)
    try:
        meta = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise BlockParseError(f"Malformed block metadata JSON: {exc.msg}") from exc
    if not isinstance(meta, dict):
        raise BlockParseError("Block metadata must be a JSON object")
    keys = list(meta.keys())
    if meta.get("status") == BlockStatus.needs_review.value:
        if keys not in (BASE_META_KEYS, NEEDS_REVIEW_META_KEYS):
            raise BlockParseError(f"Block metadata keys must be {BASE_META_KEYS} or {NEEDS_REVIEW_META_KEYS}")
    elif keys != BASE_META_KEYS:
        raise BlockParseError(f"Block metadata keys must be {BASE_META_KEYS}")
    return meta


def _find_close_line(markdown: str, start: int, close: str) -> tuple[int, int]:
    search_from = start
    while True:
        close_start = markdown.find(close, search_from)
        if close_start == -1:
            raise BlockParseError(f"Missing close marker {close}")
        close_end = close_start + len(close)
        starts_line = close_start == start or markdown[close_start - 1] == "\n"
        ends_line = close_end == len(markdown) or markdown[close_end] == "\n"
        if starts_line and ends_line:
            return close_start, close_end
        search_from = close_start + 1


def _parse_body(body: str, meta: dict[str, Any]) -> tuple[str, str, list[str], bool]:
    lines = body.split("\n")
    excerpt_index = _find_excerpt_index(lines)
    content_lines = lines[:excerpt_index]
    excerpt_lines = lines[excerpt_index:]

    pending_ids: list[str] = []
    while content_lines and content_lines[-1].startswith(PENDING_MARKER_PREFIX):
        pending_ids.insert(0, content_lines.pop().removeprefix(PENDING_MARKER_PREFIX))

    first_excerpt = excerpt_lines[0]
    first_text, excerpt_source_date = _parse_excerpt_first_line(first_excerpt, meta)
    continuation = []
    for line in excerpt_lines[1:]:
        if not line.startswith("> "):
            raise BlockParseError("Excerpt continuation lines must be blockquotes")
        continuation.append(line[2:])
    return "\n".join(content_lines), "\n".join([first_text, *continuation]), pending_ids, excerpt_source_date


def _find_excerpt_index(lines: list[str]) -> int:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith(EXCERPT_PREFIX):
            return index
    raise BlockParseError("Block body is missing an excerpt quote")


def _parse_excerpt_first_line(line: str, meta: dict[str, Any]) -> tuple[str, bool]:
    close = line.find("): ")
    if close == -1:
        raise BlockParseError("Malformed excerpt quote")
    source = line[len(EXCERPT_PREFIX) : close]
    excerpt_source_date = False
    if ", " in source:
        source_path, source_date = source.rsplit(", ", 1)
        excerpt_source_date = True
        if source_date != meta["source_date"]:
            raise BlockParseError("Excerpt source_date does not match metadata")
    else:
        source_path = source
    if source_path != meta["source_path"]:
        raise BlockParseError("Excerpt source_path does not match metadata")
    return line[close + 3 :], excerpt_source_date


def _merge_adjacent_prose(segments: list[ProseSegment | BlockSegment]) -> Page:
    merged: list[ProseSegment | BlockSegment] = []
    for segment in segments:
        if isinstance(segment, ProseSegment) and not segment.text:
            continue
        if isinstance(segment, ProseSegment) and merged and isinstance(merged[-1], ProseSegment):
            merged[-1].text += segment.text
        else:
            merged.append(segment)
    return Page(merged)

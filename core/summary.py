from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from core.blocks.model import BlockSegment, ContextBlock, Page
from core.blocks.mutations import set_generated_section
from core.blocks.parser import parse_page
from core.blocks.serializer import serialize_page, write_page
from core.config import ACWConfig, load_config
from core.db.dao import ACWDao, Row
from core.llm.calls import C5_SCHEMA, complete_validated, validate_c5_response
from core.llm.provider import LLMProvider
from core.models import BlockStatus
from core.registry import PageRegistry

_SUMMARY_FINGERPRINT_PREFIX = "<!-- acw:summary-fingerprint "


@dataclass(frozen=True, slots=True)
class SummaryRenderResult:
    page_writes: int
    index_written: bool


async def render_summaries_and_index(
    workspace: str | Path,
    db: aiosqlite.Connection,
    *,
    run_id: str,
    provider: LLMProvider,
    config: ACWConfig | None = None,
) -> SummaryRenderResult:
    ws = Path(workspace)
    cfg = config or load_config(ws)
    dao = ACWDao(db)
    registry = PageRegistry(dao)
    pages = [page for page in await registry.list_pages() if page["status"] == "active"]
    page_writes = 0
    for page in pages:
        page_path = ws / str(page["path"])
        old_text = page_path.read_text(encoding="utf-8") if page_path.exists() else f"# {page['title']}\n"
        page_model = parse_page(old_text)
        current_blocks = [block for block in page_model.blocks if block.status == BlockStatus.current]
        fingerprint = current_block_fingerprint(current_blocks)
        if _summary_is_current(old_text, fingerprint):
            continue
        summary = await _summary_for_page(page, current_blocks, provider=provider, max_words=cfg.summary_max_words)
        updated = set_generated_section(
            page_model,
            "Summary",
            f"{_summary_fingerprint_line(fingerprint)}\n{summary}",
            run_id=run_id,
        )
        new_text = serialize_page(updated)
        if new_text != old_text:
            write_page(page_path, updated, expected_text=old_text if page_path.exists() else None)
            page_writes += 1

    index_written = _write_index_if_changed(ws, _index_markdown(pages, run_id=run_id))
    return SummaryRenderResult(page_writes=page_writes, index_written=index_written)


def current_block_fingerprint(blocks: list[ContextBlock]) -> str:
    payload = [
        {
            "id": block.id,
            "key": block.key,
            "type": block.type.value,
            "content": block.content,
        }
        for block in blocks
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_summary_markdown(markdown: str) -> str:
    body = _section_body(markdown, "Summary")
    if body is None:
        return ""
    lines = [
        line
        for line in body.splitlines()
        if not line.startswith("<!-- acw:generated Summary run=")
        and not line.startswith(_SUMMARY_FINGERPRINT_PREFIX)
    ]
    return "\n".join(lines).strip()


async def _summary_for_page(
    page: Row,
    blocks: list[ContextBlock],
    *,
    provider: LLMProvider,
    max_words: int,
) -> str:
    if not blocks:
        return "No current context blocks."
    payload = {
        "title": page["title"],
        "max_words": max_words,
        "blocks": [
            {
                "key": block.key,
                "type": block.type.value,
                "content": block.content,
            }
            for block in blocks
        ],
    }
    response = await complete_validated(provider, "C5", payload, C5_SCHEMA, validate_c5_response)
    return str(response["summary_markdown"])


def blocks_with_sections(page: Page) -> list[dict[str, Any]]:
    section = ""
    output: list[dict[str, Any]] = []
    for segment in page.segments:
        if isinstance(segment, BlockSegment):
            block = segment.block
            output.append(
                {
                    "id": block.id,
                    "key": block.key,
                    "type": block.type.value,
                    "status": block.status.value,
                    "source_path": block.source_path,
                    "source_date": block.source_date,
                    "section": section,
                }
            )
            continue
        for line in segment.text.splitlines():
            if line.startswith("## ") and not line.startswith("### "):
                section = line[3:].strip()
    return output


def filtered_page_by_status(page: Page, statuses: list[str]) -> Page:
    if "*" in statuses:
        return page
    allowed = set(statuses)
    segments = [
        segment
        for segment in page.segments
        if not isinstance(segment, BlockSegment) or segment.block.status.value in allowed
    ]
    return Page(segments)


def _index_markdown(pages: list[Row], *, run_id: str) -> str:
    lines = [
        "# Wiki Index",
        f"<!-- acw:generated _index.md run={run_id} \u2014 manual edits will be overwritten -->",
        "",
    ]
    grouped: dict[str, list[Row]] = {}
    for page in sorted(pages, key=lambda row: (str(row["domain"]).casefold(), str(row["title"]).casefold())):
        grouped.setdefault(str(page["domain"] or "general"), []).append(page)
    for domain, domain_pages in grouped.items():
        lines.append(f"## {domain}")
        for page in domain_pages:
            link = _index_link(str(page["path"]))
            description = str(page["description"] or "").strip()
            suffix = f" - {description}" if description else ""
            lines.append(f"- [{page['title']}]({link}){suffix}")
        lines.append("")
    if not grouped:
        lines.append("_No active pages._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_index_if_changed(workspace: Path, desired: str) -> bool:
    path = workspace / "wiki" / "_index.md"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _generated_index_body(existing) == _generated_index_body(desired):
            return False
        if existing == desired:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desired, encoding="utf-8")
    return True


def _generated_index_body(markdown: str) -> str:
    lines = markdown.splitlines()
    if len(lines) < 2 or not lines[1].startswith("<!-- acw:generated _index.md run="):
        return markdown
    return "\n".join([lines[0], *lines[2:]]).strip()


def _summary_is_current(markdown: str, fingerprint: str) -> bool:
    body = _section_body(markdown, "Summary")
    if body is None:
        return False
    lines = body.splitlines()
    if not lines or not lines[0].startswith("<!-- acw:generated Summary run="):
        return False
    return _summary_fingerprint_line(fingerprint) in lines


def _summary_fingerprint_line(fingerprint: str) -> str:
    return f"{_SUMMARY_FINGERPRINT_PREFIX}{fingerprint} -->"


def _section_body(markdown: str, section: str) -> str | None:
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {section}":
            start = index + 1
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith("## ") and line.strip() != f"## {section}":
            end = index
            break
    return "\n".join(lines[start:end]).strip("\n")


def _index_link(page_path: str) -> str:
    path = Path(page_path)
    try:
        return path.relative_to("wiki").as_posix()
    except ValueError:
        return os.path.relpath(path, "wiki").replace("\\", "/")

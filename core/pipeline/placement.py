from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite

from core.blocks.keys import key_inventory
from core.blocks.model import ContextBlock, Page, ProseSegment
from core.blocks.mutations import insert_block
from core.blocks.parser import parse_page
from core.blocks.serializer import serialize_page, write_page
from core.db.dao import ACWDao, Row, utc_now
from core.ids import new_id
from core.ledger import ChunkLedger
from core.models import BlockStatus, BlockType
from core.registry import PageRegistry


class PlacementWriter:
    def __init__(self, workspace: Path, db: aiosqlite.Connection, ledger: ChunkLedger, registry: PageRegistry) -> None:
        self.workspace = workspace
        self.db = db
        self.dao = ACWDao(db)
        self.ledger = ledger
        self.registry = registry
        self.page_writes = 0
        self.blocks_created = 0

    async def page_context_payload(self) -> dict[str, dict[str, list[str]]]:
        context: dict[str, dict[str, list[str]]] = {}
        for page in await self.registry.list_pages():
            if page["status"] != "active":
                continue
            page_model = _read_page_model(self.workspace / page["path"], page["title"])
            context[page["id"]] = {
                "key_inventory": key_inventory(page_model),
                "section_outline": _section_outline(page_model),
            }
        return context

    async def write_candidate(
        self,
        chunk: Row,
        placement: Mapping[str, Any],
        *,
        default_transcript_type: bool = False,
    ) -> str:
        page = await self._resolve_page(placement)
        block_data = placement["block"]
        block_type = str(block_data["type"])
        if default_transcript_type and block_type not in {"decision", "note"}:
            block_type = "decision" if "decision" in str(block_data["content"]).casefold() else "note"
        block = ContextBlock(
            id=new_id("cb"),
            key=str(block_data["key"]),
            type=BlockType(block_type),
            status=BlockStatus.current,
            source_id=str(chunk["source_id"]),
            source_path=str(chunk["source_path"]),
            source_date=str(chunk.get("source_date") or "unknown"),
            chunk_ids=[str(chunk["id"])],
            user_edited=False,
            content=str(block_data["content"]),
            excerpt=_excerpt_for_block(str(block_data["excerpt"])),
        )

        # M5 seam: conflict/duplicate detection will replace this direct-write path.
        await self._insert_clean_block(page, str(placement["section"]), block)
        await self.ledger.mark_placed(str(chunk["id"]), block_ids=[block.id])
        return block.id

    async def _resolve_page(self, placement: Mapping[str, Any]) -> Row:
        page_ref = placement.get("page")
        if isinstance(page_ref, Mapping) and page_ref.get("existing_page_id"):
            page = await self.registry.get_page(str(page_ref["existing_page_id"]))
            if page is None:
                raise KeyError(page_ref["existing_page_id"])
            return page

        new_page = placement.get("new_page")
        if not isinstance(new_page, Mapping):
            raise ValueError("placement requires page or new_page")
        path = _unique_page_path(await self.registry.list_pages(), str(new_page["path_slug"]))
        return await self.registry.create_page(
            title=str(new_page["title"]),
            path=path,
            description=str(new_page["description"]),
            domain=str(new_page["domain"]),
        )

    async def _insert_clean_block(self, page_row: Row, section: str, block: ContextBlock) -> None:
        path = self.workspace / page_row["path"]
        old_text = path.read_text(encoding="utf-8") if path.exists() else f"# {page_row['title']}\n"
        page = parse_page(old_text)
        updated = insert_block(page, section, block)
        new_text = serialize_page(updated)
        content_hash = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
        await self.dao.create_block(
            block_id=block.id,
            page_id=str(page_row["id"]),
            key=block.key,
            block_type=block.type.value,
            status=block.status.value,
            source_id=str(block.source_id),
            source_path=block.source_path,
            source_date=block.source_date,
            content_hash=content_hash,
            created_at=utc_now(),
            updated_at=utc_now(),
            user_edited=block.user_edited,
        )
        write_page(path, updated, expected_text=old_text if path.exists() else None)
        self.page_writes += 1
        self.blocks_created += 1


def placement_registry_payload(pages: list[Row]) -> list[dict[str, Any]]:
    return [
        {
            "id": page["id"],
            "title": page["title"],
            "description": page["description"],
            "aliases": page.get("aliases", []),
        }
        for page in pages
        if page["status"] == "active"
    ]


def _read_page_model(path: Path, title: str) -> Page:
    if not path.exists():
        return Page([ProseSegment(f"# {title}\n")])
    return parse_page(path.read_text(encoding="utf-8"))


def _section_outline(page: Page) -> list[str]:
    headings: list[str] = []
    for segment in page.segments:
        if not isinstance(segment, ProseSegment):
            continue
        for line in segment.text.splitlines():
            if line.startswith("## ") and not line.startswith("### "):
                headings.append(line[3:].strip())
    return headings


def _unique_page_path(pages: list[Row], path_slug: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", path_slug.casefold()).strip("-") or "general"
    existing = {str(page["path"]) for page in pages}
    candidate = f"wiki/{slug}.md"
    index = 2
    while candidate in existing:
        candidate = f"wiki/{slug}-{index}.md"
        index += 1
    return candidate


def _excerpt_for_block(excerpt: str) -> str:
    if len(excerpt) <= 1500:
        return excerpt
    return f"{excerpt[:748]}[...] {excerpt[-748:]}"

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import aiosqlite
from mcp.server.fastmcp import Context, FastMCP

from core.blocks.parser import parse_page
from core.blocks.serializer import serialize_page
from core.coverage import coverage_report_markdown
from core.db.dao import ACWDao, Row
from core.registry import PageRegistry
from core.summary import blocks_with_sections, extract_summary_markdown, filtered_page_by_status

_DEFAULT_STATUSES = ["current", "conflicted", "needs_review"]
_READ_DOC = (
    "Agent Context Wiki two-tier read tool. Start with wiki_index(), then wiki_summary(page), "
    "and only call wiki_page(page) for the few pages that need full evidence. Status semantics: "
    "wiki_page defaults to current/conflicted/needs_review markdown; pass statuses=['*'] to include "
    "deprecated history, rejected/deleted records that are still present, and any other status."
)


class WikiReadHandler:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    async def wiki_index(self) -> dict[str, str]:
        path = self.workspace / "wiki" / "_index.md"
        return {"markdown": path.read_text(encoding="utf-8") if path.exists() else ""}

    async def wiki_summary(self, page: str) -> dict[str, str]:
        async with self._connect() as db:
            page_row = await self._resolve_page(db, page)
        markdown = self._read_page(page_row)
        return {
            "page": str(page_row["path"]),
            "title": str(page_row["title"]),
            "summary_markdown": extract_summary_markdown(markdown),
        }

    async def wiki_page(self, page: str, statuses: list[str] | None = None) -> dict[str, Any]:
        async with self._connect() as db:
            page_row = await self._resolve_page(db, page)
        markdown = self._read_page(page_row)
        model = parse_page(markdown)
        requested = statuses or list(_DEFAULT_STATUSES)
        filtered = filtered_page_by_status(model, requested)
        return {
            "page": str(page_row["path"]),
            "title": str(page_row["title"]),
            "markdown": serialize_page(filtered),
            "blocks": blocks_with_sections(model),
        }

    async def wiki_search(self, query: str, tier: str = "summary", limit: int = 10) -> dict[str, list[dict[str, Any]]]:
        normalized_tier = tier.strip().lower()
        if normalized_tier not in {"summary", "full"}:
            raise ValueError("tier must be 'summary' or 'full'")
        async with self._connect() as db:
            pages = [page for page in await PageRegistry(ACWDao(db)).list_pages() if page["status"] == "active"]
        if normalized_tier == "summary":
            results = await self._search_summaries(pages, query)
        else:
            results = await self._search_full_pages(pages, query)
        return {"results": results[: max(0, limit)]}

    async def wiki_coverage(self, source: str | None = None) -> str:
        async with self._connect() as db:
            return await coverage_report_markdown(self.workspace, db, source=source)

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.workspace / ".llmwiki" / "index.db")

    async def _resolve_page(self, db: aiosqlite.Connection, ref: str) -> Row:
        registry = PageRegistry(ACWDao(db))
        for candidate in _page_candidates(ref):
            page = await registry.resolve_page(candidate)
            if page is not None:
                return page
        raise KeyError(f"Unknown wiki page: {ref}")

    def _read_page(self, page: Row) -> str:
        path = self.workspace / str(page["path"])
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path.read_text(encoding="utf-8")

    async def _search_summaries(self, pages: list[Row], query: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in pages:
            markdown = self._read_page(page)
            summary = extract_summary_markdown(markdown)
            haystack = f"{page['title']} {page['description']} {summary}"
            score = _score(query, haystack)
            if score <= 0:
                continue
            results.append(
                {
                    "page": page["path"],
                    "title": page["title"],
                    "score": score,
                    "snippet": _snippet(haystack, query),
                    "tier": "summary",
                }
            )
        return sorted(results, key=lambda item: (-float(item["score"]), str(item["title"]).casefold()))

    async def _search_full_pages(self, pages: list[Row], query: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in pages:
            model = parse_page(self._read_page(page))
            best_score = 0.0
            best_text = ""
            for block in model.blocks:
                haystack = f"{block.key} {block.type.value} {block.status.value} {block.content} {block.excerpt}"
                score = _score(query, haystack)
                if score > best_score:
                    best_score = score
                    best_text = haystack
            if best_score <= 0:
                continue
            results.append(
                {
                    "page": page["path"],
                    "title": page["title"],
                    "score": best_score,
                    "snippet": _snippet(best_text, query),
                    "tier": "full",
                }
            )
        return sorted(results, key=lambda item: (-float(item["score"]), str(item["title"]).casefold()))


def register(mcp: FastMCP, get_user_id, fs_factory) -> None:
    del get_user_id, fs_factory

    @mcp.tool(name="wiki_index", description=f"{_READ_DOC} Returns {{markdown: str}} from wiki/_index.md.")
    async def wiki_index(ctx: Context) -> dict[str, str]:
        del ctx
        return await WikiReadHandler(_workspace_root()).wiki_index()

    @mcp.tool(
        name="wiki_summary",
        description=f"{_READ_DOC} Summary tier for one page by path, title, or alias.",
    )
    async def wiki_summary(ctx: Context, page: str) -> dict[str, str]:
        del ctx
        return await WikiReadHandler(_workspace_root()).wiki_summary(page)

    @mcp.tool(
        name="wiki_page",
        description=f"{_READ_DOC} Full page read with status-filtered markdown and complete block metadata.",
    )
    async def wiki_page(ctx: Context, page: str, statuses: list[str] | None = None) -> dict[str, Any]:
        del ctx
        return await WikiReadHandler(_workspace_root()).wiki_page(page, statuses=statuses)

    @mcp.tool(
        name="wiki_search",
        description=f"{_READ_DOC} Search tier='summary' for bounded reads or tier='full' for block evidence.",
    )
    async def wiki_search(ctx: Context, query: str, tier: str = "summary", limit: int = 10) -> dict[str, list[dict[str, Any]]]:
        del ctx
        return await WikiReadHandler(_workspace_root()).wiki_search(query, tier=tier, limit=limit)

    @mcp.tool(
        name="wiki_coverage",
        description=f"{_READ_DOC} Ledger-derived Source Coverage report with CLI parity.",
    )
    async def wiki_coverage(ctx: Context, source: str | None = None) -> str:
        del ctx
        return await WikiReadHandler(_workspace_root()).wiki_coverage(source=source)


def _workspace_root() -> Path:
    try:
        from vaultfs import sqlite as sqlite_vault
    except ImportError as exc:
        raise RuntimeError("wiki_* tools are available only for local SQLite workspaces") from exc
    root = getattr(sqlite_vault, "_workspace_root", None)
    if root is None:
        raise RuntimeError("wiki_* tools require an initialized local workspace")
    return Path(root)


def _page_candidates(ref: str) -> list[str]:
    cleaned = ref.strip().lstrip("/")
    candidates = [cleaned]
    if cleaned.startswith("wiki/"):
        candidates.append(cleaned.removeprefix("wiki/"))
    elif cleaned.endswith(".md"):
        candidates.append(f"wiki/{cleaned}")
    return list(dict.fromkeys(candidates))


def _score(query: str, text: str) -> float:
    lowered = text.casefold()
    terms = _terms(query)
    if not terms:
        return 0.0
    score = sum(lowered.count(term) for term in terms)
    phrase = query.strip().casefold()
    if phrase:
        score += lowered.count(phrase) * 5
    return float(score)


def _snippet(text: str, query: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    lowered = compact.casefold()
    positions = [lowered.find(term) for term in _terms(query) if lowered.find(term) >= 0]
    if not positions:
        return compact[:220]
    start = max(min(positions) - 80, 0)
    end = min(start + 220, len(compact))
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


def _terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[a-z0-9_]+", query.casefold()) if len(term) > 1]

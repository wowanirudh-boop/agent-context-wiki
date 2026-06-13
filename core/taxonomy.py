from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment
from core.blocks.mutations import insert_block
from core.blocks.parser import parse_page
from core.blocks.serializer import serialize_page, write_page
from core.db.dao import ACWDao
from core.db.migrate import apply_migrations
from core.gitops import commit_taxonomy_operation, ensure_wiki_repo
from core.lock import WorkspaceLock
from core.models import EventKind
from core.registry import PageRegistry

_WIKI_LINK_RE = re.compile(r"\[\[(?P<target>[^\]|#]+)(?P<anchor>#[^\]|]+)?(?P<label>\|[^\]]+)?\]\]")


@dataclass(frozen=True, slots=True)
class TaxonomyResult:
    operation: str
    changed_pages: list[str]
    committed: bool
    commit_message: str


@dataclass(slots=True)
class _TaxonomyContext:
    workspace: Path
    db: aiosqlite.Connection
    dao: ACWDao
    registry: PageRegistry
    changed_pages: set[str] = field(default_factory=set)


async def merge_pages(workspace: str | Path, source_page: str, target_page: str) -> TaxonomyResult:
    ws = Path(workspace)
    with WorkspaceLock(ws, "taxonomy"):
        async with _connect(ws) as ctx:
            source = await _resolve(ctx.registry, source_page)
            target = await _resolve(ctx.registry, target_page)
            source_path = ws / str(source["path"])
            target_path = ws / str(target["path"])
            source_text = source_path.read_text(encoding="utf-8") if source_path.exists() else f"# {source['title']}\n"
            target_text = target_path.read_text(encoding="utf-8") if target_path.exists() else f"# {target['title']}\n"
            source_model = parse_page(source_text)
            target_model = parse_page(target_text)
            for block, section in _blocks_with_sections(source_model):
                target_model = insert_block(target_model, section if section in _canonical_sections() else "Rules", block)
                await ctx.db.execute("UPDATE acw_blocks SET page_id = ? WHERE id = ?", (target["id"], block.id))
            write_page(target_path, target_model, expected_text=target_text if target_path.exists() else None)
            ctx.changed_pages.add(str(target["path"]))
            _write_redirect_stub(source_path, old_title=str(source["title"]), target_title=str(target["title"]), reason="Merged into")
            ctx.changed_pages.add(str(source["path"]))
            aliases = list(dict.fromkeys([*source.get("aliases", []), str(source["title"])]))
            await ctx.registry.update_page(str(source["id"]), status=f"merged_into:{target['id']}", aliases=aliases)
            await _rewrite_inbound_links(ctx, old_page=source, new_title=str(target["title"]))
            await ctx.dao.write_event(
                kind=EventKind.taxonomy_merge,
                actor="core.taxonomy",
                payload={"source_page_id": source["id"], "target_page_id": target["id"]},
            )
            await ctx.db.commit()
        commit = commit_taxonomy_operation(ws, "merge", f"{source_page} -> {target_page}")
        return TaxonomyResult("merge", sorted(ctx.changed_pages), commit.committed, commit.message)


async def rename_page(workspace: str | Path, page: str, new_title: str) -> TaxonomyResult:
    ws = Path(workspace)
    with WorkspaceLock(ws, "taxonomy"):
        async with _connect(ws) as ctx:
            row = await _resolve(ctx.registry, page)
            old_title = str(row["title"])
            old_path = str(row["path"])
            new_path = _unique_path(await ctx.registry.list_pages(), _slug(new_title), exclude_page_id=str(row["id"]))
            old_file = ws / old_path
            new_file = ws / new_path
            text = old_file.read_text(encoding="utf-8") if old_file.exists() else f"# {old_title}\n"
            model = _rename_heading(parse_page(text), old_title, new_title)
            write_page(new_file, model, expected_text=None)
            _write_redirect_stub(old_file, old_title=old_title, target_title=new_title, reason="Renamed to")
            aliases = list(dict.fromkeys([*row.get("aliases", []), old_title]))
            await ctx.registry.update_page(str(row["id"]), title=new_title, path=new_path, aliases=aliases)
            ctx.changed_pages.update({old_path, new_path})
            await _rewrite_inbound_links(ctx, old_page=row, new_title=new_title)
            await ctx.dao.write_event(
                kind=EventKind.taxonomy_rename,
                actor="core.taxonomy",
                payload={"page_id": row["id"], "old_title": old_title, "new_title": new_title},
            )
            await ctx.db.commit()
        commit = commit_taxonomy_operation(ws, "rename", f"{old_title} -> {new_title}")
        return TaxonomyResult("rename", sorted(ctx.changed_pages), commit.committed, commit.message)


async def split_page(
    workspace: str | Path,
    page: str,
    section_spec: str,
    *,
    new_title: str,
) -> TaxonomyResult:
    ws = Path(workspace)
    with WorkspaceLock(ws, "taxonomy"):
        async with _connect(ws) as ctx:
            source = await _resolve(ctx.registry, page)
            source_path = ws / str(source["path"])
            source_text = source_path.read_text(encoding="utf-8")
            source_model = parse_page(source_text)
            moving = [block for block, section in _blocks_with_sections(source_model) if section == section_spec]
            if not moving:
                raise ValueError(f"No blocks found in section {section_spec}")
            new_path = _unique_path(await ctx.registry.list_pages(), _slug(new_title))
            created = await ctx.registry.create_page(
                title=new_title,
                path=new_path,
                description=f"Split from {source['title']}",
                domain=str(source["domain"]),
            )
            new_model = Page([ProseSegment(f"# {new_title}\n\n")])
            for block in moving:
                new_model = insert_block(new_model, section_spec if section_spec in _canonical_sections() else "Rules", block)
                await ctx.db.execute("UPDATE acw_blocks SET page_id = ? WHERE id = ?", (created["id"], block.id))
            source_model = Page(
                [
                    segment
                    for segment in source_model.segments
                    if not (isinstance(segment, BlockSegment) and segment.block.id in {block.id for block in moving})
                ]
            )
            write_page(source_path, source_model, expected_text=source_text)
            write_page(ws / new_path, new_model, expected_text=None)
            ctx.changed_pages.update({str(source["path"]), new_path})
            await ctx.dao.write_event(
                kind=EventKind.taxonomy_split,
                actor="core.taxonomy",
                payload={"source_page_id": source["id"], "new_page_id": created["id"], "section": section_spec},
            )
            await ctx.db.commit()
        commit = commit_taxonomy_operation(ws, "split", f"{page}::{section_spec} -> {new_title}")
        return TaxonomyResult("split", sorted(ctx.changed_pages), commit.committed, commit.message)


@asynccontextmanager
async def _connect(workspace: Path):
    db = await aiosqlite.connect(workspace / ".llmwiki" / "index.db")
    await db.execute("PRAGMA foreign_keys=ON")
    await apply_migrations(db)
    ensure_wiki_repo(workspace)
    dao = ACWDao(db)
    ctx = _TaxonomyContext(workspace=workspace, db=db, dao=dao, registry=PageRegistry(dao))
    try:
        yield ctx
    finally:
        await db.close()


async def _resolve(registry: PageRegistry, ref: str):
    page = await registry.resolve_page(ref)
    if page is None:
        raise KeyError(ref)
    return page


def _blocks_with_sections(page: Page) -> list[tuple[ContextBlock, str]]:
    section = "Rules"
    output: list[tuple[ContextBlock, str]] = []
    for segment in page.segments:
        if isinstance(segment, ProseSegment):
            for line in segment.text.splitlines():
                if line.startswith("## ") and not line.startswith("### "):
                    section = line[3:].strip()
        elif isinstance(segment, BlockSegment):
            output.append((segment.block, section))
    return output


async def _rewrite_inbound_links(ctx: _TaxonomyContext, *, old_page, new_title: str) -> None:
    old_refs = _old_link_refs(old_page)
    for path in sorted((ctx.workspace / "wiki").rglob("*.md")):
        if "_meta" in path.parts or "_reviews" in path.parts:
            continue
        old_text = path.read_text(encoding="utf-8")
        try:
            model = parse_page(old_text)
        except ValueError:
            continue
        updated = _rewrite_model_links(model, old_refs, new_title)
        new_text = serialize_page(updated)
        if new_text != old_text:
            write_page(path, updated, expected_text=old_text)
            ctx.changed_pages.add(path.relative_to(ctx.workspace).as_posix())


def _rewrite_model_links(page: Page, old_refs: set[str], new_title: str) -> Page:
    segments = []
    for segment in page.segments:
        if isinstance(segment, ProseSegment):
            segments.append(ProseSegment(_rewrite_links(segment.text, old_refs, new_title)))
        elif segment.block.user_edited:
            segments.append(segment)
        else:
            segments.append(
                BlockSegment(
                    segment.block.model_copy(update={"content": _rewrite_links(segment.block.content, old_refs, new_title)})
                )
            )
    return Page(segments)


def _rewrite_links(text: str, old_refs: set[str], new_title: str) -> str:
    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        if _normalize_ref(target) not in old_refs:
            return match.group(0)
        anchor = match.group("anchor") or ""
        label = match.group("label") or ""
        return f"[[{new_title}{anchor}{label}]]"

    return _WIKI_LINK_RE.sub(replace, text)


def _rename_heading(page: Page, old_title: str, new_title: str) -> Page:
    segments = list(page.segments)
    if segments and isinstance(segments[0], ProseSegment):
        segments[0] = ProseSegment(re.sub(rf"\A# {re.escape(old_title)}\b", f"# {new_title}", segments[0].text, count=1))
    return Page(segments)


def _write_redirect_stub(path: Path, *, old_title: str, target_title: str, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\naliases:\n  - {old_title}\n---\n# {old_title}\n\n{reason} [[{target_title}]].\n"
    path.write_text(text, encoding="utf-8")


def _old_link_refs(page) -> set[str]:
    values = {str(page["title"]), str(page["path"]), str(page["path"]).removeprefix("wiki/"), Path(str(page["path"])).stem}
    values.update(str(alias) for alias in page.get("aliases", []))
    return {_normalize_ref(value) for value in values}


def _normalize_ref(value: str) -> str:
    cleaned = value.strip().strip("/")
    cleaned = cleaned.removeprefix("wiki/")
    if cleaned.endswith(".md"):
        cleaned = cleaned[:-3]
    return cleaned.casefold()


def _unique_path(pages: list[dict], slug: str, *, exclude_page_id: str | None = None) -> str:
    existing = {str(page["path"]) for page in pages if page["id"] != exclude_page_id}
    candidate = f"wiki/{slug}.md"
    index = 2
    while candidate in existing:
        candidate = f"wiki/{slug}-{index}.md"
        index += 1
    return candidate


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", title.casefold().replace(" ", "-")).strip("-") or "page"


def _canonical_sections() -> set[str]:
    from core.blocks.model import CANONICAL_SECTIONS

    return set(CANONICAL_SECTIONS)

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from core.blocks.mutations import set_generated_section
from core.blocks.parser import parse_page
from core.blocks.serializer import serialize_page, write_page
from core.db.dao import ACWDao, Row, fetch_all
from core.models import Disposition


@dataclass(frozen=True, slots=True)
class CoverageRenderResult:
    page_writes: int
    breakdown_writes: int


async def render_coverage_sections(
    workspace: str | Path,
    db: aiosqlite.Connection,
    *,
    run_id: str,
) -> CoverageRenderResult:
    ws = Path(workspace)
    dao = ACWDao(db)
    pages = [page for page in await dao.list_pages() if page["status"] == "active"]
    rows_by_source = await _ledger_rows_by_source(db)
    page_sources = await _page_sources(db)
    breakdown_writes = 0
    for source_id, rows in rows_by_source.items():
        if _write_generated_text_if_changed(
            _breakdown_path(ws, source_id),
            _breakdown_markdown(source_id, rows, run_id=run_id),
        ):
            breakdown_writes += 1

    page_writes = 0
    for page in pages:
        page_path = ws / str(page["path"])
        old_text = page_path.read_text(encoding="utf-8") if page_path.exists() else f"# {page['title']}\n"
        source_ids = sorted(page_sources.get(str(page["id"]), set()), key=lambda item: _source_sort_key(rows_by_source, item))
        body = _page_coverage_body(
            page_path=str(page["path"]),
            source_ids=source_ids,
            rows_by_source=rows_by_source,
        )
        if _generated_section_is_current(old_text, "Source Coverage", body):
            continue
        updated = set_generated_section(parse_page(old_text), "Source Coverage", body, run_id=run_id)
        new_text = serialize_page(updated)
        if new_text != old_text:
            write_page(page_path, updated, expected_text=old_text if page_path.exists() else None)
            page_writes += 1
    return CoverageRenderResult(page_writes=page_writes, breakdown_writes=breakdown_writes)


async def coverage_report_markdown(
    workspace: str | Path,
    db: aiosqlite.Connection,
    *,
    source: str | None = None,
    run_id: str = "coverage",
) -> str:
    ws = Path(workspace)
    rows_by_source = await _ledger_rows_by_source(db)
    if source:
        source_id = _resolve_source(rows_by_source, source)
        if source_id is None:
            return f"# Source Coverage\n\nNo ledger coverage found for `{source}`.\n"
        markdown = _breakdown_markdown(source_id, rows_by_source[source_id], run_id=run_id)
        _write_generated_text_if_changed(_breakdown_path(ws, source_id), markdown)
        return markdown

    lines = [
        "# Source Coverage",
        f"<!-- acw:generated Source Coverage run={run_id} \u2014 manual edits will be overwritten -->",
        "",
    ]
    for source_id in sorted(rows_by_source, key=lambda item: _source_sort_key(rows_by_source, item)):
        rows = rows_by_source[source_id]
        counts = _counts(rows)
        source_path = str(rows[0]["source_path"])
        lines.append(
            f"- `{source_path}`: {_usage_label(counts)} "
            f"({counts['placed']} of {counts['total']} chunks placed); "
            f"irrelevant: {counts['irrelevant']}; failed: {counts['failed']}; "
            f"pending/in-review: {counts['pending_review']}; "
            f"duplicate: {counts['duplicate']}.",
        )
    if len(lines) == 3:
        lines.append("_No ledger-tracked sources._")
    return "\n".join(lines).rstrip() + "\n"


async def _ledger_rows_by_source(db: aiosqlite.Connection) -> dict[str, list[Row]]:
    cursor = await db.execute(
        "SELECT cl.id, cl.source_id, cl.source_version_id, cl.document_chunk_id, cl.ordinal, "
        "cl.disposition, cl.disposition_reason, cl.duplicate_of_block_id, cl.attempts, "
        "d.relative_path AS source_path "
        "FROM acw_chunk_ledger cl "
        "JOIN documents d ON d.id = cl.source_id "
        "WHERE d.source_kind = 'source' "
        "ORDER BY d.relative_path, cl.ordinal, cl.id",
    )
    rows = await fetch_all(cursor)
    grouped: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["source_id"])].append(row)
    return dict(grouped)


async def _page_sources(db: aiosqlite.Connection) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    cursor = await db.execute(
        "SELECT page_id, source_id FROM acw_blocks "
        "WHERE status NOT IN ('rejected', 'deleted') ORDER BY page_id, source_id",
    )
    for row in await fetch_all(cursor):
        grouped[str(row["page_id"])].add(str(row["source_id"]))

    cursor = await db.execute(
        "SELECT page_id, candidate_json FROM acw_review_rows "
        "WHERE applied_at IS NULL AND candidate_json IS NOT NULL",
    )
    for row in await fetch_all(cursor):
        source_id = _candidate_source_id(row["candidate_json"])
        if source_id:
            grouped[str(row["page_id"])].add(source_id)
    return dict(grouped)


def _candidate_source_id(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("block"), dict):
        data = data["block"]
    if not isinstance(data, dict):
        return None
    source_id = data.get("source_id")
    return str(source_id) if source_id else None


def _page_coverage_body(
    *,
    page_path: str,
    source_ids: list[str],
    rows_by_source: dict[str, list[Row]],
) -> str:
    if not source_ids:
        return "_No ledger-tracked sources touch this page._"
    lines: list[str] = []
    footnotes: list[str] = []
    for source_id in source_ids:
        rows = rows_by_source.get(source_id, [])
        if not rows:
            continue
        counts = _counts(rows)
        source_path = str(rows[0]["source_path"])
        footnote = f"coverage-{_slug(source_id)}"
        link = _relative_coverage_link(page_path, source_id)
        lines.append(
            f"- `{source_path}`: {_usage_label(counts)} "
            f"({counts['placed']} of {counts['total']} chunks placed); "
            f"irrelevant: {counts['irrelevant']}; failed: {counts['failed']}; "
            f"pending/in-review: {counts['pending_review']}; duplicate: {counts['duplicate']}."
            f"[^{footnote}]",
        )
        footnotes.append(f"[^{footnote}]: Full chunk-level breakdown: [{source_id}]({link})")
    return "\n".join([*lines, "", *footnotes]).rstrip()


def _breakdown_markdown(source_id: str, rows: list[Row], *, run_id: str) -> str:
    source_path = str(rows[0]["source_path"]) if rows else source_id
    counts = _counts(rows)
    lines = [
        f"# Source Coverage: {source_path}",
        f"<!-- acw:generated Source Coverage run={run_id} \u2014 manual edits will be overwritten -->",
        "",
        f"- Source id: `{source_id}`",
        f"- Total tracked chunks: {counts['total']}",
        f"- Placed chunks: {counts['placed']}",
        f"- Irrelevant chunks: {counts['irrelevant']}",
        f"- Failed chunks: {counts['failed']}",
        f"- Pending or in-review chunks: {counts['pending_review']}",
        f"- Duplicate chunks: {counts['duplicate']}",
        "",
        "## Chunks",
        "",
        "| Chunk | Ordinal | Disposition | Reason | Duplicate Of |",
        "|---|---:|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            f"`{row['id']}` | {row['ordinal']} | {row['disposition']} | "
            f"{_cell(row['disposition_reason'])} | {_cell(row['duplicate_of_block_id'])} |",
        )
    return "\n".join(lines).rstrip() + "\n"


def _counts(rows: list[Row]) -> dict[str, int]:
    active = [row for row in rows if row["disposition"] != Disposition.superseded.value]
    return {
        "total": len(active),
        "placed": sum(row["disposition"] == Disposition.placed.value for row in active),
        "irrelevant": sum(row["disposition"] == Disposition.irrelevant.value for row in active),
        "failed": sum(row["disposition"] in {Disposition.failed.value, Disposition.failed_final.value} for row in active),
        "pending_review": sum(
            row["disposition"] in {Disposition.pending.value, Disposition.conflicted_pending.value}
            for row in active
        ),
        "duplicate": sum(row["disposition"] == Disposition.duplicate.value for row in active),
    }


def _usage_label(counts: dict[str, int]) -> str:
    if counts["total"] > 0 and counts["placed"] == counts["total"]:
        return "used fully"
    if counts["placed"] > 0:
        return "used partially"
    return "not used"


def _generated_section_is_current(markdown: str, section: str, desired_body: str) -> bool:
    body = _section_body(markdown, section)
    if body is None:
        return False
    lines = body.splitlines()
    if not lines or not lines[0].startswith(f"<!-- acw:generated {section} run="):
        return False
    return "\n".join(lines[1:]).strip() == desired_body.strip()


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


def _breakdown_path(workspace: Path, source_id: str) -> Path:
    return workspace / "wiki" / "_meta" / "coverage" / f"{source_id}.md"


def _relative_coverage_link(page_path: str, source_id: str) -> str:
    source = Path("wiki") / "_meta" / "coverage" / f"{source_id}.md"
    start = Path(page_path).parent
    return os.path.relpath(source, start).replace("\\", "/")


def _resolve_source(rows_by_source: dict[str, list[Row]], source: str) -> str | None:
    normalized = source.strip().strip("/")
    for source_id, rows in rows_by_source.items():
        if source_id == normalized:
            return source_id
        source_path = str(rows[0]["source_path"]) if rows else ""
        if source_path == normalized or Path(source_path).name == normalized:
            return source_id
    return None


def _source_sort_key(rows_by_source: dict[str, list[Row]], source_id: str) -> tuple[str, str]:
    rows = rows_by_source.get(source_id, [])
    return (str(rows[0]["source_path"]) if rows else "", source_id)


def _write_text_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _write_generated_text_if_changed(path: Path, text: str) -> bool:
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _without_generated_marker(existing) == _without_generated_marker(text):
            return False
        if existing == text:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _without_generated_marker(markdown: str) -> str:
    return "\n".join(
        line
        for line in markdown.splitlines()
        if not line.startswith("<!-- acw:generated Source Coverage run=")
    ).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-") or "source"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")

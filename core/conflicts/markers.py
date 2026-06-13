from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from core.blocks.mutations import add_pending_marker, set_generated_section
from core.blocks.parser import parse_page
from core.blocks.serializer import write_page
from core.db.dao import ACWDao, Row


async def apply_pending_conflict_marker(
    workspace: str | Path,
    db: aiosqlite.Connection,
    page_row: Row,
    existing_block_id: str,
    row_id: str,
    *,
    run_id: str,
) -> None:
    path = Path(workspace) / str(page_row["path"])
    old_text = path.read_text(encoding="utf-8")
    page = parse_page(old_text)
    updated = add_pending_marker(page, existing_block_id, row_id)
    body = await open_conflicts_body(db, str(page_row["id"]), path_text=None)
    updated = set_generated_section(updated, "Open Conflicts", body, run_id=run_id)
    write_page(path, updated, expected_text=old_text)


async def open_conflicts_body(
    db: aiosqlite.Connection,
    page_id: str,
    *,
    path_text: str | None = None,
) -> str:
    del path_text
    dao = ACWDao(db)
    rows = [
        row
        for row in await dao.list_review_rows(open_only=True)
        if row["page_id"] == page_id and row["row_kind"] == "conflict"
    ]
    if not rows:
        return "_No open conflicts._"
    lines = []
    blocks = {row["id"]: row for row in await dao.list_blocks_for_page(page_id)}
    for row in rows:
        block_id = str(row["existing_block_id"] or "")
        block = blocks.get(block_id)
        key = str(block["key"]) if block is not None else "unknown"
        candidate = _candidate_json(row)
        source = ""
        if isinstance(candidate, dict):
            source_path = candidate.get("source_path")
            if isinstance(source_path, str) and source_path:
                source = f" from {source_path}"
        lines.append(
            f"- {row['id']}: {row['conflict_type']} on `{key}`{source} "
            f"(recommendation: {row['recommendation']})"
        )
    return "\n".join(lines)


def _candidate_json(row: Row) -> object:
    raw = row.get("candidate_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None

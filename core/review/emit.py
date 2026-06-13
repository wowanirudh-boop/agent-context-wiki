from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import aiosqlite

from core.blocks.model import ContextBlock
from core.blocks.parser import parse_page
from core.db.dao import ACWDao, Row, json_dumps
from core.models import EventKind, ReviewRowKind
from core.registry import PageRegistry
from core.review.parse import unresolved_review_file


async def emit_review_file(workspace: str | Path, db: aiosqlite.Connection, run_id: str) -> Path | None:
    dao = ACWDao(db)
    rows = await dao.list_review_rows(run_id=run_id)
    if not rows:
        return None
    run = await dao.get_run(run_id)
    started_at = str(run["started_at"]) if run is not None else ""
    pages = {str(page["id"]): page for page in await PageRegistry(dao).list_pages()}
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["page_id"]), []).append(row)

    lines = [
        f"# Review RR-{run_id}",
        f"Run: {run_id} \u00b7 Started: {started_at} \u00b7 Rows: {len(rows)} \u00b7 Status: open",
        "",
    ]
    for page_id, page_rows in grouped.items():
        page = pages[page_id]
        lines.extend([f"## Page: [[{page['title']}]] ({page['path']})", ""])
        page_blocks = _page_blocks(Path(workspace), page)
        for row in page_rows:
            lines.extend(_render_row(row, page, page_blocks))
            lines.append("")

    review_dir = Path(workspace) / "wiki" / "_reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / f"RR-{run_id}.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    await dao.write_event(
        kind=EventKind.review_emitted,
        actor="core.review.emit",
        payload={"run_id": run_id, "path": path.relative_to(Path(workspace)).as_posix(), "rows": len(rows)},
    )
    return path


async def create_taxonomy_merge_review_rows(
    db: aiosqlite.Connection,
    run_id: str,
    *,
    threshold: float = 0.72,
) -> list[Row]:
    dao = ACWDao(db)
    pages = [page for page in await PageRegistry(dao).list_pages() if page["status"] == "active"]
    existing_pairs = {
        _taxonomy_pair_key(row)
        for row in await dao.list_review_rows(run_id=run_id)
        if row["row_kind"] == ReviewRowKind.taxonomy_merge.value
    }
    rows: list[Row] = []
    for index, left in enumerate(pages):
        for right in pages[index + 1 :]:
            score = _registry_similarity(left, right)
            if score < threshold:
                continue
            pair_key = f"{left['id']}::{right['id']}"
            if pair_key in existing_pairs:
                continue
            rows.append(
                await dao.create_review_row(
                    run_id=run_id,
                    page_id=str(left["id"]),
                    row_kind=ReviewRowKind.taxonomy_merge.value,
                    recommendation="merge",
                    candidate_json=json_dumps(
                        {
                            "page_id": right["id"],
                            "title": right["title"],
                            "path": right["path"],
                            "description": right["description"],
                            "similarity": round(score, 3),
                        }
                    ),
                )
            )
    return rows


def find_unresolved_review_files(workspace: str | Path) -> list[str]:
    root = Path(workspace)
    review_dir = root / "wiki" / "_reviews"
    if not review_dir.exists():
        return []
    unresolved = []
    for path in sorted(review_dir.glob("RR-*.md")):
        if unresolved_review_file(path.read_text(encoding="utf-8")):
            unresolved.append(path.relative_to(root).as_posix())
    return unresolved


def _render_row(row: Row, page: Row, page_blocks: dict[str, ContextBlock]) -> list[str]:
    heading = f"### Row {row['id']} \u00b7 {row['row_kind']}"
    if row["conflict_type"]:
        heading = f"{heading} \u00b7 {row['conflict_type']}"
    if row["row_kind"] == ReviewRowKind.taxonomy_merge.value:
        return _render_taxonomy_row(row, heading)

    existing = page_blocks.get(str(row["existing_block_id"]))
    candidate = _candidate_block(row)
    lines = [heading]
    if candidate is not None:
        lines.append(f"- source: {candidate.source_path} (source_date: {candidate.source_date})")
    if existing is not None:
        lines.extend(
            [
                f"- existing block: {existing.id} \u00b7 key `{existing.key}` \u00b7 status {existing.status.value}",
                f"  - content: {_single_line(existing.content)}",
                f"  - excerpt: {_single_line(existing.excerpt)} ({existing.source_path})",
            ]
        )
    else:
        lines.append(f"- existing block: {row['existing_block_id']} \u00b7 key `unknown` \u00b7 status unknown")
    if candidate is not None:
        lines.extend(
            [
                f"- candidate block: {candidate.id} \u00b7 key `{candidate.key}` \u00b7 status {candidate.status.value}",
                f"  - content: {_single_line(candidate.content)}",
                f"  - excerpt: {_single_line(candidate.excerpt)} ({candidate.source_path})",
            ]
        )
    else:
        lines.append("- candidate block:")
    lines.extend(
        [
            f"- recommendation: {row['recommendation']} \u2014 basis: {row['recommendation_basis']}",
            "- decision:",
            "- notes:",
        ]
    )
    return lines


def _render_taxonomy_row(row: Row, heading: str) -> list[str]:
    candidate = _candidate_json(row)
    lines = [heading]
    if isinstance(candidate, dict):
        lines.append(f"- merge candidate: [[{candidate['title']}]] ({candidate['path']})")
        lines.append(f"- similarity: {candidate['similarity']}")
    lines.extend(
        [
            f"- recommendation: {row['recommendation']} \u2014 high registry similarity",
            "- decision:",
            "- notes:",
        ]
    )
    return lines


def _page_blocks(workspace: Path, page: Row) -> dict[str, ContextBlock]:
    path = workspace / str(page["path"])
    if not path.exists():
        return {}
    return {block.id: block for block in parse_page(path.read_text(encoding="utf-8")).blocks}


def _candidate_block(row: Row) -> ContextBlock | None:
    raw = _candidate_json(row)
    if not isinstance(raw, dict):
        return None
    if "block" in raw and isinstance(raw["block"], dict):
        raw = raw["block"]
    try:
        return ContextBlock(**raw)
    except ValueError:
        return None


def _candidate_json(row: Row) -> Any:
    raw = row.get("candidate_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _registry_similarity(left: Row, right: Row) -> float:
    left_tokens = _tokens(f"{left['title']} {left['description']}")
    right_tokens = _tokens(f"{right['title']} {right['description']}")
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _taxonomy_pair_key(row: Row) -> str:
    candidate = _candidate_json(row)
    if isinstance(candidate, dict) and "page_id" in candidate:
        return f"{row['page_id']}::{candidate['page_id']}"
    return ""

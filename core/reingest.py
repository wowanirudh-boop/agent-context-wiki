from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import aiosqlite

from core.db.dao import ACWDao, fetch_all, utc_now
from core.ledger import ChunkLedger
from core.models import BlockStatus


async def sync_block_statuses(workspace: Path, db: aiosqlite.Connection) -> None:
    """FR-REING-02/03: reflect ledger staleness in the parsed page model."""
    from core.blocks.mutations import set_status
    from core.blocks.parser import parse_page
    from core.blocks.serializer import write_page

    dao = ACWDao(db)
    for page_row in await dao.list_pages():
        path = workspace / str(page_row["path"])
        if not path.exists():
            continue
        old_text = path.read_text(encoding="utf-8")
        page = parse_page(old_text)
        metadata = {row["id"]: row for row in await dao.list_blocks_for_page(str(page_row["id"]))}
        changed = False
        for block in page.blocks:
            row = metadata.get(block.id)
            if row is not None and row["status"] == "needs_review" and (
                block.status != BlockStatus.needs_review or block.needs_review_reason != row["needs_review_reason"]
            ):
                page = set_status(page, block.id, BlockStatus.needs_review, needs_review_reason=row["needs_review_reason"])
                changed = True
        if changed:
            write_page(path, page, expected_text=old_text)


async def create_stale_block_reviews(workspace: Path, db: aiosqlite.Connection, run_id: str) -> None:
    from core.blocks.mutations import set_generated_section
    from core.blocks.parser import parse_page
    from core.blocks.serializer import write_page
    from core.conflicts.markers import open_conflicts_body

    dao = ACWDao(db)
    reviewed = {row["existing_block_id"] for row in await dao.list_review_rows(open_only=True)}
    rows = await fetch_all(await db.execute("SELECT id, page_id, needs_review_reason FROM acw_blocks WHERE status = 'needs_review'"))
    changed_pages = set()
    for row in rows:
        if row["id"] in reviewed:
            continue
        await dao.create_review_row(
            run_id=run_id, page_id=row["page_id"], row_kind="needs_review", existing_block_id=row["id"],
            recommendation="deprecate_existing", recommendation_basis=row["needs_review_reason"],
        )
        changed_pages.add(row["page_id"])
    for page_id in changed_pages:
        page_row = await dao.get_page(page_id)
        path = workspace / str(page_row["path"])
        old_text = path.read_text(encoding="utf-8")
        page = set_generated_section(parse_page(old_text), "Open Conflicts", await open_conflicts_body(db, page_id), run_id=run_id)
        write_page(path, page, expected_text=old_text)


async def reconcile_source_version(
    db: aiosqlite.Connection,
    source_id: str,
    current_source_version_id: str,
    current_content_hashes: Iterable[str],
) -> None:
    current_hashes = set(current_content_hashes)
    ledger = ChunkLedger(ACWDao(db))
    old_rows = await fetch_all(
        await db.execute(
            "SELECT id, content_hash, disposition FROM acw_chunk_ledger "
            "WHERE source_id = ? AND source_version_id != ?",
            (source_id, current_source_version_id),
        ),
    )

    for row in old_rows:
        if row["content_hash"] not in current_hashes and row["disposition"] != "superseded":
            await ledger.mark_superseded(
                row["id"],
                reason="source_no_longer_contains",
                updated_at=utc_now(),
            )

    await flag_blocks_with_only_superseded_chunks(db, source_id, "source_no_longer_contains")


async def mark_source_deleted(db: aiosqlite.Connection, source_id: str) -> None:
    ledger = ChunkLedger(ACWDao(db))
    rows = await fetch_all(
        await db.execute(
            "SELECT id, disposition FROM acw_chunk_ledger WHERE source_id = ?",
            (source_id,),
        ),
    )
    for row in rows:
        if row["disposition"] != "superseded":
            await ledger.mark_superseded(row["id"], reason="source_deleted", updated_at=utc_now())

    await db.execute(
        "UPDATE documents SET status = 'failed', error_message = 'Source file removed', "
        "updated_at = datetime('now') WHERE id = ?",
        (source_id,),
    )
    await db.commit()
    await flag_blocks_with_only_superseded_chunks(db, source_id, "source_deleted")


async def flag_blocks_with_only_superseded_chunks(
    db: aiosqlite.Connection,
    source_id: str,
    reason: str,
) -> None:
    rows = await fetch_all(
        await db.execute(
            "SELECT b.id "
            "FROM acw_blocks b "
            "WHERE b.source_id = ? "
            "AND b.status NOT IN ('rejected', 'deleted') "
            "AND EXISTS (SELECT 1 FROM acw_block_chunks bc WHERE bc.block_id = b.id) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM acw_block_chunks bc "
            "  JOIN acw_chunk_ledger cl ON cl.id = bc.chunk_id "
            "  WHERE bc.block_id = b.id AND cl.disposition != 'superseded'"
            ")",
            (source_id,),
        ),
    )
    await db.executemany(
        "UPDATE acw_blocks SET status = ?, needs_review_reason = ?, updated_at = ? WHERE id = ?",
        [(BlockStatus.needs_review.value, reason, utc_now(), row["id"]) for row in rows],
    )
    await db.commit()

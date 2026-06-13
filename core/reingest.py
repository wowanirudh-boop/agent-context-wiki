from __future__ import annotations

from collections.abc import Iterable

import aiosqlite

from core.db.dao import ACWDao, fetch_all, utc_now
from core.ledger import ChunkLedger
from core.models import BlockStatus


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

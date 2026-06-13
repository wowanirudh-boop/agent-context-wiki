from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiosqlite

from core.db.dao import Row, utc_now
from core.ledger import ChunkLedger


async def apply_transcript_prepass(
    db: aiosqlite.Connection,
    ledger: ChunkLedger,
    chunks: list[Row],
    response: Mapping[str, Any],
) -> list[Row]:
    by_id = {str(chunk["id"]): chunk for chunk in chunks}
    survivors: list[Row] = []
    for segment in response.get("segments", []):
        chunk_id = str(segment["chunk_id"])
        chunk = by_id[chunk_id]
        source_date = segment.get("source_date_extracted")
        if isinstance(source_date, str) and source_date.strip() and chunk.get("source_version_id"):
            await db.execute(
                "UPDATE acw_source_versions SET source_date = ?, source_date_origin = 'content' WHERE id = ?",
                (source_date, chunk["source_version_id"]),
            )

        if not segment["relevant"]:
            await ledger.mark_irrelevant(chunk_id, reason=str(segment["reason"]))
            continue

        superseded_by = segment.get("superseded_by_chunk_id")
        if superseded_by:
            await ledger.dao.update_chunk(
                chunk_id,
                disposition="duplicate",
                disposition_reason=str(segment.get("reason") or "intra_transcript_supersession"),
                duplicate_of_block_id=f"pending:{superseded_by}",
                attempts=int(chunk.get("attempts", 0)),
                updated_at=utc_now(),
            )
            continue
        survivors.append(chunk)
    await db.commit()
    return survivors


async def resolve_transcript_duplicate_placeholders(
    db: aiosqlite.Connection,
    *,
    survivor_chunk_id: str,
    block_id: str,
) -> None:
    await db.execute(
        "UPDATE acw_chunk_ledger SET duplicate_of_block_id = ? WHERE duplicate_of_block_id = ?",
        (block_id, f"pending:{survivor_chunk_id}"),
    )
    await db.commit()

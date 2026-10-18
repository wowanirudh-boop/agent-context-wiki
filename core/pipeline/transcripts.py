from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aiosqlite

from core.db.dao import ACWDao, Row, utc_now
from core.ledger import ChunkLedger


async def apply_transcript_prepass(
    db: aiosqlite.Connection,
    ledger: ChunkLedger,
    chunks: list[Row],
    response: Mapping[str, Any],
) -> list[Row]:
    by_id = {str(chunk["id"]): chunk for chunk in chunks}
    segments = {str(segment["chunk_id"]): segment for segment in response.get("segments", [])}
    survivors: list[Row] = []
    for segment in response.get("segments", []):
        chunk_id = str(segment["chunk_id"])
        chunk = by_id[chunk_id]
        source_date = segment.get("source_date_extracted")
        if isinstance(source_date, str) and source_date.strip() and chunk.get("source_version_id"):
            chunk["source_date"] = source_date
            await db.execute(
                "UPDATE acw_source_versions SET source_date = ?, source_date_origin = 'content' WHERE id = ?",
                (source_date, chunk["source_version_id"]),
            )

        if not segment["relevant"]:
            await ledger.mark_irrelevant(chunk_id, reason=str(segment["reason"]))
            continue

        superseded_by = segment.get("superseded_by_chunk_id")
        if superseded_by:
            while segments[superseded_by].get("superseded_by_chunk_id"):
                superseded_by = segments[superseded_by]["superseded_by_chunk_id"]
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


async def settle_transcript_supersession(db: aiosqlite.Connection) -> None:
    """A completed run never leaves a temporary duplicate target behind."""
    dao = ACWDao(db)
    cursor = await db.execute("SELECT id, duplicate_of_block_id FROM acw_chunk_ledger WHERE duplicate_of_block_id LIKE 'pending:%'")
    for chunk_id, placeholder in await cursor.fetchall():
        survivor = await dao.get_chunk(placeholder.removeprefix("pending:"))
        links = await db.execute("SELECT block_id FROM acw_block_chunks WHERE chunk_id = ? LIMIT 1", (survivor["id"],))
        linked = await links.fetchone()
        target = survivor["duplicate_of_block_id"] or (linked[0] if linked else None)
        status = survivor["disposition"]
        if status in {"placed", "duplicate"} and target:
            await resolve_transcript_duplicate_placeholders(db, survivor_chunk_id=survivor["id"], block_id=target)
            continue
        if status == "conflicted_pending":
            for row in await dao.list_review_rows(open_only=True):
                candidate = json.loads(row["candidate_json"] or "{}")
                if survivor["id"] in candidate.get("chunk_ids", []):
                    candidate["chunk_ids"] = list(dict.fromkeys([*candidate["chunk_ids"], chunk_id]))
                    await db.execute("UPDATE acw_review_rows SET candidate_json = ? WHERE id = ?", (json.dumps(candidate), row["id"]))
        await dao.update_chunk(
            chunk_id, disposition=status, duplicate_of_block_id=None, attempts=survivor["attempts"],
            disposition_reason="intra_transcript_supersession" if status == "conflicted_pending" else survivor["disposition_reason"],
            updated_at=utc_now(),
        )
    await db.commit()


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

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from core.db.dao import fetch_all
from core.db.migrate import apply_migrations
from core.source_exclusions import is_acw_source_excluded


@dataclass(frozen=True, slots=True)
class PruneLedgerResult:
    ledger_rows: int
    block_chunk_links: int
    document_chunks: int
    source_versions: int


async def prune_ledger(workspace: str | Path) -> PruneLedgerResult:
    db_path = Path(workspace) / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await apply_migrations(db)
        return await prune_ledger_rows(db)


async def prune_ledger_rows(db: aiosqlite.Connection) -> PruneLedgerResult:
    excluded_source_ids = await _excluded_source_ids(db)
    ledger_ids = await _orphaned_ledger_ids(db, excluded_source_ids)
    document_chunk_ids = await _orphaned_document_chunk_ids(db, excluded_source_ids)
    source_version_ids = await _excluded_source_version_ids(db, excluded_source_ids)
    block_chunk_links = await _count_block_chunk_links(db, ledger_ids)

    await _delete_ids(db, "acw_block_chunks", "chunk_id", ledger_ids)
    await _delete_ids(db, "acw_chunk_ledger", "id", ledger_ids)
    await _delete_ids(db, "document_chunks", "id", document_chunk_ids)
    await _delete_ids(db, "acw_source_versions", "id", source_version_ids)
    await db.commit()

    return PruneLedgerResult(
        ledger_rows=len(ledger_ids),
        block_chunk_links=block_chunk_links,
        document_chunks=len(document_chunk_ids),
        source_versions=len(source_version_ids),
    )


async def _excluded_source_ids(db: aiosqlite.Connection) -> list[str]:
    rows = await fetch_all(await db.execute("SELECT id, relative_path FROM documents ORDER BY id"))
    return [str(row["id"]) for row in rows if is_acw_source_excluded(str(row["relative_path"]))]


async def _orphaned_ledger_ids(db: aiosqlite.Connection, excluded_source_ids: list[str]) -> list[str]:
    rows = await fetch_all(
        await db.execute(
            "SELECT cl.id FROM acw_chunk_ledger cl "
            "LEFT JOIN acw_source_versions sv ON sv.id = cl.source_version_id "
            "WHERE sv.id IS NULL "
            "ORDER BY cl.id",
        )
    )
    ids = {str(row["id"]) for row in rows}
    if excluded_source_ids:
        rows = await fetch_all(
            await db.execute(
                f"SELECT id FROM acw_chunk_ledger WHERE source_id IN ({_placeholders(excluded_source_ids)})",
                tuple(excluded_source_ids),
            )
        )
        ids.update(str(row["id"]) for row in rows)
    return sorted(ids)


async def _orphaned_document_chunk_ids(db: aiosqlite.Connection, excluded_source_ids: list[str]) -> list[str]:
    rows = await fetch_all(
        await db.execute(
            "SELECT dc.id FROM document_chunks dc "
            "LEFT JOIN acw_source_versions sv ON sv.id = dc.source_version_id "
            "WHERE dc.source_version_id IS NOT NULL AND sv.id IS NULL "
            "ORDER BY dc.id",
        )
    )
    ids = {str(row["id"]) for row in rows}
    if excluded_source_ids:
        rows = await fetch_all(
            await db.execute(
                f"SELECT id FROM document_chunks WHERE document_id IN ({_placeholders(excluded_source_ids)})",
                tuple(excluded_source_ids),
            )
        )
        ids.update(str(row["id"]) for row in rows)
    return sorted(ids)


async def _excluded_source_version_ids(db: aiosqlite.Connection, excluded_source_ids: list[str]) -> list[str]:
    if not excluded_source_ids:
        return []
    rows = await fetch_all(
        await db.execute(
            f"SELECT id FROM acw_source_versions WHERE source_id IN ({_placeholders(excluded_source_ids)}) ORDER BY id",
            tuple(excluded_source_ids),
        )
    )
    return [str(row["id"]) for row in rows]


async def _count_block_chunk_links(db: aiosqlite.Connection, ledger_ids: list[str]) -> int:
    if not ledger_ids:
        return 0
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM acw_block_chunks WHERE chunk_id IN ({_placeholders(ledger_ids)})",
        tuple(ledger_ids),
    )
    return int((await cursor.fetchone())[0])


async def _delete_ids(db: aiosqlite.Connection, table: str, column: str, ids: list[str]) -> None:
    if not ids:
        return
    await db.execute(
        f"DELETE FROM {table} WHERE {column} IN ({_placeholders(ids)})",
        tuple(ids),
    )


def _placeholders(values: list[str]) -> str:
    return ", ".join("?" for _ in values)

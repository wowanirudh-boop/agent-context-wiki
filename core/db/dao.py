from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from core.ids import new_id
from core.models import EventKind, RunStatus

Row = dict[str, Any]


class ACWDao:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def create_source_version(
        self,
        *,
        source_id: str,
        version_hash: str,
        source_date: str,
        source_date_origin: str,
        source_version_id: str | None = None,
        seen_at: str | None = None,
    ) -> Row:
        existing = await self.get_source_version(source_id, version_hash)
        if existing is not None:
            return existing

        row_id = source_version_id or new_id("sv")
        await self.db.execute(
            "INSERT INTO acw_source_versions "
            "(id, source_id, version_hash, seen_at, source_date, source_date_origin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, source_id, version_hash, seen_at or utc_now(), source_date, source_date_origin),
        )
        await self.db.commit()
        created = await self.get_source_version_by_id(row_id)
        if created is None:
            raise RuntimeError("Failed to create source version")
        return created

    async def get_source_version(self, source_id: str, version_hash: str) -> Row | None:
        cursor = await self.db.execute(
            "SELECT id, source_id, version_hash, seen_at, source_date, source_date_origin "
            "FROM acw_source_versions WHERE source_id = ? AND version_hash = ?",
            (source_id, version_hash),
        )
        return await fetch_one(cursor)

    async def get_source_version_by_id(self, source_version_id: str) -> Row | None:
        cursor = await self.db.execute(
            "SELECT id, source_id, version_hash, seen_at, source_date, source_date_origin "
            "FROM acw_source_versions WHERE id = ?",
            (source_version_id,),
        )
        return await fetch_one(cursor)

    async def create_chunk(
        self,
        *,
        source_id: str,
        source_version_id: str,
        content_hash: str,
        ordinal: int,
        document_chunk_id: str | None = None,
        chunk_id: str | None = None,
        updated_at: str | None = None,
    ) -> Row:
        existing = await self.get_chunk_by_identity(source_id, source_version_id, content_hash, ordinal)
        if existing is not None:
            return existing

        row_id = chunk_id or new_id("ch")
        await self.db.execute(
            "INSERT INTO acw_chunk_ledger "
            "(id, source_id, source_version_id, content_hash, document_chunk_id, ordinal, "
            "disposition, disposition_reason, duplicate_of_block_id, attempts, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, ?)",
            (row_id, source_id, source_version_id, content_hash, document_chunk_id, ordinal, updated_at or utc_now()),
        )
        await self.db.commit()
        created = await self.get_chunk(row_id)
        if created is None:
            raise RuntimeError("Failed to create chunk ledger row")
        return created

    async def get_chunk_by_identity(
        self,
        source_id: str,
        source_version_id: str,
        content_hash: str,
        ordinal: int,
    ) -> Row | None:
        cursor = await self.db.execute(
            "SELECT id, source_id, source_version_id, content_hash, document_chunk_id, ordinal, "
            "disposition, disposition_reason, duplicate_of_block_id, attempts, updated_at "
            "FROM acw_chunk_ledger "
            "WHERE source_id = ? AND source_version_id = ? AND content_hash = ? AND ordinal = ?",
            (source_id, source_version_id, content_hash, ordinal),
        )
        return await fetch_one(cursor)

    async def get_chunk(self, chunk_id: str) -> Row | None:
        cursor = await self.db.execute(
            "SELECT id, source_id, source_version_id, content_hash, document_chunk_id, ordinal, "
            "disposition, disposition_reason, duplicate_of_block_id, attempts, updated_at "
            "FROM acw_chunk_ledger WHERE id = ?",
            (chunk_id,),
        )
        return await fetch_one(cursor)

    async def update_chunk(
        self,
        chunk_id: str,
        *,
        disposition: str,
        disposition_reason: str | None,
        duplicate_of_block_id: str | None,
        attempts: int,
        updated_at: str,
    ) -> Row:
        await self.db.execute(
            "UPDATE acw_chunk_ledger SET disposition = ?, disposition_reason = ?, "
            "duplicate_of_block_id = ?, attempts = ?, updated_at = ? WHERE id = ?",
            (disposition, disposition_reason, duplicate_of_block_id, attempts, updated_at, chunk_id),
        )
        await self.db.commit()
        updated = await self.get_chunk(chunk_id)
        if updated is None:
            raise KeyError(chunk_id)
        return updated

    async def create_page(
        self,
        *,
        page_id: str,
        path: str,
        title: str,
        description: str,
        status: str,
        domain: str,
        created_at: str,
        aliases: list[str],
    ) -> Row:
        await self.db.execute(
            "INSERT INTO acw_pages (id, path, title, description, status, domain, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (page_id, path, title, description, status, domain, created_at),
        )
        await self.replace_aliases(page_id, aliases)
        await self.db.commit()
        page = await self.get_page(page_id)
        if page is None:
            raise RuntimeError("Failed to create page")
        return page

    async def get_page(self, page_id: str) -> Row | None:
        cursor = await self.db.execute(
            "SELECT id, path, title, description, status, domain, created_at FROM acw_pages WHERE id = ?",
            (page_id,),
        )
        return await fetch_one(cursor)

    async def list_pages(self) -> list[Row]:
        cursor = await self.db.execute(
            "SELECT id, path, title, description, status, domain, created_at FROM acw_pages ORDER BY id",
        )
        return await fetch_all(cursor)

    async def update_page(self, page_id: str, fields: dict[str, str]) -> Row:
        if fields:
            assignments = ", ".join(f"{field} = ?" for field in fields)
            await self.db.execute(
                f"UPDATE acw_pages SET {assignments} WHERE id = ?",
                [*fields.values(), page_id],
            )
            await self.db.commit()
        page = await self.get_page(page_id)
        if page is None:
            raise KeyError(page_id)
        return page

    async def replace_aliases(self, page_id: str, aliases: list[str]) -> None:
        await self.db.execute("DELETE FROM acw_page_aliases WHERE page_id = ?", (page_id,))
        await self.db.executemany(
            "INSERT INTO acw_page_aliases (page_id, alias) VALUES (?, ?)",
            [(page_id, alias) for alias in sorted(set(aliases), key=str.lower)],
        )

    async def list_aliases(self, page_id: str) -> list[str]:
        cursor = await self.db.execute(
            "SELECT alias FROM acw_page_aliases WHERE page_id = ? ORDER BY lower(alias), alias",
            (page_id,),
        )
        return [row[0] for row in await cursor.fetchall()]

    async def create_block(
        self,
        *,
        block_id: str,
        page_id: str,
        key: str,
        block_type: str,
        status: str,
        source_id: str,
        source_path: str,
        source_date: str,
        content_hash: str,
        created_at: str,
        updated_at: str,
        needs_review_reason: str | None = None,
        user_edited: bool = False,
    ) -> Row:
        await self.db.execute(
            "INSERT INTO acw_blocks "
            "(id, page_id, key, type, status, needs_review_reason, source_id, source_path, "
            "source_date, content_hash, user_edited, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                block_id,
                page_id,
                key,
                block_type,
                status,
                needs_review_reason,
                source_id,
                source_path,
                source_date,
                content_hash,
                1 if user_edited else 0,
                created_at,
                updated_at,
            ),
        )
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT id, page_id, key, type, status, needs_review_reason, source_id, source_path, "
            "source_date, content_hash, user_edited, created_at, updated_at "
            "FROM acw_blocks WHERE id = ?",
            (block_id,),
        )
        row = await fetch_one(cursor)
        if row is None:
            raise RuntimeError("Failed to create block")
        return row

    async def link_block_chunk(self, block_id: str, chunk_id: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO acw_block_chunks (block_id, chunk_id) VALUES (?, ?)",
            (block_id, chunk_id),
        )
        await self.db.commit()

    async def list_block_chunks(self) -> list[Row]:
        cursor = await self.db.execute(
            "SELECT block_id, chunk_id FROM acw_block_chunks ORDER BY block_id, chunk_id",
        )
        return await fetch_all(cursor)

    async def create_run(self, *, run_id: str | None = None, started_at: str | None = None) -> Row:
        row_id = run_id or new_id("run")
        await self.db.execute(
            "INSERT INTO acw_runs (id, started_at, finished_at, status, stats_json) "
            "VALUES (?, ?, NULL, ?, '{}')",
            (row_id, started_at or utc_now(), RunStatus.running.value),
        )
        await self.db.commit()
        run = await self.get_run(row_id)
        if run is None:
            raise RuntimeError("Failed to create run")
        return run

    async def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        stats: dict[str, Any],
        finished_at: str | None = None,
    ) -> Row:
        await self.db.execute(
            "UPDATE acw_runs SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
            (finished_at or utc_now(), status.value, json_dumps(stats), run_id),
        )
        await self.db.commit()
        run = await self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    async def get_run(self, run_id: str) -> Row | None:
        cursor = await self.db.execute(
            "SELECT id, started_at, finished_at, status, stats_json FROM acw_runs WHERE id = ?",
            (run_id,),
        )
        return await fetch_one(cursor)

    async def write_event(
        self,
        *,
        kind: EventKind,
        actor: str,
        payload: dict[str, Any],
        event_id: str | None = None,
        ts: str | None = None,
    ) -> Row:
        row_id = event_id or new_id("ev")
        await self.db.execute(
            "INSERT INTO acw_events (id, ts, actor, kind, payload_json) VALUES (?, ?, ?, ?, ?)",
            (row_id, ts or utc_now(), actor, kind.value, json_dumps(payload)),
        )
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT id, ts, actor, kind, payload_json FROM acw_events WHERE id = ?",
            (row_id,),
        )
        event = await fetch_one(cursor)
        if event is None:
            raise RuntimeError("Failed to write event")
        return event

    async def list_events(self) -> list[Row]:
        cursor = await self.db.execute(
            "SELECT id, ts, actor, kind, payload_json FROM acw_events ORDER BY id",
        )
        return await fetch_all(cursor)

    async def list_chunks_for_export(self) -> list[Row]:
        cursor = await self.db.execute(
            "SELECT id, source_id, source_version_id, content_hash, document_chunk_id, ordinal, "
            "disposition, disposition_reason, duplicate_of_block_id, attempts, updated_at "
            "FROM acw_chunk_ledger ORDER BY id",
        )
        return await fetch_all(cursor)


async def fetch_one(cursor: aiosqlite.Cursor) -> Row | None:
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(cursor, row)


async def fetch_all(cursor: aiosqlite.Cursor) -> list[Row]:
    return [_row_to_dict(cursor, row) for row in await cursor.fetchall()]


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple[Any, ...]) -> Row:
    return dict(zip([description[0] for description in cursor.description], row, strict=True))

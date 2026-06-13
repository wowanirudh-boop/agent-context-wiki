from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from core.models import Disposition, EventKind, RunStatus

ROOT = Path(__file__).resolve().parents[3]
BASE_SCHEMA = ROOT / "shared" / "sqlite_schema.sql"


async def _apply_base_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(BASE_SCHEMA.read_text(encoding="utf-8"))
    await db.commit()


async def _seed_document(db: aiosqlite.Connection, document_id: str = "doc_1") -> str:
    await db.execute(
        "INSERT INTO workspace (id, name, user_id) VALUES ('ws_1', 'Test Workspace', 'user_1')",
    )
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, source_kind, "
        "file_type, status, content, document_number) "
        "VALUES (?, 'user_1', 'source.md', 'Source', '/docs/', 'docs/source.md', 'source', "
        "'md', 'ready', 'source text', 1)",
        (document_id,),
    )
    await db.execute(
        "INSERT INTO document_chunks (id, document_id, chunk_index, content, source_content, token_count) "
        "VALUES ('dc_1', ?, 0, 'chunk text', 'chunk text', 2)",
        (document_id,),
    )
    await db.commit()
    return document_id


@pytest.mark.asyncio
async def test_fr_ledger_01_migration_idempotence_preserves_existing_index_data(tmp_sqlite) -> None:
    from core.db.migrate import apply_migrations

    await _apply_base_schema(tmp_sqlite)
    await _seed_document(tmp_sqlite)

    await apply_migrations(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    versions = await _fetch_column(tmp_sqlite, "SELECT version FROM acw_schema_version")
    assert versions == [1]

    chunk_columns = await _table_columns(tmp_sqlite, "document_chunks")
    assert "content_hash" in chunk_columns
    assert "source_version_id" in chunk_columns

    document_rows = await _fetch_column(tmp_sqlite, "SELECT relative_path FROM documents")
    assert document_rows == ["docs/source.md"]
    chunk_rows = await _fetch_column(tmp_sqlite, "SELECT content FROM document_chunks")
    assert chunk_rows == ["chunk text"]


@pytest.mark.asyncio
async def test_fr_ledger_01_chunk_creation_starts_pending(tmp_sqlite) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ledger import ChunkLedger

    await _apply_base_schema(tmp_sqlite)
    source_id = await _seed_document(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    dao = ACWDao(tmp_sqlite)
    ledger = ChunkLedger(dao)
    source_version = await ledger.ensure_source_version(
        source_id=source_id,
        version_hash="hash-v1",
        source_date="unknown",
        source_date_origin="unknown",
        source_version_id="sv_test",
        seen_at="2026-06-13T00:00:00Z",
    )
    chunk = await ledger.create_pending_chunk(
        source_id=source_id,
        source_version_id=source_version["id"],
        content_hash="chunk-hash",
        ordinal=0,
        document_chunk_id="dc_1",
        chunk_id="ch_test",
        updated_at="2026-06-13T00:00:01Z",
    )

    assert chunk["id"] == "ch_test"
    assert chunk["disposition"] == Disposition.pending
    assert chunk["attempts"] == 0


@pytest.mark.asyncio
async def test_fr_ledger_02_transition_matrix_enforced(tmp_sqlite) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ledger import ChunkLedger, InvalidDispositionTransition, LedgerValidationError
    from core.registry import PageRegistry

    await _apply_base_schema(tmp_sqlite)
    source_id = await _seed_document(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    dao = ACWDao(tmp_sqlite)
    registry = PageRegistry(dao)
    page = await registry.create_page(
        title="Refunds",
        path="wiki/refunds.md",
        description="Refund policy",
        page_id="pg_refunds",
        created_at="2026-06-13T00:00:00Z",
    )
    block = await dao.create_block(
        block_id="cb_refunds",
        page_id=page["id"],
        key="refunds.window_days",
        block_type="rule",
        status="current",
        source_id=source_id,
        source_path="docs/source.md",
        source_date="unknown",
        content_hash="block-hash",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )
    ledger = ChunkLedger(dao, max_attempts=3)
    source_version = await ledger.ensure_source_version(
        source_id=source_id,
        version_hash="hash-v1",
        source_date="unknown",
        source_date_origin="unknown",
        source_version_id="sv_test",
        seen_at="2026-06-13T00:00:00Z",
    )

    placed = await _new_chunk(ledger, source_id, source_version["id"], "ch_placed", 0)
    await ledger.mark_placed(placed["id"], block_ids=[block["id"]], updated_at="2026-06-13T00:01:00Z")
    placed_row = await dao.get_chunk("ch_placed")
    assert placed_row["disposition"] == Disposition.placed
    assert await dao.list_block_chunks() == [{"block_id": "cb_refunds", "chunk_id": "ch_placed"}]

    duplicate = await _new_chunk(ledger, source_id, source_version["id"], "ch_duplicate", 1)
    await ledger.mark_duplicate(
        duplicate["id"],
        duplicate_of_block_id=block["id"],
        reason="same evidence",
        updated_at="2026-06-13T00:02:00Z",
    )
    assert (await dao.get_chunk("ch_duplicate"))["duplicate_of_block_id"] == block["id"]

    irrelevant = await _new_chunk(ledger, source_id, source_version["id"], "ch_irrelevant", 2)
    await ledger.mark_irrelevant(irrelevant["id"], reason="recipe", updated_at="2026-06-13T00:03:00Z")
    assert (await dao.get_chunk("ch_irrelevant"))["disposition_reason"] == "recipe"

    conflicted = await _new_chunk(ledger, source_id, source_version["id"], "ch_conflicted", 3)
    await ledger.mark_conflicted_pending(conflicted["id"], reason="changed_value", updated_at="2026-06-13T00:04:00Z")
    await ledger.mark_duplicate(
        conflicted["id"],
        duplicate_of_block_id=block["id"],
        reason="resolved by decision",
        updated_at="2026-06-13T00:05:00Z",
    )
    assert (await dao.get_chunk("ch_conflicted"))["disposition"] == Disposition.duplicate

    failed = await _new_chunk(ledger, source_id, source_version["id"], "ch_failed", 4)
    await ledger.mark_failed(failed["id"], reason="transient", updated_at="2026-06-13T00:06:00Z")
    assert (await dao.get_chunk("ch_failed"))["attempts"] == 1
    await ledger.mark_pending_for_retry(failed["id"], updated_at="2026-06-13T00:07:00Z")
    assert (await dao.get_chunk("ch_failed"))["disposition"] == Disposition.pending

    await ledger.mark_failed(failed["id"], reason="still broken", updated_at="2026-06-13T00:08:00Z")
    await ledger.mark_pending_for_retry(failed["id"], updated_at="2026-06-13T00:09:00Z")
    final = await ledger.mark_failed(failed["id"], reason="final failure", updated_at="2026-06-13T00:10:00Z")
    assert final["disposition"] == Disposition.failed_final
    assert final["attempts"] == 3

    superseded = await ledger.mark_superseded(placed["id"], reason="source version changed", updated_at="2026-06-13T00:11:00Z")
    assert superseded["disposition"] == Disposition.superseded

    with pytest.raises(InvalidDispositionTransition):
        await ledger.mark_irrelevant(duplicate["id"], reason="too late")

    pending = await _new_chunk(ledger, source_id, source_version["id"], "ch_pending", 5)
    with pytest.raises(LedgerValidationError):
        await ledger.mark_irrelevant(pending["id"], reason="")
    with pytest.raises(LedgerValidationError):
        await ledger.mark_duplicate(pending["id"], duplicate_of_block_id="")


@pytest.mark.asyncio
async def test_fr_ledger_05_run_event_writers_and_export_stability(tmp_workspace, tmp_sqlite) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ledger import ChunkLedger

    await _apply_base_schema(tmp_sqlite)
    source_id = await _seed_document(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    dao = ACWDao(tmp_sqlite)
    run = await dao.create_run(run_id="run_test", started_at="2026-06-13T00:00:00Z")
    assert run["status"] == RunStatus.running
    await dao.finish_run("run_test", status=RunStatus.completed, stats={"placed": 1}, finished_at="2026-06-13T00:01:00Z")
    await dao.write_event(
        kind=EventKind.run_completed,
        actor="test",
        payload={"run_id": "run_test"},
        event_id="ev_test",
        ts="2026-06-13T00:01:01Z",
    )

    ledger = ChunkLedger(dao)
    source_version = await ledger.ensure_source_version(
        source_id=source_id,
        version_hash="hash-v1",
        source_date="unknown",
        source_date_origin="unknown",
        source_version_id="sv_test",
        seen_at="2026-06-13T00:00:00Z",
    )
    await _new_chunk(ledger, source_id, source_version["id"], "ch_b", 1)
    await _new_chunk(ledger, source_id, source_version["id"], "ch_a", 0)

    await ledger.export_json(tmp_workspace, run_id="run_test", exported_at="2026-06-13T00:02:00Z")
    first = (tmp_workspace / "wiki" / "_meta" / "ledger.json").read_text(encoding="utf-8")
    await ledger.export_json(tmp_workspace, run_id="run_test", exported_at="2026-06-13T00:02:00Z")
    second = (tmp_workspace / "wiki" / "_meta" / "ledger.json").read_text(encoding="utf-8")

    assert first == second
    payload = json.loads(first)
    assert payload["run_id"] == "run_test"
    assert [chunk["id"] for chunk in payload["chunks"]] == ["ch_a", "ch_b"]
    assert payload["block_chunks"] == []

    events = await dao.list_events()
    assert events == [
        {
            "id": "ev_test",
            "ts": "2026-06-13T00:01:01Z",
            "actor": "test",
            "kind": "run.completed",
            "payload_json": '{"run_id":"run_test"}',
        }
    ]


async def _new_chunk(
    ledger,
    source_id: str,
    source_version_id: str,
    chunk_id: str,
    ordinal: int,
) -> dict:
    return await ledger.create_pending_chunk(
        source_id=source_id,
        source_version_id=source_version_id,
        content_hash=f"hash-{chunk_id}",
        ordinal=ordinal,
        chunk_id=chunk_id,
        updated_at=f"2026-06-13T00:00:{ordinal:02d}Z",
    )


async def _fetch_column(db: aiosqlite.Connection, sql: str) -> list:
    cursor = await db.execute(sql)
    return [row[0] for row in await cursor.fetchall()]


async def _table_columns(db: aiosqlite.Connection, table_name: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in await cursor.fetchall()}

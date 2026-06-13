from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from core.models import BlockStatus, Disposition

ROOT = Path(__file__).resolve().parents[3]
BASE_SCHEMA = ROOT / "shared" / "sqlite_schema.sql"
FIXTURE_ROOT = ROOT / "tests" / "v2" / "fixtures" / "workspaces"


async def _apply_base_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(BASE_SCHEMA.read_text(encoding="utf-8"))
    await db.commit()


async def _seed_workspace(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO workspace (id, name, user_id) VALUES ('ws_1', 'Test Workspace', 'user_1')",
    )
    await db.commit()


@pytest.mark.asyncio
async def test_fr_ledger_06_source_metadata_stores_date_ingestion_mtime_and_kind(tmp_sqlite, tmp_workspace) -> None:
    from core.db.migrate import apply_migrations
    from core.ingest import index_document_chunks

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    source = tmp_workspace / "transcripts" / "standup_2026-05-02.txt"
    source.parent.mkdir()
    source.write_text(
        "Meeting date: 2026-05-02\n\n"
        "Decision: Refund retries remain at 3 attempts. The team confirmed this during "
        "the operating review so support, testing, and release notes all use the same "
        "source-backed value for the refund workflow.",
        encoding="utf-8",
    )
    await _insert_document(tmp_sqlite, doc_id="doc_transcript", relative_path="transcripts/standup_2026-05-02.txt")

    await index_document_chunks(tmp_sqlite, "doc_transcript", tmp_workspace, content=source.read_text(encoding="utf-8"))

    version = await _fetch_one(
        tmp_sqlite,
        "SELECT source_date, source_date_origin, seen_at FROM acw_source_versions",
    )
    document = await _fetch_one(
        tmp_sqlite,
        "SELECT date, metadata, mtime_ns, last_indexed_at FROM documents WHERE id = 'doc_transcript'",
    )
    chunk = await _fetch_one(
        tmp_sqlite,
        "SELECT content_hash, source_version_id FROM document_chunks WHERE document_id = 'doc_transcript'",
    )
    ledger = await _fetch_one(
        tmp_sqlite,
        "SELECT disposition, content_hash, document_chunk_id FROM acw_chunk_ledger",
    )

    assert version["source_date"] == "2026-05-02"
    assert version["source_date_origin"] == "content"
    assert version["seen_at"] != "2026-05-02"
    assert document["date"] == "2026-05-02"
    assert document["last_indexed_at"] != "2026-05-02"
    assert document["mtime_ns"] is not None
    assert json.loads(document["metadata"])["acw_source_kind"] == "transcript"
    assert chunk["content_hash"]
    assert chunk["source_version_id"]
    assert ledger["disposition"] == Disposition.pending
    assert ledger["content_hash"] == chunk["content_hash"]
    assert ledger["document_chunk_id"]


@pytest.mark.asyncio
async def test_fr_ledger_06_source_date_falls_back_to_user_then_unknown(tmp_sqlite, tmp_workspace) -> None:
    from core.db.migrate import apply_migrations
    from core.ingest import index_document_chunks

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    user_source = tmp_workspace / "docs" / "policy.md"
    user_source.parent.mkdir()
    user_source.write_text(
        "No explicit date appears in this policy body. The paragraph is intentionally "
        "long enough to become a chunk while leaving the user supplied document date as "
        "the only reliable source date for this source.",
        encoding="utf-8",
    )
    await _insert_document(
        tmp_sqlite,
        doc_id="doc_user_date",
        relative_path="docs/policy.md",
        date="2026-04-03",
    )
    await index_document_chunks(tmp_sqlite, "doc_user_date", tmp_workspace, content=user_source.read_text(encoding="utf-8"))

    unknown_source = tmp_workspace / "docs" / "unknown.md"
    unknown_source.write_text(
        "Still no explicit date appears here. The paragraph has enough operational "
        "detail to be indexed as a chunk, but it does not include a source date in "
        "content and no user date is stored on the document row.",
        encoding="utf-8",
    )
    await _insert_document(tmp_sqlite, doc_id="doc_unknown_date", relative_path="docs/unknown.md")
    await index_document_chunks(
        tmp_sqlite,
        "doc_unknown_date",
        tmp_workspace,
        content=unknown_source.read_text(encoding="utf-8"),
    )

    rows = await _fetch_all(
        tmp_sqlite,
        "SELECT source_id, source_date, source_date_origin FROM acw_source_versions ORDER BY source_id",
    )

    assert rows == [
        {"source_id": "doc_unknown_date", "source_date": "unknown", "source_date_origin": "unknown"},
        {"source_id": "doc_user_date", "source_date": "2026-04-03", "source_date_origin": "user"},
    ]


@pytest.mark.asyncio
async def test_fr_reing_01_hash_stable_chunk_keeps_disposition_on_changed_source(tmp_sqlite, tmp_workspace) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ingest import index_document_chunks
    from core.ledger import ChunkLedger

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    source = tmp_workspace / "docs" / "guide.md"
    source.parent.mkdir()
    initial = (
        "## Stable\n\nShared operational paragraph keeps its exact wording across versions "
        "so the ledger can carry forward the previous disposition without another placement "
        "decision from the pipeline.\n\n## Removed\n\nThis old paragraph disappears after the "
        "source update and should not remain current for the latest source version."
    )
    changed = (
        "## Stable\n\nShared operational paragraph keeps its exact wording across versions "
        "so the ledger can carry forward the previous disposition without another placement "
        "decision from the pipeline.\n\n## Added\n\nThis new paragraph appears after the source "
        "update and should start as pending for the next processing run."
    )
    source.write_text(initial, encoding="utf-8")
    await _insert_document(tmp_sqlite, doc_id="doc_guide", relative_path="docs/guide.md")

    stable_text = (
        "Shared operational paragraph keeps its exact wording across versions so the ledger "
        "can carry forward the previous disposition without another placement decision."
    )
    removed_text = "This old paragraph disappears after the source update and should not remain current."
    added_text = "This new paragraph appears after the source update and should start as pending."
    await index_document_chunks(
        tmp_sqlite,
        "doc_guide",
        tmp_workspace,
        content=initial,
        chunks=[_chunk(0, stable_text), _chunk(1, removed_text)],
    )
    stable = await _fetch_one(
        tmp_sqlite,
        "SELECT id FROM acw_chunk_ledger WHERE ordinal = 0",
    )
    await ChunkLedger(ACWDao(tmp_sqlite)).mark_irrelevant(
        stable["id"],
        reason="already classified",
        updated_at="2026-06-13T00:00:00Z",
    )

    source.write_text(changed, encoding="utf-8")
    await tmp_sqlite.execute(
        "UPDATE documents SET content = ?, content_hash = 'changed-version', version = version + 1 "
        "WHERE id = 'doc_guide'",
        (changed,),
    )
    await tmp_sqlite.commit()
    await index_document_chunks(
        tmp_sqlite,
        "doc_guide",
        tmp_workspace,
        content=changed,
        chunks=[_chunk(0, stable_text), _chunk(1, added_text)],
    )

    rows = await _fetch_all(
        tmp_sqlite,
        "SELECT cl.ordinal, cl.disposition, cl.disposition_reason "
        "FROM acw_chunk_ledger cl "
        "JOIN document_chunks dc ON cl.document_chunk_id = dc.id "
        "WHERE dc.document_id = 'doc_guide' "
        "ORDER BY cl.ordinal",
    )

    assert rows == [
        {"ordinal": 0, "disposition": Disposition.irrelevant, "disposition_reason": "already classified"},
        {"ordinal": 1, "disposition": Disposition.pending, "disposition_reason": None},
    ]


@pytest.mark.asyncio
async def test_fr_reing_02_matrix_changed_added_removed_chunks_supersedes_only_absent_chunks(
    tmp_sqlite,
    tmp_workspace,
) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ingest import index_document_chunks
    from core.ledger import ChunkLedger

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    source = tmp_workspace / "docs" / "source.md"
    source.parent.mkdir()
    initial = (
        "## One\n\nAlpha chunk remains unchanged with enough operational detail to be a "
        "stable indexed chunk across source versions and preserve its disposition.\n\n"
        "## Two\n\nBeta chunk will be removed by the next version, so its old ledger row "
        "should become superseded during re-ingestion."
    )
    changed = (
        "## One\n\nAlpha chunk remains unchanged with enough operational detail to be a "
        "stable indexed chunk across source versions and preserve its disposition.\n\n"
        "## Three\n\nGamma chunk is newly added in the updated source and should enter the "
        "ledger as pending."
    )
    source.write_text(initial, encoding="utf-8")
    await _insert_document(tmp_sqlite, doc_id="doc_source", relative_path="docs/source.md")
    stable_text = (
        "Alpha chunk remains unchanged with enough operational detail to be a stable indexed "
        "chunk across source versions and preserve its disposition."
    )
    removed_text = "Beta chunk will be removed by the next version, so its old ledger row should become superseded."
    added_text = "Gamma chunk is newly added in the updated source and should enter the ledger as pending."
    await index_document_chunks(
        tmp_sqlite,
        "doc_source",
        tmp_workspace,
        content=initial,
        chunks=[_chunk(0, stable_text), _chunk(1, removed_text)],
    )

    ledger = ChunkLedger(ACWDao(tmp_sqlite))
    old_chunks = await _fetch_all(
        tmp_sqlite,
        "SELECT id, ordinal FROM acw_chunk_ledger ORDER BY ordinal",
    )
    await ledger.mark_irrelevant(old_chunks[0]["id"], reason="stable classified")
    await ledger.mark_irrelevant(old_chunks[1]["id"], reason="old classified")

    source.write_text(changed, encoding="utf-8")
    await tmp_sqlite.execute(
        "UPDATE documents SET content = ?, content_hash = 'changed-version', version = version + 1 "
        "WHERE id = 'doc_source'",
        (changed,),
    )
    await tmp_sqlite.commit()
    await index_document_chunks(
        tmp_sqlite,
        "doc_source",
        tmp_workspace,
        content=changed,
        chunks=[_chunk(0, stable_text), _chunk(1, added_text)],
    )

    old_rows = await _fetch_all(
        tmp_sqlite,
        "SELECT ordinal, disposition FROM acw_chunk_ledger "
        "WHERE id IN (?, ?) ORDER BY ordinal",
        (old_chunks[0]["id"], old_chunks[1]["id"]),
    )
    current_rows = await _fetch_all(
        tmp_sqlite,
        "SELECT cl.ordinal, cl.disposition, cl.disposition_reason "
        "FROM acw_chunk_ledger cl "
        "JOIN document_chunks dc ON cl.document_chunk_id = dc.id "
        "WHERE dc.document_id = 'doc_source' "
        "ORDER BY cl.ordinal",
    )

    assert old_rows == [
        {"ordinal": 0, "disposition": Disposition.irrelevant},
        {"ordinal": 1, "disposition": Disposition.superseded},
    ]
    assert current_rows == [
        {"ordinal": 0, "disposition": Disposition.irrelevant, "disposition_reason": "stable classified"},
        {"ordinal": 1, "disposition": Disposition.pending, "disposition_reason": None},
    ]


@pytest.mark.asyncio
async def test_fr_reing_03_deleted_source_supersedes_chunks_and_flags_blocks(tmp_sqlite, tmp_workspace) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ingest import index_document_chunks
    from core.reingest import mark_source_deleted

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    source = tmp_workspace / "docs" / "source.md"
    source.parent.mkdir()
    content = (
        "## Rule\n\nDeleted source chunk backs this block with enough detail to be indexed "
        "and linked before the file is removed from the workspace."
    )
    source.write_text(content, encoding="utf-8")
    await _insert_document(tmp_sqlite, doc_id="doc_deleted", relative_path="docs/source.md")
    await index_document_chunks(tmp_sqlite, "doc_deleted", tmp_workspace, content=content)

    chunk = await _fetch_one(tmp_sqlite, "SELECT id FROM acw_chunk_ledger")
    dao = ACWDao(tmp_sqlite)
    await dao.create_page(
        page_id="pg_deleted",
        path="wiki/deleted.md",
        title="Deleted",
        description="Deleted source page",
        status="active",
        domain="",
        created_at="2026-06-13T00:00:00Z",
        aliases=[],
    )
    await dao.create_block(
        block_id="cb_deleted",
        page_id="pg_deleted",
        key="deleted.source.rule",
        block_type="rule",
        status="current",
        source_id="doc_deleted",
        source_path="docs/source.md",
        source_date="unknown",
        content_hash="block-hash",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )
    await dao.link_block_chunk("cb_deleted", chunk["id"])

    source.unlink()
    await mark_source_deleted(tmp_sqlite, "doc_deleted")

    chunk_row = await _fetch_one(tmp_sqlite, "SELECT disposition FROM acw_chunk_ledger WHERE id = ?", (chunk["id"],))
    block_row = await _fetch_one(
        tmp_sqlite,
        "SELECT status, needs_review_reason FROM acw_blocks WHERE id = 'cb_deleted'",
    )

    assert chunk_row["disposition"] == Disposition.superseded
    assert block_row == {
        "status": BlockStatus.needs_review,
        "needs_review_reason": "source_deleted",
    }


def test_m3_dod_reindex_uc1_minimal_yields_fully_pending_ledger(tmp_path) -> None:
    module = _load_llmwiki_module()
    workspace = tmp_path / "uc1_minimal"
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", workspace)

    module.cmd_init(str(workspace))
    module.cmd_reindex(str(workspace))

    import sqlite3

    conn = sqlite3.connect(str(workspace / ".llmwiki" / "index.db"))
    try:
        rows = conn.execute(
            "SELECT d.relative_path, dc.content_hash, dc.source_version_id, cl.disposition "
            "FROM document_chunks dc "
            "JOIN documents d ON d.id = dc.document_id "
            "JOIN acw_chunk_ledger cl ON cl.document_chunk_id = dc.id "
            "WHERE d.source_kind = 'source' "
            "ORDER BY d.relative_path, dc.chunk_index",
        ).fetchall()
    finally:
        conn.close()

    assert rows
    assert {row[3] for row in rows} == {Disposition.pending}
    assert all(row[1] and row[2] for row in rows)


async def _insert_document(
    db: aiosqlite.Connection,
    *,
    doc_id: str,
    relative_path: str,
    date: str | None = None,
) -> None:
    filename = Path(relative_path).name
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, source_kind, "
        "file_type, status, content, date, content_hash, mtime_ns, last_indexed_at, document_number) "
        "VALUES (?, 'user_1', ?, ?, ?, ?, 'source', ?, 'ready', '', ?, 'initial-version', "
        "111, '2026-06-13T00:00:00Z', "
        "(SELECT COALESCE(MAX(document_number), 0) + 1 FROM documents))",
        (
            doc_id,
            filename,
            filename.rsplit(".", 1)[0].title(),
            "/" + str(Path(relative_path).parent).replace("\\", "/").strip(".") + "/",
            relative_path,
            filename.rsplit(".", 1)[-1],
            date,
        ),
    )
    await db.commit()


async def _fetch_one(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple = (),
) -> dict:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    assert row is not None
    return dict(zip([description[0] for description in cursor.description], row, strict=True))


async def _fetch_all(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple = (),
) -> list[dict]:
    cursor = await db.execute(sql, params)
    return [
        dict(zip([description[0] for description in cursor.description], row, strict=True))
        for row in await cursor.fetchall()
    ]


def _load_llmwiki_module():
    path = ROOT / "llmwiki"
    loader = importlib.machinery.SourceFileLoader("llmwiki_cli_m3_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _chunk(index: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        content=content,
        page=None,
        start_char=index * 100,
        token_count=max(1, len(content) // 4),
        header_breadcrumb="",
    )

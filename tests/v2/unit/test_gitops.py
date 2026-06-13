from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment
from core.blocks.serializer import serialize_page
from core.models import BlockStatus, BlockType, Disposition
from tests.v2.unit.test_reingest import _apply_base_schema, _insert_document, _seed_workspace


def test_fr_git_01_auto_init_and_structured_commits_for_triggers(tmp_workspace: Path) -> None:
    from core.gitops import (
        commit_apply_decisions,
        commit_processing_run,
        commit_taxonomy_operation,
        ensure_wiki_repo,
    )

    ensure_wiki_repo(tmp_workspace)
    wiki = tmp_workspace / "wiki"
    (wiki / "one.md").write_text("one\n", encoding="utf-8")
    first = commit_processing_run(tmp_workspace, "run_one")
    (wiki / "two.md").write_text("two\n", encoding="utf-8")
    second = commit_apply_decisions(tmp_workspace, ["RR-run_one.md"])
    (wiki / "three.md").write_text("three\n", encoding="utf-8")
    third = commit_taxonomy_operation(tmp_workspace, "rename-page", "Refunds")

    subjects = _git(wiki, "log", "--format=%s").splitlines()
    assert first.committed and second.committed and third.committed
    assert subjects == [
        "acw taxonomy rename-page: Refunds",
        "acw apply-decisions: RR-run_one.md",
        "acw process: run_one",
    ]


@pytest.mark.asyncio
async def test_fr_git_02_pre_run_diff_marks_user_edited_and_annotates_recommendations(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.gitops import annotate_recommendation_basis, ensure_wiki_repo, mark_user_edited_blocks

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_tnc", relative_path="docs/tnc.md")
    dao = ACWDao(tmp_sqlite)
    await dao.create_page(
        page_id="pg_refunds",
        path="wiki/refunds.md",
        title="Refunds",
        description="",
        status="active",
        domain="",
        created_at="2026-06-13T00:00:00Z",
        aliases=[],
    )
    await dao.create_block(
        block_id="cb_existing",
        page_id="pg_refunds",
        key="refunds.retry_count",
        block_type="rule",
        status="current",
        source_id="doc_tnc",
        source_path="docs/tnc.md",
        source_date="unknown",
        content_hash="hash-existing",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )
    page_path = tmp_workspace / "wiki" / "refunds.md"
    page_path.write_text(
        serialize_page(Page([ProseSegment("# Refunds\n\n"), BlockSegment(_block(content="Refund retries use 3 attempts."))])),
        encoding="utf-8",
    )
    ensure_wiki_repo(tmp_workspace)
    _git(tmp_workspace / "wiki", "add", ".")
    _git(tmp_workspace / "wiki", "commit", "-m", "baseline")

    page_path.write_text(page_path.read_text(encoding="utf-8").replace("3 attempts", "4 attempts"), encoding="utf-8")

    changed = await mark_user_edited_blocks(tmp_workspace, tmp_sqlite)

    text = page_path.read_text(encoding="utf-8")
    db_row = await _fetch_one(tmp_sqlite, "SELECT user_edited FROM acw_blocks WHERE id = 'cb_existing'")
    assert changed == ["cb_existing"]
    assert '"user_edited":true' in text
    assert db_row["user_edited"] == 1
    assert annotate_recommendation_basis("source_date", "accept_new", user_edited=True) == (
        "source_date; edits a user-authored block"
    )
    assert annotate_recommendation_basis("source_date", "keep_existing", user_edited=True) == "source_date"


@pytest.mark.asyncio
async def test_fr_git_03_hard_delete_removes_page_and_ledger_content_and_prints_rewrite_warning(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.gitops import hard_delete_block
    from core.ledger import ChunkLedger

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_tnc", relative_path="docs/tnc.md")
    await tmp_sqlite.execute(
        "INSERT INTO document_chunks (id, document_id, chunk_index, content, source_content, token_count) "
        "VALUES ('dc_sensitive', 'doc_tnc', 0, 'sensitive evidence', 'sensitive evidence', 2)",
    )
    await tmp_sqlite.commit()
    dao = ACWDao(tmp_sqlite)
    await dao.create_page(
        page_id="pg_refunds",
        path="wiki/refunds.md",
        title="Refunds",
        description="",
        status="active",
        domain="",
        created_at="2026-06-13T00:00:00Z",
        aliases=[],
    )
    await dao.create_block(
        block_id="cb_sensitive",
        page_id="pg_refunds",
        key="refunds.retry_count",
        block_type="rule",
        status="current",
        source_id="doc_tnc",
        source_path="docs/tnc.md",
        source_date="unknown",
        content_hash="hash-sensitive",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )
    ledger = ChunkLedger(dao)
    version = await ledger.ensure_source_version(
        source_id="doc_tnc",
        version_hash="tnc-v1",
        source_date="unknown",
        source_date_origin="unknown",
        source_version_id="sv_tnc",
    )
    await ledger.create_pending_chunk(
        source_id="doc_tnc",
        source_version_id=version["id"],
        content_hash="hash-sensitive-chunk",
        ordinal=0,
        document_chunk_id="dc_sensitive",
        chunk_id="ch_sensitive",
    )
    await ledger.mark_placed("ch_sensitive", block_ids=["cb_sensitive"])
    page_path = tmp_workspace / "wiki" / "refunds.md"
    page_path.write_text(
        serialize_page(Page([ProseSegment("# Refunds\n\n"), BlockSegment(_block(block_id="cb_sensitive", content="Sensitive content."))])),
        encoding="utf-8",
    )

    result = await hard_delete_block(tmp_workspace, tmp_sqlite, "cb_sensitive")

    page_text = page_path.read_text(encoding="utf-8")
    block = await _fetch_one(tmp_sqlite, "SELECT status FROM acw_blocks WHERE id = 'cb_sensitive'")
    chunk = await _fetch_one(
        tmp_sqlite,
        "SELECT cl.disposition, dc.content, dc.source_content "
        "FROM acw_chunk_ledger cl JOIN document_chunks dc ON dc.id = cl.document_chunk_id "
        "WHERE cl.id = 'ch_sensitive'",
    )
    assert "Sensitive content." not in page_text
    assert "<!-- cb " not in page_text
    assert block["status"] == BlockStatus.deleted
    assert chunk == {"disposition": Disposition.superseded, "content": "", "source_content": ""}
    assert "git filter-repo" in result.warning
    assert "does not rewrite git history" in result.warning


def _block(
    *,
    block_id: str = "cb_existing",
    content: str,
) -> ContextBlock:
    return ContextBlock(
        id=block_id,
        key="refunds.retry_count",
        type=BlockType.rule,
        status=BlockStatus.current,
        source_id="doc_tnc",
        source_path="docs/tnc.md",
        source_date="unknown",
        chunk_ids=["ch_sensitive"],
        user_edited=False,
        content=content,
        excerpt="retry evidence",
    )


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


async def _fetch_one(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> dict:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    assert row is not None
    return dict(zip([description[0] for description in cursor.description], row, strict=True))

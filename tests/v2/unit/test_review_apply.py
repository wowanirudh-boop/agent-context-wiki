from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment
from core.blocks.serializer import serialize_page
from core.models import BlockStatus, BlockType, Disposition, ReviewDecision
from tests.v2.fakes.fake_llm import FakeLLM
from tests.v2.unit.test_reingest import _apply_base_schema, _insert_document, _seed_workspace


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (ReviewDecision.accept_new, {"existing": BlockStatus.deprecated, "candidate": BlockStatus.current, "chunk": Disposition.placed}),
        (ReviewDecision.keep_existing, {"existing": BlockStatus.current, "candidate": None, "chunk": Disposition.duplicate}),
        (ReviewDecision.merge, {"existing": BlockStatus.deprecated, "candidate": None, "chunk": Disposition.conflicted_pending}),
        (ReviewDecision.mark_conflicted, {"existing": BlockStatus.conflicted, "candidate": BlockStatus.conflicted, "chunk": Disposition.placed}),
        (
            ReviewDecision.deprecate_existing,
            {"existing": BlockStatus.deprecated, "candidate": BlockStatus.current, "chunk": Disposition.placed},
        ),
        (ReviewDecision.reject_new, {"existing": BlockStatus.current, "candidate": None, "chunk": Disposition.duplicate}),
        (ReviewDecision.delete_duplicate, {"existing": BlockStatus.deleted, "candidate": None, "chunk": Disposition.duplicate}),
        (ReviewDecision.needs_more_info, {"existing": BlockStatus.current, "candidate": None, "chunk": Disposition.conflicted_pending}),
    ],
)
@pytest.mark.asyncio
async def test_fr_rev_03_each_decision_applies_exact_table_effects(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
    decision: ReviewDecision,
    expected: dict[str, object],
) -> None:
    from core.review.apply import apply_review_file

    fixture = await _seed_review_case(tmp_workspace, tmp_sqlite, decision=decision.value)

    result = await apply_review_file(tmp_workspace, tmp_sqlite, fixture.review_path, provider=FakeLLM.rule_based())

    page = _read_page(tmp_workspace, fixture.page_path)
    existing = _block_by_id(page, fixture.existing_id)
    candidate = _block_by_id(page, fixture.candidate_id)
    chunk = await _fetch_one(tmp_sqlite, "SELECT disposition, duplicate_of_block_id FROM acw_chunk_ledger WHERE id = 'ch_candidate'")
    row = await _fetch_one(tmp_sqlite, "SELECT decision, applied_at FROM acw_review_rows WHERE id = ?", (fixture.row_id,))
    events = await _fetch_all(tmp_sqlite, "SELECT kind FROM acw_events ORDER BY kind")

    if expected["existing"] == BlockStatus.deleted:
        assert existing is None
        db_block = await _fetch_one(tmp_sqlite, "SELECT status FROM acw_blocks WHERE id = ?", (fixture.existing_id,))
        assert db_block["status"] == BlockStatus.deleted
    else:
        assert existing is not None
        assert existing.status == expected["existing"]

    if expected["candidate"] is None:
        assert candidate is None
    else:
        assert candidate is not None
        assert candidate.status == expected["candidate"]

    assert chunk["disposition"] == expected["chunk"]
    if expected["chunk"] == Disposition.duplicate:
        assert chunk["duplicate_of_block_id"] == fixture.canonical_existing_id
    assert row["decision"] == decision.value
    if decision == ReviewDecision.needs_more_info:
        assert row["applied_at"] is None
        assert fixture.row_id in existing.pending_review_ids
    else:
        assert row["applied_at"] is not None
    if decision == ReviewDecision.merge:
        assert result.follow_up_rows == 1
        follow_up = await _fetch_one(
            tmp_sqlite,
            "SELECT row_kind, candidate_json FROM acw_review_rows WHERE row_kind = 'needs_review'",
        )
        merged = json.loads(follow_up["candidate_json"])
        assert follow_up["row_kind"] == "needs_review"
        assert merged["content"] == "Refund retries use 3 attempts.\n\nRefund retries use 2 attempts."
    assert {"kind": "decision.applied"} in events


@pytest.mark.asyncio
async def test_fr_rev_04_validation_abort_applies_no_rows(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.review.apply import ReviewValidationError, apply_review_file

    fixture = await _seed_review_case(tmp_workspace, tmp_sqlite, decision="accept_new")
    before_page = (tmp_workspace / fixture.page_path).read_text(encoding="utf-8")
    text = fixture.review_path.read_text(encoding="utf-8")
    fixture.review_path.write_text(text.replace("- decision: accept_new", "- decision: not_a_decision"), encoding="utf-8")

    with pytest.raises(ReviewValidationError) as excinfo:
        await apply_review_file(tmp_workspace, tmp_sqlite, fixture.review_path, provider=FakeLLM.rule_based())

    assert "RR-run_review-1: unknown decision: not_a_decision" in str(excinfo.value)
    assert (tmp_workspace / fixture.page_path).read_text(encoding="utf-8") == before_page
    row = await _fetch_one(tmp_sqlite, "SELECT decision, applied_at FROM acw_review_rows WHERE id = ?", (fixture.row_id,))
    chunk = await _fetch_one(tmp_sqlite, "SELECT disposition FROM acw_chunk_ledger WHERE id = 'ch_candidate'")
    assert row == {"decision": None, "applied_at": None}
    assert chunk["disposition"] == Disposition.conflicted_pending


@pytest.mark.asyncio
async def test_fr_rev_03_preapproved_merge_text_skips_c4_and_applies_current_block(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.review.apply import apply_review_file

    fixture = await _seed_review_case(
        tmp_workspace,
        tmp_sqlite,
        decision="merge",
        notes="approved-merge: Refund retries use 4 attempts after reconciling both sources.",
    )
    fake = FakeLLM.rule_based()

    result = await apply_review_file(tmp_workspace, tmp_sqlite, fixture.review_path, provider=fake)

    page = _read_page(tmp_workspace, fixture.page_path)
    existing = _block_by_id(page, fixture.existing_id)
    merged = [block for block in page.blocks if block.id not in {fixture.existing_id, fixture.candidate_id}]
    chunk = await _fetch_one(tmp_sqlite, "SELECT disposition FROM acw_chunk_ledger WHERE id = 'ch_candidate'")
    assert existing.status == BlockStatus.deprecated
    assert len(merged) == 1
    assert merged[0].status == BlockStatus.current
    assert merged[0].content == "Refund retries use 4 attempts after reconciling both sources."
    assert merged[0].excerpt == "retry 3 times\nmaxRetries: 2"
    assert chunk["disposition"] == Disposition.placed
    assert result.follow_up_rows == 0
    assert [call.call_site for call in fake.calls] == []


class _ReviewFixture:
    def __init__(
        self,
        *,
        review_path: Path,
        page_path: str,
        row_id: str,
        existing_id: str,
        candidate_id: str,
        canonical_existing_id: str,
    ) -> None:
        self.review_path = review_path
        self.page_path = page_path
        self.row_id = row_id
        self.existing_id = existing_id
        self.candidate_id = candidate_id
        self.canonical_existing_id = canonical_existing_id


async def _seed_review_case(
    workspace: Path,
    db: aiosqlite.Connection,
    *,
    decision: str,
    notes: str = "",
) -> _ReviewFixture:
    from core.db.dao import ACWDao, json_dumps
    from core.db.migrate import apply_migrations
    from core.ledger import ChunkLedger
    from core.registry import PageRegistry
    from core.review.emit import emit_review_file

    await _apply_base_schema(db)
    await _seed_workspace(db)
    await apply_migrations(db)
    await _insert_document(db, doc_id="doc_tnc", relative_path="docs/tnc.md")
    await _insert_document(db, doc_id="doc_api", relative_path="docs/api.md")
    dao = ACWDao(db)
    registry = PageRegistry(dao)
    await dao.create_run(run_id="run_review", started_at="2026-06-13T00:00:00Z")
    page = await registry.create_page(title="Refunds", path="wiki/refunds.md", description="", page_id="pg_refunds")
    canonical = _block(block_id="cb_existing", source_id="doc_tnc", content="Refund retries use 3 attempts.", excerpt="retry 3 times")
    duplicate = _block(
        block_id="cb_duplicate",
        source_id="doc_tnc",
        content="Refund retries use 3 attempts.",
        excerpt="retry 3 times",
        chunk_ids=["ch_duplicate"],
    )
    candidate = _block(
        block_id="cb_candidate",
        source_id="doc_api",
        source_path="docs/api.md",
        source_date="2026-01-10",
        content="Refund retries use 2 attempts.",
        excerpt="maxRetries: 2",
        chunk_ids=["ch_candidate"],
    )
    row_existing = duplicate if decision == ReviewDecision.delete_duplicate.value else canonical
    row_existing = row_existing.model_copy(update={"pending_review_ids": ["RR-run_review-1"]})
    page_blocks = [row_existing] if row_existing.id == canonical.id else [canonical, row_existing]
    _write_page(workspace, page["path"], page["title"], page_blocks)

    for block in page_blocks:
        await dao.create_block(
            block_id=block.id,
            page_id=page["id"],
            key=block.key,
            block_type=block.type.value,
            status=block.status.value,
            source_id=str(block.source_id),
            source_path=block.source_path,
            source_date=block.source_date,
            content_hash=f"hash-{block.id}",
            created_at="2026-06-13T00:00:00Z",
            updated_at="2026-06-13T00:00:00Z",
        )

    ledger = ChunkLedger(dao)
    sv_tnc = await ledger.ensure_source_version(
        source_id="doc_tnc",
        version_hash="tnc-v1",
        source_date="unknown",
        source_date_origin="unknown",
        source_version_id="sv_tnc",
    )
    sv_api = await ledger.ensure_source_version(
        source_id="doc_api",
        version_hash="api-v1",
        source_date="2026-01-10",
        source_date_origin="content",
        source_version_id="sv_api",
    )
    await ledger.create_pending_chunk(
        source_id="doc_tnc",
        source_version_id=sv_tnc["id"],
        content_hash="hash-existing-chunk",
        ordinal=0,
        chunk_id="ch_existing",
    )
    await ledger.mark_placed("ch_existing", block_ids=[canonical.id])
    await ledger.create_pending_chunk(
        source_id="doc_api",
        source_version_id=sv_api["id"],
        content_hash="hash-candidate-chunk",
        ordinal=0,
        chunk_id="ch_candidate",
    )
    await ledger.mark_conflicted_pending("ch_candidate", reason="changed_value")
    if row_existing.id == duplicate.id:
        await ledger.create_pending_chunk(
            source_id="doc_tnc",
            source_version_id=sv_tnc["id"],
            content_hash="hash-duplicate-chunk",
            ordinal=1,
            chunk_id="ch_duplicate",
        )
        await ledger.mark_placed("ch_duplicate", block_ids=[duplicate.id])

    await dao.create_review_row(
        row_id="RR-run_review-1",
        run_id="run_review",
        page_id=page["id"],
        row_kind="conflict",
        existing_block_id=row_existing.id,
        candidate_json=json_dumps(candidate.model_dump(mode="json")),
        conflict_type="changed_value",
        recommendation="accept_new",
        recommendation_basis="source_date",
    )
    review_path = await emit_review_file(workspace, db, "run_review")
    text = review_path.read_text(encoding="utf-8").replace("- decision:", f"- decision: {decision}")
    text = text.replace("- notes:", f"- notes: {notes}")
    review_path.write_text(text, encoding="utf-8")
    return _ReviewFixture(
        review_path=review_path,
        page_path=str(page["path"]),
        row_id="RR-run_review-1",
        existing_id=row_existing.id,
        candidate_id=candidate.id,
        canonical_existing_id=canonical.id,
    )


def _block(
    *,
    block_id: str,
    source_id: str,
    source_path: str = "docs/tnc.md",
    source_date: str = "unknown",
    content: str,
    excerpt: str,
    chunk_ids: list[str] | None = None,
) -> ContextBlock:
    return ContextBlock(
        id=block_id,
        key="refunds.retry_count",
        type=BlockType.rule,
        status=BlockStatus.current,
        source_id=source_id,
        source_path=source_path,
        source_date=source_date,
        chunk_ids=chunk_ids or ["ch_existing"],
        user_edited=False,
        content=content,
        excerpt=excerpt,
    )


def _write_page(workspace: Path, path: str, title: str, blocks: list[ContextBlock]) -> None:
    page_path = workspace / path
    page_path.parent.mkdir(parents=True, exist_ok=True)
    segments = [ProseSegment(f"# {title}\n\n"), *[BlockSegment(block) for block in blocks], ProseSegment("\n")]
    page_path.write_text(serialize_page(Page(segments)), encoding="utf-8")


def _read_page(workspace: Path, page_path: str) -> Page:
    from core.blocks.parser import parse_page

    return parse_page((workspace / page_path).read_text(encoding="utf-8"))


def _block_by_id(page: Page, block_id: str) -> ContextBlock | None:
    return next((block for block in page.blocks if block.id == block_id), None)


async def _fetch_one(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> dict:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    assert row is not None
    return dict(zip([description[0] for description in cursor.description], row, strict=True))


async def _fetch_all(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cursor = await db.execute(sql, params)
    return [
        dict(zip([description[0] for description in cursor.description], row, strict=True))
        for row in await cursor.fetchall()
    ]

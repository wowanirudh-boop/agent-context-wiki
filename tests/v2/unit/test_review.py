from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment
from core.blocks.serializer import serialize_page
from core.models import BlockStatus, BlockType
from tests.v2.unit.test_reingest import _apply_base_schema, _insert_document, _seed_workspace


@pytest.mark.asyncio
async def test_fr_rev_01_emit_format_persists_rows_and_parser_round_trips(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.registry import PageRegistry
    from core.review.emit import emit_review_file
    from core.review.parse import parse_review_file, serialize_review_file

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_tnc", relative_path="docs/tnc.md")
    await _insert_document(tmp_sqlite, doc_id="doc_api", relative_path="docs/api.md")
    dao = ACWDao(tmp_sqlite)
    await dao.create_run(run_id="run_review", started_at="2026-06-13T00:00:00Z")

    registry = PageRegistry(dao)
    page = await registry.create_page(title="Refunds", path="wiki/refunds.md", description="", page_id="pg_refunds")
    existing = _block(
        block_id="cb_existing",
        source_id="doc_tnc",
        source_path="docs/tnc.md",
        content="Refund retries use 3 attempts.",
        excerpt="retry 3 times",
    )
    candidate = _block(
        block_id="cb_candidate",
        source_id="doc_api",
        source_path="docs/api.md",
        source_date="2026-01-10",
        content="Refund retries use 2 attempts.",
        excerpt="maxRetries: 2",
        chunk_ids=["ch_api"],
    )
    _write_page(tmp_workspace, page["path"], page["title"], [existing])
    await dao.create_block(
        block_id=existing.id,
        page_id=page["id"],
        key=existing.key,
        block_type=existing.type.value,
        status=existing.status.value,
        source_id=str(existing.source_id),
        source_path=existing.source_path,
        source_date=existing.source_date,
        content_hash="hash-existing",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )
    row = await dao.create_review_row(
        row_id="RR-run_review-1",
        run_id="run_review",
        page_id=page["id"],
        row_kind="conflict",
        existing_block_id=existing.id,
        candidate_json=json.dumps(candidate.model_dump(mode="json"), sort_keys=True),
        conflict_type="changed_value",
        recommendation="accept_new",
        recommendation_basis="source_date",
    )

    review_path = await emit_review_file(tmp_workspace, tmp_sqlite, "run_review")

    assert row["id"] == "RR-run_review-1"
    assert review_path == tmp_workspace / "wiki" / "_reviews" / "RR-run_review.md"
    text = review_path.read_text(encoding="utf-8")
    assert "# Review RR-run_review" in text
    assert "Run: run_review \u00b7 Started: 2026-06-13T00:00:00Z \u00b7 Rows: 1 \u00b7 Status: open" in text
    assert "## Page: [[Refunds]] (wiki/refunds.md)" in text
    assert "### Row RR-run_review-1 \u00b7 conflict \u00b7 changed_value" in text
    assert "- existing block: cb_existing \u00b7 key `refunds.retry_count` \u00b7 status current" in text
    assert "- candidate block: cb_candidate \u00b7 key `refunds.retry_count` \u00b7 status current" in text
    assert "- recommendation: accept_new \u2014 basis: source_date" in text
    assert "- decision:\n- notes:\n" in text

    parsed = parse_review_file(text)
    assert [parsed_row.id for parsed_row in parsed.rows] == ["RR-run_review-1"]
    assert parsed.rows[0].decision is None
    assert parsed.rows[0].validation_errors == []
    assert serialize_review_file(parsed) == text


def test_fr_rev_01_parser_normalizes_decisions_and_reports_invalid_rows() -> None:
    from core.review.parse import parse_review_file

    parsed = parse_review_file(
        "# Review RR-run_test\n"
        "Run: run_test \u00b7 Started: 2026-06-13T00:00:00Z \u00b7 Rows: 2 \u00b7 Status: open\n\n"
        "## Page: [[Refunds]] (wiki/refunds.md)\n\n"
        "### Row RR-run_test-1 \u00b7 conflict \u00b7 changed_value\n"
        "- recommendation: accept_new \u2014 basis: source_date\n"
        "- decision: ACCEPT_NEW\n"
        "- notes: use latest API\n\n"
        "### Row RR-run_test-2 \u00b7 taxonomy_merge\n"
        "- recommendation: merge \u2014 high registry similarity\n"
        "- decision: accept_new\n"
        "- notes:\n"
    )

    assert parsed.rows[0].decision == "accept_new"
    assert parsed.rows[0].notes == "use latest API"
    assert parsed.rows[0].validation_errors == []
    assert parsed.rows[1].decision == "accept_new"
    assert parsed.rows[1].validation_errors == ["taxonomy_merge rows only accept merge, reject_new, or needs_more_info"]


@pytest.mark.asyncio
async def test_fr_reg_04_taxonomy_merge_advisory_rows_are_persisted(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.registry import PageRegistry
    from core.review.emit import create_taxonomy_merge_review_rows, emit_review_file

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    dao = ACWDao(tmp_sqlite)
    await dao.create_run(run_id="run_taxonomy", started_at="2026-06-13T00:00:00Z")
    registry = PageRegistry(dao)
    refunds = await registry.create_page(
        title="Refund Rules",
        path="wiki/refund-rules.md",
        description="Refund policy and retry behavior",
        page_id="pg_refund_rules",
    )
    await registry.create_page(
        title="Refund Policy",
        path="wiki/refund-policy.md",
        description="Refund policy and retry behavior",
        page_id="pg_refund_policy",
    )
    await registry.create_page(
        title="Shipping",
        path="wiki/shipping.md",
        description="Carrier policy",
        page_id="pg_shipping",
    )

    rows = await create_taxonomy_merge_review_rows(tmp_sqlite, "run_taxonomy")
    review_path = await emit_review_file(tmp_workspace, tmp_sqlite, "run_taxonomy")

    assert len(rows) == 1
    candidate = json.loads(rows[0]["candidate_json"])
    assert {rows[0]["page_id"], candidate["page_id"]} == {refunds["id"], "pg_refund_policy"}
    assert rows[0]["row_kind"] == "taxonomy_merge"
    text = review_path.read_text(encoding="utf-8")
    assert "### Row RR-run_taxonomy-1 \u00b7 taxonomy_merge" in text
    assert (
        "- merge candidate: [[Refund Policy]] (wiki/refund-policy.md)" in text
        or "- merge candidate: [[Refund Rules]] (wiki/refund-rules.md)" in text
    )


@pytest.mark.asyncio
async def test_fr_rev_05_unresolved_review_files_are_surfaced_at_run_start(tmp_workspace: Path) -> None:
    from core.db.migrate import apply_migrations
    from core.pipeline.run import run_processing_run
    from tests.v2.fakes.fake_llm import FakeLLM

    db_path = tmp_workspace / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        await _apply_base_schema(db)
        await _seed_workspace(db)
        await apply_migrations(db)
    review_dir = tmp_workspace / "wiki" / "_reviews"
    review_dir.mkdir(parents=True)
    (review_dir / "RR-run_old.md").write_text(
        "# Review RR-run_old\n"
        "Run: run_old \u00b7 Started: 2026-06-12T00:00:00Z \u00b7 Rows: 1 \u00b7 Status: open\n\n"
        "## Page: [[Refunds]] (wiki/refunds.md)\n\n"
        "### Row RR-run_old-1 \u00b7 conflict \u00b7 changed_value\n"
        "- decision:\n"
        "- notes:\n",
        encoding="utf-8",
    )

    result = await run_processing_run(tmp_workspace, provider=FakeLLM.rule_based())

    assert result.stats["unresolved_reviews"] == 1
    assert result.stats["unresolved_review_files"] == ["wiki/_reviews/RR-run_old.md"]


def _block(
    *,
    block_id: str,
    source_id: str = "doc_tnc",
    source_path: str,
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
        chunk_ids=chunk_ids or ["ch_tnc"],
        user_edited=False,
        content=content,
        excerpt=excerpt,
    )


def _write_page(workspace: Path, path: str, title: str, blocks: list[ContextBlock]) -> None:
    page_path = workspace / path
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(serialize_page(Page([ProseSegment(f"# {title}\n\n"), *[BlockSegment(block) for block in blocks]])), encoding="utf-8")

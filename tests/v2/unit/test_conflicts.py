from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment
from core.blocks.serializer import serialize_page
from core.models import BlockStatus, BlockType, ConflictType, ReviewDecision
from tests.v2.fakes.fake_llm import FakeLLM, fingerprint_payload
from tests.v2.unit.test_reingest import _apply_base_schema, _insert_document, _seed_workspace


@pytest.mark.asyncio
async def test_fr_conf_01_retrieval_uses_key_match_and_fts_over_target_and_related_pages(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.conflicts.detect import retrieve_comparison_candidates
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.registry import PageRegistry

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_existing", relative_path="docs/existing.md")

    registry = PageRegistry(ACWDao(tmp_sqlite))
    target = await registry.create_page(
        title="Refunds",
        path="wiki/refunds.md",
        description="Refund retry policy",
        page_id="pg_refunds",
    )
    related = await registry.create_page(
        title="Related Refunds",
        path="wiki/related-refunds.md",
        description="Payment provider retry details",
        page_id="pg_related",
    )
    unrelated = await registry.create_page(
        title="Unrelated Retries",
        path="wiki/unrelated.md",
        description="Retry details outside this page graph",
        page_id="pg_unrelated",
    )

    target_block = _block(block_id="cb_key", key="refunds.retry_count", content="Refund retries use 3 attempts.")
    related_block = _block(
        block_id="cb_related",
        key="payments.provider.retry_limit",
        content="The payment provider retry limit is 5 attempts before handoff.",
    )
    unrelated_block = _block(
        block_id="cb_unrelated",
        key="shipping.retry_limit",
        content="The shipping retry limit is 8 attempts.",
    )
    _write_page(tmp_workspace, target["path"], target["title"], [target_block], extra_prose="See [[Related Refunds]].\n")
    _write_page(tmp_workspace, related["path"], related["title"], [related_block])
    _write_page(tmp_workspace, unrelated["path"], unrelated["title"], [unrelated_block])
    await _persist_blocks(tmp_sqlite, target["id"], [target_block])
    await _persist_blocks(tmp_sqlite, related["id"], [related_block])
    await _persist_blocks(tmp_sqlite, unrelated["id"], [unrelated_block])

    candidate = _block(
        block_id="cb_candidate",
        key="refunds.retry_count",
        content="Refund retries use 2 attempts.",
        excerpt="maxRetries: 2",
        source_path="docs/api.md",
    )

    retrieved = await retrieve_comparison_candidates(tmp_workspace, tmp_sqlite, target, candidate)

    assert [item.block.id for item in retrieved][:2] == ["cb_key", "cb_related"]
    assert "cb_unrelated" not in {item.block.id for item in retrieved}


@pytest.mark.asyncio
async def test_fr_conf_03_exact_duplicate_short_circuits_without_c3_call(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.conflicts.detect import detect_candidate_conflict
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.registry import PageRegistry

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_existing", relative_path="docs/tnc.md")

    registry = PageRegistry(ACWDao(tmp_sqlite))
    page = await registry.create_page(title="Refunds", path="wiki/refunds.md", description="", page_id="pg_refunds")
    existing = _block(
        block_id="cb_existing",
        key="refunds.retry_count",
        content="Refund retries use 3 attempts.",
        excerpt="retry 3 times",
        source_path="docs/tnc.md",
    )
    _write_page(tmp_workspace, page["path"], page["title"], [existing])
    await _persist_blocks(tmp_sqlite, page["id"], [existing])
    candidate = _block(
        block_id="cb_candidate",
        key="refunds.retry_count",
        content="Refund retries use  3 attempts.",
        excerpt="retry 3 times",
        source_path="docs/tnc.md",
    )
    fake = FakeLLM.rule_based()

    result = await detect_candidate_conflict(tmp_workspace, tmp_sqlite, page, candidate, provider=fake)

    assert result.kind == "duplicate"
    assert result.existing.block.id == "cb_existing"
    assert fake.call_count == 0


@pytest.mark.asyncio
async def test_fr_conf_04_c3_judge_records_code_computed_recommendation_basis() -> None:
    from core.conflicts.detect import (
        ComparisonBlock,
        build_c3_payload,
        compute_recommendation_basis,
        judge_conflict_pair,
    )

    candidate = _block(
        block_id="cb_candidate",
        key="refunds.retry_count",
        content="Refund retries use 2 attempts.",
        excerpt="maxRetries: 2",
        source_path="docs/api.md",
        source_date="2026-01-10",
        chunk_ids=["ch_candidate"],
    )
    existing = ComparisonBlock(
        page_id="pg_refunds",
        page_path="wiki/refunds.md",
        page_title="Refunds",
        block=_block(
            block_id="cb_existing",
            key="refunds.retry_count",
            content="Refund retries use 3 attempts.",
            excerpt="retry 3 times",
            source_path="docs/tnc.md",
            source_date="2025-12-01",
        ),
        retrieval_reason="key",
    )
    basis = compute_recommendation_basis(candidate, existing.block)
    payload = build_c3_payload(candidate, existing, basis)
    fake = FakeLLM.scripted(
        {
            ("C3", fingerprint_payload(payload)): {
                "verdict": "conflict",
                "conflict_type": "changed_value",
                "recommendation": "accept_new",
                "rationale": "The candidate has the newer source_date.",
            }
        }
    )

    result = await judge_conflict_pair(fake, candidate, existing, basis)

    assert result.verdict == "conflict"
    assert result.conflict_type == ConflictType.changed_value
    assert result.recommendation == ReviewDecision.accept_new
    assert result.recommendation_basis == "source_date"
    assert fake.calls[0].payload["recommendation_basis"]["basis"] == "source_date"


@pytest.mark.asyncio
async def test_fr_conf_05_pending_marker_and_open_conflicts_use_page_mutation_api(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.conflicts.markers import apply_pending_conflict_marker
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ids import review_row_id
    from core.registry import PageRegistry

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_existing", relative_path="docs/tnc.md")

    registry = PageRegistry(ACWDao(tmp_sqlite))
    page = await registry.create_page(title="Refunds", path="wiki/refunds.md", description="", page_id="pg_refunds")
    existing = _block(block_id="cb_existing", key="refunds.retry_count", content="Refund retries use 3 attempts.")
    _write_page(tmp_workspace, page["path"], page["title"], [existing])
    await _persist_blocks(tmp_sqlite, page["id"], [existing])
    row_id = review_row_id("run_test", 1)
    dao = ACWDao(tmp_sqlite)
    await dao.create_run(run_id="run_test", started_at="2026-06-13T00:00:00Z")
    await dao.create_review_row(
        row_id=row_id,
        run_id="run_test",
        page_id=page["id"],
        row_kind="conflict",
        existing_block_id=existing.id,
        candidate_json=json.dumps(_block(block_id="cb_candidate").model_dump(mode="json"), sort_keys=True),
        conflict_type="changed_value",
        recommendation="accept_new",
        recommendation_basis="source_date",
    )

    await apply_pending_conflict_marker(tmp_workspace, tmp_sqlite, page, existing.id, row_id, run_id="run_test")

    text = (tmp_workspace / page["path"]).read_text(encoding="utf-8")
    assert "Refund retries use 3 attempts.\n\u26a0 pending review: RR-run_test-1\n> Excerpt" in text
    assert "## Open Conflicts" in text
    assert "<!-- acw:generated Open Conflicts run=run_test" in text
    assert "- RR-run_test-1: changed_value on `refunds.retry_count`" in text
    assert serialize_page(__import__("core.blocks.parser", fromlist=["parse_page"]).parse_page(text)) == text


def _block(
    *,
    block_id: str = "cb_test",
    key: str = "refunds.retry_count",
    content: str = "Refund retries use 3 attempts.",
    excerpt: str = "retry 3 times",
    source_path: str = "docs/existing.md",
    source_date: str = "unknown",
    chunk_ids: list[str] | None = None,
) -> ContextBlock:
    return ContextBlock(
        id=block_id,
        key=key,
        type=BlockType.rule,
        status=BlockStatus.current,
        source_id="doc_existing",
        source_path=source_path,
        source_date=source_date,
        chunk_ids=chunk_ids or ["ch_existing"],
        user_edited=False,
        content=content,
        excerpt=excerpt,
    )


def _write_page(workspace: Path, path: str, title: str, blocks: list[ContextBlock], *, extra_prose: str = "") -> None:
    page = Page([ProseSegment(f"# {title}\n\n{extra_prose}"), *[BlockSegment(block) for block in blocks]])
    page_path = workspace / path
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(serialize_page(page), encoding="utf-8")


async def _persist_blocks(db: aiosqlite.Connection, page_id: str, blocks: list[ContextBlock]) -> None:
    from core.db.dao import ACWDao

    dao = ACWDao(db)
    for block in blocks:
        await dao.create_block(
            block_id=block.id,
            page_id=page_id,
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

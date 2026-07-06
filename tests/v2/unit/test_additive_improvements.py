from __future__ import annotations

import asyncio
import csv
import json
import sys
import types
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment
from core.blocks.serializer import serialize_page
from core.models import BlockStatus, BlockType, Disposition, ReviewDecision, ReviewRowKind
from tests.v2.fakes.fake_llm import FakeLLM
from tests.v2.unit.test_reingest import (
    _apply_base_schema,
    _fetch_all,
    _insert_document,
    _load_llmwiki_module,
    _seed_workspace,
)


@pytest.mark.asyncio
async def test_fr_place_08_reset_dispositions_cli_resets_matching_source_batches(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ledger import ChunkLedger

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_a", relative_path="docs/source-a.md")
    await _insert_document(tmp_sqlite, doc_id="doc_b", relative_path="docs/source-b.md")
    ledger = ChunkLedger(ACWDao(tmp_sqlite))
    sv_a = await ledger.ensure_source_version(
        source_id="doc_a",
        version_hash="a-v1",
        source_date="unknown",
        source_date_origin="unknown",
        source_version_id="sv_a",
    )
    sv_b = await ledger.ensure_source_version(
        source_id="doc_b",
        version_hash="b-v1",
        source_date="unknown",
        source_date_origin="unknown",
        source_version_id="sv_b",
    )
    for chunk_id, source_id, sv_id, ordinal in [
        ("ch_a1", "doc_a", sv_a["id"], 0),
        ("ch_a2", "doc_a", sv_a["id"], 1),
        ("ch_b1", "doc_b", sv_b["id"], 0),
    ]:
        await ledger.create_pending_chunk(
            source_id=source_id,
            source_version_id=sv_id,
            content_hash=f"hash-{chunk_id}",
            ordinal=ordinal,
            chunk_id=chunk_id,
        )
        await ledger.mark_irrelevant(chunk_id, reason=f"reason-{chunk_id}")

    module = _load_llmwiki_module()
    await asyncio.to_thread(
        module.cmd_reset_dispositions,
        str(tmp_workspace),
        source="source-a",
        from_disposition=Disposition.irrelevant.value,
        to_disposition=Disposition.pending.value,
    )

    output = capsys.readouterr().out
    rows = await _fetch_all(
        tmp_sqlite,
        "SELECT id, disposition, disposition_reason FROM acw_chunk_ledger ORDER BY id",
    )
    events = await _fetch_all(tmp_sqlite, "SELECT kind, payload_json FROM acw_events")

    assert "reset_chunks: 2" in output
    assert rows == [
        {"id": "ch_a1", "disposition": Disposition.pending, "disposition_reason": None},
        {"id": "ch_a2", "disposition": Disposition.pending, "disposition_reason": None},
        {"id": "ch_b1", "disposition": Disposition.irrelevant, "disposition_reason": "reason-ch_b1"},
    ]
    assert len(events) == 1
    assert events[0]["kind"] == "chunk.disposition"
    assert json.loads(events[0]["payload_json"]) == {
        "count": 2,
        "from": "irrelevant",
        "source": "source-a",
        "to": "pending",
    }


def test_c1_c6_instructions_are_provider_and_queue_visible(tmp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import ACWConfig
    from core.llm.agent_provider import AgentQueue
    from core.llm.instructions import INSTRUCTIONS
    from core.llm.provider import OpenAIProvider

    assert set(INSTRUCTIONS) == {"C1", "C2", "C3", "C4", "C5", "C6"}
    assert "workspace_domain_hint" in INSTRUCTIONS["C1"]
    assert "structured/tabular reference data" in INSTRUCTIONS["C1"]
    assert "prefer relevant" in INSTRUCTIONS["C1"]

    recorded: dict[str, Any] = {}

    class _Responses:
        async def create(self, **kwargs: Any) -> Any:
            recorded.update(kwargs)
            return types.SimpleNamespace(output_text='{"summary_markdown":"ok"}')

    class _AsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            recorded["client"] = kwargs
            self.responses = _Responses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=_AsyncOpenAI))

    provider = OpenAIProvider(ACWConfig(llm_api_key="key"))
    import asyncio

    response = asyncio.run(
        provider.complete_structured(
            "C5",
            {"title": "Refunds", "blocks": []},
            {"type": "object", "required": ["summary_markdown"]},
        )
    )
    assert response == {"summary_markdown": "ok"}
    assert recorded["input"][0] == {"role": "system", "content": INSTRUCTIONS["C5"]}
    assert recorded["input"][1]["role"] == "user"

    request = AgentQueue(tmp_workspace).create_request(
        call_site="C1",
        payload={"chunks": []},
        schema={"type": "object"},
    )
    assert request["instructions"] == INSTRUCTIONS["C1"]


@pytest.mark.asyncio
async def test_domain_hint_file_is_read_once_per_run_and_sent_to_c1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.pipeline.run import run_processing_run

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".llmwiki").mkdir()
    (workspace / ".llmwiki" / "domain_hint.md").write_text(
        "Refund operations, support RCA, and structured product reference tables.",
        encoding="utf-8",
    )
    source = workspace / "docs" / "policy.md"
    source.parent.mkdir()
    source.write_text(
        "Refund operations require supervisors to approve returns within 30 days for warranty-backed orders. "
        "Support agents use this policy when deciding whether a case should be escalated, refunded, "
        "or routed back to the merchant team. The policy includes structured decision data that "
        "should remain available to downstream testing and RCA agents.",
        encoding="utf-8",
    )
    module = _load_llmwiki_module()
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    await asyncio.to_thread(module.cmd_init, str(workspace))
    await asyncio.to_thread(module.cmd_reindex, str(workspace))
    fake = FakeLLM.rule_based()

    await run_processing_run(workspace, provider=fake)

    c1_calls = [call for call in fake.calls if call.call_site == "C1"]
    assert c1_calls
    assert {call.payload["workspace_domain_hint"] for call in c1_calls} == {
        "Refund operations, support RCA, and structured product reference tables."
    }


@pytest.mark.asyncio
async def test_fr_rev_01_review_csv_sidecar_and_import_decisions_updates_markdown(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.db.dao import ACWDao, json_dumps
    from core.db.migrate import apply_migrations
    from core.registry import PageRegistry
    from core.review.csv_io import ImportDecisionError, import_decisions_from_csv
    from core.review.emit import emit_review_file

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_existing", relative_path="docs/existing.md")
    await _insert_document(tmp_sqlite, doc_id="doc_candidate", relative_path="docs/candidate.md")
    dao = ACWDao(tmp_sqlite)
    await dao.create_run(run_id="run_csv", started_at="2026-06-13T00:00:00Z")
    page = await PageRegistry(dao).create_page(title="Refunds", path="wiki/refunds.md", description="", page_id="pg_csv")
    existing = _block(
        block_id="cb_existing",
        source_id="doc_existing",
        source_path="docs/existing.md",
        content="Existing content " + "x" * 220,
        excerpt="existing excerpt",
    )
    candidate = _block(
        block_id="cb_candidate",
        source_id="doc_candidate",
        source_path="docs/candidate.md",
        content="Candidate content " + "y" * 220,
        excerpt="candidate excerpt",
        chunk_ids=["ch_candidate"],
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
    await dao.create_review_row(
        row_id="RR-run_csv-1",
        run_id="run_csv",
        page_id=page["id"],
        row_kind=ReviewRowKind.conflict.value,
        existing_block_id=existing.id,
        candidate_json=json_dumps(candidate.model_dump(mode="json")),
        conflict_type="changed_value",
        recommendation="accept_new",
        recommendation_basis="source_date",
    )

    review_path = await emit_review_file(tmp_workspace, tmp_sqlite, "run_csv")
    csv_path = review_path.with_suffix(".csv")

    assert csv_path.is_file()
    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0]["row_id"] == "RR-run_csv-1"
    assert rows[0]["page"] == "wiki/refunds.md"
    assert rows[0]["key"] == "refunds.retry_count"
    assert len(rows[0]["existing_content_short"]) <= 150
    assert len(rows[0]["candidate_content_short"]) <= 150

    rows[0]["decision"] = ReviewDecision.accept_new.value
    rows[0]["notes"] = "Use the newer candidate."
    _write_csv(csv_path, rows)
    result = import_decisions_from_csv(tmp_workspace, csv_path)
    text = review_path.read_text(encoding="utf-8")

    assert result.updated_rows == 1
    assert "- decision: accept_new" in text
    assert "- notes: Use the newer candidate." in text

    rows[0]["row_id"] = "RR-run_csv-404"
    _write_csv(csv_path, rows)
    with pytest.raises(ImportDecisionError, match="unknown row_id"):
        import_decisions_from_csv(tmp_workspace, csv_path)

    rows[0]["row_id"] = "RR-run_csv-1"
    rows[0]["decision"] = "not_a_decision"
    _write_csv(csv_path, rows)
    with pytest.raises(ImportDecisionError, match="invalid decision"):
        import_decisions_from_csv(tmp_workspace, csv_path)


@pytest.mark.asyncio
async def test_bulk_exclusion_checkpoint_review_apply_and_lint(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.config import ACWConfig
    from core.db.migrate import apply_migrations
    from core.ingest import index_document_chunks
    from core.lint.runner import lint_workspace
    from core.pipeline.run import run_processing_run
    from core.review.apply import apply_review_file

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_bulk", relative_path="docs/reference-table.md")
    source = tmp_workspace / "docs" / "reference-table.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("bulk source", encoding="utf-8")
    await index_document_chunks(
        tmp_sqlite,
        "doc_bulk",
        tmp_workspace,
        content="bulk source",
        chunks=[
            _chunk(0, "Structured reference table row A should be kept for product support."),
            _chunk(1, "Structured reference table row B should be kept for product support."),
            _chunk(2, "Structured reference table row C should be kept for product support."),
        ],
    )
    chunk_rows = await _fetch_all(tmp_sqlite, "SELECT id FROM acw_chunk_ledger ORDER BY ordinal")
    fake = FakeLLM.scripted(
        {
            (
                "C1",
                _fingerprint([row["id"] for row in chunk_rows]),
            ): {
                "chunks": [
                    {
                        "chunk_id": row["id"],
                        "relevant": False,
                        "irrelevant_reason": "table rows look unrelated to the workspace",
                        "placements": [],
                    }
                    for row in chunk_rows
                ]
            }
        }
    )

    result = await run_processing_run(
        tmp_workspace,
        provider=fake,
        config=ACWConfig(exclusion_review_threshold=0.5),
    )

    ledger_rows = await _fetch_all(
        tmp_sqlite,
        "SELECT id, disposition, disposition_reason FROM acw_chunk_ledger ORDER BY ordinal",
    )
    review_rows = await _fetch_all(
        tmp_sqlite,
        "SELECT id, row_kind, candidate_json FROM acw_review_rows ORDER BY id",
    )
    review_path = tmp_workspace / result.stats["review_file"]
    review_text = review_path.read_text(encoding="utf-8")
    csv_text = review_path.with_suffix(".csv").read_text(encoding="utf-8")
    findings = await lint_workspace(tmp_workspace, config=ACWConfig(exclusion_review_threshold=0.5))

    assert {row["disposition"] for row in ledger_rows} == {Disposition.exclusion_pending}
    assert result.stats["exclusion_pending"] == 3
    assert review_rows[0]["row_kind"] == ReviewRowKind.exclusion
    assert "excluded: 3/3" in review_text
    assert "table rows look unrelated to the workspace" in review_text
    assert "Structured reference table row A" in review_text
    assert "RR-" in csv_text and "table rows look unrelated" in csv_text
    assert any(finding.code == "LINT-04" and "exclusion" in finding.message for finding in findings)

    review_path.write_text(
        review_text.replace("- decision:", f"- decision: {ReviewDecision.reinstate.value}"),
        encoding="utf-8",
    )
    await apply_review_file(tmp_workspace, tmp_sqlite, review_path, provider=FakeLLM.rule_based())
    reinstated = await _fetch_all(tmp_sqlite, "SELECT disposition FROM acw_chunk_ledger ORDER BY ordinal")
    assert {row["disposition"] for row in reinstated} == {Disposition.pending}

    await tmp_sqlite.execute("UPDATE acw_chunk_ledger SET disposition = 'exclusion_pending', disposition_reason = 'reason'")
    await tmp_sqlite.execute("UPDATE acw_review_rows SET applied_at = NULL, decision = NULL")
    await tmp_sqlite.commit()
    review_path.write_text(
        review_text.replace("- decision:", f"- decision: {ReviewDecision.approve_exclusion.value}"),
        encoding="utf-8",
    )
    await apply_review_file(tmp_workspace, tmp_sqlite, review_path, provider=FakeLLM.rule_based())
    approved = await _fetch_all(
        tmp_sqlite,
        "SELECT disposition, disposition_reason FROM acw_chunk_ledger ORDER BY ordinal",
    )
    assert {row["disposition"] for row in approved} == {Disposition.irrelevant}
    assert {row["disposition_reason"] for row in approved} == {"table rows look unrelated to the workspace"}


def _block(
    *,
    block_id: str,
    source_id: str,
    source_path: str,
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
        source_date="unknown",
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


def _write_csv(csv_path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0])
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _chunk(index: int, content: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        index=index,
        content=content,
        page=None,
        start_char=index * 100,
        token_count=10,
        header_breadcrumb="",
    )


def _fingerprint(chunk_ids: list[str]) -> str:
    from tests.v2.fakes.fake_llm import fingerprint_payload

    return fingerprint_payload({"chunks": [{"id": chunk_id} for chunk_id in chunk_ids]})

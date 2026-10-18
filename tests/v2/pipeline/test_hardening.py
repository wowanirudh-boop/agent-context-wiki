from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from core.llm.provider import StructuredPayload, StructuredResponse, StructuredSchema
from core.pipeline.run import run_processing_run
from tests.v2.acceptance.test_m9_acceptance import _init_and_reindex
from tests.v2.fakes.fake_llm import FakeLLM
from tests.v2.invariants import assert_invariants, snapshot_sources


def test_fr_lock_02_local_server_can_import_shared_processing_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import sys
    from types import SimpleNamespace

    from tests.v2.unit.test_reingest import _load_llmwiki_module

    module = _load_llmwiki_module()
    (tmp_path / ".llmwiki").mkdir()
    (tmp_path / ".llmwiki/index.db").touch()
    launches: list[dict[str, Any]] = []

    def capture_launch(*args: Any, **kwargs: Any) -> SimpleNamespace:
        launches.append(kwargs)
        return SimpleNamespace(wait=lambda: 0)

    with monkeypatch.context() as patch:
        patch.setenv("PYTHONPATH", "")
        patch.setattr(module.subprocess, "Popen", capture_launch)
        patch.setattr(module.shutil, "which", lambda name: "test-npm")
        module.cmd_serve(str(tmp_path), open_browser=False)
    result = subprocess.run(
        [sys.executable, "-c", "from domain.local_processor import process_document; "
         "from core.pipeline.run import run_processing_run; from main import app; "
         "from fastapi.testclient import TestClient\n"
         "with TestClient(app) as client:\n    assert client.get('/health').status_code == 200\n"],
        cwd=launches[0]["cwd"], env=launches[0]["env"], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_fr_lock_01_process_checks_lock_before_indexing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.lock import WorkspaceLock, WorkspaceLockError
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    workspace = await _workspace(tmp_path)
    source = workspace / "docs/new.md"
    source.write_text("A new source that must wait for the lock.", encoding="utf-8")
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    with WorkspaceLock(workspace, "other-operation"), pytest.raises(WorkspaceLockError):
        await asyncio.to_thread(_load_llmwiki_module().cmd_process, str(workspace))
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        assert db.execute("SELECT id FROM documents WHERE relative_path = 'docs/new.md'").fetchone() is None


async def test_fr_ledger_01_process_extracts_spreadsheet_and_tracks_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openpyxl import Workbook

    from tests.v2.unit.test_reingest import _load_llmwiki_module

    workspace = await _workspace(tmp_path)
    workbook = Workbook()
    workbook.active.append(["Refund window", "30 days"])
    workbook.save(workspace / "docs/rules.xlsx")
    workbook.close()
    (workspace / "docs/broken.xlsx").write_bytes(b"not a spreadsheet")
    source_snapshot = snapshot_sources(workspace)
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    module = _load_llmwiki_module()
    await asyncio.to_thread(module.cmd_process, str(workspace))
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        rows = dict(db.execute("SELECT d.relative_path, cl.disposition FROM acw_chunk_ledger cl JOIN documents d ON d.id = cl.source_id"))
        assert rows["docs/rules.xlsx"] in {"placed", "conflicted_pending"}
        assert rows["docs/broken.xlsx"] == "failed"
        chunks_before = db.execute("SELECT id FROM acw_chunk_ledger WHERE source_id = (SELECT id FROM documents WHERE relative_path = 'docs/rules.xlsx')").fetchall()
    assert_invariants(workspace, source_snapshot=source_snapshot)
    await asyncio.to_thread(module.cmd_process, str(workspace))
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        assert db.execute("SELECT id FROM acw_chunk_ledger WHERE source_id = (SELECT id FROM documents WHERE relative_path = 'docs/rules.xlsx')").fetchall() == chunks_before
    assert_invariants(workspace, source_snapshot=source_snapshot)
    await asyncio.to_thread(module.cmd_reindex, str(workspace))
    await asyncio.to_thread(module.cmd_process, str(workspace))
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        assert db.execute("SELECT count(*) FROM document_chunks WHERE document_id = (SELECT id FROM documents WHERE relative_path = 'docs/rules.xlsx')").fetchone()[0] > 0
        assert db.execute("SELECT id FROM acw_chunk_ledger WHERE source_id = (SELECT id FROM documents WHERE relative_path = 'docs/rules.xlsx')").fetchall() == chunks_before
    assert_invariants(workspace, source_snapshot=source_snapshot)


def test_fr_ledger_01_local_page_chunking_keeps_short_pages() -> None:
    from api.services.chunker import chunk_pages

    chunks = chunk_pages([(1, "Refund window: 30 days."), (2, "A detailed policy. " * 100)], min_chunk_tokens=1)
    assert {chunk.page for chunk in chunks} == {1, 2}


async def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs/refunds.md").write_text(
        "# Refunds\n\nRefund requests are accepted within 30 days of purchase. "
        "Support must check the purchase date before approving a request and retain the "
        "purchase receipt as evidence for each refund decision.", encoding="utf-8",
    )
    await _init_and_reindex(workspace)
    return workspace


async def test_fr_read_03_cli_mcp_connects_with_spaces_and_reads_generated_wiki(tmp_path: Path) -> None:
    import sys

    from mcp.client.stdio import stdio_client

    from mcp import ClientSession, StdioServerParameters

    workspace = await _workspace(tmp_path / "folder with spaces")
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    root = Path(__file__).resolve().parents[3]
    server = StdioServerParameters(command=sys.executable, args=[str(root / "llmwiki"), "mcp", str(workspace)])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        available = {tool.name for tool in (await session.list_tools()).tools}
        assert {"wiki_index", "wiki_summary", "wiki_page", "wiki_search"} <= available
        for tool, arguments in [
            ("wiki_index", {}), ("wiki_summary", {"page": "Refunds"}),
            ("wiki_page", {"page": "Refunds"}), ("wiki_search", {"query": "refund", "tier": "summary"}),
        ]:
            result = await session.call_tool(tool, arguments)
            assert not result.isError, result
            if tool == "wiki_page":
                assert "30 days" in str(result.content)


async def test_fr_reg_03_taxonomy_refreshes_agent_reads_and_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.taxonomy import merge_pages, rename_page, split_page

    workspace = await _workspace(tmp_path)
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    snapshot = snapshot_sources(workspace)
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        title = db.execute("SELECT title FROM acw_pages WHERE status = 'active'").fetchone()[0]
    await rename_page(workspace, title, "Purchase refunds")
    assert "Purchase refunds" in (workspace / "wiki/_index.md").read_text(encoding="utf-8")
    assert_invariants(workspace, source_snapshot=snapshot)
    await split_page(workspace, "Purchase refunds", "Rules", new_title="Refund rules")
    assert_invariants(workspace, source_snapshot=snapshot)
    await merge_pages(workspace, "Refund rules", "Purchase refunds")
    assert_invariants(workspace, source_snapshot=snapshot)


async def test_fr_flow_01_large_flow_keeps_all_nodes_in_one_block(tmp_path: Path) -> None:
    from core.blocks.parser import parse_page

    workspace = tmp_path / "workspace"
    (workspace / "flows").mkdir(parents=True)
    graph = {"nodes": [{"id": f"node_{i}", "label": f"Step {i}"} for i in range(110)],
             "edges": [{"from": f"node_{i}", "to": f"node_{i + 1}"} for i in range(109)]}
    (workspace / "flows/graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")
    await _init_and_reindex(workspace)
    snapshot = snapshot_sources(workspace)
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    blocks = [b for path in (workspace / "wiki").glob("*.md") for b in parse_page(path.read_text(encoding="utf-8")).blocks]
    flows = [b for b in blocks if b.type.value == "flow"]
    assert len(flows) == 1
    assert all(f'node_{i}["Step {i}"]' in flows[0].content for i in range(110))
    assert_invariants(workspace, source_snapshot=snapshot)


async def test_fr_trans_01_failed_survivor_has_no_dangling_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = await _workspace(tmp_path)
    (workspace / "transcripts").mkdir()
    (workspace / "transcripts/meeting.txt").write_text(
        "Decision: use 2 retries. " + "Details of the old decision. " * 100 + "\n\n"
        "Decision update: use 5 retries. " + "Details of the final decision. " * 100, encoding="utf-8",
    )
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    await asyncio.to_thread(_load_llmwiki_module().cmd_reindex, str(workspace))
    fake = FakeLLM.rule_based()
    original = fake.complete_structured

    async def fail_survivor(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> StructuredResponse:
        response = await original(call_site, payload, schema)
        if call_site == "C1" and payload["chunks"][0]["source_path"].startswith("transcripts/"):
            return {"chunks": []}
        return response

    monkeypatch.setattr(fake, "complete_structured", fail_survivor)
    await run_processing_run(workspace, provider=fake)
    assert_invariants(workspace)


async def test_fr_lock_03_retry_keeps_concurrent_note_and_no_orphan_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.pipeline.placement as placement
    from core.blocks.serializer import PageWriteConflict

    workspace = await _workspace(tmp_path)
    original = placement.write_page
    attempts = 0

    def concurrent_write(path: Path, page: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            path.write_text("# Refunds\n\nManual concurrent note.\n", encoding="utf-8")
            raise PageWriteConflict("User edited the page")
        return original(path, page, **kwargs)

    monkeypatch.setattr(placement, "write_page", concurrent_write)
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    assert "Manual concurrent note." in (workspace / "wiki/refunds.md").read_text(encoding="utf-8")
    assert attempts == 2
    assert_invariants(workspace)


async def test_fr_place_06_related_page_links_are_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    workspace = await _workspace(tmp_path)
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    (workspace / "docs/shipping.md").write_text("Shipping takes 2 days. " * 10, encoding="utf-8")
    await asyncio.to_thread(_load_llmwiki_module().cmd_reindex, str(workspace))
    fake = FakeLLM.rule_based()
    original = fake.complete_structured

    async def related(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> StructuredResponse:
        response = await original(call_site, payload, schema)
        if call_site == "C1":
            placement = response["chunks"][0]["placements"][0]
            placement.update(page=None, new_page={"title": "Shipping", "path_slug": "shipping", "description": "Shipping policy",
                                                 "domain": "commerce", "no_registry_match_assertion": "No shipping page exists."}, links=["Refunds"])
            placement["block"]["key"] = "shipping.delivery.days"
        return response

    monkeypatch.setattr(fake, "complete_structured", related)
    await run_processing_run(workspace, provider=fake)
    assert "[[Refunds]]" in (workspace / "wiki/shipping.md").read_text(encoding="utf-8")
    assert_invariants(workspace)


async def test_fr_lock_02_watcher_queues_source_changes_until_unlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aiosqlite
    import watchfiles
    from domain.watcher import watch_workspace

    from core.lock import WorkspaceLock

    workspace = await _workspace(tmp_path)
    source = workspace / "docs/queued.md"
    source.write_text("Queued source content.", encoding="utf-8")

    async def changes(*args: Any, **kwargs: Any) -> AsyncIterator[set[tuple[watchfiles.Change, str]]]:
        yield {(watchfiles.Change.added, str(source))}

    monkeypatch.setattr(watchfiles, "awatch", changes)
    async with aiosqlite.connect(workspace / ".llmwiki/index.db") as db:
        with WorkspaceLock(workspace, "process"):
            task = asyncio.create_task(watch_workspace(db, workspace))
            await asyncio.sleep(0.1)
            cursor = await db.execute("SELECT id FROM documents WHERE relative_path = 'docs/queued.md'")
            indexed_while_locked = await cursor.fetchone()
        await asyncio.wait_for(task, timeout=5)
        assert indexed_while_locked is None
        cursor = await db.execute("SELECT id FROM documents WHERE relative_path = 'docs/queued.md'")
        assert await cursor.fetchone() is not None


async def test_fr_read_01_invalid_summary_is_skipped_without_losing_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = await _workspace(tmp_path)
    fake = FakeLLM.rule_based()
    original = fake.complete_structured

    async def invalid_summary(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> StructuredResponse:
        response = await original(call_site, payload, schema)
        return {"summary_markdown": ""} if call_site == "C5" else response

    monkeypatch.setattr(fake, "complete_structured", invalid_summary)
    result = await run_processing_run(workspace, provider=fake)
    assert result.stats["pending"] == 0
    assert (workspace / "wiki/_index.md").exists()
    assert_invariants(workspace)


async def test_nfr_03_invalid_model_batches_reach_failed_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = await _workspace(tmp_path)
    source_snapshot = snapshot_sources(workspace)
    fake = FakeLLM.rule_based()
    original = fake.complete_structured

    async def invalid(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> StructuredResponse:
        response = await original(call_site, payload, schema)
        return {"chunks": []} if call_site == "C1" else response

    monkeypatch.setattr(fake, "complete_structured", invalid)
    for attempt in range(1, 4):
        result = await run_processing_run(workspace, provider=fake)
        assert result.stats["pending"] == 0
        with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
            row = db.execute("SELECT disposition, attempts, disposition_reason FROM acw_chunk_ledger").fetchone()
        assert row[0] == ("failed_final" if attempt == 3 else "failed")
        assert row[1] == attempt and "missing" in row[2]
        assert_invariants(workspace, source_snapshot=source_snapshot)


async def test_fr_reing_03_deleted_source_updates_page_and_emits_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    workspace = await _workspace(tmp_path)
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    assert_invariants(workspace)
    (workspace / "docs/refunds.md").unlink()
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    import asyncio

    await asyncio.to_thread(_load_llmwiki_module().cmd_process, str(workspace))
    assert_invariants(workspace)
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        rows = db.execute("SELECT row_kind, existing_block_id FROM acw_review_rows WHERE applied_at IS NULL").fetchall()
    assert rows and all(row[0] == "needs_review" for row in rows)
    assert list((workspace / "wiki/_reviews").glob("RR-*.md"))


async def test_fr_place_02_multitopic_chunk_keeps_all_placements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = await _workspace(tmp_path)
    fake = FakeLLM.rule_based()
    original = fake.complete_structured

    async def multiple(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> StructuredResponse:
        response = await original(call_site, payload, schema)
        if call_site == "C1":
            placement = response["chunks"][0]["placements"][0]
            extra = json.loads(json.dumps(placement))
            extra["block"]["key"] = "refunds.receipt.evidence"
            response["chunks"][0]["placements"].append(extra)
        return response

    monkeypatch.setattr(fake, "complete_structured", multiple)
    await run_processing_run(workspace, provider=fake)
    from core.blocks.model import CANONICAL_SECTIONS

    assert next(call.payload for call in fake.calls if call.call_site == "C1")["canonical_sections"] == list(CANONICAL_SECTIONS)
    assert_invariants(workspace)


async def test_fr_place_02_mixed_placement_keeps_unresolved_conflict_tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    workspace = await _workspace(tmp_path)
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    (workspace / "docs/new.md").write_text("Refunds expire after 45 days. " * 8, encoding="utf-8")
    await asyncio.to_thread(_load_llmwiki_module().cmd_reindex, str(workspace))
    fake = FakeLLM.rule_based()
    original = fake.complete_structured

    async def multiple(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> StructuredResponse:
        response = await original(call_site, payload, schema)
        if call_site == "C1":
            placement = response["chunks"][0]["placements"][0]
            placement["block"]["key"] = "refunds.window_days"
            extra = json.loads(json.dumps(placement))
            extra["block"]["key"] = "refunds.receipt.evidence"
            response["chunks"][0]["placements"].append(extra)
        elif call_site == "C3":
            conflict = payload["candidate"]["key"] == payload["existing"]["key"]
            response = {"verdict": "conflict" if conflict else "distinct", "conflict_type": "changed_value" if conflict else None,
                        "recommendation": "accept_new", "rationale": "The refund window changed."}
        return response

    monkeypatch.setattr(fake, "complete_structured", multiple)
    await run_processing_run(workspace, provider=fake)
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        row = db.execute("SELECT disposition FROM acw_chunk_ledger cl JOIN documents d ON d.id = cl.source_id WHERE d.relative_path = 'docs/new.md'").fetchone()
    assert row[0] == "conflicted_pending"
    assert_invariants(workspace)
    from core.review.apply import apply_decisions

    for review in (workspace / "wiki/_reviews").glob("RR-*.md"):
        review.write_text(review.read_text(encoding="utf-8").replace("- decision:", "- decision: keep_existing"), encoding="utf-8")
    await apply_decisions(workspace, provider=fake)
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        row = db.execute("SELECT disposition FROM acw_chunk_ledger cl JOIN documents d ON d.id = cl.source_id WHERE d.relative_path = 'docs/new.md'").fetchone()
    assert row[0] == "placed", "The clean placement remains represented after rejecting a different candidate."
    assert_invariants(workspace)

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from core.blocks.parser import parse_page
from tests.v2.fakes.fake_llm import FakeLLM
from tests.v2.invariants import assert_invariants
from tests.v2.unit.test_reingest import _load_llmwiki_module

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = ROOT / "tests" / "v2" / "fixtures" / "workspaces"
MCP_ROOT = ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))


@pytest.mark.asyncio
async def test_as_uc1_1_workflow_page_combines_flow_faq_rules_api_and_requirements(tmp_path: Path) -> None:
    from core.pipeline.run import run_processing_run

    workspace = _copy_fixture(tmp_path, "uc1_minimal")
    await _init_and_reindex(workspace)

    result = await run_processing_run(workspace, provider=FakeLLM.rule_based())

    pages = _active_pages(workspace)
    assert [page["title"] for page in pages] == ["Refunds"]
    page_text = (workspace / pages[0]["path"]).read_text(encoding="utf-8")
    blocks = parse_page(page_text).blocks
    block_types = {block.type.value for block in blocks}
    assert {"flow", "faq", "rule", "api", "requirement"} <= block_types
    assert _flow_is_node_edge_complete(page_text)
    assert result.stats["pending"] == 0
    assert _ledger_disposition_count(workspace, "pending") == 0
    assert_invariants(workspace)


@pytest.mark.asyncio
async def test_as_uc1_2_retry_conflict_review_row_preserves_pending_marker(tmp_path: Path) -> None:
    from core.pipeline.run import run_processing_run

    workspace = _copy_fixture(tmp_path, "uc1_minimal")
    await _init_and_reindex(workspace)

    await run_processing_run(workspace, provider=FakeLLM.rule_based())

    review_path = _single_review_file(workspace)
    review_text = review_path.read_text(encoding="utf-8")
    page_text = _single_active_page_text(workspace)
    assert "changed_value" in review_text
    assert "maxRetries: 2" in review_text
    assert "retry 3 times" in review_text or "3 times before escalation" in review_text
    assert "\u26a0 pending review:" in page_text
    assert "maxRetries: 2" in page_text
    assert "retry 3 times before escalation" not in page_text
    assert _ledger_disposition_count(workspace, "pending") == 0
    assert _ledger_disposition_count(workspace, "conflicted_pending") == 1
    assert_invariants(workspace)


def test_as_uc2_1_node_docs_support_bounded_summary_then_full_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.wiki_read import WikiReadHandler

    workspace = _copy_fixture(tmp_path, "uc2_nodes")
    shutil.rmtree(workspace / "uc2_nodes_v2")
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    module = _load_llmwiki_module()

    module.cmd_process(str(workspace))

    async def _read() -> tuple[int, dict, list[str], str]:
        handler = WikiReadHandler(workspace)
        index = await handler.wiki_index()
        pages = _active_pages(workspace)
        summaries = [await handler.wiki_summary(str(page["path"])) for page in pages]
        bounded_words = _word_count(index["markdown"]) + sum(
            _word_count(summary["summary_markdown"]) for summary in summaries
        )
        api_page = await handler.wiki_page("nodes/api-call")
        searches = [
            (await handler.wiki_search("api call timeout", tier="summary", limit=3))["results"][0]["page"],
            (await handler.wiki_search("condition expression language", tier="summary", limit=3))["results"][0]["page"],
        ]
        return bounded_words, api_page, searches, "\n".join((workspace / page["path"]).read_text(encoding="utf-8") for page in pages)

    bounded_words, api_page, searches, all_page_text = asyncio.run(_read())
    assert bounded_words < 4000
    assert "wiki/api-call-node.md" in api_page["page"]
    assert {"nodes.api_call.default_timeout", "nodes.api_call.retry_count"} <= {
        block["key"] for block in api_page["blocks"]
    }
    assert "default_timeout" in api_page["markdown"]
    assert "30s" in api_page["markdown"]
    for setting, default in _node_source_settings(workspace / "docs" / "nodes"):
        assert setting in all_page_text
        assert default in all_page_text
    assert searches
    assert_invariants(workspace)


def test_as_uc2_2_single_node_update_scopes_page_change_and_preserves_old_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_fixture(tmp_path, "uc2_nodes")
    overlay = workspace / "uc2_nodes_v2" / "docs" / "nodes" / "api_call.md"
    updated_api_call = overlay.read_text(encoding="utf-8")
    shutil.rmtree(workspace / "uc2_nodes_v2")
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    module = _load_llmwiki_module()

    module.cmd_process(str(workspace))
    (workspace / "docs" / "nodes" / "api_call.md").write_text(updated_api_call, encoding="utf-8")
    module.cmd_process(str(workspace))
    review_path = _single_review_file(workspace)
    review_path.write_text(
        review_path.read_text(encoding="utf-8").replace("- decision:", "- decision: accept_new"),
        encoding="utf-8",
    )
    module.cmd_apply_decisions(str(workspace), str(review_path))

    api_page = workspace / "wiki" / "api-call-node.md"
    page = parse_page(api_page.read_text(encoding="utf-8"))
    assert any(block.key == "nodes.api_call.default_timeout" and block.status.value == "current" and "45s" in block.content for block in page.blocks)
    assert any(block.key == "nodes.api_call.default_timeout" and block.status.value == "deprecated" and "30s" in block.content for block in page.blocks)
    assert _changed_domain_pages_in_last_commit(workspace) == ["api-call-node.md"]
    assert_invariants(workspace)


def test_as_uc3_1_rca_deprecates_older_guidance_with_source_date_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.wiki_read import WikiReadHandler

    workspace = _copy_fixture(tmp_path, "uc3_support")
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    module = _load_llmwiki_module()

    module.cmd_process(str(workspace))
    review_path = _single_review_file(workspace)
    review_text = review_path.read_text(encoding="utf-8")
    assert "changed_value" in review_text
    assert "basis: source_date" in review_text
    review_path.write_text(review_text.replace("- decision:", "- decision: deprecate_existing"), encoding="utf-8")
    module.cmd_apply_decisions(str(workspace), str(review_path))

    async def _read() -> dict:
        return await WikiReadHandler(workspace).wiki_page("Checkout Webhooks", statuses=["*"])

    page = asyncio.run(_read())
    assert "## Deprecated" in page["markdown"]
    assert "retry the webhook twice" in page["markdown"]
    assert "Retry checkout webhooks 5 times" in page["markdown"]
    statuses = {block["status"] for block in page["blocks"] if block["key"] == "support.webhook.retry_count"}
    assert {"current", "deprecated"} <= statuses
    assert_invariants(workspace)


@pytest.mark.asyncio
async def test_nfr_03_synthetic_scale_smoke_50_sources_5000_chunks_completes_with_ledger_dispositions(
    tmp_path: Path,
) -> None:
    from core.db.dao import ACWDao
    from core.ingest import index_document_chunks
    from core.pipeline.run import run_processing_run

    workspace = tmp_path / "scale"
    workspace.mkdir()
    await _init_and_reindex(workspace)
    db_path = workspace / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        dao = ACWDao(db)
        for source_index in range(50):
            relative_path = f"scale/source_{source_index:02d}.md"
            source_path = workspace / relative_path
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(
                f"# Scale source {source_index:02d}\n\nSynthetic scale source for NFR-03.\n",
                encoding="utf-8",
            )
            doc_id = f"scale-doc-{source_index:02d}"
            await dao.db.execute(
                "INSERT INTO documents (id, user_id, filename, title, path, relative_path, source_kind, "
                "file_type, status, content, content_hash, mtime_ns, last_indexed_at, document_number) "
                "VALUES (?, (SELECT user_id FROM workspace LIMIT 1), ?, ?, '/scale/', ?, 'source', "
                "'md', 'ready', ?, ?, ?, datetime('now'), "
                "(SELECT COALESCE(MAX(document_number), 0) + 1 FROM documents))",
                (
                    doc_id,
                    source_path.name,
                    f"Scale Source {source_index:02d}",
                    relative_path,
                    source_path.read_text(encoding="utf-8"),
                    f"scale-version-{source_index:02d}",
                    source_path.stat().st_mtime_ns,
                ),
            )
            await db.commit()
            chunks = [
                _chunk(
                    chunk_index,
                    f"Synthetic scale irrelevant source {source_index:02d} chunk {chunk_index:03d}. "
                    "This generated operational filler is intentionally out of scope.",
                )
                for chunk_index in range(100)
            ]
            await index_document_chunks(db, doc_id, workspace, chunks=chunks)

    result = await run_processing_run(workspace, provider=FakeLLM.rule_based())

    assert result.stats["pending"] == 0
    assert _ledger_total(workspace) == 5000
    assert _ledger_disposition_count(workspace, "pending") == 0
    assert _ledger_disposition_count(workspace, "failed") == 0
    assert _ledger_disposition_count(workspace, "failed_final") == 0
    assert _ledger_disposition_count(workspace, "irrelevant") == 5000
    assert_invariants(workspace)


@pytest.mark.asyncio
async def test_nfr_01_second_process_has_zero_page_writes_and_zero_llm_calls(tmp_path: Path) -> None:
    from core.pipeline.run import run_processing_run

    workspace = _copy_fixture(tmp_path, "uc1_minimal")
    await _init_and_reindex(workspace)
    fake = FakeLLM.rule_based()

    first = await run_processing_run(workspace, provider=fake)
    calls_after_first = fake.call_count
    page_texts_after_first = _page_texts(workspace)
    second = await run_processing_run(workspace, provider=fake)

    assert first.stats["page_writes"] > 0
    assert second.stats["pending"] == 0
    assert second.stats["page_writes"] == 0
    assert second.stats["llm_calls"] == 0
    assert fake.call_count == calls_after_first
    assert _page_texts(workspace) == page_texts_after_first
    assert_invariants(workspace)


def _copy_fixture(tmp_path: Path, name: str) -> Path:
    workspace = tmp_path / name
    shutil.copytree(FIXTURE_ROOT / name, workspace)
    return workspace


async def _init_and_reindex(workspace: Path) -> None:
    module = _load_llmwiki_module()
    await asyncio.to_thread(module.cmd_init, str(workspace))
    await asyncio.to_thread(module.cmd_reindex, str(workspace))


def _active_pages(workspace: Path) -> list[dict]:
    with sqlite3.connect(workspace / ".llmwiki" / "index.db") as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute("SELECT title, path, domain FROM acw_pages WHERE status = 'active' ORDER BY path")
        ]


def _single_active_page_text(workspace: Path) -> str:
    pages = _active_pages(workspace)
    assert len(pages) == 1
    return (workspace / pages[0]["path"]).read_text(encoding="utf-8")


def _single_review_file(workspace: Path) -> Path:
    review_files = sorted((workspace / "wiki" / "_reviews").glob("RR-*.md"))
    assert len(review_files) == 1
    return review_files[0]


def _ledger_total(workspace: Path) -> int:
    with sqlite3.connect(workspace / ".llmwiki" / "index.db") as conn:
        return int(conn.execute("SELECT COUNT(*) FROM acw_chunk_ledger").fetchone()[0])


def _ledger_disposition_count(workspace: Path, disposition: str) -> int:
    with sqlite3.connect(workspace / ".llmwiki" / "index.db") as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM acw_chunk_ledger WHERE disposition = ?",
                (disposition,),
            ).fetchone()[0]
        )


def _flow_is_node_edge_complete(page_text: str) -> bool:
    expected_nodes = {
        'start["Customer requests refund"]',
        'check_window["Check purchase age"]',
        'check_condition["Confirm item condition"]',
        'approve["Approve refund"]',
        'reject["Reject refund"]',
        'notify["Notify customer"]',
    }
    expected_edges = {
        "start --> check_window",
        "check_window -->|within 30 days| check_condition",
        "check_window -->|older than 30 days| reject",
        "check_condition -->|item unused| approve",
        "check_condition -->|item damaged| reject",
        "approve --> notify",
        "reject --> notify",
    }
    compact = {line.strip() for line in page_text.splitlines()}
    return expected_nodes <= compact and expected_edges <= compact


def _word_count(markdown: str) -> int:
    return len(markdown.split())


def _node_source_settings(nodes_dir: Path) -> list[tuple[str, str]]:
    settings: list[tuple[str, str]] = []
    for source in sorted(nodes_dir.glob("*.md")):
        for line in source.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("|") or "---" in stripped or "Setting" in stripped:
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) >= 2:
                settings.append((cells[0], cells[1]))
    return settings


def _changed_domain_pages_in_last_commit(workspace: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "show", "--name-only", "--format="],
        cwd=workspace / "wiki",
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(
        Path(line).name
        for line in completed.stdout.splitlines()
        if line.endswith(".md") and not line.startswith("_")
    )


def _page_texts(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((workspace / "wiki").rglob("*.md"))
    }


def _chunk(index: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        content=content,
        page=None,
        start_char=index * 100,
        token_count=max(1, len(content) // 4),
        header_breadcrumb="",
    )

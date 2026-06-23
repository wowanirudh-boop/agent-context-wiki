from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import aiosqlite
import pytest
import yaml

from tests.v2.invariants import assert_invariants
from tests.v2.unit.test_reingest import _load_llmwiki_module

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = ROOT / "tests" / "v2" / "fixtures" / "workspaces"
MCP_ROOT = ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))


@pytest.mark.asyncio
async def test_fr_ledger_04_nfr_01_process_uc1_resumes_and_second_run_zero_calls_zero_writes(tmp_path) -> None:
    from core.db.dao import ACWDao
    from core.ledger import ChunkLedger
    from core.models import Disposition
    from core.pipeline.run import run_processing_run
    from tests.v2.fakes.fake_llm import FakeLLM

    workspace = tmp_path / "uc1_minimal"
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", workspace)
    module = _load_llmwiki_module()
    await asyncio.to_thread(module.cmd_init, str(workspace))
    await asyncio.to_thread(module.cmd_reindex, str(workspace))

    db_path = workspace / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT id FROM acw_chunk_ledger WHERE disposition = 'pending' ORDER BY ordinal LIMIT 1",
        )
        chunk_id = (await cursor.fetchone())[0]
        await ChunkLedger(ACWDao(db)).mark_failed(chunk_id, reason="interrupted before placement")

    fake = FakeLLM.rule_based()
    first = await run_processing_run(workspace, provider=fake)

    assert first.stats["pending"] == 0
    assert first.stats["placed"] > 0
    assert first.stats["page_writes"] > 0
    assert fake.call_count > 0
    assert_invariants(workspace)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM acw_chunk_ledger WHERE disposition = 'pending'")
        assert (await cursor.fetchone())[0] == 0
        cursor = await db.execute("SELECT DISTINCT disposition FROM acw_chunk_ledger")
        dispositions = {row[0] for row in await cursor.fetchall()}
        assert Disposition.pending not in dispositions

    page_texts = _page_texts(workspace)
    calls_after_first = fake.call_count
    second = await run_processing_run(workspace, provider=fake)

    assert second.stats["pending"] == 0
    assert second.stats["llm_calls"] == 0
    assert second.stats["page_writes"] == 0
    assert fake.call_count == calls_after_first
    assert _page_texts(workspace) == page_texts
    assert_invariants(workspace)


def test_fr_place_08_llmwiki_process_cli_uses_fake_rules_provider(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "uc1_minimal"
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", workspace)
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")

    module = _load_llmwiki_module()
    module.cmd_process(str(workspace))

    assert (workspace / "wiki" / "_meta" / "ledger.json").is_file()
    assert any("<!-- cb " in path.read_text(encoding="utf-8") for path in (workspace / "wiki").glob("*.md"))


def test_fr_rev_04_m6_dod_uc1_process_decide_apply_loop(tmp_path, monkeypatch) -> None:
    from core.blocks.parser import parse_page

    workspace = tmp_path / "uc1_minimal"
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", workspace)
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    module = _load_llmwiki_module()

    module.cmd_init(str(workspace))
    module.cmd_reindex(str(workspace))
    module.cmd_process(str(workspace))
    review_path = next((workspace / "wiki" / "_reviews").glob("RR-*.md"))
    review_path.write_text(
        review_path.read_text(encoding="utf-8").replace("- decision:", "- decision: accept_new"),
        encoding="utf-8",
    )

    module.cmd_apply_decisions(str(workspace))

    page_path = next((workspace / "wiki").glob("refunds*.md"))
    blocks = parse_page(page_path.read_text(encoding="utf-8")).blocks
    subjects = _git_log_subjects(workspace / "wiki")
    assert any(block.status == "deprecated" for block in blocks)
    assert any(block.status == "current" and block.source_path == "docs/tnc.md" for block in blocks)
    assert all(not block.pending_review_ids for block in blocks)
    assert subjects[:2] == [
        f"acw apply-decisions: {review_path.name}",
        next(subject for subject in subjects if subject.startswith("acw process: ")),
    ]
    assert_invariants(workspace)


@pytest.mark.asyncio
async def test_fr_conf_02_fr_conf_05_uc1_retry_count_conflict_emits_review_without_overwrite(tmp_path) -> None:
    from core.blocks.parser import parse_page
    from core.models import Disposition
    from core.pipeline.run import run_processing_run
    from tests.v2.fakes.fake_llm import FakeLLM

    workspace = tmp_path / "uc1_minimal"
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", workspace)
    module = _load_llmwiki_module()
    await asyncio.to_thread(module.cmd_init, str(workspace))
    await asyncio.to_thread(module.cmd_reindex, str(workspace))

    result = await run_processing_run(workspace, provider=FakeLLM.rule_based())

    review_files = sorted((workspace / "wiki" / "_reviews").glob("RR-*.md"))
    assert len(review_files) == 1
    review_text = review_files[0].read_text(encoding="utf-8")
    assert "changed_value" in review_text
    assert "maxRetries: 2" in review_text
    assert "retry 3 times" in review_text or "3 times before escalation" in review_text

    refund_pages = list((workspace / "wiki").glob("refunds*.md"))
    assert len(refund_pages) == 1
    page_text = refund_pages[0].read_text(encoding="utf-8")
    assert "\u26a0 pending review:" in page_text
    assert "## Open Conflicts" in page_text
    blocks = parse_page(page_text).blocks
    assert any(block.source_path == "docs/api.md" and block.key == "refunds.retry_count" for block in blocks)
    assert not any(block.source_path == "docs/tnc.md" and block.key == "refunds.retry_count" for block in blocks)

    db_path = workspace / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT disposition, disposition_reason FROM acw_chunk_ledger")
        rows = await cursor.fetchall()
        assert (Disposition.conflicted_pending.value, "changed_value") in rows
        cursor = await db.execute("SELECT COUNT(*) FROM acw_review_rows WHERE row_kind = 'conflict'")
        assert (await cursor.fetchone())[0] == 1

    assert result.stats["conflicted_pending"] == 1


@pytest.mark.asyncio
async def test_agent_provider_uc1_minimal_completes_when_mcp_agent_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.agent_bridge import AgentBridgeHandler

    from core.pipeline.run import run_processing_run
    from tests.v2.fakes.fake_llm import FakeLLM

    fake_workspace = tmp_path / "uc1_fake"
    agent_workspace = tmp_path / "uc1_agent"
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", fake_workspace)
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", agent_workspace)
    module = _load_llmwiki_module()
    await asyncio.to_thread(module.cmd_init, str(fake_workspace))
    await asyncio.to_thread(module.cmd_reindex, str(fake_workspace))
    await asyncio.to_thread(module.cmd_init, str(agent_workspace))
    await asyncio.to_thread(module.cmd_reindex, str(agent_workspace))

    fake_result = await run_processing_run(fake_workspace, provider=FakeLLM.rule_based())

    monkeypatch.setenv("ACW_LLM_PROVIDER", "agent")
    monkeypatch.setenv("ACW_AGENT_TIMEOUT_SECONDS", "10")
    run_task = asyncio.create_task(run_processing_run(agent_workspace))
    bridge = AgentBridgeHandler(agent_workspace)
    responder = FakeLLM.rule_based()
    try:
        await asyncio.wait_for(_answer_agent_requests_until_done(run_task, bridge, responder), timeout=20)
        agent_result = await run_task
    finally:
        if not run_task.done():
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task

    assert agent_result.stats["pending"] == 0
    assert agent_result.stats["placed"] == fake_result.stats["placed"]
    assert agent_result.stats["conflicted_pending"] == fake_result.stats["conflicted_pending"]
    assert _ledger_disposition_counts(agent_workspace) == _ledger_disposition_counts(fake_workspace)
    assert _block_outcomes(agent_workspace) == _block_outcomes(fake_workspace)
    assert _active_page_paths(agent_workspace) == _active_page_paths(fake_workspace)
    request_statuses = _request_statuses(agent_workspace)
    assert request_statuses
    assert set(request_statuses.values()) == {"done"}
    assert_invariants(agent_workspace)


@pytest.mark.asyncio
async def test_as_uc2_1_fr_read_03_bounded_read_uses_index_summaries_then_full_pages(tmp_path) -> None:
    from tools.wiki_read import WikiReadHandler

    from core.pipeline.run import run_processing_run
    from tests.v2.fakes.fake_llm import FakeLLM

    workspace = tmp_path / "uc2_nodes"
    shutil.copytree(FIXTURE_ROOT / "uc2_nodes", workspace)
    module = _load_llmwiki_module()
    await asyncio.to_thread(module.cmd_init, str(workspace))
    await asyncio.to_thread(module.cmd_reindex, str(workspace))

    await run_processing_run(workspace, provider=FakeLLM.rule_based())

    handler = WikiReadHandler(workspace)
    index = await handler.wiki_index()
    pages = _active_page_paths(workspace)
    summaries = [await handler.wiki_summary(path) for path in pages]
    bounded_words = _word_count(index["markdown"]) + sum(
        _word_count(summary["summary_markdown"]) for summary in summaries
    )
    assert bounded_words < 4000

    full_pages = [await handler.wiki_page(path) for path in pages]
    keys = {
        block["key"]
        for page in full_pages
        for block in page["blocks"]
        if block["status"] == "current"
    }
    markdown = "\n".join(page["markdown"] for page in full_pages)
    questions = yaml.safe_load((workspace / "eval" / "questions.yaml").read_text(encoding="utf-8"))
    for question in questions:
        for key in question["expected_keys"]:
            assert key in keys
        for substring in question["expected_substrings"]:
            assert substring in markdown
        search = await handler.wiki_search(question["question"], tier="summary", limit=3)
        assert search["results"], question["question"]

    assert_invariants(workspace)


def _page_texts(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((workspace / "wiki").rglob("*.md"))
    }


def _git_log_subjects(wiki: Path) -> list[str]:
    import subprocess

    completed = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=wiki,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.splitlines()


def _active_page_paths(workspace: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(workspace / ".llmwiki" / "index.db") as conn:
        return [
            row[0]
            for row in conn.execute("SELECT path FROM acw_pages WHERE status = 'active' ORDER BY path").fetchall()
        ]


def _word_count(markdown: str) -> int:
    return len(markdown.split())


async def _answer_agent_requests_until_done(run_task: asyncio.Task, bridge, responder) -> None:
    while not run_task.done():
        request = await bridge.acw_next_request()
        if not request["pending"]:
            await asyncio.sleep(0.01)
            continue
        response = await responder.complete_structured(
            str(request["call_site"]),
            request["payload"],
            request["schema"],
        )
        result = await bridge.acw_answer_request(str(request["request_id"]), response)
        assert result == {"success": True, "request_id": request["request_id"]}


def _ledger_disposition_counts(workspace: Path) -> dict[str, int]:
    import sqlite3

    with sqlite3.connect(workspace / ".llmwiki" / "index.db") as conn:
        return dict(
            conn.execute(
                "SELECT disposition, COUNT(*) FROM acw_chunk_ledger GROUP BY disposition ORDER BY disposition",
            ).fetchall()
        )


def _block_outcomes(workspace: Path) -> list[tuple[str, str, str, str, int]]:
    import sqlite3

    with sqlite3.connect(workspace / ".llmwiki" / "index.db") as conn:
        return conn.execute(
            "SELECT key, type, status, source_path, COUNT(*) FROM acw_blocks "
            "GROUP BY key, type, status, source_path ORDER BY key, type, status, source_path",
        ).fetchall()


def _request_statuses(workspace: Path) -> dict[str, str]:
    import json

    request_dir = workspace / ".llmwiki" / "agent_queue" / "requests"
    return {
        path.stem: str(json.loads(path.read_text(encoding="utf-8")).get("status"))
        for path in sorted(request_dir.glob("*.json"))
    }

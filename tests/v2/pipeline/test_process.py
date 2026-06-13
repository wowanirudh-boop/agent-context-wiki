from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import aiosqlite
import pytest

from tests.v2.invariants import assert_invariants
from tests.v2.unit.test_reingest import _load_llmwiki_module

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = ROOT / "tests" / "v2" / "fixtures" / "workspaces"


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


def _page_texts(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((workspace / "wiki").rglob("*.md"))
    }

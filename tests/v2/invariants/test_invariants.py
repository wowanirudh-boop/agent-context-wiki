from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from core.blocks.model import BlockSegment
from core.blocks.parser import parse_page
from core.blocks.serializer import serialize_page
from core.pipeline.run import run_processing_run
from tests.v2.acceptance.test_m9_acceptance import _init_and_reindex
from tests.v2.fakes.fake_llm import FakeLLM
from tests.v2.invariants import assert_invariants, snapshot_sources


@pytest.fixture
async def checked_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    fixture = Path(__file__).parents[1] / "fixtures/workspaces/uc1_minimal"
    shutil.copytree(fixture, workspace)
    await _init_and_reindex(workspace)
    await run_processing_run(workspace, provider=FakeLLM.rule_based())
    for path in (workspace / "wiki").rglob("*.md"):
        path.write_bytes(path.read_text(encoding="utf-8").encode("utf-8"))
    return workspace


@pytest.mark.parametrize("defect", ["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9"])
async def test_invariant_suite_detects_seeded_corruption(checked_workspace: Path, defect: str) -> None:
    workspace = checked_workspace
    assert_invariants(workspace)
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        page_path = workspace / db.execute("SELECT path FROM acw_pages WHERE status = 'active'").fetchone()[0]
        text = page_path.read_text(encoding="utf-8")
        page = parse_page(text)
        block = page.blocks[0]
        if defect == "I1":
            db.execute("UPDATE acw_chunk_ledger SET disposition_reason = NULL WHERE disposition = 'irrelevant'")
        elif defect == "I2":
            db.execute("UPDATE acw_chunk_ledger SET disposition = 'pending' WHERE id = ?", (block.chunk_ids[0],))
        elif defect == "I3":
            db.execute("DELETE FROM acw_block_chunks WHERE block_id = ?", (block.id,))
        elif defect == "I4":
            page.segments = [s for s in page.segments if not isinstance(s, BlockSegment) or s.block.id != block.id]
            text = serialize_page(page)
        elif defect == "I5":
            text = text.replace(f"<!-- /cb {block.id} -->", "<!-- broken close -->")
        elif defect == "I6":
            block.excerpt = ""
            text = serialize_page(page)
        elif defect == "I7":
            text = text.replace("chunks placed", "chunks silently lost")
        elif defect == "I8":
            for item in page.blocks:
                item.pending_review_ids = []
            text = serialize_page(page)
        elif defect == "I9":
            other = page.blocks[1]
            other.key = block.key
            db.execute("UPDATE acw_blocks SET key = ? WHERE id = ?", (block.key, other.id))
            text = serialize_page(page)
        page_path.write_bytes(text.encode("utf-8"))
    with pytest.raises(AssertionError, match=defect):
        assert_invariants(workspace)


async def test_i10_detects_source_changes(checked_workspace: Path) -> None:
    snapshot = snapshot_sources(checked_workspace)
    (checked_workspace / "docs/faq.md").write_text("lost source", encoding="utf-8")
    with pytest.raises(AssertionError, match="I10"):
        assert_invariants(checked_workspace, source_snapshot=snapshot)

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import aiosqlite
import pytest

from core.blocks.model import BlockSegment, ContextBlock, Page, ProseSegment
from core.blocks.serializer import serialize_page
from core.models import BlockStatus, BlockType

ROOT = Path(__file__).resolve().parents[3]
BASE_SCHEMA = ROOT / "shared" / "sqlite_schema.sql"
FIXTURE_ROOT = ROOT / "tests" / "v2" / "fixtures" / "workspaces"
MCP_ROOT = ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("defect", "code"),
    [
        ("pending_chunk", "LINT-01"),
        ("missing_fidelity_token", "LINT-02"),
        ("duplicate_active_key", "LINT-03"),
        ("unresolved_review", "LINT-04"),
        ("missing_conflict_listing", "LINT-05"),
        ("broken_link", "LINT-06"),
        ("broken_round_trip", "LINT-07"),
        ("flow_count_mismatch", "LINT-08"),
    ],
)
async def test_fr_lint_01_08_seeded_defect_fixtures_emit_expected_findings(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
    defect: str,
    code: str,
) -> None:
    from core.lint.runner import lint_workspace

    await _seed_clean_lint_workspace(tmp_workspace, tmp_sqlite)
    await _seed_lint_defect(tmp_workspace, tmp_sqlite, defect)

    findings = await lint_workspace(tmp_workspace)

    assert code in {finding.code for finding in findings}
    assert any(finding.severity == "error" for finding in findings if finding.code == code)


@pytest.mark.asyncio
async def test_fr_lint_01_08_clean_workspace_passes_and_json_cli_exits_by_error_state(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    await _seed_clean_lint_workspace(tmp_workspace, tmp_sqlite)
    module = _load_llmwiki_module()

    assert await asyncio.to_thread(module.cmd_lint, str(tmp_workspace), json_output=True) == 0
    clean = capsys.readouterr().out
    assert clean == ""

    await _seed_lint_defect(tmp_workspace, tmp_sqlite, "pending_chunk")
    assert await asyncio.to_thread(module.cmd_lint, str(tmp_workspace), json_output=True) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["code"] for line in lines] == ["LINT-01"]
    assert lines[0]["severity"] == "error"


def test_fr_lock_01_fr_lock_02_fr_lock_03_lock_matrix_and_read_tools_unblocked(tmp_workspace: Path) -> None:
    from tools.wiki_read import WikiReadHandler

    from core.lock import WorkspaceLock, WorkspaceLockError

    (tmp_workspace / "wiki" / "_index.md").write_text("# Wiki Index\n", encoding="utf-8")

    with WorkspaceLock(tmp_workspace, "process"):
        with pytest.raises(WorkspaceLockError, match="locked"), WorkspaceLock(tmp_workspace, "apply-decisions"):
            pass
        assert _run_async(WikiReadHandler(tmp_workspace).wiki_index()) == {"markdown": "# Wiki Index\n"}

    lock_path = tmp_workspace / ".llmwiki" / "lock"
    lock_path.write_text(
        json.dumps({"pid": 99999999, "op": "process", "started_at": "2026-06-13T00:00:00Z"}),
        encoding="utf-8",
    )
    with WorkspaceLock(tmp_workspace, "taxonomy") as acquired:
        assert acquired.stolen is True
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()


def test_acw_auto_process_config_defaults_off_and_env_enables_hook(tmp_workspace: Path) -> None:
    from core.config import load_config

    assert load_config(tmp_workspace, environ={}).auto_process is False
    assert load_config(tmp_workspace, environ={"ACW_AUTO_PROCESS": "1"}).auto_process is True


@pytest.mark.asyncio
async def test_fr_reg_03_merge_pages_moves_blocks_redirects_and_rewrites_inbound_links(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.taxonomy import merge_pages

    ids = await _seed_taxonomy_workspace(tmp_workspace, tmp_sqlite)

    result = await merge_pages(tmp_workspace, "Old Page", "New Page")

    assert result.changed_pages
    assert "Merged into [[New Page]]." in (tmp_workspace / "wiki" / "old-page.md").read_text(encoding="utf-8")
    assert "Old page fact." in (tmp_workspace / "wiki" / "new-page.md").read_text(encoding="utf-8")
    assert "[[Old Page]]" not in (tmp_workspace / "wiki" / "inbound.md").read_text(encoding="utf-8")
    assert "[[New Page]]" in (tmp_workspace / "wiki" / "inbound.md").read_text(encoding="utf-8")

    source_page = await _fetch_one(tmp_sqlite, "SELECT status FROM acw_pages WHERE id = ?", (ids["old_page"],))
    moved_block = await _fetch_one(tmp_sqlite, "SELECT page_id FROM acw_blocks WHERE id = 'cb_old'")
    assert source_page["status"] == f"merged_into:{ids['new_page']}"
    assert moved_block["page_id"] == ids["new_page"]


@pytest.mark.asyncio
async def test_fr_reg_03_rename_page_rewrites_links_and_leaves_redirect_stub(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.taxonomy import rename_page

    await _seed_taxonomy_workspace(tmp_workspace, tmp_sqlite)

    await rename_page(tmp_workspace, "New Page", "Renamed Page")

    assert (tmp_workspace / "wiki" / "renamed-page.md").is_file()
    assert "Renamed to [[Renamed Page]]." in (tmp_workspace / "wiki" / "new-page.md").read_text(encoding="utf-8")
    assert "[[Renamed Page]]" in (tmp_workspace / "wiki" / "inbound.md").read_text(encoding="utf-8")
    page = await _fetch_one(tmp_sqlite, "SELECT title, path FROM acw_pages WHERE id = 'pg_new'")
    assert page == {"title": "Renamed Page", "path": "wiki/renamed-page.md"}


@pytest.mark.asyncio
async def test_fr_reg_03_split_page_moves_section_blocks_to_new_page(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.taxonomy import split_page

    await _seed_taxonomy_workspace(tmp_workspace, tmp_sqlite, include_faq=True)

    await split_page(tmp_workspace, "New Page", "FAQs", new_title="FAQ Page")

    assert "FAQ answer." in (tmp_workspace / "wiki" / "faq-page.md").read_text(encoding="utf-8")
    assert "FAQ answer." not in (tmp_workspace / "wiki" / "new-page.md").read_text(encoding="utf-8")
    moved = await _fetch_one(tmp_sqlite, "SELECT page_id FROM acw_blocks WHERE id = 'cb_faq'")
    assert moved["page_id"] != "pg_new"


@pytest.mark.asyncio
async def test_section_12_6_eval_scores_expected_keys_and_substrings_and_flags_regression(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.eval.harness import evaluate_workspace

    await _seed_eval_workspace(tmp_workspace, tmp_sqlite)

    first = await evaluate_workspace(tmp_workspace, update_baseline=True)
    assert first.score == 1.0
    assert first.regressed is False
    assert json.loads((tmp_workspace / "eval" / "baseline.json").read_text(encoding="utf-8"))["score"] == 1.0

    questions = tmp_workspace / "eval" / "questions.yaml"
    questions.write_text(
        "- question: What is the refund window?\n"
        "  expected_keys: [refunds.window_days]\n"
        "  expected_substrings: [\"not present\"]\n",
        encoding="utf-8",
    )
    second = await evaluate_workspace(tmp_workspace)
    assert second.score == 0.0
    assert second.regressed is True


def test_section_12_6_llmwiki_eval_cli_returns_regression_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    workspace = tmp_path / "uc1_minimal"
    shutil.copytree(FIXTURE_ROOT / "uc1_minimal", workspace)
    monkeypatch.setenv("ACW_LLM_PROVIDER", "fake-rules")
    module = _load_llmwiki_module()

    module.cmd_init(str(workspace))
    module.cmd_reindex(str(workspace))
    module.cmd_process(str(workspace))

    assert module.cmd_eval(str(workspace), update_baseline=True, json_output=True) == 0
    assert module.cmd_eval(str(workspace), json_output=True) == 0


async def _seed_clean_lint_workspace(workspace: Path, db: aiosqlite.Connection) -> None:
    from core.coverage import render_coverage_sections
    from core.db.dao import ACWDao
    from core.ledger import ChunkLedger

    await _apply_schema(db)
    dao = ACWDao(db)
    ledger = ChunkLedger(dao)
    await _insert_source(db)
    version = await ledger.ensure_source_version(
        source_id="doc_source",
        version_hash="hash-v1",
        source_date="2026-06-01",
        source_date_origin="content",
        source_version_id="sv_source",
        seen_at="2026-06-13T00:00:00Z",
    )
    page = await dao.create_page(
        page_id="pg_refunds",
        path="wiki/refunds.md",
        title="Refunds",
        description="Refund rules",
        status="active",
        domain="commerce",
        created_at="2026-06-13T00:00:00Z",
        aliases=[],
    )
    block = _block()
    await dao.create_block(
        block_id=block.id,
        page_id=str(page["id"]),
        key=block.key,
        block_type=block.type.value,
        status=block.status.value,
        source_id="doc_source",
        source_path=block.source_path,
        source_date=block.source_date,
        content_hash="hash-block",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )
    chunk = await ledger.create_pending_chunk(
        source_id="doc_source",
        source_version_id=str(version["id"]),
        content_hash="hash-chunk",
        ordinal=0,
        document_chunk_id="dc_source",
        chunk_id="ch_source",
        updated_at="2026-06-13T00:00:00Z",
    )
    await ledger.mark_placed(str(chunk["id"]), block_ids=[block.id])
    (workspace / "wiki" / "refunds.md").write_text(_page_text([("Rules", block)]), encoding="utf-8")
    await render_coverage_sections(workspace, db, run_id="run_lint")


async def _seed_lint_defect(workspace: Path, db: aiosqlite.Connection, defect: str) -> None:
    from core.db.dao import ACWDao

    page_path = workspace / "wiki" / "refunds.md"
    text = page_path.read_text(encoding="utf-8")
    if defect == "pending_chunk":
        await db.execute("UPDATE acw_chunk_ledger SET disposition = 'pending' WHERE id = 'ch_source'")
        await db.commit()
    elif defect == "missing_fidelity_token":
        page_path.write_text(text.replace("Refunds are accepted within 30 days.\n<!-- /cb", "Refunds are accepted.\n<!-- /cb"), encoding="utf-8")
    elif defect == "duplicate_active_key":
        duplicate = _block(block_id="cb_duplicate", chunks=["ch_duplicate"], content="Duplicate active key.", excerpt="Duplicate active key.")
        await ACWDao(db).create_block(
            block_id=duplicate.id,
            page_id="pg_refunds",
            key=duplicate.key,
            block_type=duplicate.type.value,
            status=duplicate.status.value,
            source_id="doc_source",
            source_path=duplicate.source_path,
            source_date=duplicate.source_date,
            content_hash="hash-duplicate",
            created_at="2026-06-13T00:00:01Z",
            updated_at="2026-06-13T00:00:01Z",
        )
        model = Page([*__import__("core.blocks.parser", fromlist=["parse_page"]).parse_page(text).segments, BlockSegment(duplicate)])
        page_path.write_text(serialize_page(model), encoding="utf-8")
    elif defect == "unresolved_review":
        review = workspace / "wiki" / "_reviews" / "RR-run_lint.md"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("# Review RR-run_lint\nRun: run_lint · Started: now · Rows: 1 · Status: open\n", encoding="utf-8")
    elif defect == "missing_conflict_listing":
        conflicted = _block(status=BlockStatus.conflicted, pending=["RR-run_lint-1"])
        await db.execute("UPDATE acw_blocks SET status = 'conflicted' WHERE id = 'cb_refunds'")
        await db.commit()
        page_path.write_text(_page_text([("Rules", conflicted)]), encoding="utf-8")
    elif defect == "broken_link":
        page_path.write_text(text + "\nSee [[Missing Page]].\n", encoding="utf-8")
    elif defect == "broken_round_trip":
        page_path.write_text(text.replace("<!-- /cb cb_refunds -->", "<!-- /cb cb_missing -->"), encoding="utf-8")
    elif defect == "flow_count_mismatch":
        flow = _block(
            block_id="cb_flow",
            key="refunds.flow",
            block_type=BlockType.flow,
            content="```mermaid\nflowchart TD\n  A --> B\n```",
            excerpt="nodes:\n  - id: A\n  - id: B\n  - id: C\nedges:\n  - from: A\n    to: B\n  - from: B\n    to: C",
            chunks=["ch_flow"],
        )
        await ACWDao(db).create_block(
            block_id=flow.id,
            page_id="pg_refunds",
            key=flow.key,
            block_type=flow.type.value,
            status=flow.status.value,
            source_id="doc_source",
            source_path=flow.source_path,
            source_date=flow.source_date,
            content_hash="hash-flow",
            created_at="2026-06-13T00:00:02Z",
            updated_at="2026-06-13T00:00:02Z",
        )
        page_path.write_text(_page_text([("Rules", _block()), ("Flow", flow)]), encoding="utf-8")
    else:
        raise AssertionError(defect)


async def _seed_taxonomy_workspace(
    workspace: Path,
    db: aiosqlite.Connection,
    *,
    include_faq: bool = False,
) -> dict[str, str]:
    from core.db.dao import ACWDao
    from core.gitops import ensure_wiki_repo

    await _apply_schema(db)
    dao = ACWDao(db)
    await _insert_source(db)
    old = await dao.create_page(
        page_id="pg_old",
        path="wiki/old-page.md",
        title="Old Page",
        description="Old",
        status="active",
        domain="test",
        created_at="2026-06-13T00:00:00Z",
        aliases=[],
    )
    new = await dao.create_page(
        page_id="pg_new",
        path="wiki/new-page.md",
        title="New Page",
        description="New",
        status="active",
        domain="test",
        created_at="2026-06-13T00:00:00Z",
        aliases=[],
    )
    old_block = _block(block_id="cb_old", key="old.page.fact", content="Old page fact.", excerpt="Old page fact.")
    new_block = _block(block_id="cb_new", key="new.page.fact", content="New page fact.", excerpt="New page fact.")
    await _store_block(dao, old_block, str(old["id"]))
    await _store_block(dao, new_block, str(new["id"]))
    page_blocks = [("Rules", new_block)]
    if include_faq:
        faq = _block(
            block_id="cb_faq",
            key="new.page.faq",
            block_type=BlockType.faq,
            content="FAQ answer.",
            excerpt="FAQ answer.",
        )
        await _store_block(dao, faq, str(new["id"]))
        page_blocks.append(("FAQs", faq))
    (workspace / "wiki" / "old-page.md").write_text(_page_text([("Rules", old_block)], title="Old Page"), encoding="utf-8")
    (workspace / "wiki" / "new-page.md").write_text(_page_text(page_blocks, title="New Page"), encoding="utf-8")
    (workspace / "wiki" / "inbound.md").write_text("# Inbound\n\nSee [[Old Page]] and [[New Page]].\n", encoding="utf-8")
    ensure_wiki_repo(workspace)
    return {"old_page": str(old["id"]), "new_page": str(new["id"])}


async def _seed_eval_workspace(workspace: Path, db: aiosqlite.Connection) -> None:
    from core.db.dao import ACWDao
    from core.summary import render_summaries_and_index
    from tests.v2.fakes.fake_llm import FakeLLM

    await _apply_schema(db)
    dao = ACWDao(db)
    await _insert_source(db)
    page = await dao.create_page(
        page_id="pg_refunds",
        path="wiki/refunds.md",
        title="Refunds",
        description="Refund policy",
        status="active",
        domain="commerce",
        created_at="2026-06-13T00:00:00Z",
        aliases=[],
    )
    block = _block()
    await _store_block(dao, block, str(page["id"]))
    (workspace / "wiki" / "refunds.md").write_text(_page_text([("Rules", block)]), encoding="utf-8")
    await render_summaries_and_index(workspace, db, run_id="run_eval", provider=FakeLLM.rule_based())
    (workspace / "eval").mkdir(exist_ok=True)
    (workspace / "eval" / "questions.yaml").write_text(
        "- question: What is the refund window?\n"
        "  expected_keys: [refunds.window_days]\n"
        "  expected_substrings: [\"30 days\"]\n",
        encoding="utf-8",
    )


async def _apply_schema(db: aiosqlite.Connection) -> None:
    from core.db.migrate import apply_migrations

    await db.executescript(BASE_SCHEMA.read_text(encoding="utf-8"))
    await db.execute("INSERT INTO workspace (id, name, user_id) VALUES ('ws_1', 'Test Workspace', 'user_1')")
    await db.commit()
    await apply_migrations(db)


async def _insert_source(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO documents (id, user_id, filename, title, path, relative_path, source_kind, "
        "file_type, status, content, document_number) "
        "VALUES ('doc_source', 'user_1', 'source.md', 'Source', '/docs/', 'docs/source.md', "
        "'source', 'md', 'ready', 'Refunds are accepted within 30 days.', 1)",
    )
    await db.execute(
        "INSERT OR IGNORE INTO document_chunks (id, document_id, chunk_index, content, source_content, token_count) "
        "VALUES ('dc_source', 'doc_source', 0, 'Refunds are accepted within 30 days.', "
        "'Refunds are accepted within 30 days.', 10)",
    )
    await db.commit()


async def _store_block(dao, block: ContextBlock, page_id: str) -> None:
    await dao.create_block(
        block_id=block.id,
        page_id=page_id,
        key=block.key,
        block_type=block.type.value,
        status=block.status.value,
        source_id="doc_source",
        source_path=block.source_path,
        source_date=block.source_date,
        content_hash=f"hash-{block.id}",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )


async def _fetch_one(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> dict:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    assert row is not None
    return dict(zip([description[0] for description in cursor.description], row, strict=True))


def _block(
    *,
    block_id: str = "cb_refunds",
    key: str = "refunds.window_days",
    block_type: BlockType = BlockType.rule,
    status: BlockStatus = BlockStatus.current,
    content: str = "Refunds are accepted within 30 days.",
    excerpt: str = "Refunds are accepted within 30 days.",
    chunks: list[str] | None = None,
    pending: list[str] | None = None,
) -> ContextBlock:
    return ContextBlock(
        id=block_id,
        key=key,
        type=block_type,
        status=status,
        source_id="doc_source",
        source_path="docs/source.md",
        source_date="2026-06-01",
        chunk_ids=chunks or ["ch_source"],
        user_edited=False,
        content=content,
        excerpt=excerpt,
        pending_review_ids=pending or [],
    )


def _page_text(blocks: list[tuple[str, ContextBlock]], *, title: str = "Refunds") -> str:
    segments: list[ProseSegment | BlockSegment] = [ProseSegment(f"# {title}\n\n")]
    for section, block in blocks:
        segments.extend([ProseSegment(f"## {section}\n\n"), BlockSegment(block), ProseSegment("\n\n")])
    return serialize_page(Page(segments))


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)

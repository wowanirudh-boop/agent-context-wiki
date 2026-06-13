from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiosqlite
import pytest

from core.blocks.model import ContextBlock
from core.blocks.serializer import serialize_block
from core.models import BlockStatus, BlockType
from tests.v2.fakes.fake_llm import FakeLLM

ROOT = Path(__file__).resolve().parents[3]
BASE_SCHEMA = ROOT / "shared" / "sqlite_schema.sql"
MCP_ROOT = ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))


@pytest.mark.asyncio
async def test_fr_cov_01_fr_cov_02_coverage_is_ledger_derived_and_overwrites_manual_section(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from core.coverage import render_coverage_sections

    page_path = await _seed_page_with_source(tmp_workspace, tmp_sqlite)
    page_path.write_text(
        page_path.read_text(encoding="utf-8").replace(
            "## Source Coverage\n\nmanual coverage that must disappear\n",
            "## Source Coverage\n\nmanual edit that must be overwritten\n",
        ),
        encoding="utf-8",
    )

    result = await render_coverage_sections(tmp_workspace, tmp_sqlite, run_id="run_cov")

    page_text = page_path.read_text(encoding="utf-8")
    assert result.page_writes == 1
    assert "manual edit that must be overwritten" not in page_text
    assert "## Source Coverage\n<!-- acw:generated Source Coverage run=run_cov" in page_text
    assert "docs/source.md" in page_text
    assert "used partially (1 of 5 chunks placed)" in page_text
    assert "irrelevant: 1" in page_text
    assert "failed: 1" in page_text
    assert "pending/in-review: 2" in page_text

    breakdown = tmp_workspace / "wiki" / "_meta" / "coverage" / "doc_source.md"
    breakdown_text = breakdown.read_text(encoding="utf-8")
    assert "ch_placed" in breakdown_text
    assert "ch_irrelevant" in breakdown_text
    assert "not product context" in breakdown_text
    assert "ch_conflicted" in breakdown_text

    second = await render_coverage_sections(tmp_workspace, tmp_sqlite, run_id="run_cov")
    assert second.page_writes == 0


@pytest.mark.asyncio
async def test_fr_cov_01_llmwiki_coverage_cli_prints_ledger_report(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.v2.unit.test_reingest import _load_llmwiki_module

    await _seed_page_with_source(tmp_workspace, tmp_sqlite)
    module = _load_llmwiki_module()

    await asyncio.to_thread(module.cmd_coverage, str(tmp_workspace))
    all_sources = capsys.readouterr().out
    await asyncio.to_thread(module.cmd_coverage, str(tmp_workspace), "docs/source.md")
    one_source = capsys.readouterr().out

    assert "# Source Coverage" in all_sources
    assert "`docs/source.md`: used partially" in all_sources
    assert "# Source Coverage: docs/source.md" in one_source
    assert "ch_placed" in one_source


@pytest.mark.asyncio
async def test_fr_read_01_read_02_nfr_04_summary_index_and_commonmark_are_generated_incrementally(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from markdown_it import MarkdownIt

    from core.summary import render_summaries_and_index

    page_path = await _seed_page_with_source(tmp_workspace, tmp_sqlite)
    provider = FakeLLM.rule_based()

    first = await render_summaries_and_index(
        tmp_workspace,
        tmp_sqlite,
        run_id="run_summary",
        provider=provider,
    )

    page_text = page_path.read_text(encoding="utf-8")
    index_text = (tmp_workspace / "wiki" / "_index.md").read_text(encoding="utf-8")
    assert first.page_writes == 1
    assert first.index_written is True
    assert [call.call_site for call in provider.calls] == ["C5"]
    assert "## Summary\n<!-- acw:generated Summary run=run_summary" in page_text
    assert "Refunds are accepted within 30 days." in page_text
    assert "<!-- acw:summary-fingerprint " in page_text
    assert "[Refunds](refunds.md)" in index_text
    assert "<!-- acw:generated _index.md run=run_summary" in index_text
    MarkdownIt().parse(page_text)
    MarkdownIt().parse(index_text)

    second = await render_summaries_and_index(
        tmp_workspace,
        tmp_sqlite,
        run_id="run_summary_2",
        provider=provider,
    )
    assert second.page_writes == 0
    assert second.index_written is False
    assert [call.call_site for call in provider.calls] == ["C5"]

    page_path.write_text(
        page_text.replace("Refunds are accepted within 30 days.", "Refunds are accepted within 45 days."),
        encoding="utf-8",
    )
    third = await render_summaries_and_index(
        tmp_workspace,
        tmp_sqlite,
        run_id="run_summary_3",
        provider=provider,
    )
    assert third.page_writes == 1
    assert [call.call_site for call in provider.calls] == ["C5", "C5"]
    assert "Refunds are accepted within 45 days." in page_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_fr_read_03_fr_read_04_mcp_read_contracts_filter_markdown_not_metadata(
    tmp_workspace: Path,
    tmp_sqlite: aiosqlite.Connection,
) -> None:
    from tools.wiki_read import WikiReadHandler

    from core.coverage import render_coverage_sections
    from core.summary import render_summaries_and_index

    page_path = await _seed_page_with_source(tmp_workspace, tmp_sqlite, include_deprecated=True)
    provider = FakeLLM.rule_based()
    await render_coverage_sections(tmp_workspace, tmp_sqlite, run_id="run_read")
    await render_summaries_and_index(tmp_workspace, tmp_sqlite, run_id="run_read", provider=provider)

    handler = WikiReadHandler(tmp_workspace)
    index = await handler.wiki_index()
    summary = await handler.wiki_summary("Refunds")
    default_page = await handler.wiki_page("wiki/refunds.md")
    all_statuses = await handler.wiki_page("wiki/refunds.md", statuses=["*"])
    summary_results = await handler.wiki_search("30 days", tier="summary", limit=3)
    full_results = await handler.wiki_search("legacy refunds", tier="full", limit=3)
    coverage = await handler.wiki_coverage()

    assert index == {"markdown": (tmp_workspace / "wiki" / "_index.md").read_text(encoding="utf-8")}
    assert summary["page"] == "wiki/refunds.md"
    assert summary["title"] == "Refunds"
    assert "30 days" in summary["summary_markdown"]
    assert default_page["page"] == "wiki/refunds.md"
    assert "Legacy refunds were accepted within 60 days." not in default_page["markdown"]
    assert "Legacy refunds were accepted within 60 days." in all_statuses["markdown"]
    assert {block["status"] for block in default_page["blocks"]} == {"current", "deprecated"}
    assert {block["section"] for block in default_page["blocks"]} == {"Rules", "Historical Notes"}
    assert summary_results["results"][0]["tier"] == "summary"
    assert full_results["results"][0]["tier"] == "full"
    assert "Source Coverage" in coverage
    assert page_path.is_file()


def test_fr_read_01_c5_validator_rejects_summaries_over_max_words() -> None:
    from core.llm.calls import CallValidationError, validate_c5_response

    payload = {"title": "Long", "max_words": 3, "blocks": []}
    response = {"summary_markdown": "one two three four"}

    with pytest.raises(CallValidationError, match="300|3|words"):
        validate_c5_response(payload, response)


def test_fr_read_03_fr_read_04_wiki_read_registers_exact_tools_and_status_docs() -> None:
    from tools.wiki_read import register

    fake = _FakeMCP()
    register(fake, None, None)

    assert set(fake.descriptions) == {
        "wiki_index",
        "wiki_summary",
        "wiki_page",
        "wiki_search",
        "wiki_coverage",
    }
    combined = "\n".join(fake.descriptions.values()).casefold()
    assert "two-tier" in combined
    assert "status" in combined
    assert "current" in combined
    assert "deprecated" in combined


async def _seed_page_with_source(
    workspace: Path,
    db: aiosqlite.Connection,
    *,
    include_deprecated: bool = False,
) -> Path:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ledger import ChunkLedger
    from core.registry import PageRegistry

    await _apply_schema(db)
    dao = ACWDao(db)
    ledger = ChunkLedger(dao)
    registry = PageRegistry(dao)
    page = await registry.create_page(
        title="Refunds",
        path="wiki/refunds.md",
        description="Refund policy details",
        domain="commerce",
        page_id="pg_refunds",
        created_at="2026-06-13T00:00:00Z",
    )
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, source_kind, "
        "file_type, status, content, document_number) "
        "VALUES ('doc_source', 'user_1', 'source.md', 'Source', '/docs/', 'docs/source.md', "
        "'source', 'md', 'ready', 'source text', 1)",
    )
    await db.commit()
    await apply_migrations(db)
    source_version = await ledger.ensure_source_version(
        source_id="doc_source",
        version_hash="hash-v1",
        source_date="2026-06-01",
        source_date_origin="content",
        source_version_id="sv_source",
        seen_at="2026-06-13T00:00:00Z",
    )

    current = _block(
        block_id="cb_current",
        key="refunds.window_days",
        status=BlockStatus.current,
        source_date="2026-06-01",
        chunks=["ch_placed"],
        content="Refunds are accepted within 30 days.",
        excerpt='"Customers may request refunds within 30 days."',
    )
    await dao.create_block(
        block_id=current.id,
        page_id=str(page["id"]),
        key=current.key,
        block_type=current.type.value,
        status=current.status.value,
        source_id="doc_source",
        source_path=current.source_path,
        source_date=current.source_date,
        content_hash="current-hash",
        created_at="2026-06-13T00:00:01Z",
        updated_at="2026-06-13T00:00:01Z",
    )
    chunks = [
        await ledger.create_pending_chunk(
            source_id="doc_source",
            source_version_id=str(source_version["id"]),
            content_hash=f"hash-{chunk_id}",
            ordinal=ordinal,
            chunk_id=chunk_id,
            updated_at=f"2026-06-13T00:00:0{ordinal}Z",
        )
        for ordinal, chunk_id in enumerate(
            ["ch_placed", "ch_irrelevant", "ch_failed", "ch_conflicted", "ch_pending"],
        )
    ]
    await ledger.mark_placed(str(chunks[0]["id"]), block_ids=[current.id])
    await ledger.mark_irrelevant(str(chunks[1]["id"]), reason="not product context")
    await ledger.mark_failed(str(chunks[2]["id"]), reason="parser failed")
    await ledger.mark_conflicted_pending(str(chunks[3]["id"]), reason="changed_value")

    blocks = [current]
    if include_deprecated:
        deprecated = _block(
            block_id="cb_deprecated",
            key="refunds.legacy_window_days",
            status=BlockStatus.deprecated,
            source_date="2026-05-01",
            chunks=[],
            content="Legacy refunds were accepted within 60 days.",
            excerpt='"Legacy refunds used a 60 day window."',
        )
        await dao.create_block(
            block_id=deprecated.id,
            page_id=str(page["id"]),
            key=deprecated.key,
            block_type=deprecated.type.value,
            status=deprecated.status.value,
            source_id="doc_source",
            source_path=deprecated.source_path,
            source_date=deprecated.source_date,
            content_hash="deprecated-hash",
            created_at="2026-06-13T00:00:02Z",
            updated_at="2026-06-13T00:00:02Z",
        )
        blocks.append(deprecated)

    page_path = workspace / "wiki" / "refunds.md"
    page_path.write_text(_page_text(blocks), encoding="utf-8")
    return page_path


async def _apply_schema(db: aiosqlite.Connection) -> None:
    from core.db.migrate import apply_migrations

    await db.executescript(BASE_SCHEMA.read_text(encoding="utf-8"))
    await db.execute("INSERT INTO workspace (id, name, user_id) VALUES ('ws_1', 'Test Workspace', 'user_1')")
    await db.commit()
    await apply_migrations(db)


def _page_text(blocks: list[ContextBlock]) -> str:
    current = serialize_block(blocks[0])
    deprecated = ""
    if len(blocks) > 1:
        deprecated = f"\n\n## Historical Notes\n\n{serialize_block(blocks[1])}\n"
    return (
        "# Refunds\n\n"
        "## Summary\n\n"
        "old summary\n\n"
        "## Rules\n\n"
        f"{current}\n"
        f"{deprecated}\n"
        "## Source Coverage\n\n"
        "manual coverage that must disappear\n"
    )


def _block(
    *,
    block_id: str,
    key: str,
    status: BlockStatus,
    source_date: str,
    chunks: list[str],
    content: str,
    excerpt: str,
) -> ContextBlock:
    return ContextBlock(
        id=block_id,
        key=key,
        type=BlockType.rule,
        status=status,
        source_id="doc_source",
        source_path="docs/source.md",
        source_date=source_date,
        chunk_ids=chunks,
        user_edited=False,
        content=content,
        excerpt=excerpt,
    )


class _FakeMCP:
    def __init__(self) -> None:
        self.descriptions: dict[str, str] = {}

    def tool(self, *, name: str, description: str):
        self.descriptions[name] = description

        def _decorator(fn):
            return fn

        return _decorator

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

ROOT = Path(__file__).resolve().parents[3]
BASE_SCHEMA = ROOT / "shared" / "sqlite_schema.sql"
FIXTURE_ROOT = ROOT / "tests" / "v2" / "fixtures" / "workspaces"


async def _apply_base_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(BASE_SCHEMA.read_text(encoding="utf-8"))
    await db.commit()


@pytest.mark.asyncio
async def test_fr_reg_01_registry_crud_and_alias_resolution(tmp_sqlite) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.registry import PageRegistry, RegistryValidationError

    await _apply_base_schema(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    registry = PageRegistry(ACWDao(tmp_sqlite))
    page = await registry.create_page(
        title="Refund Rules",
        path="wiki/refunds.md",
        description="Refund policy and retry behavior",
        domain="commerce",
        aliases=["Returns", "Refund Policy"],
        page_id="pg_refunds",
        created_at="2026-06-13T00:00:00Z",
    )
    assert page == {
        "id": "pg_refunds",
        "path": "wiki/refunds.md",
        "title": "Refund Rules",
        "description": "Refund policy and retry behavior",
        "status": "active",
        "domain": "commerce",
        "created_at": "2026-06-13T00:00:00Z",
        "aliases": ["Refund Policy", "Returns"],
    }

    assert (await registry.resolve_page("Refund Rules"))["id"] == "pg_refunds"
    assert (await registry.resolve_page("wiki/refunds.md"))["id"] == "pg_refunds"
    assert (await registry.resolve_page("returns"))["id"] == "pg_refunds"

    updated = await registry.update_page(
        "pg_refunds",
        title="Refunds",
        description="Current refund rules",
        aliases=["Returns", "Refunds FAQ"],
    )
    assert updated["title"] == "Refunds"
    assert updated["aliases"] == ["Refunds FAQ", "Returns"]

    archived = await registry.archive_page("pg_refunds")
    assert archived["status"] == "archived"
    assert await registry.resolve_page("returns") is None

    merged = await registry.update_page("pg_refunds", status="merged_into:pg_other")
    assert merged["status"] == "merged_into:pg_other"

    with pytest.raises(RegistryValidationError):
        await registry.update_page("pg_refunds", status="merged")


@pytest.mark.asyncio
async def test_fr_ledger_05_registry_export_stable_order(tmp_workspace, tmp_sqlite) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.registry import PageRegistry

    await _apply_base_schema(tmp_sqlite)
    await apply_migrations(tmp_sqlite)

    registry = PageRegistry(ACWDao(tmp_sqlite))
    await registry.create_page(
        title="Zed",
        path="wiki/zed.md",
        description="Last page",
        aliases=["z alias"],
        page_id="pg_z",
        created_at="2026-06-13T00:00:00Z",
    )
    await registry.create_page(
        title="Alpha",
        path="wiki/alpha.md",
        description="First page",
        aliases=["alpha two", "alpha one"],
        page_id="pg_a",
        created_at="2026-06-13T00:00:00Z",
    )

    await registry.export_json(tmp_workspace, exported_at="2026-06-13T00:02:00Z")
    first = (tmp_workspace / "wiki" / "_meta" / "registry.json").read_text(encoding="utf-8")
    await registry.export_json(tmp_workspace, exported_at="2026-06-13T00:02:00Z")
    second = (tmp_workspace / "wiki" / "_meta" / "registry.json").read_text(encoding="utf-8")

    assert first == second
    payload = json.loads(first)
    assert [page["id"] for page in payload["pages"]] == ["pg_a", "pg_z"]
    assert payload["pages"][0]["aliases"] == ["alpha one", "alpha two"]


def test_m1_golden_workspace_fixture_trees_are_static_and_complete() -> None:
    expected_files = {
        "uc1_minimal": [
            "flows/order_refund.yaml",
            "docs/faq.md",
            "docs/tnc.md",
            "docs/api.md",
            "docs/brd.md",
            "notes/misc.txt",
            "eval/questions.yaml",
        ],
        "uc2_nodes": [
            "docs/nodes/api_call.md",
            "docs/nodes/decision.md",
            "docs/nodes/condition.md",
            "docs/nodes/transform.md",
            "docs/nodes/webhook.md",
            "docs/nodes/email.md",
            "docs/nodes/form.md",
            "docs/nodes/knowledge_lookup.md",
            "docs/nodes/handoff.md",
            "docs/nodes/wait.md",
            "uc2_nodes_v2/docs/nodes/api_call.md",
            "eval/questions.yaml",
        ],
        "uc3_support": [
            "docs/troubleshooting_2024.md",
            "rca/RCA-2026-014.md",
            "tickets/T-1001.md",
            "transcripts/standup_2026-05-02.txt",
            "eval/questions.yaml",
        ],
    }

    for workspace_name, relative_files in expected_files.items():
        workspace = FIXTURE_ROOT / workspace_name
        assert workspace.is_dir()
        for relative_file in relative_files:
            assert (workspace / relative_file).is_file()

        questions = (workspace / "eval" / "questions.yaml").read_text(encoding="utf-8")
        assert questions.count("- question:") >= 6

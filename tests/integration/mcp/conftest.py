"""Fixtures for MCP VaultFS and tool handler tests.

Uses SqliteVaultFS with a temp workspace — no Postgres needed.
"""

import json
import sys
import os
import uuid
from pathlib import Path

import pytest

TEST_USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def mcp_imports(monkeypatch):
    """The API and MCP are separate processes with overlapping module names."""
    roots = {"auth", "config", "db", "services", "tools", "vaultfs"}
    saved = {name: module for name, module in sys.modules.copy().items() if name.split(".")[0] in roots}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "mcp"))
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name.split(".")[0] in roots:
                del sys.modules[name]
        sys.modules.update(saved)


@pytest.fixture
async def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "wiki").mkdir()
    (ws / ".llmwiki").mkdir()
    (ws / ".llmwiki" / "cache").mkdir()
    return ws


@pytest.fixture
async def fs(workspace):
    from vaultfs.sqlite import SqliteVaultFS
    await SqliteVaultFS.init(str(workspace))
    instance = SqliteVaultFS(TEST_USER_ID)
    kb_id = await instance.ensure_workspace("test-workspace")
    yield instance, kb_id
    await SqliteVaultFS.close()


@pytest.fixture
async def insert_page(fs):
    async def _insert(doc_id: str, page: int, content: str, elements: dict | None = None):
        from vaultfs.sqlite import SqliteVaultFS
        db = SqliteVaultFS._db_or_raise()
        await db.execute(
            "INSERT INTO document_pages (id, document_id, page, content, elements) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), doc_id, page, content, json.dumps(elements) if elements else None),
        )
        await db.commit()
    return _insert


@pytest.fixture
async def insert_chunk(fs):
    _idx = [0]

    async def _insert(doc_id: str, kb_id: str, content: str, page: int = 1, breadcrumb: str = ""):
        from vaultfs.sqlite import SqliteVaultFS
        db = SqliteVaultFS._db_or_raise()
        chunk_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO document_chunks (id, document_id, "
            "chunk_index, content, page, header_breadcrumb, token_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 10)",
            (chunk_id, doc_id, _idx[0], content, page, breadcrumb),
        )
        _idx[0] += 1
        await db.commit()
    return _insert

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from tests.v2.fakes.fake_llm import FakeLLM


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".llmwiki").mkdir(parents=True)
    (workspace / "wiki").mkdir()
    return workspace


@pytest.fixture
async def tmp_sqlite(tmp_workspace: Path) -> AsyncIterator[aiosqlite.Connection]:
    db_path = tmp_workspace / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        yield db


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM.rule_based()

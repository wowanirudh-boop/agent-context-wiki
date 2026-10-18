from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest

from core.config import ACWConfig
from core.db.dao import ACWDao
from core.db.migrate import apply_migrations
from core.llm.calls import C5_SCHEMA
from core.llm.provider import OpenAIProvider
from tests.v2.fakes.fake_llm import FakeLLM
from tests.v2.unit.test_reingest import _apply_base_schema


@pytest.mark.parametrize("invalid", [False, True])
async def test_llm_provider_logs_usage_failures_and_closes_client(
    tmp_sqlite: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch, invalid: bool,
) -> None:
    import openai

    await _apply_base_schema(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    fake = FakeLLM.rule_based()
    closed = []

    async def create(**kwargs: Any) -> SimpleNamespace:
        request = json.loads(kwargs["input"][1]["content"])
        response = await fake.complete_structured("C5", request["payload"], C5_SCHEMA)
        return SimpleNamespace(status="completed", output_text="invalid JSON" if invalid else json.dumps(response),
                               usage=SimpleNamespace(input_tokens=123, output_tokens=42))

    async def close() -> None:
        closed.append(True)

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create), close=close))
    provider = OpenAIProvider(ACWConfig(llm_api_key="test-only"), ACWDao(tmp_sqlite))
    payload = {"title": "Refunds", "max_words": 40, "blocks": [{"content": "Refunds expire after 30 days."}]}
    if invalid:
        with pytest.raises(ValueError):
            await provider.complete_structured("C5", payload, C5_SCHEMA)
    else:
        assert (await provider.complete_structured("C5", payload, C5_SCHEMA))["summary_markdown"]
    assert closed == [True]
    assert provider.call_count == 1
    cursor = await tmp_sqlite.execute("SELECT payload_json FROM acw_events WHERE kind = 'llm.call'")
    event = json.loads((await cursor.fetchone())[0])
    assert event["input_tokens"] == 123 and event["output_tokens"] == 42
    assert event["ok"] is not invalid

from __future__ import annotations

import importlib

import pytest


def test_m0_core_import_and_id_prefixes() -> None:
    core = importlib.import_module("core")
    assert core is not None

    from core.ids import new_id, review_row_id

    block_id = new_id("cb")
    assert block_id.startswith("cb_")
    assert block_id == block_id.lower()
    assert review_row_id("run_01abc", 3) == "RR-run_01abc-3"


def test_m0_config_defaults_and_env(monkeypatch: pytest.MonkeyPatch, tmp_workspace) -> None:
    from core.config import load_config

    monkeypatch.setenv("ACW_AUTO_PROCESS", "1")
    monkeypatch.setenv("ACW_BATCH_MAX_CHUNKS", "5")
    monkeypatch.setenv("ACW_LLM_MODEL", "test-model")

    config = load_config(tmp_workspace)

    assert config.auto_process is True
    assert config.batch_max_chunks == 5
    assert config.llm_model == "test-model"
    assert config.summary_max_words == 300


@pytest.mark.asyncio
async def test_m0_tmp_workspace_and_sqlite_fixtures(tmp_workspace, tmp_sqlite) -> None:
    assert (tmp_workspace / ".llmwiki").is_dir()
    assert (tmp_workspace / "wiki").is_dir()

    await tmp_sqlite.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    await tmp_sqlite.execute("INSERT INTO probe (id) VALUES (1)")
    cursor = await tmp_sqlite.execute("SELECT id FROM probe")
    row = await cursor.fetchone()

    assert row == (1,)


@pytest.mark.asyncio
async def test_m0_fake_llm_scripted_uses_stable_fingerprint() -> None:
    from tests.v2.fakes.fake_llm import FakeLLM, fingerprint_payload

    payload = {"chunks": [{"id": "ch_1", "text": "first text"}]}
    fingerprint = fingerprint_payload(payload)
    fake = FakeLLM.scripted({("C1", fingerprint): {"ok": True}})

    response = await fake.complete_structured("C1", payload, {})

    assert response == {"ok": True}
    assert fake.call_count == 1
    assert fake.calls[0].fingerprint == fingerprint

    with pytest.raises(KeyError):
        await fake.complete_structured("C1", {"chunks": [{"id": "ch_missing"}]}, {})


@pytest.mark.asyncio
async def test_m0_fake_llm_rule_based_is_deterministic() -> None:
    from tests.v2.fakes.fake_llm import FakeLLM

    fake = FakeLLM.rule_based()
    payload = {"chunks": [{"id": "ch_1", "text": "Refunds are available within 30 days."}]}

    first = await fake.complete_structured("C1", payload, {})
    second = await fake.complete_structured("C1", payload, {})

    assert first == second
    assert fake.call_count == 2

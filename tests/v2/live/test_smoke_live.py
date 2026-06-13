from __future__ import annotations

import os

import pytest


@pytest.mark.asyncio
async def test_live_llm_provider_c5_smoke() -> None:
    if os.environ.get("ACW_LIVE_TESTS") != "1":
        pytest.skip("Set ACW_LIVE_TESTS=1 to run live LLM smoke tests.")
    if not os.environ.get("ACW_LLM_API_KEY"):
        pytest.skip("ACW_LLM_API_KEY is required for live LLM smoke tests.")

    from core.llm.calls import C5_SCHEMA, complete_validated, validate_c5_response
    from core.llm.provider import provider_from_config

    provider = provider_from_config(environ={**os.environ, "ACW_LLM_PROVIDER": "openai"})
    response = await complete_validated(
        provider,
        "C5",
        {
            "title": "Live Smoke",
            "max_words": 40,
            "blocks": [
                {
                    "key": "smoke.live.summary",
                    "type": "note",
                    "content": "The live provider should return a short JSON summary for Agent Context Wiki.",
                }
            ],
        },
        C5_SCHEMA,
        validate_c5_response,
    )

    assert response["summary_markdown"]
    assert len(response["summary_markdown"].split()) <= 40

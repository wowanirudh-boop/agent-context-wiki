from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.config import ACWConfig, load_config
from core.db.dao import ACWDao
from core.models import EventKind

StructuredSchema = Mapping[str, Any]
StructuredPayload = Mapping[str, Any]
StructuredResponse = dict[str, Any]


@runtime_checkable
class LLMProvider(Protocol):
    async def complete_structured(
        self,
        call_site: str,
        payload: StructuredPayload,
        schema: StructuredSchema,
    ) -> StructuredResponse:
        ...


class OpenAIProvider:
    def __init__(self, config: ACWConfig, dao: ACWDao | None = None) -> None:
        self.config = config
        self.dao = dao

    async def complete_structured(
        self,
        call_site: str,
        payload: StructuredPayload,
        schema: StructuredSchema,
    ) -> StructuredResponse:
        from openai import AsyncOpenAI

        started = time.perf_counter()
        ok = False
        model = self._model_for_call(call_site)
        client = AsyncOpenAI(api_key=self.config.llm_api_key, base_url=self.config.llm_base_url)
        try:
            response = await client.responses.create(
                model=model,
                input=[
                    {
                        "role": "user",
                        "content": json.dumps({"call_site": call_site, "payload": payload}, ensure_ascii=False),
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": f"acw_{call_site.lower()}",
                        "schema": dict(schema),
                        "strict": True,
                    }
                },
            )
            text = response.output_text
            ok = True
            return json.loads(text)
        finally:
            await self._log_call(call_site, model=model, started=started, ok=ok)

    def _model_for_call(self, call_site: str) -> str:
        if call_site in {"C1", "C3", "C6"} and self.config.llm_model_light:
            return self.config.llm_model_light
        return self.config.llm_model

    async def _log_call(self, call_site: str, *, model: str, started: float, ok: bool) -> None:
        if self.dao is None:
            return
        await self.dao.write_event(
            kind=EventKind.llm_call,
            actor="core.llm.provider",
            payload={
                "call_site": call_site,
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "ok": ok,
            },
        )


def provider_from_config(
    workspace: str | Path | None = None,
    *,
    dao: ACWDao | None = None,
    environ: Mapping[str, str] | None = None,
) -> LLMProvider:
    config = load_config(workspace, environ=environ)
    provider_name = config.llm_provider.strip().lower()
    if provider_name == "openai":
        return OpenAIProvider(config, dao)
    if provider_name == "agent":
        if workspace is None:
            raise ValueError("ACW_LLM_PROVIDER=agent requires a workspace path")
        from core.llm.agent_provider import AgentProvider

        return AgentProvider(workspace=workspace, dao=dao)
    if provider_name == "fake-rules":
        from tests.v2.fakes.fake_llm import FakeLLM

        return FakeLLM.rule_based()
    if provider_name == "fake-scripted":
        from tests.v2.fakes.fake_llm import FakeLLM

        return FakeLLM.scripted(_load_scripted_responses(environ or os.environ))
    raise ValueError(f"Unsupported ACW_LLM_PROVIDER: {config.llm_provider}")


def _load_scripted_responses(environ: Mapping[str, str]) -> dict[tuple[str, str], Mapping[str, Any]]:
    path = environ.get("ACW_LLM_SCRIPTED_RESPONSES")
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    responses: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in raw:
        responses[(str(item["call_site"]), str(item["fingerprint"]))] = item["response"]
    return responses

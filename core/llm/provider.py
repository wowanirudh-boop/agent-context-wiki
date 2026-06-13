from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

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

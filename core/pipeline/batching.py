from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def batch_chunks_by_source(chunks: list[Mapping[str, Any]], *, max_chunks: int) -> list[list[Mapping[str, Any]]]:
    if max_chunks < 1:
        raise ValueError("max_chunks must be >= 1")
    batches: list[list[Mapping[str, Any]]] = []
    current_source: str | None = None
    current_batch: list[Mapping[str, Any]] = []
    for chunk in chunks:
        source_id = str(chunk["source_id"])
        if current_batch and (source_id != current_source or len(current_batch) >= max_chunks):
            batches.append(current_batch)
            current_batch = []
        current_source = source_id
        current_batch.append(chunk)
    if current_batch:
        batches.append(current_batch)
    return batches

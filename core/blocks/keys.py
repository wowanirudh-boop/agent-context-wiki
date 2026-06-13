from __future__ import annotations

import re
from typing import Any

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,5}$")


class SemanticKeyError(ValueError):
    pass


def validate_semantic_key(key: str) -> str:
    if KEY_RE.fullmatch(key) is None:
        raise SemanticKeyError(f"Invalid semantic key: {key}")
    return key


def key_inventory(page: Any) -> list[str]:
    keys: set[str] = set()
    for segment in page.segments:
        block = getattr(segment, "block", None)
        if block is not None:
            keys.add(block.key)
    return sorted(keys)

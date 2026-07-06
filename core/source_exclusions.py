from __future__ import annotations

from pathlib import PurePosixPath

_EXCLUDED_ROOTS = {"wiki", ".llmwiki"}


def normalize_source_path(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").strip()
    normalized = normalized.removeprefix("./").lstrip("/")
    return PurePosixPath(normalized).as_posix().casefold()


def is_acw_source_excluded(relative_path: str) -> bool:
    normalized = normalize_source_path(relative_path)
    root = normalized.split("/", 1)[0]
    return root in _EXCLUDED_ROOTS

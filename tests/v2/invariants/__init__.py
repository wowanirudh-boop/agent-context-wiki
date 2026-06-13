from __future__ import annotations

from pathlib import Path


def assert_invariants(workspace: str | Path) -> None:
    Path(workspace)

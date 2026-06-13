from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
_MCP_TOP_LEVEL = ("auth", "config", "hosted", "services", "tools", "vaultfs")


@contextmanager
def isolated_mcp_imports() -> Iterator[None]:
    saved_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name in _MCP_TOP_LEVEL or name.startswith(tuple(f"{prefix}." for prefix in _MCP_TOP_LEVEL))
    }
    _remove_mcp_top_level_modules()
    sys.path.insert(0, str(MCP_DIR))
    try:
        yield
    finally:
        _remove_mcp_top_level_modules()
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def _remove_mcp_top_level_modules() -> None:
    prefixes = tuple(f"{prefix}." for prefix in _MCP_TOP_LEVEL)
    for name in list(sys.modules):
        if name in _MCP_TOP_LEVEL or name.startswith(prefixes):
            del sys.modules[name]

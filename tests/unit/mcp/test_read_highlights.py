"""Unit tests for MCP read highlight materialization."""

import importlib.util
import sys
import types

import pytest

from tests.unit.mcp._importing import MCP_DIR, isolated_mcp_imports


@pytest.fixture(autouse=True)
def _mcp_imports():
    with isolated_mcp_imports():
        yield


def _materializer():
    """Import the pure helper without loading VaultFS implementations.

    Other unit tests put api/services on sys.path, which can collide with
    mcp/services during import. This test only needs read.py's formatter.
    """
    fake_vaultfs = types.ModuleType("vaultfs")
    fake_vaultfs.VaultFS = object
    previous = sys.modules.get("vaultfs")
    sys.modules["vaultfs"] = fake_vaultfs
    try:
        spec = importlib.util.spec_from_file_location(
            "tools.read_tool_highlight_test",
            MCP_DIR / "tools" / "read.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._materialize_highlights
    finally:
        if previous is None:
            sys.modules.pop("vaultfs", None)
        else:
            sys.modules["vaultfs"] = previous


class TestMaterializeHighlights:

    def test_includes_pdf_highlight_with_page(self):
        _materialize_highlights = _materializer()

        out = _materialize_highlights({
            "highlights": [{
                "id": "h1",
                "type": "pdf",
                "pdfAnchor": {"page": 3, "textContent": "important PDF quote"},
                "comment": "check this",
            }],
        })

        assert "important PDF quote" in out
        assert "(p.3)" in out
        assert "check this" in out

    def test_includes_markdown_text_anchor_highlight(self):
        _materialize_highlights = _materializer()

        out = _materialize_highlights({
            "highlights": [{
                "id": "h1",
                "type": "text",
                "anchor": None,
                "textAnchor": {
                    "textStart": 10,
                    "textEnd": 26,
                    "textContent": "markdown quote",
                },
                "comment": None,
            }],
        })

        assert "markdown quote" in out

    def test_includes_legacy_dom_anchor_highlight(self):
        _materialize_highlights = _materializer()

        out = _materialize_highlights({
            "highlights": [{
                "id": "h1",
                "type": "text",
                "anchor": {"textContent": "legacy page quote"},
                "comment": None,
            }],
        })

        assert "legacy page quote" in out

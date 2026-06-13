"""Unit tests for hosted-mode MCP server configuration helpers."""

import os

import pytest

from tests.unit.mcp._importing import isolated_mcp_imports

# The `hosted` module instantiates a FastMCP() at import time, which
# validates SUPABASE_URL/MCP_URL via Pydantic AnyHttpUrl. The top-level
# tests/conftest.py sets SUPABASE_URL="", which makes that validation
# fail with "Input should be a valid URL". Override with placeholders
# valid enough to construct AnyHttpUrl(f"{SUPABASE_URL}/auth/v1").
os.environ["SUPABASE_URL"] = "https://example.supabase.co"
os.environ.setdefault("MCP_URL", "http://example.com")

@pytest.fixture(autouse=True)
def _mcp_imports():
    with isolated_mcp_imports():
        yield


class TestBuildAllowedHosts:

    def test_includes_bare_host_and_port_wildcard(self):
        """The Host header inside a Docker network includes the internal port
        (e.g. ``llmwiki-mcp:8080``). Without the ``host:*`` wildcard,
        TransportSecurityMiddleware rejects the request as 421
        Misdirected Request, even though the bare hostname matches.
        """
        from hosted import _build_allowed_hosts

        allowed = _build_allowed_hosts("http://llmwiki-mcp:8080")

        assert "llmwiki-mcp" in allowed
        assert "llmwiki-mcp:*" in allowed

    def test_handles_https_url(self):
        from hosted import _build_allowed_hosts

        allowed = _build_allowed_hosts("https://wiki.example.com")

        assert allowed == ["wiki.example.com", "wiki.example.com:*"]

    def test_falls_back_to_localhost_for_invalid_url(self):
        """``urlparse(...).hostname`` is None for inputs without a scheme."""
        from hosted import _build_allowed_hosts

        allowed = _build_allowed_hosts("not-a-url")

        assert allowed == ["localhost", "localhost:*"]

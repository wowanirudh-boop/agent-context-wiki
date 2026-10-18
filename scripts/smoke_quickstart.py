from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def smoke(workspace: Path) -> None:
    from tests.v2.invariants import assert_invariants, snapshot_sources

    (workspace / "docs").mkdir(parents=True)
    (workspace / "eval").mkdir()
    source = workspace / "docs/refunds.md"
    source.write_text("# Refunds\n\nThe refund window is 30 days from purchase.\n", encoding="utf-8")
    expected_sources = snapshot_sources(workspace)
    env = {**os.environ, "ACW_LLM_PROVIDER": "fake-rules", "PYTHONUTF8": "1"}

    def cli(command: str, *args: str) -> str:
        result = subprocess.run([sys.executable, str(ROOT / "llmwiki"), command, str(workspace), *args],
                                env=env, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"{command} failed:\n{result.stdout}\n{result.stderr}")
        return result.stdout

    print(cli("process"), end="")
    assert_invariants(workspace, source_snapshot=expected_sources)
    before = {p.relative_to(workspace): hashlib.sha256(p.read_bytes()).hexdigest() for p in (workspace / "wiki").rglob("*.md")}
    cli("process")
    after = {p.relative_to(workspace): hashlib.sha256(p.read_bytes()).hexdigest() for p in (workspace / "wiki").rglob("*.md")}
    assert before == after, "Unchanged processing wrote page content"
    with closing(sqlite3.connect(workspace / ".llmwiki/index.db")) as db:
        stats = json.loads(db.execute("SELECT stats_json FROM acw_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert stats["llm_calls"] == stats["page_writes"] == 0
    source.write_text(source.read_text(encoding="utf-8").replace("30 days", "45 days"), encoding="utf-8")
    expected_sources = snapshot_sources(workspace)
    cli("process")
    assert_invariants(workspace, source_snapshot=expected_sources)
    reviews = list((workspace / "wiki/_reviews").glob("RR-*.md"))
    assert reviews, "Changed policy did not produce a review"
    for review in reviews:
        review.write_text(review.read_text(encoding="utf-8").replace("- decision:", "- decision: accept_new"), encoding="utf-8")
    cli("apply-decisions")
    assert_invariants(workspace, source_snapshot=expected_sources)
    cli("lint")
    (workspace / "eval/questions.yaml").write_text(
        '- question: What is the refund window?\n  expected_keys: [refunds.window_days]\n  expected_substrings: ["45 days"]\n',
        encoding="utf-8",
    )
    assert json.loads(cli("eval", "--json"))["score"] == 1.0
    assert "Refunds" in (workspace / "wiki/_index.md").read_text(encoding="utf-8")
    config = cli("mcp-config")
    assert json.JSONDecoder().raw_decode(config[config.index("{"):])[0]["mcpServers"]
    assert "docs/refunds.md" in cli("coverage")
    print(f"ACW v2 quickstart passed: processing, review, idempotency, invariants, lint, eval, coverage, MCP config.\n{workspace}")


def main() -> None:
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    explicit = os.environ.get("ACW_SMOKE_WORKSPACE")
    if explicit:
        workspace = Path(explicit).resolve()
        workspace.mkdir(parents=True, exist_ok=False)
        smoke(workspace)
    elif os.environ.get("ACW_SMOKE_KEEP") == "1":
        smoke(Path(tempfile.mkdtemp(prefix="quickstart-", dir=scratch)))
    else:
        with tempfile.TemporaryDirectory(prefix="quickstart-", dir=scratch) as directory:
            smoke(Path(directory))


if __name__ == "__main__":
    main()

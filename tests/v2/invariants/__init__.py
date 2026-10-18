from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from core.blocks.model import ContextBlock, Page
from core.blocks.parser import parse_page
from core.blocks.serializer import serialize_page
from core.coverage import _generated_section_is_current, _page_coverage_body
from core.db.dao import Row
from core.models import Disposition


def snapshot_sources(workspace: str | Path) -> dict[str, str]:
    root = Path(workspace)
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).parts[0] not in {"wiki", ".llmwiki", ".git"}
    }


def assert_invariants(workspace: str | Path, *, source_snapshot: dict[str, str] | None = None) -> None:
    """Check I1–I10 against persisted state, including from async tests."""
    root = Path(workspace)
    db_path = root / ".llmwiki/index.db"
    assert db_path.is_file(), "I1: workspace database is missing"
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        chunks = {row["id"]: dict(row) for row in db.execute("SELECT * FROM acw_chunk_ledger")}
        blocks = {row["id"]: dict(row) for row in db.execute("SELECT * FROM acw_blocks")}
        pages = [dict(row) for row in db.execute("SELECT * FROM acw_pages")]
        reviews = [dict(row) for row in db.execute("SELECT * FROM acw_review_rows WHERE applied_at IS NULL")]
        links = {(row[0], row[1]) for row in db.execute("SELECT block_id, chunk_id FROM acw_block_chunks")}
        sources = {row["id"]: dict(row) for row in db.execute("SELECT id, relative_path, source_kind FROM documents")}
        versions = {row[0] for row in db.execute("SELECT id FROM acw_source_versions")}
        last_run = db.execute("SELECT status FROM acw_runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        _assert_ledger(chunks, blocks, sources, versions, links, last_run)

        parsed, markdown_blocks, markers = _parse_registered_pages(root, pages, blocks, links)
        visible = {key for key, block in blocks.items() if block["status"] not in {"deleted", "rejected"}}
        assert set(markdown_blocks) == visible, f"I4: block inventory differs {visible ^ set(markdown_blocks)}"

        grouped = defaultdict(list)
        page_sources = defaultdict(set)
        for chunk in chunks.values():
            source = sources[chunk["source_id"]]
            if source["source_kind"] == "source":
                grouped[chunk["source_id"]].append({**chunk, "source_path": source["relative_path"]})
        for block_id in visible:
            block = blocks[block_id]
            page_sources[block["page_id"]].add(block["source_id"])
        pending_reviews = {}
        for row in reviews:
            candidate = json.loads(row["candidate_json"] or "{}")
            candidate = candidate.get("block", candidate)
            if candidate.get("source_id"):
                page_sources[row["page_id"]].add(candidate["source_id"])
            if row["row_kind"] in {"conflict", "needs_review"}:
                pending_reviews[row["id"]] = candidate
        for page in pages:
            text, model = parsed[page["id"]]
            if page["status"] == "active":
                source_ids = sorted(page_sources[page["id"]], key=lambda s: (sources[s]["relative_path"], s))
                expected = _page_coverage_body(page_path=page["path"], source_ids=source_ids, rows_by_source=grouped)
                assert _generated_section_is_current(text, "Source Coverage", expected), f"I7: coverage differs {page['path']}"
            active_keys = Counter(b.key for b in model.blocks if b.status.value in {"current", "needs_review"})
            assert all(count == 1 for count in active_keys.values()), f"I9: duplicate active keys {page['path']}"
        conflicted = {key for key, chunk in chunks.items() if chunk["disposition"] == "conflicted_pending"}
        covered = set()
        for row_id, candidate in pending_reviews.items():
            candidate_chunks = set(candidate.get("chunk_ids", [])) & conflicted
            if candidate_chunks:
                assert markers[row_id] == 1, f"I8: missing or duplicate marker {row_id}"
                covered.update(candidate_chunks)
        assert covered == conflicted, f"I8: chunks without review {conflicted - covered}"
        assert all(row_id in pending_reviews for row_id in markers), "I8: marker without open review"
    if source_snapshot is not None:
        assert snapshot_sources(root) == source_snapshot, "I10: source files changed"


def _assert_ledger(
    chunks: dict[str, Row], blocks: dict[str, Row], sources: dict[str, Row],
    versions: set[str], links: set[tuple[str, str]], last_run: sqlite3.Row | None,
) -> None:
    for chunk in chunks.values():
        assert chunk["disposition"] in set(Disposition), f"I1: invalid disposition {chunk['id']}"
        assert chunk["source_id"] in sources and chunk["source_version_id"] in versions, f"I1: orphan {chunk['id']}"
        if chunk["disposition"] in {"irrelevant", "exclusion_pending", "failed", "failed_final"}:
            assert (chunk["disposition_reason"] or "").strip(), f"I1: missing reason {chunk['id']}"
        if chunk["disposition"] == "duplicate":
            assert chunk["duplicate_of_block_id"] in blocks, f"I1: missing duplicate target {chunk['id']}"
    if last_run is not None and last_run[0] == "completed":
        assert all(c["disposition"] != "pending" for c in chunks.values()), "I2: completed run has pending chunks"
    for block_id, chunk_id in links:
        assert block_id in blocks and chunk_id in chunks, f"I3: orphan link {block_id}/{chunk_id}"
    linked_chunks = {chunk_id for _, chunk_id in links}
    linked_blocks = {block_id for block_id, _ in links}
    for chunk in chunks.values():
        if chunk["disposition"] == "placed":
            assert chunk["id"] in linked_chunks, f"I3: placed chunk has no block {chunk['id']}"
    assert set(blocks) <= linked_blocks, f"I3: blocks without chunks {set(blocks) - linked_blocks}"


def _parse_registered_pages(
    root: Path, pages: list[Row], blocks: dict[str, Row], links: set[tuple[str, str]],
) -> tuple[dict[str, tuple[str, Page]], dict[str, ContextBlock], Counter[str]]:
    parsed = {}
    markdown_blocks = {}
    markers: Counter[str] = Counter()
    for page in pages:
        path = root / page["path"]
        assert path.is_file(), f"I4: missing page {page['path']}"
        raw = path.read_bytes()
        try:
            model = parse_page(raw.decode("utf-8"))
            assert serialize_page(model).encode("utf-8") == raw, f"I5: round trip differs {page['path']}"
        except ValueError as exc:
            raise AssertionError(f"I5: cannot parse {page['path']}: {exc}") from exc
        parsed[page["id"]] = (raw.decode("utf-8"), model)
        for block in model.blocks:
            assert block.id not in markdown_blocks, f"I4: repeated block {block.id}"
            assert block.id in blocks and blocks[block.id]["page_id"] == page["id"], f"I4: unregistered block {block.id}"
            assert blocks[block.id]["status"] == block.status.value, f"I4: status differs {block.id}"
            assert block.source_path.strip() and block.excerpt.strip(), f"I6: missing evidence {block.id}"
            assert block.chunk_ids and all((block.id, c) in links for c in block.chunk_ids), f"I3: missing block links {block.id}"
            markdown_blocks[block.id] = block
            markers.update(block.pending_review_ids)
    return parsed, markdown_blocks, markers

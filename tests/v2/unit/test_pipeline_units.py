from __future__ import annotations

import pytest


def test_fr_place_07_batches_pending_chunks_by_source_and_max_size() -> None:
    from core.pipeline.batching import batch_chunks_by_source

    chunks = [
        {"id": "ch_1", "source_id": "doc_a"},
        {"id": "ch_2", "source_id": "doc_a"},
        {"id": "ch_3", "source_id": "doc_a"},
        {"id": "ch_4", "source_id": "doc_b"},
    ]

    batches = batch_chunks_by_source(chunks, max_chunks=2)

    assert [[chunk["id"] for chunk in batch] for batch in batches] == [
        ["ch_1", "ch_2"],
        ["ch_3"],
        ["ch_4"],
    ]


def test_fr_flow_01_whimsical_json_to_mermaid_preserves_nodes_edges_and_conditions() -> None:
    from core.pipeline.flows import whimsical_json_to_mermaid

    mermaid = whimsical_json_to_mermaid(
        {
            "nodes": [
                {"id": "start", "label": "Start"},
                {"id": "approve", "label": "Approve refund"},
                {"id": "reject", "label": "Reject refund"},
            ],
            "edges": [
                {"from": "start", "to": "approve", "condition": "within 30 days"},
                {"from": "start", "to": "reject", "condition": "older than 30 days"},
            ],
        }
    )

    assert "flowchart TD" in mermaid
    assert 'start["Start"]' in mermaid
    assert 'approve["Approve refund"]' in mermaid
    assert 'reject["Reject refund"]' in mermaid
    assert "start -->|within 30 days| approve" in mermaid
    assert "start -->|older than 30 days| reject" in mermaid


@pytest.mark.asyncio
async def test_fr_trans_01_prepass_marks_chatter_irrelevant_and_superseded_duplicate(tmp_sqlite) -> None:
    from core.db.dao import ACWDao
    from core.db.migrate import apply_migrations
    from core.ingest import index_document_chunks
    from core.ledger import ChunkLedger
    from core.pipeline.transcripts import apply_transcript_prepass
    from tests.v2.unit.test_reingest import _apply_base_schema, _insert_document, _seed_workspace

    await _apply_base_schema(tmp_sqlite)
    await _seed_workspace(tmp_sqlite)
    await apply_migrations(tmp_sqlite)
    await _insert_document(tmp_sqlite, doc_id="doc_transcript", relative_path="transcripts/standup.txt")
    await index_document_chunks(
        tmp_sqlite,
        "doc_transcript",
        content="chatter\nold decision\nnew decision",
        chunks=[
            _chunk(0, "Morning chatter about lunch."),
            _chunk(1, "Decision: use 2 retries."),
            _chunk(2, "Decision update: use 5 retries."),
        ],
    )

    rows = await _ledger_rows(tmp_sqlite)
    response = {
        "segments": [
            {
                "chunk_id": rows[0]["id"],
                "relevant": False,
                "reason": "chatter",
                "superseded_by_chunk_id": None,
                "key_hint": None,
                "source_date_extracted": "2026-05-02",
            },
            {
                "chunk_id": rows[1]["id"],
                "relevant": True,
                "reason": "intra_transcript_supersession",
                "superseded_by_chunk_id": rows[2]["id"],
                "key_hint": "webhooks.retries.count",
                "source_date_extracted": "2026-05-02",
            },
            {
                "chunk_id": rows[2]["id"],
                "relevant": True,
                "reason": None,
                "superseded_by_chunk_id": None,
                "key_hint": "webhooks.retries.count",
                "source_date_extracted": "2026-05-02",
            },
        ]
    }

    survivors = await apply_transcript_prepass(
        tmp_sqlite,
        ChunkLedger(ACWDao(tmp_sqlite)),
        rows,
        response,
    )

    updated = await _ledger_rows(tmp_sqlite)
    assert updated[0]["disposition"] == "irrelevant"
    assert updated[0]["disposition_reason"] == "chatter"
    assert updated[1]["disposition"] == "duplicate"
    assert updated[1]["disposition_reason"] == "intra_transcript_supersession"
    assert survivors == [rows[2]]


def _chunk(index: int, content: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        index=index,
        content=content,
        page=None,
        start_char=index * 10,
        token_count=10,
        header_breadcrumb="",
    )


async def _ledger_rows(db):
    cursor = await db.execute("SELECT id, disposition, disposition_reason FROM acw_chunk_ledger ORDER BY ordinal")
    return [dict(zip([description[0] for description in cursor.description], row, strict=True)) for row in await cursor.fetchall()]

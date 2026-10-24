from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from core.conflicts.detect import detect_candidate_conflict, retrieve_comparison_candidates
from core.db.dao import ACWDao, Row
from core.db.migrate import apply_migrations
from core.llm.calls import C3_SCHEMA, CallValidationError, validate_c3_response
from core.registry import PageRegistry
from tests.v2.fakes.fake_llm import FakeLLM, fingerprint_payload
from tests.v2.unit.test_conflicts import _block, _persist_blocks, _write_page
from tests.v2.unit.test_reingest import _apply_base_schema, _insert_document, _seed_workspace


async def _pages(workspace: Path, db: aiosqlite.Connection) -> tuple[Row, Row]:
    await _apply_base_schema(db)
    await _seed_workspace(db)
    await apply_migrations(db)
    await _insert_document(db, doc_id="doc_existing", relative_path="docs/policy.md")
    registry = PageRegistry(ACWDao(db))
    target = await registry.create_page(title="Support FAQ", path="wiki/faq.md", description="Customer answers")
    other = await registry.create_page(title="Purchase policy", path="wiki/policy.md", description="Purchase rules")
    _write_page(workspace, target["path"], target["title"], [])
    return target, other


@pytest.mark.parametrize("candidate_key", ["refunds.window_days", "support.refund_deadline"])
async def test_fr_conf_01_unlinked_page_matches_survive_local_search_limit(
    tmp_workspace: Path, tmp_sqlite: aiosqlite.Connection, candidate_key: str,
) -> None:
    target, other = await _pages(tmp_workspace, tmp_sqlite)
    existing = _block(block_id="cb_policy", key="refunds.window_days", content="Refund requests have a 30 day deadline.")
    noise = [_block(block_id=f"cb_noise_{index}", key=f"support.topic_{index}",
                    content="Refund requests have a deadline for customer answers.") for index in range(10)]
    _write_page(tmp_workspace, target["path"], target["title"], noise)
    _write_page(tmp_workspace, other["path"], other["title"], [existing])
    await _persist_blocks(tmp_sqlite, target["id"], noise)
    await _persist_blocks(tmp_sqlite, other["id"], [existing])
    candidate = _block(key=candidate_key, content="Refund requests have a 14 day deadline.")

    retrieved = await retrieve_comparison_candidates(tmp_workspace, tmp_sqlite, target, candidate, limit=2)

    assert "cb_policy" in {item.block.id for item in retrieved}
    assert len(retrieved) <= 5  # Exact keys plus two local and two workspace search results.
    await tmp_sqlite.execute("UPDATE acw_pages SET status = 'archived' WHERE id = ?", (other["id"],))
    assert "cb_policy" not in {item.block.id for item in await retrieve_comparison_candidates(
        tmp_workspace, tmp_sqlite, target, candidate, limit=2,
    )}


@pytest.mark.parametrize(
    ("resolved_key", "verdict", "source_path", "expected"),
    [("refunds.window_days", "conflict", "docs/faq.md", "conflict"),
     ("refunds.window_days", "duplicate", "docs/faq.md", "conflict"),
     ("refunds.window_days", "duplicate", "docs/policy.md", "conflict"),
     (None, "conflict", "docs/faq.md", "distinct")],
)
async def test_fr_conf_03_different_labels_require_explicit_fact_match_and_review(
    tmp_workspace: Path, tmp_sqlite: aiosqlite.Connection,
    resolved_key: str | None, verdict: str, source_path: str, expected: str,
) -> None:
    target, other = await _pages(tmp_workspace, tmp_sqlite)
    existing = _block(block_id="cb_policy", key="refunds.window_days", content="Refund requests within 30 days.",
                      source_path="docs/policy.md")
    _write_page(tmp_workspace, other["path"], other["title"], [existing])
    await _persist_blocks(tmp_sqlite, other["id"], [existing])
    candidate = _block(key="support.refund_deadline", content="Refund requests within 14 days.", source_path=source_path)
    response = {"verdict": verdict, "conflict_type": "changed_value" if verdict == "conflict" else None,
                "recommendation": "needs_more_info", "rationale": "Both excerpts describe the refund request deadline.",
                "resolved_key": resolved_key}
    fake = FakeLLM.scripted({("C3", fingerprint_payload({"chunk_id": candidate.chunk_ids[0]})): response})

    result = await detect_candidate_conflict(tmp_workspace, tmp_sqlite, target, candidate, provider=fake)

    assert fake.call_count == 1
    assert result.kind == expected
    assert candidate.key == "support.refund_deadline"  # Detection does not mutate its inputs.
    if expected == "conflict":
        assert result.resolved_key == "refunds.window_days"
        assert result.existing.block.id == existing.id


@pytest.mark.parametrize("resolved_key", ["unrelated.new_key", True, "", "support.refund_deadline"])
def test_fr_block_06_judge_cannot_invent_a_canonical_key(resolved_key: object) -> None:
    payload = {"candidate": {"key": "support.refund_deadline"}, "existing": {"key": "refunds.window_days"}}
    response = {"verdict": "conflict", "conflict_type": "changed_value", "recommendation": "needs_more_info",
                "rationale": "Same refund deadline.", "resolved_key": resolved_key}
    with pytest.raises(CallValidationError, match="resolved_key"):
        validate_c3_response(payload, response)


def test_fr_block_06_distinct_facts_cannot_resolve_to_one_key() -> None:
    payload = {"candidate": {"key": "shipping.delivery_days"}, "existing": {"key": "refunds.window_days"}}
    response = {"verdict": "distinct", "conflict_type": None, "recommendation": "needs_more_info",
                "rationale": "Different deadlines.", "resolved_key": "refunds.window_days"}
    with pytest.raises(CallValidationError, match="resolved_key"):
        validate_c3_response(payload, response)


def test_fr_block_06_resolved_key_is_nullable_in_judge_contract() -> None:
    assert "resolved_key" in C3_SCHEMA["required"]
    assert C3_SCHEMA["properties"]["resolved_key"]["anyOf"] == [{"type": "string"}, {"type": "null"}]

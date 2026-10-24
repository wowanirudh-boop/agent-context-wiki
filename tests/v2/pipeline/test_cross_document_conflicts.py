from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from core.blocks.parser import parse_page
from core.llm.provider import StructuredPayload, StructuredResponse, StructuredSchema
from core.pipeline.run import run_processing_run
from core.review.apply import apply_decisions
from tests.v2.acceptance.test_m9_acceptance import _init_and_reindex
from tests.v2.fakes.fake_llm import FakeLLM
from tests.v2.invariants import assert_invariants, snapshot_sources
from tests.v2.unit.test_reingest import _load_llmwiki_module


@pytest.mark.parametrize("candidate_key", ["refunds.window_days", "support.refund_deadline"])
@pytest.mark.parametrize("decision", ["accept_new", "keep_existing", "mark_conflicted"])
async def test_fr_conf_01_separate_documents_on_unlinked_pages_require_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_key: str, decision: str,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    policy_text = "Refund requests must arrive within 30 days of purchase."
    faq_text = "Customers have 14 days after purchase to request a refund."
    shipping_text = "Shipping deliveries arrive within 14 days of purchase."
    (workspace / "docs/policy.md").write_text(policy_text, encoding="utf-8")
    (workspace / "docs/shipping.md").write_text(shipping_text, encoding="utf-8")
    await _init_and_reindex(workspace)
    fake = FakeLLM.rule_based()
    original = fake.complete_structured

    async def model(call_site: str, payload: StructuredPayload, schema: StructuredSchema) -> StructuredResponse:
        response = await original(call_site, payload, schema)
        if call_site == "C1":
            for item, chunk in zip(response["chunks"], payload["chunks"], strict=True):
                source = chunk["source_path"]
                title, key = {
                    "docs/policy.md": ("Purchase policy", "refunds.window_days"),
                    "docs/shipping.md": ("Delivery", "shipping.delivery_days"),
                    "docs/faq.md": ("Customer questions", candidate_key),
                }[source]
                placement = item["placements"][0]
                placement.update(page=None, new_page={"title": title, "description": title, "domain": "commerce",
                                 "path_slug": Path(source).stem, "no_registry_match_assertion": "Different topic page."}, links=[])
                placement["block"].update(key=key, content=chunk["text"], excerpt=chunk["text"])
        elif call_site == "C3":
            candidate, existing = payload["candidate"], payload["existing"]
            same_fact = candidate["source_path"] == "docs/faq.md" and existing["source_path"] == "docs/policy.md"
            response = {"verdict": "conflict" if same_fact else "distinct",
                        "conflict_type": "changed_value" if same_fact else None,
                        "resolved_key": existing["key"] if same_fact and candidate["key"] != existing["key"] else None,
                        "recommendation": "accept_new" if same_fact else "needs_more_info",
                        "rationale": "Both excerpts name the refund request deadline." if same_fact else "Delivery is a different fact."}
        return response

    monkeypatch.setattr(fake, "complete_structured", model)
    snapshot = snapshot_sources(workspace)
    await run_processing_run(workspace, provider=fake)
    assert_invariants(workspace, source_snapshot=snapshot)
    shipping_page = workspace / "wiki/shipping.md"
    shipping_before = shipping_page.read_bytes()
    assert not list((workspace / "wiki/_reviews").glob("*.md"))

    # Add a different file, leaving the original policy entirely unchanged.
    (workspace / "docs/faq.md").write_text(faq_text, encoding="utf-8")
    await asyncio.to_thread(_load_llmwiki_module().cmd_reindex, str(workspace))
    snapshot = snapshot_sources(workspace)
    await run_processing_run(workspace, provider=fake)
    assert_invariants(workspace, source_snapshot=snapshot)
    with sqlite3.connect(workspace / ".llmwiki/index.db") as db:
        rows = db.execute("SELECT candidate_json, conflict_type, applied_at FROM acw_review_rows WHERE row_kind = 'conflict'").fetchall()
    assert len(rows) == 1
    candidate = json.loads(rows[0][0])
    assert candidate["key"] == "refunds.window_days"
    assert candidate["source_path"] == "docs/faq.md"
    assert candidate["excerpt"] == faq_text
    assert rows[0][1:] == ("changed_value", None)
    policy_page = workspace / "wiki/policy.md"
    old = parse_page(policy_page.read_text(encoding="utf-8")).blocks
    assert len(old) == 1 and old[0].content == policy_text and old[0].pending_review_ids
    assert shipping_page.read_bytes() == shipping_before
    review = next((workspace / "wiki/_reviews").glob("*.md"))
    review_text = review.read_text(encoding="utf-8")
    assert policy_text in review_text and faq_text in review_text
    assert "docs/policy.md" in review_text and "docs/faq.md" in review_text

    review.write_text(review_text.replace("- decision:", f"- decision: {decision}"), encoding="utf-8")
    await apply_decisions(workspace, review_paths=[review], provider=fake)
    assert_invariants(workspace, source_snapshot=snapshot)
    blocks = parse_page(policy_page.read_text(encoding="utf-8")).blocks
    by_source = {block.source_path: block for block in blocks}
    assert by_source["docs/policy.md"].excerpt == policy_text
    if decision == "keep_existing":
        assert len(blocks) == 1 and blocks[0].status.value == "current"
    else:
        assert by_source["docs/faq.md"].excerpt == faq_text
        assert by_source["docs/faq.md"].status.value == ("current" if decision == "accept_new" else "conflicted")
        assert by_source["docs/policy.md"].status.value == ("deprecated" if decision == "accept_new" else "conflicted")
    calls_before = fake.call_count
    repeated = await run_processing_run(workspace, provider=fake)
    assert repeated.stats["page_writes"] == 0 and fake.call_count == calls_before
    assert_invariants(workspace, source_snapshot=snapshot)

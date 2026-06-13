from __future__ import annotations

import pytest


def test_fr_place_04_c1_validator_requires_excerpt_substring_of_chunk() -> None:
    from core.llm.calls import CallValidationError, validate_c1_response

    payload = {
        "chunks": [{"id": "ch_a", "text": "Refunds are allowed within 30 days.", "breadcrumb": ""}],
        "registry": [{"id": "pg_refunds", "title": "Refunds", "description": "Refund rules", "aliases": []}],
        "page_context": {"pg_refunds": {"key_inventory": [], "section_outline": []}},
    }
    response = {
        "chunks": [
            {
                "chunk_id": "ch_a",
                "relevant": True,
                "irrelevant_reason": None,
                "placements": [
                    {
                        "page": {"existing_page_id": "pg_refunds"},
                        "new_page": None,
                        "section": "Rules",
                        "block": {
                            "key": "refunds.window_days",
                            "type": "rule",
                            "content": "Refunds are allowed within 30 days.",
                            "excerpt": "Refunds are allowed within 45 days.",
                            "new_key_justification": "New refund key.",
                        },
                        "links": [],
                    }
                ],
            }
        ]
    }

    with pytest.raises(CallValidationError, match="excerpt"):
        validate_c1_response(payload, response)


def test_fr_place_01_c1_validator_requires_reason_for_irrelevant_chunk() -> None:
    from core.llm.calls import CallValidationError, validate_c1_response

    payload = {
        "chunks": [{"id": "ch_recipe", "text": "Tomato soup uses olive oil.", "breadcrumb": ""}],
        "registry": [],
        "page_context": {},
    }
    response = {
        "chunks": [
            {
                "chunk_id": "ch_recipe",
                "relevant": False,
                "irrelevant_reason": "",
                "placements": [],
            }
        ]
    }

    with pytest.raises(CallValidationError, match="irrelevant_reason"):
        validate_c1_response(payload, response)


def test_fr_place_02_c1_validator_requires_new_page_registry_assertion() -> None:
    from core.llm.calls import CallValidationError, validate_c1_response

    payload = {
        "chunks": [{"id": "ch_new", "text": "Webhook retries use 5 attempts.", "breadcrumb": ""}],
        "registry": [],
        "page_context": {},
    }
    response = {
        "chunks": [
            {
                "chunk_id": "ch_new",
                "relevant": True,
                "irrelevant_reason": None,
                "placements": [
                    {
                        "page": None,
                        "new_page": {
                            "title": "Webhook Retries",
                            "description": "Webhook retry behavior",
                            "domain": "webhooks",
                            "path_slug": "webhook-retries",
                            "no_registry_match_assertion": None,
                        },
                        "section": "Rules",
                        "block": {
                            "key": "webhooks.retries.count",
                            "type": "rule",
                            "content": "Webhook retries use 5 attempts.",
                            "excerpt": "Webhook retries use 5 attempts.",
                            "new_key_justification": "New webhook key.",
                        },
                        "links": [],
                    }
                ],
            }
        ]
    }

    with pytest.raises(CallValidationError, match="no_registry_match_assertion"):
        validate_c1_response(payload, response)


def test_fr_trans_01_c6_validator_accepts_segment_supersession_shape() -> None:
    from core.llm.calls import validate_c6_response

    payload = {
        "segments": [
            {"chunk_id": "ch_early", "text": "Decision: use 2 retries."},
            {"chunk_id": "ch_late", "text": "Decision update: use 5 retries."},
        ]
    }
    response = {
        "segments": [
            {
                "chunk_id": "ch_early",
                "relevant": True,
                "reason": "superseded by later decision",
                "superseded_by_chunk_id": "ch_late",
                "key_hint": "webhooks.retries.count",
                "source_date_extracted": "2026-05-02",
            },
            {
                "chunk_id": "ch_late",
                "relevant": True,
                "reason": None,
                "superseded_by_chunk_id": None,
                "key_hint": "webhooks.retries.count",
                "source_date_extracted": "2026-05-02",
            },
        ]
    }

    assert validate_c6_response(payload, response) == response

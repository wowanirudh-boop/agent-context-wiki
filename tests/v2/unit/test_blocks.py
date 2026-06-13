from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from core.models import BlockStatus, BlockType

ELLIPSIS = "\u2026"
PENDING = "\u26a0 pending review: RR-run_test-1"
GENERATED_DASH = "\u2014"

CANONICAL_BLOCK = (
    f'<!-- cb {{"id":"cb_01jf8z{ELLIPSIS}","key":"refunds.window_days","type":"rule",'
    f'"status":"current","source_path":"docs/tnc_v2.pdf","source_date":"2025-11-02",'
    f'"chunks":["ch_01jf8y{ELLIPSIS}"],"user_edited":false}} -->\n'
    "**Refund window:** Refunds are accepted within 30 days of delivery.\n"
    '> Excerpt (docs/tnc_v2.pdf, 2025-11-02): "Customers may request a refund within thirty (30) days of the delivery date."\n'
    f"<!-- /cb cb_01jf8z{ELLIPSIS} -->"
)


def test_fr_block_03_canonical_example_parses_and_reserializes_byte_identically() -> None:
    from core.blocks.model import BlockSegment
    from core.blocks.parser import parse_page
    from core.blocks.serializer import serialize_page

    page = parse_page(CANONICAL_BLOCK)

    assert serialize_page(page) == CANONICAL_BLOCK
    assert len(page.segments) == 1
    assert isinstance(page.segments[0], BlockSegment)
    block = page.segments[0].block
    assert block.id == f"cb_01jf8z{ELLIPSIS}"
    assert block.key == "refunds.window_days"
    assert block.type == BlockType.rule
    assert block.status == BlockStatus.current
    assert block.source_path == "docs/tnc_v2.pdf"
    assert block.source_date == "2025-11-02"
    assert block.chunk_ids == [f"ch_01jf8y{ELLIPSIS}"]
    assert block.user_edited is False
    assert block.content == "**Refund window:** Refunds are accepted within 30 days of delivery."
    assert block.excerpt == '"Customers may request a refund within thirty (30) days of the delivery date."'


def test_fr_block_01_context_block_schema_and_status_serialization() -> None:
    from core.blocks.model import BlockSegment, Page
    from core.blocks.serializer import BlockSerializationError, serialize_page

    block = _block(status=BlockStatus.needs_review, needs_review_reason="source_deleted")

    serialized = serialize_page(Page([BlockSegment(block)]))

    assert '"needs_review_reason":"source_deleted"' in serialized
    for status in (BlockStatus.rejected, BlockStatus.deleted):
        with pytest.raises(BlockSerializationError):
            serialize_page(Page([BlockSegment(_block(status=status))]))


def test_fr_block_04_parser_preserves_prose_nested_comments_and_other_close_markers() -> None:
    from core.blocks.parser import parse_page
    from core.blocks.serializer import serialize_page

    markdown = (
        "# Refunds\n\n"
        "<!-- ordinary html comment -->\n"
        "Prose before.\n\n"
        '<!-- cb {"id":"cb_a","key":"refunds.window_days","type":"rule","status":"current",'
        '"source_path":"docs/source.md","source_date":"unknown","chunks":["ch_a"],"user_edited":false} -->\n'
        "Nested comment is allowed: <!-- /cb cb_other --> and <!-- note -->.\n"
        "> quoted content is body, not excerpt\n"
        "> Excerpt (docs/source.md): verbatim excerpt\n"
        "<!-- /cb cb_a -->\n\n"
        "Prose after.\n"
    )

    assert serialize_page(parse_page(markdown)) == markdown


def test_fr_block_04_mutations_preserve_untouched_bytes_and_insert_in_canonical_order() -> None:
    from core.blocks.mutations import insert_block
    from core.blocks.parser import parse_page
    from core.blocks.serializer import serialize_block, serialize_page

    original = "# Refunds\n\n## Summary\n\nExisting summary.\n\n## Source Coverage\n\nold coverage\n"
    block = _block(block_id="cb_rules", key="refunds.window_days")

    page = insert_block(parse_page(original), "Rules", block)

    expected = (
        "# Refunds\n\n## Summary\n\nExisting summary.\n\n"
        "## Rules\n\n"
        f"{serialize_block(block)}\n\n"
        "## Source Coverage\n\nold coverage\n"
    )
    assert serialize_page(page) == expected


def test_fr_block_04_insert_existing_section_keeps_following_section_bytes_unchanged() -> None:
    from core.blocks.mutations import insert_block
    from core.blocks.parser import parse_page
    from core.blocks.serializer import serialize_block, serialize_page

    original = "# Refunds\n\n## Rules\n\nManual note.\n\n## API Details\n\nGET /refunds\n"
    block = _block(block_id="cb_api", key="refunds.api.timeout", block_type=BlockType.api)

    page = insert_block(parse_page(original), "Rules", block)

    expected = "# Refunds\n\n## Rules\n\nManual note.\n\n" f"{serialize_block(block)}\n\n" "## API Details\n\nGET /refunds\n"
    assert serialize_page(page) == expected


def test_fr_block_04_atomic_write_refuses_changed_file(tmp_path: Path) -> None:
    from core.blocks.mutations import insert_block
    from core.blocks.parser import parse_page
    from core.blocks.serializer import PageWriteConflict, serialize_page, write_page

    path = tmp_path / "refunds.md"
    original = "# Refunds\n"
    path.write_text(original, encoding="utf-8")
    page = insert_block(parse_page(original), "Rules", _block())

    write_page(path, page, expected_text=original)
    assert path.read_text(encoding="utf-8") == serialize_page(page)

    path.write_text(original + "user edit\n", encoding="utf-8")
    with pytest.raises(PageWriteConflict):
        write_page(path, page, expected_text=original)


def test_fr_block_04_pending_marker_insert_remove_is_inside_block_body() -> None:
    from core.blocks.mutations import add_pending_marker, remove_pending_marker
    from core.blocks.parser import parse_page
    from core.blocks.serializer import serialize_page

    page = parse_page(CANONICAL_BLOCK)

    marked = add_pending_marker(page, f"cb_01jf8z{ELLIPSIS}", "RR-run_test-1")
    marked_again = add_pending_marker(marked, f"cb_01jf8z{ELLIPSIS}", "RR-run_test-1")

    assert serialize_page(marked_again).count(PENDING) == 1
    assert (
        "**Refund window:** Refunds are accepted within 30 days of delivery.\n"
        f"{PENDING}\n"
        "> Excerpt"
    ) in serialize_page(marked)
    assert serialize_page(remove_pending_marker(marked, f"cb_01jf8z{ELLIPSIS}", "RR-run_test-1")) == CANONICAL_BLOCK


def test_fr_block_04_generated_section_marker_replaces_only_that_section() -> None:
    from core.blocks.mutations import set_generated_section
    from core.blocks.parser import parse_page
    from core.blocks.serializer import serialize_page

    original = "# Refunds\n\n## Source Coverage\n\nmanual coverage\n\n## Related Pages\n\n- [[Orders]]\n"

    page = set_generated_section(parse_page(original), "Source Coverage", "- docs/source.md: 1 of 1", run_id="run_test")

    assert serialize_page(page) == (
        "# Refunds\n\n"
        "## Source Coverage\n\n"
        f"<!-- acw:generated Source Coverage run=run_test {GENERATED_DASH} manual edits will be overwritten -->\n"
        "- docs/source.md: 1 of 1\n\n"
        "## Related Pages\n\n"
        "- [[Orders]]\n"
    )


def test_fr_block_04_replace_content_refuses_user_edited_block() -> None:
    from core.blocks.model import BlockSegment, Page
    from core.blocks.mutations import UserEditedBlockError, replace_block_content

    block = _block(user_edited=True)
    page = Page([BlockSegment(block)])

    with pytest.raises(UserEditedBlockError):
        replace_block_content(page, block.id, content="Changed by system")


def test_fr_block_04_set_status_updates_metadata_without_touching_body() -> None:
    from core.blocks.model import BlockSegment, Page
    from core.blocks.mutations import set_status
    from core.blocks.serializer import serialize_page

    page = Page([BlockSegment(_block(content="Body stays put.", excerpt="Body evidence."))])

    updated = set_status(page, "cb_test", BlockStatus.needs_review, needs_review_reason="source_no_longer_contains")
    serialized = serialize_page(updated)

    assert "Body stays put." in serialized
    assert '"status":"needs_review"' in serialized
    assert '"needs_review_reason":"source_no_longer_contains"' in serialized


def test_fr_block_04_close_marker_injection_rejected_for_content_and_excerpt() -> None:
    marker = "<!-- /cb cb_test -->"

    with pytest.raises(ValidationError):
        _block(content=f"bad {marker}")
    with pytest.raises(ValidationError):
        _block(excerpt=f"bad {marker}")


def test_fr_block_06_semantic_key_grammar_and_per_page_inventory() -> None:
    from core.blocks.keys import SemanticKeyError, key_inventory, validate_semantic_key
    from core.blocks.model import BlockSegment, Page

    valid = [
        "refunds.window_days",
        "agent_studio.nodes.api_call.timeout",
        "a.b",
        "one.two.three.four.five.six",
    ]
    invalid = [
        "Refunds.window_days",
        "refunds",
        "refunds.",
        "refunds.window-days",
        "1refunds.window",
        "one.two.three.four.five.six.seven",
    ]

    for key in valid:
        assert validate_semantic_key(key) == key
    for key in invalid:
        with pytest.raises(SemanticKeyError):
            validate_semantic_key(key)

    page = Page(
        [
            BlockSegment(_block(block_id="cb_b", key="refunds.window_days")),
            BlockSegment(_block(block_id="cb_a", key="agent_studio.nodes.api_call.timeout")),
            BlockSegment(_block(block_id="cb_c", key="refunds.window_days")),
        ],
    )
    assert key_inventory(page) == ["agent_studio.nodes.api_call.timeout", "refunds.window_days"]


@settings(max_examples=200, deadline=None)
@given(page=st.deferred(lambda: _page_strategy()))
def test_fr_block_04_round_trip_property_byte_identical_with_adversarial_content(page) -> None:
    from core.blocks.parser import parse_page
    from core.blocks.serializer import serialize_page

    markdown = serialize_page(page)

    assert serialize_page(parse_page(markdown)) == markdown


def _block(
    *,
    block_id: str = "cb_test",
    key: str = "refunds.window_days",
    block_type: BlockType = BlockType.rule,
    status: BlockStatus = BlockStatus.current,
    needs_review_reason: str | None = None,
    source_path: str = "docs/source.md",
    source_date: str = "unknown",
    chunk_ids: list[str] | None = None,
    user_edited: bool = False,
    content: str = "**Refund window:** Refunds are accepted within 30 days.",
    excerpt: str = "Customers may request a refund within thirty (30) days.",
):
    from core.blocks.model import ContextBlock

    return ContextBlock(
        id=block_id,
        key=key,
        type=block_type,
        status=status,
        needs_review_reason=needs_review_reason,
        source_id="doc_1",
        source_path=source_path,
        source_date=source_date,
        chunk_ids=chunk_ids or ["ch_test"],
        user_edited=user_edited,
        content=content,
        excerpt=excerpt,
    )


def _page_strategy():
    from core.blocks.model import BlockSegment, Page, ProseSegment

    return st.builds(
        lambda before, block, after: Page([ProseSegment(before), BlockSegment(block), ProseSegment(after)]),
        before=_prose_strategy(),
        block=_block_strategy(),
        after=_prose_strategy(),
    )


def _block_strategy():
    return st.builds(
        _block,
        block_id=st.integers(min_value=1, max_value=999_999).map(lambda value: f"cb_prop_{value}"),
        key=_key_strategy(),
        block_type=st.sampled_from(list(BlockType)),
        status=st.sampled_from([BlockStatus.current, BlockStatus.needs_review, BlockStatus.conflicted, BlockStatus.deprecated]),
        needs_review_reason=st.none(),
        source_path=_safe_line_strategy(min_size=1),
        source_date=st.sampled_from(["unknown", "2025-11-02", "2026-06-13"]),
        chunk_ids=st.lists(st.integers(min_value=1, max_value=999_999).map(lambda value: f"ch_prop_{value}"), min_size=1, max_size=3),
        user_edited=st.booleans(),
        content=_body_strategy(),
        excerpt=_excerpt_strategy(),
    )


def _key_strategy():
    segment = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
    return st.lists(segment, min_size=2, max_size=6).map(".".join)


def _prose_strategy():
    return st.lists(_adversarial_line_strategy(), min_size=0, max_size=6).map(_join_lines)


def _body_strategy():
    return st.lists(_adversarial_line_strategy(), min_size=1, max_size=6).map(_join_lines)


def _excerpt_strategy():
    return st.lists(_safe_line_strategy(), min_size=1, max_size=4).map(_join_lines)


def _adversarial_line_strategy():
    return st.one_of(
        _safe_line_strategy(),
        st.sampled_from(
            [
                "<!-- nested html comment -->",
                "<!-- /cb cb_other -->",
                "> quoted line",
                "> Excerpt-looking body line",
                "```",
                "```python",
                "value with unicode snowman \u2603 and devanagari \u0905",
                "## Heading-like prose",
            ],
        ),
    )


def _safe_line_strategy(*, min_size: int = 0):
    alphabet = st.characters(blacklist_characters="\n\r<>", blacklist_categories=("Cs",))
    return st.text(alphabet=alphabet, min_size=min_size, max_size=60)


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines)

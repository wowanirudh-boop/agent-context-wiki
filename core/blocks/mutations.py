from __future__ import annotations

from core.blocks.model import (
    CANONICAL_SECTIONS,
    BlockSegment,
    ContextBlock,
    Heading,
    Page,
    Position,
    ProseSegment,
)
from core.models import BlockStatus


class BlockMutationError(ValueError):
    pass


class UserEditedBlockError(BlockMutationError):
    pass


def insert_block(page: Page, section: str, block: ContextBlock) -> Page:
    _validate_section(section)
    headings = _find_headings(page)
    existing = _heading_by_name(headings, section)
    if existing is not None:
        position = _section_end(page, headings, existing)
        return _insert_block_at(page, position, block)

    position = _new_section_position(page, headings, section)
    heading_text = _prefix_for_insert(page, position) + f"## {section}\n\n"
    return _insert_at(page, position, [ProseSegment(heading_text), BlockSegment(block), ProseSegment("\n\n")])


def set_status(
    page: Page,
    block_id: str,
    status: BlockStatus,
    *,
    needs_review_reason: str | None = None,
) -> Page:
    def update(block: ContextBlock) -> ContextBlock:
        reason = needs_review_reason if status == BlockStatus.needs_review else None
        return _copy_block(block, status=status, needs_review_reason=reason)

    return _update_block(page, block_id, update)


def replace_block_content(
    page: Page,
    block_id: str,
    *,
    content: str,
    excerpt: str | None = None,
) -> Page:
    def update(block: ContextBlock) -> ContextBlock:
        _refuse_user_edited(block)
        changes: dict[str, object] = {"content": content}
        if excerpt is not None:
            changes["excerpt"] = excerpt
        return _copy_block(block, **changes)

    return _update_block(page, block_id, update)


def add_pending_marker(page: Page, block_id: str, review_row_id: str) -> Page:
    def update(block: ContextBlock) -> ContextBlock:
        _refuse_user_edited(block)
        if review_row_id in block.pending_review_ids:
            return block
        return _copy_block(block, pending_review_ids=[*block.pending_review_ids, review_row_id])

    return _update_block(page, block_id, update)


def remove_pending_marker(page: Page, block_id: str, review_row_id: str | None = None) -> Page:
    def update(block: ContextBlock) -> ContextBlock:
        _refuse_user_edited(block)
        if review_row_id is None:
            pending_review_ids: list[str] = []
        else:
            pending_review_ids = [row_id for row_id in block.pending_review_ids if row_id != review_row_id]
        return _copy_block(block, pending_review_ids=pending_review_ids)

    return _update_block(page, block_id, update)


def generated_section_marker(section: str, run_id: str) -> str:
    return f"<!-- acw:generated {section} run={run_id} \u2014 manual edits will be overwritten -->"


def set_generated_section(page: Page, section: str, body: str, *, run_id: str) -> Page:
    _validate_section(section)
    headings = _find_headings(page)
    existing = _heading_by_name(headings, section)
    replacement = f"\n{generated_section_marker(section, run_id)}\n{body}\n\n"
    if existing is None:
        position = _new_section_position(page, headings, section)
        heading_text = _prefix_for_insert(page, position) + f"## {section}\n"
        return _insert_at(page, position, [ProseSegment(heading_text + replacement)])
    return _replace_range(page, existing.line_end, _section_end(page, headings, existing), [ProseSegment(replacement)])


def _update_block(page: Page, block_id: str, update: object) -> Page:
    updated = []
    found = False
    for segment in page.segments:
        if isinstance(segment, BlockSegment) and segment.block.id == block_id:
            found = True
            updated.append(BlockSegment(update(segment.block)))  # type: ignore[operator]
        else:
            updated.append(segment)
    if not found:
        raise KeyError(block_id)
    return Page(updated)


def _copy_block(block: ContextBlock, **changes: object) -> ContextBlock:
    data = block.model_dump()
    data.update(changes)
    return ContextBlock(**data)


def _refuse_user_edited(block: ContextBlock) -> None:
    if block.user_edited:
        raise UserEditedBlockError(f"Block {block.id} is user-edited")


def _validate_section(section: str) -> None:
    if section not in CANONICAL_SECTIONS:
        raise BlockMutationError(f"Unknown canonical section: {section}")


def _insert_block_at(page: Page, position: Position, block: ContextBlock) -> Page:
    prefix = _prefix_for_insert(page, position)
    return _insert_at(page, position, [ProseSegment(prefix), BlockSegment(block), ProseSegment("\n\n")])


def _prefix_for_insert(page: Page, position: Position) -> str:
    before = _text_before_position(page, position)
    if not before or before.endswith("\n\n"):
        return ""
    if before.endswith("\n"):
        return "\n"
    return "\n\n"


def _new_section_position(page: Page, headings: list[Heading], section: str) -> Position:
    target_index = CANONICAL_SECTIONS.index(section)
    for heading in headings:
        if heading.name in CANONICAL_SECTIONS and CANONICAL_SECTIONS.index(heading.name) > target_index:
            return heading.start
    return _page_end(page)


def _section_end(page: Page, headings: list[Heading], heading: Heading) -> Position:
    index = headings.index(heading)
    if index + 1 < len(headings):
        return headings[index + 1].start
    return _page_end(page)


def _heading_by_name(headings: list[Heading], section: str) -> Heading | None:
    for heading in headings:
        if heading.name == section:
            return heading
    return None


def _find_headings(page: Page) -> list[Heading]:
    headings: list[Heading] = []
    for segment_index, segment in enumerate(page.segments):
        if not isinstance(segment, ProseSegment):
            continue
        offset = 0
        for line in segment.text.splitlines(keepends=True):
            stripped = line.rstrip("\r\n")
            if stripped.startswith("## ") and not stripped.startswith("### "):
                headings.append(
                    Heading(
                        name=stripped[3:].strip(),
                        start=Position(segment_index, offset),
                        line_end=Position(segment_index, offset + len(line)),
                    ),
                )
            offset += len(line)
    return headings


def _insert_at(page: Page, position: Position, inserted: list[ProseSegment | BlockSegment]) -> Page:
    if position.segment_index == len(page.segments):
        return Page([*page.segments, *inserted])

    segments = list(page.segments)
    segment = segments[position.segment_index]
    if not isinstance(segment, ProseSegment):
        if position.offset != 0:
            raise BlockMutationError("Cannot insert inside a block segment")
        return Page([*segments[: position.segment_index], *inserted, *segments[position.segment_index :]])

    before = segment.text[: position.offset]
    after = segment.text[position.offset :]
    replacement: list[ProseSegment | BlockSegment] = []
    if before:
        replacement.append(ProseSegment(before))
    replacement.extend(inserted)
    if after:
        replacement.append(ProseSegment(after))
    return Page([*segments[: position.segment_index], *replacement, *segments[position.segment_index + 1 :]])


def _replace_range(
    page: Page,
    start: Position,
    end: Position,
    replacement: list[ProseSegment | BlockSegment],
) -> Page:
    start_prefix = _prefix_segment(page, start)
    end_suffix = _suffix_segment(page, end)
    before = list(page.segments[: start.segment_index])
    after_index = end.segment_index + 1 if end.segment_index < len(page.segments) else end.segment_index
    after = list(page.segments[after_index:])
    middle: list[ProseSegment | BlockSegment] = []
    if start_prefix:
        middle.append(ProseSegment(start_prefix))
    middle.extend(replacement)
    if end_suffix:
        middle.append(ProseSegment(end_suffix))
    return Page([*before, *middle, *after])


def _prefix_segment(page: Page, position: Position) -> str:
    if position.segment_index >= len(page.segments):
        return ""
    segment = page.segments[position.segment_index]
    if not isinstance(segment, ProseSegment):
        return ""
    return segment.text[: position.offset]


def _suffix_segment(page: Page, position: Position) -> str:
    if position.segment_index >= len(page.segments):
        return ""
    segment = page.segments[position.segment_index]
    if not isinstance(segment, ProseSegment):
        return ""
    return segment.text[position.offset :]


def _page_end(page: Page) -> Position:
    if not page.segments:
        return Position(0, 0)
    last_index = len(page.segments) - 1
    last = page.segments[last_index]
    if isinstance(last, ProseSegment):
        return Position(last_index, len(last.text))
    return Position(len(page.segments), 0)


def _text_before_position(page: Page, position: Position) -> str:
    text = []
    for index, segment in enumerate(page.segments):
        if index > position.segment_index:
            break
        if isinstance(segment, BlockSegment):
            if index < position.segment_index:
                text.append("\n")
            continue
        if index == position.segment_index:
            text.append(segment.text[: position.offset])
        else:
            text.append(segment.text)
    return "".join(text)

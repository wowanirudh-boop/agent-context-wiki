from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from core.blocks.model import BlockSegment, ContextBlock, Page
from core.blocks.mutations import (
    add_pending_marker,
    insert_block,
    move_block_to_section,
    remove_pending_marker,
    set_generated_section,
    set_status,
)
from core.blocks.parser import parse_page
from core.blocks.serializer import write_page
from core.config import load_config
from core.conflicts.markers import open_conflicts_body
from core.coverage import render_coverage_sections
from core.db.dao import ACWDao, Row, json_dumps, utc_now
from core.db.migrate import apply_migrations
from core.gitops import commit_apply_decisions, ensure_wiki_repo, mark_user_edited_blocks
from core.ids import new_id
from core.ledger import ChunkLedger
from core.llm.calls import C4_SCHEMA, complete_validated, validate_c4_response
from core.llm.provider import LLMProvider, provider_from_config
from core.lock import WorkspaceLock
from core.models import BlockStatus, EventKind, ReviewDecision, ReviewRowKind, RunStatus
from core.registry import PageRegistry
from core.review.parse import ParsedReviewFile, ParsedReviewRow, parse_review_file
from core.summary import render_summaries_and_index


@dataclass(frozen=True, slots=True)
class ApplyReviewResult:
    review_path: Path
    applied_rows: int
    follow_up_rows: int


@dataclass(frozen=True, slots=True)
class ApplyDecisionsResult:
    review_files: list[Path]
    applied_rows: int
    follow_up_rows: int
    committed: bool
    commit_message: str


@dataclass(slots=True)
class _ReviewContext:
    workspace: Path
    db: aiosqlite.Connection
    dao: ACWDao
    ledger: ChunkLedger
    provider: LLMProvider
    follow_up_rows: int = 0
    changed_pages: set[str] = field(default_factory=set)
    affected_chunks: set[str] = field(default_factory=set)
    duplicate_targets: dict[str, str] = field(default_factory=dict)


class ReviewValidationError(ValueError):
    pass


async def apply_decisions(
    workspace: str | Path,
    *,
    review_paths: list[str | Path] | None = None,
    provider: LLMProvider | None = None,
) -> ApplyDecisionsResult:
    ws = Path(workspace)
    with WorkspaceLock(ws, "apply-decisions") as lock:
        return await _apply_decisions_locked(ws, review_paths=review_paths, provider=provider, lock_stolen=lock.stolen)


async def _apply_decisions_locked(
    ws: Path,
    *,
    review_paths: list[str | Path] | None = None,
    provider: LLMProvider | None = None,
    lock_stolen: bool = False,
) -> ApplyDecisionsResult:
    db_path = ws / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await apply_migrations(db)
        dao = ACWDao(db)
        await dao.write_event(
            kind=EventKind.lock_stolen if lock_stolen else EventKind.lock_acquired,
            actor="core.lock",
            payload={"op": "apply-decisions"},
        )
        llm = provider or provider_from_config(ws, dao=dao)
        ensure_wiki_repo(ws)
        await mark_user_edited_blocks(ws, db)
        paths = _review_paths(ws, review_paths)
        applied = 0
        follow_ups = 0
        for path in paths:
            result = await apply_review_file(ws, db, path, provider=llm)
            applied += result.applied_rows
            follow_ups += result.follow_up_rows
        await render_coverage_sections(ws, db, run_id="apply-decisions")
        await render_summaries_and_index(ws, db, run_id="apply-decisions", provider=llm, config=load_config(ws))
        await ChunkLedger(dao).export_json(ws, run_id="apply-decisions")
        await PageRegistry(dao).export_json(ws)
    commit = commit_apply_decisions(ws, [path.name for path in paths])
    return ApplyDecisionsResult(
        review_files=paths,
        applied_rows=applied,
        follow_up_rows=follow_ups,
        committed=commit.committed,
        commit_message=commit.message,
    )


async def apply_review_file(
    workspace: str | Path,
    db: aiosqlite.Connection,
    review_path: str | Path,
    *,
    provider: LLMProvider,
) -> ApplyReviewResult:
    path = Path(review_path)
    parsed = parse_review_file(path.read_text(encoding="utf-8"))
    dao = ACWDao(db)
    rows = await _validate_review_file(dao, parsed)
    ctx = _ReviewContext(workspace=Path(workspace), db=db, dao=dao, ledger=ChunkLedger(dao), provider=provider)
    applied = 0
    for parsed_row in parsed.rows:
        if parsed_row.decision is None:
            continue
        db_row = rows[parsed_row.id]
        if db_row["applied_at"] is not None:
            continue
        await _store_decision(ctx, parsed_row)
        if parsed_row.decision == ReviewDecision.needs_more_info.value:
            await _write_decision_event(ctx, parsed_row, applied=False)
            continue
        await _apply_row(ctx, db_row, parsed_row)
        await _mark_applied(ctx, parsed_row.id)
        await _write_decision_event(ctx, parsed_row, applied=True)
        applied += 1
    await _finalize_candidate_dispositions(ctx)
    for page_path in sorted(ctx.changed_pages):
        page_row = await PageRegistry(ctx.dao).resolve_page(page_path)
        if page_row is None or page_row["status"] != "active":
            continue
        page_path_abs = ctx.workspace / page_path
        old_text = page_path_abs.read_text(encoding="utf-8")
        body = await open_conflicts_body(db, str(page_row["id"]))
        if "## Open Conflicts" in old_text or body != "_No open conflicts._":
            updated = set_generated_section(parse_page(old_text), "Open Conflicts", body, run_id=parsed.run_id)
            write_page(page_path_abs, updated, expected_text=old_text)
    return ApplyReviewResult(review_path=path, applied_rows=applied, follow_up_rows=ctx.follow_up_rows)


async def _validate_review_file(dao: ACWDao, parsed: ParsedReviewFile) -> dict[str, Row]:
    errors: list[str] = []
    rows: dict[str, Row] = {}
    for parsed_row in parsed.rows:
        if parsed_row.id in rows:
            errors.append(f"{parsed_row.id}: duplicate review row")
        for error in parsed_row.validation_errors:
            errors.append(f"{parsed_row.id}: {error}")
        db_row = await dao.get_review_row(parsed_row.id)
        if db_row is None:
            errors.append(f"{parsed_row.id}: unknown review row")
        else:
            if parsed_row.row_kind != db_row["row_kind"]:
                errors.append(f"{parsed_row.id}: row kind differs from database")
            if parsed.run_id != db_row["run_id"]:
                errors.append(f"{parsed_row.id}: run differs from database")
            if db_row["row_kind"] == "needs_review" and db_row["candidate_json"] is None and parsed_row.decision not in {
                None, "deprecate_existing", "keep_existing", "needs_more_info",
            }:
                errors.append(f"{parsed_row.id}: source staleness accepts deprecate_existing, keep_existing, or needs_more_info")
            rows[parsed_row.id] = db_row
    if errors:
        raise ReviewValidationError("\n".join(errors))
    return rows


async def _apply_row(ctx: _ReviewContext, row: Row, parsed_row: ParsedReviewRow) -> None:
    if row["row_kind"] == ReviewRowKind.taxonomy_merge.value:
        if parsed_row.decision == ReviewDecision.merge.value:
            from core.taxonomy import _merge_pages, _TaxonomyContext

            peer = _candidate_json(row)
            taxonomy = _TaxonomyContext(ctx.workspace, ctx.db, ctx.dao, PageRegistry(ctx.dao))
            await _merge_pages(taxonomy, str(row["page_id"]), str(peer["page_id"]))
            ctx.changed_pages.update(taxonomy.changed_pages)
        return
    if row["row_kind"] == ReviewRowKind.exclusion.value:
        await _apply_exclusion(ctx, row, parsed_row)
        return
    if row["row_kind"] == ReviewRowKind.needs_review.value and row["candidate_json"] is None:
        await _apply_staleness(ctx, row, parsed_row)
        return
    decision = ReviewDecision(parsed_row.decision)
    if decision == ReviewDecision.accept_new:
        await _accept_new(ctx, row)
    elif decision == ReviewDecision.keep_existing:
        await _keep_existing(ctx, row, remove_marker=True)
    elif decision == ReviewDecision.merge:
        await _merge(ctx, row, parsed_row.notes)
    elif decision == ReviewDecision.mark_conflicted:
        await _mark_conflicted(ctx, row)
    elif decision == ReviewDecision.deprecate_existing:
        await _deprecate_existing(ctx, row)
    elif decision == ReviewDecision.reject_new:
        await _keep_existing(ctx, row, remove_marker=True)
    elif decision == ReviewDecision.delete_duplicate:
        await _delete_duplicate(ctx, row)


async def _apply_staleness(ctx: _ReviewContext, row: Row, parsed_row: ParsedReviewRow) -> None:
    page_row, page, old_text = await _load_page(ctx, row)
    status = BlockStatus.current if parsed_row.decision == "keep_existing" else BlockStatus.deprecated
    updated = set_status(page, str(row["existing_block_id"]), status)
    if status == BlockStatus.deprecated:
        updated = move_block_to_section(updated, str(row["existing_block_id"]), "Deprecated")
    await _update_block_status(ctx, str(row["existing_block_id"]), status)
    await _write_page(ctx, page_row, updated, old_text)


async def _apply_exclusion(ctx: _ReviewContext, row: Row, parsed_row: ParsedReviewRow) -> None:
    decision = ReviewDecision(parsed_row.decision)
    data = _candidate_json(row)
    if not isinstance(data, dict):
        raise ValueError(f"Review row {row['id']} has no exclusion candidate_json")
    chunk_ids = [str(chunk_id) for chunk_id in data.get("chunk_ids", [])]
    reason = str(data.get("reason") or "approved exclusion")
    if decision == ReviewDecision.approve_exclusion:
        for chunk_id in chunk_ids:
            await ctx.ledger.mark_irrelevant(chunk_id, reason=reason)
    elif decision == ReviewDecision.reinstate:
        for chunk_id in chunk_ids:
            await ctx.ledger.mark_pending(chunk_id)


async def _accept_new(ctx: _ReviewContext, row: Row) -> None:
    page_row, page, old_text = await _load_page(ctx, row)
    existing = _find_block(page, str(row["existing_block_id"]))
    candidate = _candidate_block(row)
    updated = page
    if existing.key == candidate.key:
        updated = set_status(updated, existing.id, BlockStatus.deprecated)
        updated = move_block_to_section(updated, existing.id, "Deprecated")
        await _update_block_status(ctx, existing.id, BlockStatus.deprecated)
    updated = remove_pending_marker(updated, existing.id, str(row["id"]))
    updated = insert_block(updated, _section_for(candidate), candidate)
    await _create_block_and_place_chunks(ctx, page_row, candidate)
    await _write_page(ctx, page_row, updated, old_text)


async def _deprecate_existing(ctx: _ReviewContext, row: Row) -> None:
    page_row, page, old_text = await _load_page(ctx, row)
    existing = _find_block(page, str(row["existing_block_id"]))
    candidate = _candidate_block(row)
    updated = set_status(page, existing.id, BlockStatus.deprecated)
    updated = move_block_to_section(updated, existing.id, "Deprecated")
    updated = remove_pending_marker(updated, existing.id, str(row["id"]))
    updated = insert_block(updated, _section_for(candidate), candidate)
    await _update_block_status(ctx, existing.id, BlockStatus.deprecated)
    await _create_block_and_place_chunks(ctx, page_row, candidate)
    await _write_page(ctx, page_row, updated, old_text)


async def _keep_existing(ctx: _ReviewContext, row: Row, *, remove_marker: bool) -> None:
    page_row, page, old_text = await _load_page(ctx, row)
    existing = _find_block(page, str(row["existing_block_id"]))
    candidate = _candidate_block(row)
    if remove_marker:
        updated = remove_pending_marker(page, existing.id, str(row["id"]))
        await _write_page(ctx, page_row, updated, old_text)
    await _mark_candidate_chunks_duplicate(ctx, candidate, existing.id)


async def _merge(ctx: _ReviewContext, row: Row, notes: str) -> None:
    approved = _approved_merge_text(notes)
    if approved is not None:
        await _apply_approved_merge(ctx, row, approved)
        return

    page_row, page, old_text = await _load_page(ctx, row)
    existing = _find_block(page, str(row["existing_block_id"]))
    candidate = _candidate_block(row)
    response = await complete_validated(
        ctx.provider,
        "C4",
        {
            "a_existing_content": existing.content,
            "b_candidate_content": candidate.content,
            "existing": existing.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
        },
        C4_SCHEMA,
        validate_c4_response,
    )
    updated = set_status(page, existing.id, BlockStatus.deprecated)
    updated = move_block_to_section(updated, existing.id, "Deprecated")
    updated = remove_pending_marker(updated, existing.id, str(row["id"]))
    await _update_block_status(ctx, existing.id, BlockStatus.deprecated)
    follow_up = _merged_candidate(existing, candidate, str(response["content"]))
    follow_up_id = await _create_follow_up_review_row(ctx, row, existing.id, follow_up)
    updated = add_pending_marker(updated, existing.id, follow_up_id)
    await _write_page(ctx, page_row, updated, old_text)
    from core.review.emit import emit_review_file

    await emit_review_file(ctx.workspace, ctx.db, f"{row['run_id']}_followup")


async def _apply_approved_merge(ctx: _ReviewContext, row: Row, content: str) -> None:
    page_row, page, old_text = await _load_page(ctx, row)
    existing = _find_block(page, str(row["existing_block_id"]))
    candidate = _candidate_block(row)
    merged = _merged_candidate(existing, candidate, content)
    updated = set_status(page, existing.id, BlockStatus.deprecated)
    updated = move_block_to_section(updated, existing.id, "Deprecated")
    updated = remove_pending_marker(updated, existing.id, str(row["id"]))
    updated = insert_block(updated, _section_for(merged), merged)
    await _update_block_status(ctx, existing.id, BlockStatus.deprecated)
    await _create_block_and_place_chunks(ctx, page_row, merged)
    await _write_page(ctx, page_row, updated, old_text)


async def _mark_conflicted(ctx: _ReviewContext, row: Row) -> None:
    page_row, page, old_text = await _load_page(ctx, row)
    existing = _find_block(page, str(row["existing_block_id"]))
    candidate = _candidate_block(row).model_copy(update={"status": BlockStatus.conflicted})
    updated = set_status(page, existing.id, BlockStatus.conflicted)
    updated = remove_pending_marker(updated, existing.id, str(row["id"]))
    updated = insert_block(updated, "Rules", candidate)
    updated = set_generated_section(
        updated,
        "Open Conflicts",
        f"- {row['id']}: {row['conflict_type']} on `{existing.key}` (status: conflicted)",
        run_id=str(row["run_id"]),
    )
    await _update_block_status(ctx, existing.id, BlockStatus.conflicted)
    await _create_block_and_place_chunks(ctx, page_row, candidate)
    await _write_page(ctx, page_row, updated, old_text)


async def _delete_duplicate(ctx: _ReviewContext, row: Row) -> None:
    page_row, page, old_text = await _load_page(ctx, row)
    duplicate_id = str(row["existing_block_id"])
    duplicate = _find_block(page, duplicate_id)
    candidate = _candidate_block(row)
    duplicate_target = _duplicate_target(page, duplicate)
    updated = Page(
        [
            segment
            for segment in page.segments
            if not (isinstance(segment, BlockSegment) and segment.block.id == duplicate_id)
        ]
    )
    await _mark_existing_chunks_duplicate(ctx, duplicate_id, duplicate_target)
    for chunk_id in candidate.chunk_ids:
        await ctx.ledger.mark_duplicate(
            chunk_id,
            duplicate_of_block_id=duplicate_target,
            reason="delete_duplicate",
        )
    await _update_block_status(ctx, duplicate_id, BlockStatus.deleted)
    await _write_page(ctx, page_row, updated, old_text)


async def _load_page(ctx: _ReviewContext, row: Row) -> tuple[Row, Page, str]:
    page = await ctx.dao.get_page(str(row["page_id"]))
    if page is None:
        raise KeyError(str(row["page_id"]))
    path = ctx.workspace / str(page["path"])
    text = path.read_text(encoding="utf-8")
    return page, parse_page(text), text


async def _write_page(ctx: _ReviewContext, page: Row, model: Page, old_text: str) -> None:
    write_page(ctx.workspace / str(page["path"]), model, expected_text=old_text)
    ctx.changed_pages.add(str(page["path"]))


async def _create_block_and_place_chunks(ctx: _ReviewContext, page: Row, block: ContextBlock) -> None:
    await ctx.dao.create_block(
        block_id=block.id,
        page_id=str(page["id"]),
        key=block.key,
        block_type=block.type.value,
        status=block.status.value,
        source_id=str(block.source_id),
        source_path=block.source_path,
        source_date=block.source_date,
        content_hash=f"decision-{block.id}",
        created_at=utc_now(),
        updated_at=utc_now(),
        user_edited=block.user_edited,
    )
    for chunk_id in block.chunk_ids:
        await ctx.dao.link_block_chunk(block.id, chunk_id)
        ctx.affected_chunks.add(chunk_id)


async def _mark_candidate_chunks_duplicate(ctx: _ReviewContext, candidate: ContextBlock, existing_block_id: str) -> None:
    for chunk_id in candidate.chunk_ids:
        ctx.affected_chunks.add(chunk_id)
        ctx.duplicate_targets[chunk_id] = existing_block_id


async def _finalize_candidate_dispositions(ctx: _ReviewContext) -> None:
    unresolved = set()
    for row in await ctx.dao.list_review_rows(open_only=True):
        candidate = _candidate_json(row) or {}
        unresolved.update(candidate.get("chunk_ids", []))
    for chunk_id in ctx.affected_chunks - unresolved:
        chunk = await ctx.dao.get_chunk(chunk_id)
        if chunk["disposition"] != "conflicted_pending":
            continue
        cursor = await ctx.db.execute(
            "SELECT b.id FROM acw_block_chunks bc JOIN acw_blocks b ON b.id = bc.block_id "
            "WHERE bc.chunk_id = ? AND b.status NOT IN ('deleted', 'rejected')", (chunk_id,),
        )
        blocks = [str(row[0]) for row in await cursor.fetchall()]
        if blocks and chunk["disposition_reason"] == "intra_transcript_supersession":
            await ctx.ledger.mark_duplicate(chunk_id, duplicate_of_block_id=blocks[0], reason="intra_transcript_supersession")
        elif blocks:
            await ctx.ledger.mark_placed(chunk_id, block_ids=blocks)
        else:
            await ctx.ledger.mark_duplicate(chunk_id, duplicate_of_block_id=ctx.duplicate_targets[chunk_id], reason="rejected_candidate")


async def _mark_existing_chunks_duplicate(ctx: _ReviewContext, block_id: str, duplicate_of_block_id: str) -> None:
    for chunk_id in await _block_chunk_ids(ctx.db, block_id):
        await ctx.db.execute(
            "UPDATE acw_chunk_ledger SET disposition = 'duplicate', disposition_reason = ?, "
            "duplicate_of_block_id = ?, updated_at = ? WHERE id = ?",
            ("delete_duplicate", duplicate_of_block_id, utc_now(), chunk_id),
        )
    await ctx.db.commit()


def _duplicate_target(page: Page, duplicate: ContextBlock) -> str:
    for block in page.blocks:
        if block.id != duplicate.id and block.key == duplicate.key:
            return block.id
    return duplicate.id


async def _create_follow_up_review_row(
    ctx: _ReviewContext,
    row: Row,
    existing_block_id: str,
    candidate: ContextBlock,
) -> str:
    run_id = f"{row['run_id']}_followup"
    if await ctx.dao.get_run(run_id) is None:
        await ctx.dao.create_run(run_id=run_id)
        await ctx.dao.finish_run(run_id, status=RunStatus.completed, stats={"source": "merge_followup"})
    created = await ctx.dao.create_review_row(
        run_id=run_id,
        page_id=str(row["page_id"]),
        row_kind=ReviewRowKind.needs_review.value,
        existing_block_id=existing_block_id,
        candidate_json=json_dumps(candidate.model_dump(mode="json")),
        conflict_type=str(row["conflict_type"]) if row["conflict_type"] else None,
        recommendation=ReviewDecision.needs_more_info.value,
        recommendation_basis=str(row["recommendation_basis"]) if row["recommendation_basis"] else None,
    )
    ctx.follow_up_rows += 1
    return str(created["id"])


async def _update_block_status(ctx: _ReviewContext, block_id: str, status: BlockStatus) -> None:
    await ctx.db.execute(
        "UPDATE acw_blocks SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, utc_now(), block_id),
    )
    await ctx.db.commit()


async def _store_decision(ctx: _ReviewContext, parsed_row: ParsedReviewRow) -> None:
    await ctx.db.execute(
        "UPDATE acw_review_rows SET decision = ?, notes = ? WHERE id = ?",
        (parsed_row.decision, parsed_row.notes, parsed_row.id),
    )
    await ctx.db.commit()


async def _mark_applied(ctx: _ReviewContext, row_id: str) -> None:
    await ctx.db.execute("UPDATE acw_review_rows SET applied_at = ? WHERE id = ?", (utc_now(), row_id))
    await ctx.db.commit()


async def _write_decision_event(ctx: _ReviewContext, parsed_row: ParsedReviewRow, *, applied: bool) -> None:
    await ctx.dao.write_event(
        kind=EventKind.decision_applied,
        actor="core.review.apply",
        payload={"row_id": parsed_row.id, "decision": parsed_row.decision, "applied": applied},
    )


async def _block_chunk_ids(db: aiosqlite.Connection, block_id: str) -> list[str]:
    cursor = await db.execute(
        "SELECT chunk_id FROM acw_block_chunks WHERE block_id = ? ORDER BY chunk_id",
        (block_id,),
    )
    return [str(row[0]) for row in await cursor.fetchall()]


def _candidate_block(row: Row) -> ContextBlock:
    data = _candidate_json(row)
    if data is None:
        raise ValueError(f"Review row {row['id']} has no candidate_json")
    if isinstance(data, dict) and isinstance(data.get("block"), dict):
        data = data["block"]
    return ContextBlock(**data)


def _candidate_json(row: Row) -> Any:
    raw = row.get("candidate_json")
    if not isinstance(raw, str) or not raw:
        return None
    return json.loads(raw)


def _find_block(page: Page, block_id: str) -> ContextBlock:
    for block in page.blocks:
        if block.id == block_id:
            return block
    raise KeyError(block_id)


def _section_for(block: ContextBlock) -> str:
    if block.status == BlockStatus.conflicted:
        return "Open Conflicts"
    if block.type.value == "api":
        return "API Details"
    if block.type.value == "faq":
        return "FAQs"
    if block.type.value == "requirement":
        return "Requirements"
    if block.type.value == "flow":
        return "Flow"
    if block.type.value in {"decision", "note"}:
        return "Decisions"
    return "Rules"


def _approved_merge_text(notes: str) -> str | None:
    prefix = "approved-merge:"
    if not notes.casefold().startswith(prefix):
        return None
    return notes[len(prefix) :].strip()


def _merged_candidate(existing: ContextBlock, candidate: ContextBlock, content: str) -> ContextBlock:
    return candidate.model_copy(
        update={
            "id": new_id("cb"),
            "status": BlockStatus.current,
            "content": content,
            "excerpt": f"{existing.excerpt}\n{candidate.excerpt}",
            "chunk_ids": list(dict.fromkeys([*existing.chunk_ids, *candidate.chunk_ids])),
            "pending_review_ids": [],
        }
    )


def _review_paths(workspace: Path, review_paths: list[str | Path] | None) -> list[Path]:
    if review_paths:
        return [Path(path) if Path(path).is_absolute() else workspace / path for path in review_paths]
    review_dir = workspace / "wiki" / "_reviews"
    if not review_dir.exists():
        return []
    return sorted(review_dir.glob("RR-*.md"))

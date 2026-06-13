from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from core.config import ACWConfig, load_config
from core.db.dao import ACWDao, Row
from core.db.migrate import apply_migrations
from core.gitops import commit_processing_run, ensure_wiki_repo, mark_user_edited_blocks
from core.ledger import ChunkLedger
from core.llm.calls import (
    C1_SCHEMA,
    C6_SCHEMA,
    CallValidationError,
    complete_validated,
    validate_c1_response,
    validate_c6_response,
)
from core.llm.provider import LLMProvider, provider_from_config
from core.models import EventKind, RunStatus
from core.pipeline.batching import batch_chunks_by_source
from core.pipeline.flows import extract_flow_mermaid
from core.pipeline.placement import PlacementWriter, placement_registry_payload
from core.pipeline.transcripts import apply_transcript_prepass, resolve_transcript_duplicate_placeholders
from core.registry import PageRegistry
from core.review.emit import create_taxonomy_merge_review_rows, emit_review_file, find_unresolved_review_files


@dataclass(frozen=True, slots=True)
class ProcessingRunResult:
    run_id: str
    stats: dict[str, Any]


async def run_processing_run(
    workspace: str | Path,
    *,
    provider: LLMProvider | None = None,
    config: ACWConfig | None = None,
) -> ProcessingRunResult:
    ws = Path(workspace)
    db_path = ws / ".llmwiki" / "index.db"
    cfg = config or load_config(ws)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await apply_migrations(db)
        ensure_wiki_repo(ws)
        dao = ACWDao(db)
        run = await dao.create_run()
        run_id = str(run["id"])
        await dao.write_event(kind=EventKind.run_started, actor="core.pipeline.run", payload={"run_id": run_id})
        user_edited_blocks = await mark_user_edited_blocks(ws, db)
        llm = provider or provider_from_config(ws, dao=dao)
        ledger = ChunkLedger(dao, max_attempts=cfg.max_attempts)
        registry = PageRegistry(dao)
        writer = PlacementWriter(ws, db, ledger, registry, run_id=run_id, provider=llm)
        unresolved_reviews = find_unresolved_review_files(ws)
        stats: dict[str, Any] = {
            "placed": 0,
            "duplicate": 0,
            "conflicted_pending": 0,
            "irrelevant": 0,
            "failed": 0,
            "llm_calls": 0,
            "page_writes": 0,
            "unresolved_reviews": len(unresolved_reviews),
            "unresolved_review_files": unresolved_reviews,
            "user_edited_blocks": len(user_edited_blocks),
        }
        calls_before = _provider_call_count(llm)
        try:
            await _mark_out_of_scope_chunks(db, ledger)
            worklist = await _worklist(db, max_attempts=cfg.max_attempts)
            for chunk in worklist:
                if chunk["disposition"] == "failed":
                    await ledger.mark_pending_for_retry(str(chunk["id"]))

            worklist = await _worklist(db, max_attempts=cfg.max_attempts)
            had_work = bool(worklist)
            for batch in batch_chunks_by_source(worklist, max_chunks=cfg.batch_max_chunks):
                processed = await _process_batch(db, ledger, registry, writer, llm, batch, ws)
                for key, value in processed.items():
                    stats[key] = int(stats.get(key, 0)) + value

            if had_work:
                await create_taxonomy_merge_review_rows(db, run_id)
                review_path = await emit_review_file(ws, db, run_id)
                if review_path is not None:
                    stats["review_file"] = review_path.relative_to(ws).as_posix()
            pending = await _pending_count(db)
            stats["pending"] = pending
            stats["page_writes"] = writer.page_writes
            stats["blocks_created"] = writer.blocks_created
            stats["llm_calls"] = _provider_call_count(llm) - calls_before
            await ledger.export_json(ws, run_id=run_id)
            await registry.export_json(ws)
            if pending:
                raise RuntimeError(f"Processing run left {pending} pending chunk(s)")
            await dao.finish_run(run_id, status=RunStatus.completed, stats=stats)
            await dao.write_event(kind=EventKind.run_completed, actor="core.pipeline.run", payload={"run_id": run_id})
            commit = commit_processing_run(ws, run_id)
            if commit.committed:
                await dao.write_event(
                    kind=EventKind.git_commit,
                    actor="core.gitops",
                    payload={"trigger": "process", "run_id": run_id, "sha": commit.sha, "message": commit.message},
                )
            return ProcessingRunResult(run_id=run_id, stats=stats)
        except Exception as exc:
            await dao.finish_run(run_id, status=RunStatus.aborted, stats={**stats, "error": str(exc)})
            await dao.write_event(
                kind=EventKind.run_aborted,
                actor="core.pipeline.run",
                payload={"run_id": run_id, "error": str(exc)},
            )
            raise


async def _process_batch(
    db: aiosqlite.Connection,
    ledger: ChunkLedger,
    registry: PageRegistry,
    writer: PlacementWriter,
    provider: LLMProvider,
    batch: list[Row],
    workspace: Path,
) -> dict[str, int]:
    del workspace
    if not batch:
        return {}
    if all(chunk["source_kind"] == "transcript" for chunk in batch):
        batch = await _preprocess_transcript_batch(db, ledger, provider, batch)
        if not batch:
            return {"irrelevant": 0, "placed": 0, "duplicate": 0, "conflicted_pending": 0, "failed": 0}

    if all(chunk["source_kind"] == "flow" for chunk in batch):
        return await _process_flow_batch(ledger, writer, provider, batch)

    payload = {
        "workspace_domain_hint": "local workspace",
        "chunks": [_chunk_payload(chunk) for chunk in batch],
        "registry": placement_registry_payload(await registry.list_pages()),
        "page_context": await writer.page_context_payload(),
    }
    response = await complete_validated(provider, "C1", payload, C1_SCHEMA, validate_c1_response)
    counts = {"placed": 0, "duplicate": 0, "conflicted_pending": 0, "irrelevant": 0, "failed": 0}
    by_id = {str(chunk["id"]): chunk for chunk in batch}
    for item in response["chunks"]:
        chunk = by_id[str(item["chunk_id"])]
        if not item["relevant"]:
            await ledger.mark_irrelevant(str(chunk["id"]), reason=str(item["irrelevant_reason"]))
            counts["irrelevant"] += 1
            continue
        try:
            for placement in item["placements"]:
                outcome = await writer.write_candidate(
                    chunk,
                    placement,
                    default_transcript_type=chunk["source_kind"] == "transcript",
                )
                counts[outcome.kind] += 1
                if outcome.kind == "placed" and outcome.block_id is not None:
                    await resolve_transcript_duplicate_placeholders(
                        db,
                        survivor_chunk_id=str(chunk["id"]),
                        block_id=outcome.block_id,
                    )
        except (CallValidationError, KeyError, RuntimeError, ValueError) as exc:
            await ledger.mark_failed(str(chunk["id"]), reason=str(exc))
            counts["failed"] += 1
    return counts


async def _preprocess_transcript_batch(
    db: aiosqlite.Connection,
    ledger: ChunkLedger,
    provider: LLMProvider,
    batch: list[Row],
) -> list[Row]:
    payload = {"segments": [_chunk_payload(chunk) for chunk in batch]}
    response = await complete_validated(provider, "C6", payload, C6_SCHEMA, validate_c6_response)
    return await apply_transcript_prepass(db, ledger, batch, response)


async def _process_flow_batch(
    ledger: ChunkLedger,
    writer: PlacementWriter,
    provider: LLMProvider,
    batch: list[Row],
) -> dict[str, int]:
    counts = {"placed": 0, "duplicate": 0, "conflicted_pending": 0, "irrelevant": 0, "failed": 0}
    for chunk in batch:
        try:
            mermaid = await extract_flow_mermaid(provider, str(chunk["text"]), source_path=str(chunk["source_path"]))
            placement = {
                "page": None,
                "new_page": {
                    "title": _title_from_path(str(chunk["source_path"])),
                    "description": "Flow structure",
                    "domain": "flows",
                    "path_slug": _slug_from_path(str(chunk["source_path"])),
                    "no_registry_match_assertion": "Flow files create a dedicated flow page when no registry match exists.",
                },
                "section": "Flow",
                "block": {
                    "key": f"{_key_prefix_from_path(str(chunk['source_path']))}.flow",
                    "type": "flow",
                    "content": f"```mermaid\n{mermaid}\n```",
                    "excerpt": str(chunk["text"]),
                    "new_key_justification": "Flow source creates its structural key.",
                },
                "links": [],
            }
            outcome = await writer.write_candidate(chunk, placement)
            counts[outcome.kind] += 1
        except (CallValidationError, ValueError) as exc:
            await ledger.mark_failed(str(chunk["id"]), reason=str(exc))
            counts["failed"] += 1
    return counts


async def _worklist(db: aiosqlite.Connection, *, max_attempts: int) -> list[Row]:
    cursor = await db.execute(
        "SELECT cl.id, cl.source_id, cl.source_version_id, cl.document_chunk_id, cl.ordinal, "
        "cl.disposition, cl.attempts, d.relative_path AS source_path, d.metadata, "
        "COALESCE(dc.source_content, dc.content) AS text, dc.header_breadcrumb AS breadcrumb, "
        "COALESCE(sv.source_date, 'unknown') AS source_date "
        "FROM acw_chunk_ledger cl "
        "JOIN documents d ON d.id = cl.source_id "
        "LEFT JOIN document_chunks dc ON dc.id = cl.document_chunk_id "
        "LEFT JOIN acw_source_versions sv ON sv.id = cl.source_version_id "
        "WHERE d.source_kind = 'source' AND d.relative_path NOT LIKE 'eval/%' "
        "AND (cl.disposition = 'pending' OR (cl.disposition = 'failed' AND cl.attempts < ?)) "
        "ORDER BY d.relative_path, cl.ordinal",
        (max_attempts,),
    )
    rows = [
        dict(zip([description[0] for description in cursor.description], row, strict=True))
        for row in await cursor.fetchall()
    ]
    for row in rows:
        row["source_kind"] = _source_kind_from_metadata(row.get("metadata"))
    return rows


async def _pending_count(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM acw_chunk_ledger WHERE disposition = 'pending'")
    return int((await cursor.fetchone())[0])


async def _mark_out_of_scope_chunks(db: aiosqlite.Connection, ledger: ChunkLedger) -> None:
    cursor = await db.execute(
        "SELECT cl.id, cl.disposition FROM acw_chunk_ledger cl "
        "JOIN documents d ON d.id = cl.source_id "
        "WHERE (d.source_kind != 'source' OR d.relative_path LIKE 'eval/%') "
        "AND cl.disposition IN ('pending', 'failed')",
    )
    rows = await cursor.fetchall()
    for chunk_id, disposition in rows:
        if disposition == "failed":
            await ledger.mark_pending_for_retry(str(chunk_id))
        await ledger.mark_irrelevant(str(chunk_id), reason="not a placement source")


def _chunk_payload(chunk: Row) -> dict[str, Any]:
    return {
        "id": chunk["id"],
        "chunk_id": chunk["id"],
        "text": chunk["text"] or "",
        "breadcrumb": chunk.get("breadcrumb") or "",
        "source_path": chunk["source_path"],
    }


def _source_kind_from_metadata(metadata_raw: Any) -> str:
    import json

    if isinstance(metadata_raw, str):
        try:
            metadata = json.loads(metadata_raw) or {}
        except ValueError:
            metadata = {}
    elif isinstance(metadata_raw, dict):
        metadata = metadata_raw
    else:
        metadata = {}
    return str(metadata.get("acw_source_kind") or "doc")


def _slug_from_path(path: str) -> str:
    return Path(path).stem.replace("_", "-").replace(" ", "-").casefold()


def _title_from_path(path: str) -> str:
    return Path(path).stem.replace("_", " ").replace("-", " ").title()


def _key_prefix_from_path(path: str) -> str:
    cleaned = Path(path).stem.casefold().replace("-", "_")
    parts = [part for part in cleaned.split("_") if part]
    if len(parts) == 1:
        return f"{parts[0]}.definition"
    return ".".join(parts[:5])


def _provider_call_count(provider: LLMProvider) -> int:
    try:
        return int(provider.call_count)  # type: ignore[attr-defined]
    except AttributeError:
        return 0

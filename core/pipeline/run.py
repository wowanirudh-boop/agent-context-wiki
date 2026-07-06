from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from core.config import ACWConfig, load_config
from core.coverage import render_coverage_sections
from core.db.dao import ACWDao, Row, json_dumps
from core.db.migrate import apply_migrations
from core.gitops import commit_processing_run, ensure_wiki_repo, mark_user_edited_blocks
from core.ledger import ChunkLedger
from core.llm.calls import (
    C1_SCHEMA,
    C6_SCHEMA,
    C7_SCHEMA,
    CallValidationError,
    complete_validated,
    validate_c1_response,
    validate_c6_response,
    validate_c7_response,
)
from core.llm.provider import LLMProvider, provider_from_config
from core.lock import WorkspaceLock
from core.models import EventKind, RunStatus
from core.pipeline.batching import batch_chunks_by_source
from core.pipeline.flows import extract_flow_mermaid
from core.pipeline.placement import PlacementWriter, placement_registry_payload
from core.pipeline.transcripts import apply_transcript_prepass, resolve_transcript_duplicate_placeholders
from core.registry import PageRegistry
from core.review.emit import create_taxonomy_merge_review_rows, emit_review_file, find_unresolved_review_files
from core.summary import render_summaries_and_index


@dataclass(frozen=True, slots=True)
class ProcessingRunResult:
    run_id: str
    stats: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _IrrelevantCandidate:
    chunk: Row
    reason: str


async def run_processing_run(
    workspace: str | Path,
    *,
    provider: LLMProvider | None = None,
    config: ACWConfig | None = None,
) -> ProcessingRunResult:
    ws = Path(workspace)
    with WorkspaceLock(ws, "process") as lock:
        return await _run_processing_run_locked(ws, provider=provider, config=config, lock_stolen=lock.stolen)


async def _run_processing_run_locked(
    ws: Path,
    *,
    provider: LLMProvider | None = None,
    config: ACWConfig | None = None,
    lock_stolen: bool = False,
) -> ProcessingRunResult:
    db_path = ws / ".llmwiki" / "index.db"
    cfg = config or load_config(ws)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await apply_migrations(db)
        ensure_wiki_repo(ws)
        dao = ACWDao(db)
        await dao.write_event(
            kind=EventKind.lock_stolen if lock_stolen else EventKind.lock_acquired,
            actor="core.lock",
            payload={"op": "process"},
        )
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
            "exclusion_pending": 0,
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
            await _ensure_domain_hint(ws, registry, llm, worklist, run_id=run_id)
            had_work = bool(worklist)
            domain_hint = _workspace_domain_hint(ws)
            for source_rows in _source_groups(worklist):
                deferred_irrelevant: list[_IrrelevantCandidate] = []
                for batch in batch_chunks_by_source(source_rows, max_chunks=cfg.batch_max_chunks):
                    processed = await _process_batch(
                        db,
                        ledger,
                        registry,
                        writer,
                        llm,
                        batch,
                        ws,
                        domain_hint=domain_hint,
                        deferred_irrelevant=deferred_irrelevant,
                    )
                    for key, value in processed.items():
                        stats[key] = int(stats.get(key, 0)) + value
                processed = await _finalize_deferred_irrelevant(
                    dao,
                    ledger,
                    run_id,
                    source_rows,
                    deferred_irrelevant,
                    threshold=cfg.exclusion_review_threshold,
                )
                for key, value in processed.items():
                    stats[key] = int(stats.get(key, 0)) + value

            if had_work:
                await create_taxonomy_merge_review_rows(db, run_id)
                review_path = await emit_review_file(ws, db, run_id)
                if review_path is not None:
                    stats["review_file"] = review_path.relative_to(ws).as_posix()
            pending = await _pending_count(db)
            stats["pending"] = pending
            stats["blocks_created"] = writer.blocks_created
            coverage = await render_coverage_sections(ws, db, run_id=run_id)
            summaries = await render_summaries_and_index(ws, db, run_id=run_id, provider=llm, config=cfg)
            stats["llm_calls"] = _provider_call_count(llm) - calls_before
            stats["page_writes"] = (
                writer.page_writes
                + coverage.page_writes
                + coverage.breakdown_writes
                + summaries.page_writes
                + int(summaries.index_written)
            )
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
    *,
    domain_hint: str | None = None,
    deferred_irrelevant: list[_IrrelevantCandidate] | None = None,
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
        "workspace_domain_hint": domain_hint or "local workspace",
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
            if deferred_irrelevant is None:
                await ledger.mark_irrelevant(str(chunk["id"]), reason=str(item["irrelevant_reason"]))
                counts["irrelevant"] += 1
            else:
                deferred_irrelevant.append(
                    _IrrelevantCandidate(chunk=chunk, reason=str(item["irrelevant_reason"]))
                )
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
            target_page = _flow_target_page(
                await writer.registry.list_pages(),
                source_path=str(chunk["source_path"]),
                source_text=str(chunk["text"]),
            )
            placement = {
                "page": {"existing_page_id": target_page["id"]} if target_page is not None else None,
                "new_page": None
                if target_page is not None
                else {
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
        "AND d.relative_path NOT LIKE 'wiki/%' AND d.relative_path NOT LIKE '.llmwiki/%' "
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


async def _finalize_deferred_irrelevant(
    dao: ACWDao,
    ledger: ChunkLedger,
    run_id: str,
    source_rows: list[Row],
    deferred: list[_IrrelevantCandidate],
    *,
    threshold: float,
) -> dict[str, int]:
    if not deferred:
        return {}
    if len(deferred) / max(len(source_rows), 1) > threshold:
        reason = _dominant_reason(deferred)
        for item in deferred:
            await ledger.mark_exclusion_pending(str(item.chunk["id"]), reason=reason)
        await dao.create_review_row(
            run_id=run_id,
            page_id=None,
            row_kind="exclusion",
            candidate_json=json_dumps(
                {
                    "source_id": source_rows[0]["source_id"],
                    "source_path": source_rows[0]["source_path"],
                    "chunk_ids": [item.chunk["id"] for item in deferred],
                    "excluded_count": len(deferred),
                    "total_count": len(source_rows),
                    "reason": reason,
                    "samples": _sample_texts(deferred),
                }
            ),
            recommendation="needs_more_info",
            recommendation_basis="exclusion_review_threshold",
        )
        return {"exclusion_pending": len(deferred)}

    for item in deferred:
        await ledger.mark_irrelevant(str(item.chunk["id"]), reason=item.reason)
    return {"irrelevant": len(deferred)}


async def _pending_count(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM acw_chunk_ledger WHERE disposition = 'pending'")
    return int((await cursor.fetchone())[0])


async def _mark_out_of_scope_chunks(db: aiosqlite.Connection, ledger: ChunkLedger) -> None:
    cursor = await db.execute(
        "SELECT cl.id, cl.disposition FROM acw_chunk_ledger cl "
        "JOIN documents d ON d.id = cl.source_id "
        "WHERE (d.source_kind != 'source' OR d.relative_path LIKE 'eval/%' "
        "OR d.relative_path LIKE 'wiki/%' OR d.relative_path LIKE '.llmwiki/%') "
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


def _workspace_domain_hint(workspace: Path) -> str:
    path = workspace / ".llmwiki" / "domain_hint.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "local workspace"


async def _ensure_domain_hint(
    workspace: Path,
    registry: PageRegistry,
    provider: LLMProvider,
    worklist: list[Row],
    *,
    run_id: str,
) -> None:
    path = workspace / ".llmwiki" / "domain_hint.md"
    if path.exists():
        return
    payload = {
        "sources": _domain_hint_sources(worklist),
        "registry": _domain_hint_registry(await registry.list_pages()),
    }
    response = await complete_validated(provider, "C7", payload, C7_SCHEMA, validate_c7_response)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"<!-- acw:generated Domain Hint run={run_id} -- manual edits will be preserved -->\n"
        f"{str(response['domain_hint_markdown']).strip()}\n"
    )
    path.write_text(text, encoding="utf-8")


def _domain_hint_sources(rows: list[Row]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in _source_groups(rows):
        if len(sources) >= 30:
            break
        first = group[0]
        source_path = str(first["source_path"])
        if source_path in seen:
            continue
        seen.add(source_path)
        samples = [
            str(row.get("text") or "").strip()
            for row in group
            if str(row.get("text") or "").strip()
        ][:3]
        sources.append(
            {
                "source_path": source_path,
                "kind": str(first.get("source_kind") or "doc"),
                "sample_chunk_texts": samples,
            }
        )
    return sources


def _domain_hint_registry(pages: list[Row]) -> list[dict[str, str]]:
    return [
        {"title": str(page["title"]), "description": str(page["description"] or "")}
        for page in pages
        if page["status"] == "active"
    ]


def _source_groups(rows: list[Row]) -> list[list[Row]]:
    groups: list[list[Row]] = []
    current_source: str | None = None
    current: list[Row] = []
    for row in rows:
        source_id = str(row["source_id"])
        if current and source_id != current_source:
            groups.append(current)
            current = []
        current.append(row)
        current_source = source_id
    if current:
        groups.append(current)
    return groups


def _dominant_reason(items: list[_IrrelevantCandidate]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]


def _sample_texts(items: list[_IrrelevantCandidate]) -> list[str]:
    samples = [str(item.chunk.get("text") or "").strip() for item in items if str(item.chunk.get("text") or "").strip()]
    if len(samples) <= 5:
        return samples
    return samples[:5]


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


def _flow_target_page(pages: list[Row], *, source_path: str, source_text: str) -> Row | None:
    flow_words = set(_flow_words(f"{Path(source_path).stem} {source_text}"))
    best_page: Row | None = None
    best_score = 0
    for page in pages:
        if page.get("status") != "active":
            continue
        page_words = set(_flow_words(f"{page['title']} {page['description']}"))
        score = len(flow_words & page_words)
        if score > best_score:
            best_score = score
            best_page = page
    return best_page if best_score > 0 else None


def _flow_words(value: str) -> list[str]:
    import re

    words = re.findall(r"[a-z0-9]+", value.casefold())
    stems = [word[:-1] for word in words if len(word) > 3 and word.endswith("s")]
    return [*words, *stems]

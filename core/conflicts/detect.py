from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import aiosqlite

from core.blocks.model import ContextBlock
from core.blocks.parser import parse_page
from core.db.dao import ACWDao, Row
from core.llm.calls import C3_SCHEMA, complete_validated, validate_c3_response
from core.llm.provider import LLMProvider, StructuredPayload
from core.models import BlockStatus, ConflictType, ReviewDecision
from core.registry import PageRegistry


@dataclass(frozen=True, slots=True)
class ComparisonBlock:
    page_id: str
    page_path: str
    page_title: str
    block: ContextBlock
    retrieval_reason: str


@dataclass(frozen=True, slots=True)
class RecommendationBasis:
    basis: Literal["source_date", "mtime"]
    candidate_timestamp: str | None
    existing_timestamp: str | None

    def as_payload(self) -> dict[str, str | None]:
        return {
            "basis": self.basis,
            "candidate_timestamp": self.candidate_timestamp,
            "existing_timestamp": self.existing_timestamp,
        }


@dataclass(frozen=True, slots=True)
class JudgeResult:
    verdict: Literal["distinct", "duplicate", "conflict"]
    conflict_type: ConflictType | None
    recommendation: ReviewDecision
    rationale: str
    recommendation_basis: Literal["source_date", "mtime"]


@dataclass(frozen=True, slots=True)
class ConflictDetectionResult:
    kind: Literal["distinct", "duplicate", "conflict"]
    existing: ComparisonBlock | None = None
    conflict_type: ConflictType | None = None
    recommendation: ReviewDecision | None = None
    rationale: str | None = None
    recommendation_basis: Literal["source_date", "mtime"] | None = None


async def retrieve_comparison_candidates(
    workspace: str | Path,
    db: aiosqlite.Connection,
    target_page: Row,
    candidate: ContextBlock,
    *,
    limit: int = 8,
) -> list[ComparisonBlock]:
    dao = ACWDao(db)
    pages = await PageRegistry(dao).list_pages()
    page_ids = _target_and_related_page_ids(Path(workspace), target_page, pages)
    comparisons = await _comparison_blocks_for_pages(Path(workspace), dao, pages, page_ids)

    selected: list[ComparisonBlock] = []
    selected_ids: set[str] = set()
    for comparison in comparisons:
        if comparison.page_id != target_page["id"]:
            continue
        if _key_matches(candidate.key, comparison.block.key):
            selected.append(_with_reason(comparison, "key"))
            selected_ids.add(comparison.block.id)

    for block_id in await _fts_block_ids(db, candidate, comparisons, limit=limit):
        if block_id in selected_ids:
            continue
        comparison = next(item for item in comparisons if item.block.id == block_id)
        selected.append(_with_reason(comparison, "fts"))
        selected_ids.add(block_id)
        if len(selected) >= limit:
            break
    return selected


async def detect_candidate_conflict(
    workspace: str | Path,
    db: aiosqlite.Connection,
    target_page: Row,
    candidate: ContextBlock,
    *,
    provider: LLMProvider,
) -> ConflictDetectionResult:
    dao = ACWDao(db)
    comparisons = await retrieve_comparison_candidates(workspace, db, target_page, candidate)
    for comparison in comparisons:
        if is_exact_duplicate(candidate, comparison.block):
            return ConflictDetectionResult(kind="duplicate", existing=comparison)

        basis = compute_recommendation_basis(
            candidate,
            comparison.block,
            candidate_mtime_ns=await dao.get_document_mtime_ns(candidate.source_id),
            existing_mtime_ns=await dao.get_document_mtime_ns(comparison.block.source_id),
        )
        judged = await judge_conflict_pair(provider, candidate, comparison, basis)
        if judged.verdict == "duplicate":
            return ConflictDetectionResult(kind="duplicate", existing=comparison)
        if judged.verdict == "conflict":
            return ConflictDetectionResult(
                kind="conflict",
                existing=comparison,
                conflict_type=judged.conflict_type,
                recommendation=judged.recommendation,
                rationale=judged.rationale,
                recommendation_basis=judged.recommendation_basis,
            )
    return ConflictDetectionResult(kind="distinct")


def is_exact_duplicate(candidate: ContextBlock, existing: ContextBlock) -> bool:
    return (
        candidate.key == existing.key
        and candidate.source_path == existing.source_path
        and _normalize(candidate.content) == _normalize(existing.content)
    )


def compute_recommendation_basis(
    candidate: ContextBlock,
    existing: ContextBlock,
    *,
    candidate_mtime_ns: int | None = None,
    existing_mtime_ns: int | None = None,
) -> RecommendationBasis:
    if _valid_source_date(candidate.source_date) and _valid_source_date(existing.source_date):
        return RecommendationBasis("source_date", candidate.source_date, existing.source_date)
    return RecommendationBasis(
        "mtime",
        str(candidate_mtime_ns) if candidate_mtime_ns is not None else None,
        str(existing_mtime_ns) if existing_mtime_ns is not None else None,
    )


def build_c3_payload(
    candidate: ContextBlock,
    existing: ComparisonBlock,
    basis: RecommendationBasis,
) -> StructuredPayload:
    return {
        "chunk_id": candidate.chunk_ids[0] if candidate.chunk_ids else candidate.id,
        "candidate": _block_payload(candidate),
        "existing": _block_payload(existing.block),
        "existing_page": {
            "id": existing.page_id,
            "path": existing.page_path,
            "title": existing.page_title,
        },
        "recommendation_basis": basis.as_payload(),
    }


async def judge_conflict_pair(
    provider: LLMProvider,
    candidate: ContextBlock,
    existing: ComparisonBlock,
    basis: RecommendationBasis,
) -> JudgeResult:
    response = await complete_validated(provider, "C3", build_c3_payload(candidate, existing, basis), C3_SCHEMA, validate_c3_response)
    verdict = str(response["verdict"])
    return JudgeResult(
        verdict=verdict,  # type: ignore[arg-type]
        conflict_type=ConflictType(response["conflict_type"]) if response["conflict_type"] is not None else None,
        recommendation=ReviewDecision(response["recommendation"]),
        rationale=str(response["rationale"]),
        recommendation_basis=basis.basis,
    )


def _target_and_related_page_ids(workspace: Path, target_page: Row, pages: list[Row]) -> list[str]:
    target_id = str(target_page["id"])
    target_text = _read_page_text(workspace, str(target_page["path"]))
    linked_labels = {_clean_link_label(label) for label in _wiki_links(target_text)}
    page_ids = [target_id]
    for page in pages:
        page_id = str(page["id"])
        if page_id == target_id or page.get("status") != "active":
            continue
        names = {str(page["title"]).casefold(), Path(str(page["path"])).stem.replace("-", " ").casefold()}
        names.update(str(alias).casefold() for alias in page.get("aliases", []))
        if linked_labels & names:
            page_ids.append(page_id)
            continue
        page_text = _read_page_text(workspace, str(page["path"]))
        if str(target_page["title"]).casefold() in {_clean_link_label(label) for label in _wiki_links(page_text)}:
            page_ids.append(page_id)
    return page_ids


async def _comparison_blocks_for_pages(
    workspace: Path,
    dao: ACWDao,
    pages: list[Row],
    page_ids: list[str],
) -> list[ComparisonBlock]:
    page_by_id = {str(page["id"]): page for page in pages}
    comparisons: list[ComparisonBlock] = []
    for page_id in page_ids:
        page = page_by_id[page_id]
        db_blocks = {str(row["id"]): row for row in await dao.list_blocks_for_page(page_id)}
        path = workspace / str(page["path"])
        if not path.exists():
            continue
        for block in parse_page(path.read_text(encoding="utf-8")).blocks:
            db_row = db_blocks.get(block.id)
            if db_row is None:
                continue
            status = BlockStatus(str(db_row["status"]))
            if status in {BlockStatus.rejected, BlockStatus.deleted}:
                continue
            enriched = block.model_copy(
                update={
                    "source_id": str(db_row["source_id"]),
                    "source_date": str(db_row["source_date"]),
                },
            )
            comparisons.append(
                ComparisonBlock(
                    page_id=page_id,
                    page_path=str(page["path"]),
                    page_title=str(page["title"]),
                    block=enriched,
                    retrieval_reason="page",
                ),
            )
    return comparisons


async def _fts_block_ids(
    db: aiosqlite.Connection,
    candidate: ContextBlock,
    comparisons: list[ComparisonBlock],
    *,
    limit: int,
) -> list[str]:
    query = _fts_query(candidate)
    if not query or not comparisons:
        return []
    await db.execute("DROP TABLE IF EXISTS temp.acw_conflict_blocks")
    await db.execute(
        "CREATE VIRTUAL TABLE temp.acw_conflict_blocks USING fts5("
        "block_id UNINDEXED, search_text, tokenize='porter unicode61')",
    )
    await db.executemany(
        "INSERT INTO temp.acw_conflict_blocks (block_id, search_text) VALUES (?, ?)",
        [(item.block.id, _search_text(item.block)) for item in comparisons],
    )
    cursor = await db.execute(
        "SELECT block_id FROM temp.acw_conflict_blocks "
        "WHERE acw_conflict_blocks MATCH ? ORDER BY bm25(acw_conflict_blocks) LIMIT ?",
        (query, limit),
    )
    rows = [str(row[0]) for row in await cursor.fetchall()]
    await db.execute("DROP TABLE IF EXISTS temp.acw_conflict_blocks")
    return rows


def _with_reason(comparison: ComparisonBlock, reason: str) -> ComparisonBlock:
    return ComparisonBlock(
        page_id=comparison.page_id,
        page_path=comparison.page_path,
        page_title=comparison.page_title,
        block=comparison.block,
        retrieval_reason=reason,
    )


def _key_matches(candidate_key: str, existing_key: str) -> bool:
    return (
        candidate_key == existing_key
        or candidate_key.startswith(f"{existing_key}.")
        or existing_key.startswith(f"{candidate_key}.")
    )


def _fts_query(candidate: ContextBlock) -> str:
    terms = []
    for term in re.findall(r"[a-z0-9_]+", _search_text(candidate).casefold()):
        if len(term) < 2 and not term.isdigit():
            continue
        if term in _STOP_WORDS:
            continue
        terms.append(term.replace('"', ""))
    unique = list(dict.fromkeys(terms))[:12]
    return " OR ".join(f'"{term}"' for term in unique)


def _search_text(block: ContextBlock) -> str:
    return f"{block.key}\n{block.type.value}\n{block.content}\n{block.excerpt}"


def _block_payload(block: ContextBlock) -> dict[str, object]:
    return {
        "key": block.key,
        "type": block.type.value,
        "content": block.content,
        "excerpt": block.excerpt,
        "source_path": block.source_path,
        "source_date": block.source_date,
    }


def _read_page_text(workspace: Path, path: str) -> str:
    page_path = workspace / path
    if not page_path.exists():
        return ""
    return page_path.read_text(encoding="utf-8")


def _wiki_links(text: str) -> list[str]:
    return re.findall(r"\[\[([^\]#|]+)", text)


def _clean_link_label(label: str) -> str:
    return label.strip().casefold()


def _valid_source_date(value: str) -> bool:
    if value == "unknown":
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


_STOP_WORDS = {
    "and",
    "are",
    "before",
    "block",
    "for",
    "from",
    "key",
    "source",
    "the",
    "this",
    "use",
    "uses",
    "with",
}

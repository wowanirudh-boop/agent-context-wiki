from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import aiosqlite

from core.db.dao import ACWDao, Row, fetch_all, fetch_one, utc_now
from core.db.migrate import apply_migrations
from core.ledger import ChunkLedger

_DATE_KEYS = ("source_date", "meeting_date", "document_date", "date")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_LABELED_DATE_RE = re.compile(
    r"(?im)\b(?:meeting|document|source)?\s*date\s*[:=-]\s*['\"]?(?P<date>\d{4}-\d{2}-\d{2})",
)
_ISO_DATE_RE = re.compile(r"\b(?P<date>\d{4}-\d{2}-\d{2})\b")


async def index_document_chunks(
    db: aiosqlite.Connection,
    document_id: str,
    workspace: str | Path | None = None,
    *,
    content: str | None = None,
    chunks: list[Any] | None = None,
    replace: bool = True,
) -> None:
    """Chunk a local document, then seed v2 source-version and ledger rows."""
    await apply_migrations(db)
    document = await _get_document(db, document_id)
    if document is None:
        return

    source_text = content if content is not None else document.get("content")
    if source_text is None:
        source_text = ""

    source_version = await _ensure_source_version(db, document, workspace, source_text)

    if replace:
        chunks = chunks if chunks is not None else _chunk_text(source_text)
        await db.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
        for chunk in chunks:
            content_hash = chunk_content_hash(chunk.content)
            await db.execute(
                "INSERT INTO document_chunks "
                "(id, document_id, chunk_index, content, source_content, page, start_char, "
                "token_count, header_breadcrumb, content_hash, source_version_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    document_id,
                    chunk.index,
                    chunk.content,
                    chunk.content,
                    chunk.page,
                    chunk.start_char,
                    chunk.token_count,
                    chunk.header_breadcrumb,
                    content_hash,
                    source_version["id"],
                ),
            )
        await db.commit()
    else:
        await _stamp_existing_chunks(db, document_id, source_version["id"])

    await _seed_ledger_for_current_chunks(db, document_id, source_version["id"])


async def seed_existing_document_chunks(
    db: aiosqlite.Connection,
    document_id: str,
    workspace: str | Path | None = None,
    *,
    content: str | None = None,
) -> None:
    await index_document_chunks(db, document_id, workspace, content=content, replace=False)


def chunk_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def classify_acw_source_kind(relative_path: str, file_type: str | None = None) -> str:
    lowered = relative_path.replace("\\", "/").lower()
    suffix = (file_type or Path(lowered).suffix.lstrip(".")).lower()
    parts = set(Path(lowered).parts)

    if suffix in {"yaml", "yml", "xml"} or {"flow", "flows", "diagram", "diagrams", "whimsical"} & parts:
        return "flow"
    if (
        suffix in {"vtt", "srt"}
        or {"transcript", "transcripts", "meetings"} & parts
        or any(hint in lowered for hint in ("transcript", "standup", "meeting"))
    ):
        return "transcript"
    return "doc"


def extract_source_date(content: str, user_date: str | None) -> tuple[str, str]:
    content_date = _extract_content_date(content)
    if content_date is not None:
        return content_date, "content"
    if user_date and _valid_iso_date(user_date):
        return user_date, "user"
    return "unknown", "unknown"


async def _ensure_source_version(
    db: aiosqlite.Connection,
    document: Row,
    workspace: str | Path | None,
    content: str,
) -> Row:
    relative_path = str(document["relative_path"])
    file_path = Path(workspace) / relative_path if workspace is not None else None
    stat = file_path.stat() if file_path is not None and file_path.is_file() else None
    source_date, source_date_origin = extract_source_date(content, document.get("date"))
    ingested_at = utc_now()
    metadata = _metadata_with_source_kind(document.get("metadata"), relative_path, document.get("file_type"))
    version_hash = document.get("content_hash") or chunk_content_hash(content)

    await db.execute(
        "UPDATE documents SET date = ?, metadata = ?, mtime_ns = ?, last_indexed_at = ?, "
        "content_hash = COALESCE(content_hash, ?), updated_at = datetime('now') WHERE id = ?",
        (
            source_date if source_date != "unknown" else document.get("date"),
            json.dumps(metadata, sort_keys=True),
            int(stat.st_mtime_ns) if stat is not None else document.get("mtime_ns"),
            ingested_at,
            version_hash,
            document["id"],
        ),
    )
    await db.commit()

    ledger = ChunkLedger(ACWDao(db))
    return await ledger.ensure_source_version(
        source_id=str(document["id"]),
        version_hash=str(version_hash),
        source_date=source_date,
        source_date_origin=source_date_origin,
        seen_at=ingested_at,
    )


async def _stamp_existing_chunks(
    db: aiosqlite.Connection,
    document_id: str,
    source_version_id: str,
) -> None:
    rows = await _document_chunks(db, document_id)
    for row in rows:
        content = row["source_content"] or row["content"]
        await db.execute(
            "UPDATE document_chunks SET content_hash = ?, source_version_id = ? WHERE id = ?",
            (chunk_content_hash(content), source_version_id, row["id"]),
        )
    await db.commit()


async def _seed_ledger_for_current_chunks(
    db: aiosqlite.Connection,
    document_id: str,
    source_version_id: str,
) -> None:
    dao = ACWDao(db)
    ledger = ChunkLedger(dao)
    rows = await _document_chunks(db, document_id)
    current_hashes: set[str] = set()

    for row in rows:
        content_hash = row["content_hash"] or chunk_content_hash(row["source_content"] or row["content"])
        current_hashes.add(content_hash)
        await db.execute(
            "UPDATE document_chunks SET content_hash = ?, source_version_id = ? WHERE id = ?",
            (content_hash, source_version_id, row["id"]),
        )
        chunk = await ledger.create_pending_chunk(
            source_id=document_id,
            source_version_id=source_version_id,
            content_hash=content_hash,
            ordinal=int(row["chunk_index"]),
            document_chunk_id=str(row["id"]),
        )
        await _copy_prior_disposition_if_needed(db, chunk, int(row["chunk_index"]))

    await db.commit()

    from core.reingest import reconcile_source_version

    await reconcile_source_version(db, document_id, source_version_id, current_hashes)


async def _copy_prior_disposition_if_needed(
    db: aiosqlite.Connection,
    chunk: Row,
    ordinal: int,
) -> None:
    if chunk["disposition"] != "pending" or chunk["attempts"] != 0:
        return

    cursor = await db.execute(
        "SELECT id, disposition, disposition_reason, duplicate_of_block_id, attempts "
        "FROM acw_chunk_ledger "
        "WHERE source_id = ? AND source_version_id != ? AND content_hash = ? "
        "AND disposition != 'superseded' "
        "ORDER BY CASE WHEN ordinal = ? THEN 0 ELSE 1 END, updated_at DESC LIMIT 1",
        (chunk["source_id"], chunk["source_version_id"], chunk["content_hash"], ordinal),
    )
    prior = await fetch_one(cursor)
    if prior is None:
        return

    await db.execute(
        "UPDATE acw_chunk_ledger SET disposition = ?, disposition_reason = ?, "
        "duplicate_of_block_id = ?, attempts = ?, updated_at = ? WHERE id = ?",
        (
            prior["disposition"],
            prior["disposition_reason"],
            prior["duplicate_of_block_id"],
            prior["attempts"],
            utc_now(),
            chunk["id"],
        ),
    )
    prior_links = await fetch_all(
        await db.execute("SELECT block_id FROM acw_block_chunks WHERE chunk_id = ?", (prior["id"],)),
    )
    await db.executemany(
        "INSERT OR IGNORE INTO acw_block_chunks (block_id, chunk_id) VALUES (?, ?)",
        [(row["block_id"], chunk["id"]) for row in prior_links],
    )


async def _get_document(db: aiosqlite.Connection, document_id: str) -> Row | None:
    cursor = await db.execute(
        "SELECT id, relative_path, file_type, content, content_hash, date, metadata, mtime_ns "
        "FROM documents WHERE id = ?",
        (document_id,),
    )
    return await fetch_one(cursor)


async def _document_chunks(db: aiosqlite.Connection, document_id: str) -> list[Row]:
    cursor = await db.execute(
        "SELECT id, chunk_index, content, source_content, page, start_char, token_count, "
        "header_breadcrumb, content_hash, source_version_id "
        "FROM document_chunks WHERE document_id = ? ORDER BY chunk_index",
        (document_id,),
    )
    return await fetch_all(cursor)


def _chunk_text(content: str) -> list[Any]:
    from services.chunker import chunk_text

    return chunk_text(content)


def _metadata_with_source_kind(metadata_raw: Any, relative_path: str, file_type: str | None) -> dict[str, Any]:
    if isinstance(metadata_raw, str):
        try:
            metadata = json.loads(metadata_raw) or {}
        except (TypeError, ValueError):
            metadata = {}
    elif isinstance(metadata_raw, dict):
        metadata = dict(metadata_raw)
    else:
        metadata = {}
    metadata["acw_source_kind"] = classify_acw_source_kind(relative_path, file_type)
    return metadata


def _extract_content_date(content: str) -> str | None:
    frontmatter = _FRONTMATTER_RE.match(content)
    if frontmatter is not None:
        for line in frontmatter.group("body").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().lower() in _DATE_KEYS:
                candidate = value.strip().strip("'\"")
                if _valid_iso_date(candidate):
                    return candidate

    for regex in (_LABELED_DATE_RE, _ISO_DATE_RE):
        match = regex.search(content)
        if match and _valid_iso_date(match.group("date")):
            return match.group("date")
    return None


def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True

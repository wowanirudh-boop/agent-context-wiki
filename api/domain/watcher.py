"""Filesystem watcher for local mode.

Watches the workspace for file changes and updates the SQLite index.
Uses watchfiles for efficient cross-platform filesystem monitoring.

Key design rules:
- App-initiated writes register in _recently_written to prevent re-indexing loops
- Hidden dirs (.llmwiki, .git, node_modules, etc.) are ignored
- File identity is by path — rename = delete old + create new
"""

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import aiosqlite

from domain.local_index import (
    extension_for,
    folder_path_for,
    hash_file,
    legacy_relative_path_for,
    read_text_content,
    relative_path_for,
    source_kind_for,
    status_for_content,
    title_for,
)

logger = logging.getLogger(__name__)

IGNORE_DIRS = frozenset({
    ".llmwiki", ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".DS_Store",
})

COOLDOWN_SECONDS = 2.0
AUTO_PROCESS_DEBOUNCE_SECONDS = 5.0

_ignore_patterns: list[str] | None = None
_auto_process_tasks: dict[str, asyncio.Task] = {}
_auto_process_requested: set[str] = set()


def _load_ignore_patterns(workspace: Path) -> list[str]:
    """Load ignore patterns from .llmwikiignore, falling back to .gitignore."""
    global _ignore_patterns
    if _ignore_patterns is not None:
        return _ignore_patterns

    patterns = []
    for ignore_file in (".llmwikiignore", ".gitignore"):
        p = workspace / ignore_file
        if p.is_file():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
            break  # Use first found, don't merge
    _ignore_patterns = patterns
    return patterns


def _matches_ignore_pattern(relative: str, patterns: list[str]) -> bool:
    """Simple gitignore-style matching (directory and glob patterns)."""
    from fnmatch import fnmatch
    for pattern in patterns:
        pattern = pattern.rstrip("/")
        if fnmatch(relative, pattern):
            return True
        if fnmatch(relative, f"**/{pattern}"):
            return True
        # Check if any path component matches a directory pattern
        for part in relative.split("/"):
            if fnmatch(part, pattern):
                return True
    return False

# Paths written by the app — skip re-indexing for these
_recently_written: dict[str, float] = {}


def mark_written(path: str) -> None:
    """Mark a path as recently written by the app. Watcher will skip it."""
    _recently_written[str(Path(path).resolve())] = time.monotonic()


def _is_recently_written(path: str) -> bool:
    resolved = str(Path(path).resolve())
    ts = _recently_written.get(resolved)
    if ts and (time.monotonic() - ts) < COOLDOWN_SECONDS:
        return True
    _recently_written.pop(resolved, None)
    return False


def _should_ignore(path: Path, workspace: Path) -> bool:
    """Check if a path should be ignored based on directory rules + ignore files."""
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        return True

    relative_str = relative.as_posix()
    parts = relative.parts

    # Built-in ignores
    for part in parts:
        if part in IGNORE_DIRS or part.startswith("."):
            return True

    # User-configured ignore patterns
    patterns = _load_ignore_patterns(workspace)
    if patterns and _matches_ignore_pattern(relative_str, patterns):
        return True

    return False


async def _schedule_processing(
    db: aiosqlite.Connection,
    doc_id: str,
    workspace: Path,
    schedule_processing: bool,
) -> None:
    if not schedule_processing:
        return
    from domain.local_processor import process_document as _process
    asyncio.create_task(_process(db, doc_id, workspace))


async def _clear_extracted_rows(db: aiosqlite.Connection, doc_id: str) -> None:
    await db.execute("DELETE FROM document_pages WHERE document_id = ?", (doc_id,))
    await db.execute("DELETE FROM document_chunks WHERE document_id = ?", (doc_id,))


async def _index_file(
    db: aiosqlite.Connection,
    workspace: Path,
    file_path: Path,
    *,
    schedule_processing: bool = True,
) -> None:
    """Index or re-index a single file."""
    relative = relative_path_for(workspace, file_path)
    legacy_relative = legacy_relative_path_for(workspace, file_path)
    filename = file_path.name
    ext = extension_for(filename)
    dir_path = folder_path_for(relative)
    source_kind = source_kind_for(relative)
    stat = file_path.stat()
    title = title_for(filename)

    # Read content for text files
    content = read_text_content(file_path, ext)
    content_hash = hash_file(file_path)
    status = status_for_content(content)

    # Check if document already exists at this path
    if legacy_relative != relative:
        cursor = await db.execute(
            "SELECT id, content_hash, status, relative_path, path FROM documents "
            "WHERE relative_path IN (?, ?)",
            (relative, legacy_relative),
        )
    else:
        cursor = await db.execute(
            "SELECT id, content_hash, status, relative_path, path "
            "FROM documents WHERE relative_path = ?",
            (relative,),
        )
    existing = await cursor.fetchone()

    if existing:
        doc_id, old_hash, old_status, old_relative, old_path = existing
        metadata_current = old_relative == relative and old_path == dir_path
        if old_hash == content_hash and old_status == status and metadata_current:
            if status == "ready" and content is not None:
                from core.ingest import index_document_chunks
                await index_document_chunks(db, doc_id, workspace, content=content)
            return  # No change
        if status == "pending":
            await _clear_extracted_rows(db, doc_id)
        # Update existing
        await db.execute(
            "UPDATE documents SET filename = ?, title = ?, path = ?, relative_path = ?, "
            "source_kind = ?, file_type = ?, content = ?, file_size = ?, status = ?, "
            "content_hash = ?, mtime_ns = ?, last_indexed_at = datetime('now'), "
            "page_count = CASE WHEN ? = 'pending' THEN NULL ELSE page_count END, "
            "parser = CASE WHEN ? = 'pending' THEN NULL ELSE parser END, "
            "error_message = NULL, "
            "updated_at = datetime('now'), version = version + 1 "
            "WHERE id = ?",
            (
                filename, title, dir_path, relative, source_kind, ext or "bin",
                content, stat.st_size, status, content_hash, int(stat.st_mtime_ns),
                status, status, doc_id,
            ),
        )
        await db.commit()
        logger.info("Re-indexed (modified): %s", relative)
        if status == "ready" and content is not None:
            from core.ingest import index_document_chunks
            await index_document_chunks(db, doc_id, workspace, content=content)
        if status == "pending":
            await _schedule_processing(db, doc_id, workspace, schedule_processing)
        return
    else:
        # Create new
        doc_id = str(uuid.uuid4())
        cursor = await db.execute(
            "SELECT COALESCE(MAX(document_number), 0) + 1 FROM documents",
        )
        row = await cursor.fetchone()
        doc_number = row[0]

        await db.execute(
            "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
            "source_kind, file_type, file_size, status, content, tags, version, "
            "content_hash, mtime_ns, last_indexed_at, document_number) "
            "VALUES (?, (SELECT user_id FROM workspace LIMIT 1), ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, '[]', 0, ?, ?, datetime('now'), ?)",
            (doc_id, filename, title, dir_path, relative, source_kind,
             ext or "bin", stat.st_size, status, content, content_hash,
             int(stat.st_mtime_ns), doc_number),
        )
        logger.info("Indexed (new): %s", relative)

    await db.commit()
    if status == "ready" and content is not None:
        from core.ingest import index_document_chunks
        await index_document_chunks(db, doc_id, workspace, content=content)
    if status == "pending":
        await _schedule_processing(db, doc_id, workspace, schedule_processing)


async def _remove_file(db: aiosqlite.Connection, workspace: Path, file_path: Path) -> None:
    """Remove a file from the index."""
    try:
        relative = relative_path_for(workspace, file_path)
    except ValueError:
        return

    cursor = await db.execute("SELECT id FROM documents WHERE relative_path = ?", (relative,))
    row = await cursor.fetchone()
    if row:
        from core.reingest import mark_source_deleted
        await mark_source_deleted(db, row[0])
        logger.info("Marked source deleted: %s", relative)


async def scan_workspace(
    db: aiosqlite.Connection,
    workspace: Path,
    *,
    schedule_processing: bool = True,
) -> int:
    """Index existing files under the workspace once."""
    indexed = 0
    for root_path, dirs, files in os.walk(workspace):
        root = Path(root_path)
        dirs[:] = [
            d for d in dirs
            if not _should_ignore(root / d, workspace)
        ]
        for filename in files:
            file_path = root / filename
            if _should_ignore(file_path, workspace) or not file_path.is_file():
                continue
            await _index_file(
                db,
                workspace,
                file_path,
                schedule_processing=schedule_processing,
            )
            indexed += 1
    return indexed


async def watch_workspace(db: aiosqlite.Connection, workspace: Path) -> None:
    """Watch the workspace for file changes and update the SQLite index.

    Runs indefinitely as an async task. Cancel to stop.
    """
    from watchfiles import awatch, Change

    logger.info("File watcher started: %s", workspace)

    async for changes in awatch(
        str(workspace),
        watch_filter=lambda change, path: not _should_ignore(Path(path), workspace),
        debounce=500,
        step=200,
    ):
        for change_type, path_str in changes:
            path = Path(path_str)

            if _should_ignore(path, workspace):
                continue

            if _is_recently_written(path_str):
                continue

            try:
                if change_type == Change.added or change_type == Change.modified:
                    if path.is_file():
                        await _index_file(db, workspace, path)
                elif change_type == Change.deleted:
                    await _remove_file(db, workspace, path)
            except Exception as e:
                logger.warning("Watcher error for %s: %s", path_str, e)

        _schedule_auto_process_if_enabled(workspace)

        # Clean up expired entries from _recently_written
        now = time.monotonic()
        expired = [k for k, v in _recently_written.items() if now - v > COOLDOWN_SECONDS * 2]
        for k in expired:
            _recently_written.pop(k, None)


def auto_process_enabled(workspace: Path) -> bool:
    from core.config import load_config

    return load_config(workspace).auto_process


def _schedule_auto_process_if_enabled(workspace: Path) -> None:
    if not auto_process_enabled(workspace):
        return
    key = str(workspace.resolve())
    _auto_process_requested.add(key)
    task = _auto_process_tasks.get(key)
    if task is None or task.done():
        _auto_process_tasks[key] = asyncio.create_task(_auto_process_loop(workspace, key))


async def _auto_process_loop(workspace: Path, key: str) -> None:
    from core.lock import WorkspaceLockError
    from core.pipeline.run import run_processing_run

    try:
        while key in _auto_process_requested:
            _auto_process_requested.discard(key)
            await asyncio.sleep(AUTO_PROCESS_DEBOUNCE_SECONDS)
            try:
                await run_processing_run(workspace)
            except WorkspaceLockError:
                _auto_process_requested.add(key)
                await asyncio.sleep(1)
            except Exception as exc:
                logger.warning("Auto-process failed for %s: %s", workspace, exc)
    finally:
        _auto_process_tasks.pop(key, None)

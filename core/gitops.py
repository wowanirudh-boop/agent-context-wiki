from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from core.blocks.model import BlockSegment, Page
from core.blocks.parser import BlockParseError, parse_page
from core.blocks.serializer import serialize_block, write_page
from core.db.dao import ACWDao, utc_now
from core.ledger import ChunkLedger
from core.models import EventKind, ReviewDecision

USER_EDITED_ANNOTATION = "edits a user-authored block"


@dataclass(frozen=True, slots=True)
class GitCommitResult:
    committed: bool
    message: str
    sha: str | None = None


@dataclass(frozen=True, slots=True)
class HardDeleteResult:
    block_id: str
    removed_chunks: list[str]
    warning: str


def ensure_wiki_repo(workspace: str | Path) -> Path:
    wiki = Path(workspace) / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    if not (wiki / ".git").exists():
        _git(wiki, "init")
    _ensure_git_identity(wiki)
    return wiki


def commit_processing_run(workspace: str | Path, run_id: str) -> GitCommitResult:
    return commit_wiki_changes(Path(workspace), f"acw process: {run_id}")


def commit_apply_decisions(workspace: str | Path, review_files: list[str]) -> GitCommitResult:
    subject = ", ".join(review_files) if review_files else "no review files"
    return commit_wiki_changes(Path(workspace), f"acw apply-decisions: {subject}")


def commit_taxonomy_operation(workspace: str | Path, operation: str, subject: str) -> GitCommitResult:
    return commit_wiki_changes(Path(workspace), f"acw taxonomy {operation}: {subject}")


def commit_hard_delete(workspace: str | Path, block_id: str) -> GitCommitResult:
    return commit_wiki_changes(Path(workspace), f"acw hard-delete: {block_id}")


def commit_wiki_changes(workspace: str | Path, message: str) -> GitCommitResult:
    wiki = ensure_wiki_repo(workspace)
    _git(wiki, "add", "-A")
    if _git_status(wiki, "diff", "--cached", "--quiet").returncode == 0:
        return GitCommitResult(committed=False, message=message)
    _git(wiki, "commit", "-m", message)
    sha = _git(wiki, "rev-parse", "--short", "HEAD").strip()
    return GitCommitResult(committed=True, message=message, sha=sha)


async def mark_user_edited_blocks(workspace: str | Path, db: aiosqlite.Connection) -> list[str]:
    wiki = ensure_wiki_repo(workspace)
    if not _has_head(wiki):
        return []

    changed: list[str] = []
    for relative in _changed_markdown_files(wiki):
        path = wiki / relative
        if not path.exists():
            continue
        current_text = path.read_text(encoding="utf-8")
        try:
            head_text = _git(wiki, "show", f"HEAD:{relative.as_posix()}")
        except subprocess.CalledProcessError:
            continue
        changed_ids = _changed_block_ids(head_text, current_text)
        if not changed_ids:
            continue
        page = parse_page(current_text)
        updated = _mark_page_blocks_user_edited(page, changed_ids)
        write_page(path, updated, expected_text=current_text)
        for block_id in changed_ids:
            await db.execute(
                "UPDATE acw_blocks SET user_edited = 1, updated_at = ? WHERE id = ?",
                (utc_now(), block_id),
            )
        await db.commit()
        changed.extend(changed_ids)
    return sorted(set(changed))


def annotate_recommendation_basis(basis: str | None, recommendation: str, *, user_edited: bool) -> str | None:
    if not user_edited or recommendation not in {
        ReviewDecision.accept_new.value,
        ReviewDecision.deprecate_existing.value,
    }:
        return basis
    prefix = basis or ""
    return f"{prefix}; {USER_EDITED_ANNOTATION}" if prefix else USER_EDITED_ANNOTATION


async def hard_delete_block(workspace: str | Path, db: aiosqlite.Connection, block_id: str) -> HardDeleteResult:
    ws = Path(workspace)
    dao = ACWDao(db)
    block = await dao.get_block(block_id)
    if block is None:
        raise KeyError(block_id)
    page = await dao.get_page(str(block["page_id"]))
    if page is None:
        raise KeyError(str(block["page_id"]))

    page_path = ws / str(page["path"])
    old_text = page_path.read_text(encoding="utf-8")
    updated = _remove_block(parse_page(old_text), block_id)
    write_page(page_path, updated, expected_text=old_text)

    chunks = await _linked_chunk_ids(db, block_id)
    ledger = ChunkLedger(dao)
    for chunk_id in chunks:
        row = await dao.get_chunk(chunk_id)
        if row is not None and row["disposition"] != "superseded":
            await ledger.mark_superseded(chunk_id, reason="hard_delete")
        await db.execute(
            "UPDATE document_chunks SET content = '', source_content = '', annotations_text = NULL "
            "WHERE id = (SELECT document_chunk_id FROM acw_chunk_ledger WHERE id = ?)",
            (chunk_id,),
        )
    await db.execute(
        "UPDATE acw_blocks SET status = 'deleted', updated_at = ? WHERE id = ?",
        (utc_now(), block_id),
    )
    await db.commit()
    await dao.write_event(
        kind=EventKind.hard_delete_executed,
        actor="core.gitops",
        payload={"block_id": block_id, "chunks": chunks},
    )
    return HardDeleteResult(block_id=block_id, removed_chunks=chunks, warning=_hard_delete_warning(block_id))


def _ensure_git_identity(wiki: Path) -> None:
    if not _git_config_value(wiki, "user.email"):
        _git(wiki, "config", "user.email", "agent-context-wiki@example.local")
    if not _git_config_value(wiki, "user.name"):
        _git(wiki, "config", "user.name", "Agent Context Wiki")


def _git_config_value(wiki: Path, key: str) -> str:
    completed = _git_status(wiki, "config", "--get", key)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _has_head(wiki: Path) -> bool:
    return _git_status(wiki, "rev-parse", "--verify", "HEAD").returncode == 0


def _changed_markdown_files(wiki: Path) -> list[Path]:
    completed = _git_status(wiki, "diff", "--name-only", "HEAD", "--", "*.md")
    if completed.returncode not in {0, 1}:
        completed.check_returncode()
    return [Path(line) for line in completed.stdout.splitlines() if line.strip()]


def _changed_block_ids(head_text: str, current_text: str) -> list[str]:
    try:
        head = {block.id: block for block in parse_page(head_text).blocks}
        current = {block.id: block for block in parse_page(current_text).blocks}
    except BlockParseError:
        return []
    changed = []
    for block_id, block in current.items():
        previous = head.get(block_id)
        if previous is None:
            continue
        if serialize_block(previous) != serialize_block(block):
            changed.append(block_id)
    return changed


def _mark_page_blocks_user_edited(page: Page, block_ids: list[str]) -> Page:
    wanted = set(block_ids)
    segments = []
    for segment in page.segments:
        if isinstance(segment, BlockSegment) and segment.block.id in wanted:
            segments.append(BlockSegment(segment.block.model_copy(update={"user_edited": True})))
        else:
            segments.append(segment)
    return Page(segments)


def _remove_block(page: Page, block_id: str) -> Page:
    segments = [
        segment
        for segment in page.segments
        if not (isinstance(segment, BlockSegment) and segment.block.id == block_id)
    ]
    if len(segments) == len(page.segments):
        raise KeyError(block_id)
    return Page(segments)


async def _linked_chunk_ids(db: aiosqlite.Connection, block_id: str) -> list[str]:
    cursor = await db.execute(
        "SELECT chunk_id FROM acw_block_chunks WHERE block_id = ? ORDER BY chunk_id",
        (block_id,),
    )
    return [str(row[0]) for row in await cursor.fetchall()]


def _hard_delete_warning(block_id: str) -> str:
    return (
        f"Hard delete removed block {block_id} from pages and blanked linked indexed chunk text. "
        "This does not rewrite git history. To remove prior copies completely, inspect backups "
        "and run a history rewrite such as: git filter-repo --path <path> --invert-paths"
    )


def _git(wiki: Path, *args: str) -> str:
    return _git_status(wiki, *args, check=True).stdout


def _git_status(wiki: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=wiki,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            completed.args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed

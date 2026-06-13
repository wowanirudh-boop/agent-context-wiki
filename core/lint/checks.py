from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from core.blocks.model import BlockSegment, Page, ProseSegment
from core.blocks.parser import BlockParseError, parse_page
from core.blocks.serializer import serialize_page
from core.config import ACWConfig
from core.coverage import _ledger_rows_by_source, _page_coverage_body, _page_sources, _source_sort_key
from core.db.dao import ACWDao, Row, fetch_all
from core.models import BlockStatus, LintFinding, LintSeverity
from core.review.emit import find_unresolved_review_files

_ACTIVE_KEY_STATUSES = {BlockStatus.current.value, BlockStatus.needs_review.value}
_WIKI_LINK_RE = re.compile(r"\[\[(?P<target>[^\]|#]+)(?P<anchor>#[^\]|]+)?(?P<label>\|[^\]]+)?\]\]")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_CODE_LITERAL_RE = re.compile(r"`([^`\n]+)`")
_IDENTIFIER_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9_]*\b")


@dataclass(frozen=True, slots=True)
class ParsedWikiPage:
    row: Row
    path: Path
    text: str
    page: Page | None


async def run_checks(workspace: Path, db: aiosqlite.Connection, config: ACWConfig) -> list[LintFinding]:
    dao = ACWDao(db)
    pages = await dao.list_pages()
    parsed_pages, findings = _parse_pages(workspace, pages)
    findings.extend(await _check_lint_01_coverage(workspace, db, parsed_pages))
    findings.extend(_check_lint_02_block_fidelity(parsed_pages))
    findings.extend(await _check_lint_03_keys_and_inventory(db, parsed_pages))
    findings.extend(await _check_lint_04_reviews_and_staleness(workspace, db, config))
    findings.extend(await _check_lint_05_conflict_visibility(db, parsed_pages))
    findings.extend(_check_lint_06_links(parsed_pages, pages))
    findings.extend(_check_lint_08_flows(parsed_pages))
    return _stable_unique(findings)


def _parse_pages(workspace: Path, pages: list[Row]) -> tuple[list[ParsedWikiPage], list[LintFinding]]:
    parsed_pages: list[ParsedWikiPage] = []
    findings: list[LintFinding] = []
    for row in pages:
        page_path = workspace / str(row["path"])
        if not page_path.exists():
            parsed_pages.append(ParsedWikiPage(row=row, path=page_path, text="", page=None))
            findings.append(_finding("LINT-07", "error", str(row["path"]), str(row["id"]), "registered page file is missing"))
            continue
        text = page_path.read_text(encoding="utf-8")
        try:
            page = parse_page(text)
            if serialize_page(page) != text:
                findings.append(
                    _finding("LINT-07", "error", str(row["path"]), str(row["id"]), "page fails byte-identical round trip")
                )
        except (BlockParseError, ValueError) as exc:
            page = None
            findings.append(_finding("LINT-07", "error", str(row["path"]), str(row["id"]), f"page parse failed: {exc}"))
        parsed_pages.append(ParsedWikiPage(row=row, path=page_path, text=text, page=page))
    return parsed_pages, findings


async def _check_lint_01_coverage(
    workspace: Path,
    db: aiosqlite.Connection,
    pages: list[ParsedWikiPage],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    cursor = await db.execute(
        "SELECT cl.id, cl.disposition, d.relative_path AS source_path "
        "FROM acw_chunk_ledger cl JOIN documents d ON d.id = cl.source_id "
        "WHERE d.source_kind = 'source' AND cl.disposition = 'pending' "
        "ORDER BY d.relative_path, cl.ordinal, cl.id",
    )
    pending = await fetch_all(cursor)
    for row in pending:
        findings.append(
            _finding(
                "LINT-01",
                "error",
                str(row["source_path"]),
                str(row["id"]),
                "chunk remains pending; processing run is incomplete",
            )
        )
    if findings:
        return findings

    rows_by_source = await _ledger_rows_by_source(db)
    page_sources = await _page_sources(db)
    for parsed in pages:
        if parsed.page is None or parsed.row["status"] != "active":
            continue
        source_ids = sorted(
            page_sources.get(str(parsed.row["id"]), set()),
            key=lambda item: _source_sort_key(rows_by_source, item),
        )
        expected = _page_coverage_body(
            page_path=str(parsed.row["path"]),
            source_ids=source_ids,
            rows_by_source=rows_by_source,
        )
        actual = _section_body(parsed.text, "Source Coverage")
        if actual is None:
            findings.append(
                _finding("LINT-01", "error", str(parsed.row["path"]), str(parsed.row["id"]), "Source Coverage section is missing")
            )
            continue
        lines = actual.splitlines()
        body = "\n".join(lines[1:]).strip() if lines and lines[0].startswith("<!-- acw:generated Source Coverage run=") else actual.strip()
        if not lines or not lines[0].startswith("<!-- acw:generated Source Coverage run=") or body != expected.strip():
            findings.append(
                _finding(
                    "LINT-01",
                    "error",
                    str(parsed.row["path"]),
                    str(parsed.row["id"]),
                    "Source Coverage section does not match the chunk ledger",
                )
            )
    return findings


def _check_lint_02_block_fidelity(pages: list[ParsedWikiPage]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for parsed in pages:
        if parsed.page is None:
            continue
        for block in parsed.page.blocks:
            if not block.source_path.strip():
                findings.append(_finding("LINT-02", "error", _rel(parsed), block.id, "block has no source_path"))
            if not block.excerpt.strip():
                findings.append(_finding("LINT-02", "error", _rel(parsed), block.id, "block has an empty excerpt"))
                continue
            if block.type.value == "flow":
                continue
            missing = [token for token in _value_tokens(block.content) if token.casefold() not in block.excerpt.casefold()]
            if missing:
                sample = ", ".join(missing[:5])
                findings.append(
                    _finding(
                        "LINT-02",
                        "error",
                        _rel(parsed),
                        block.id,
                        f"value-bearing token(s) missing from excerpt: {sample}",
                    )
                )
    return findings


async def _check_lint_03_keys_and_inventory(db: aiosqlite.Connection, pages: list[ParsedWikiPage]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for parsed in pages:
        if parsed.page is None:
            continue
        active_keys = [
            block.key
            for block in parsed.page.blocks
            if block.status.value in _ACTIVE_KEY_STATUSES
        ]
        for key, count in sorted(Counter(active_keys).items()):
            if count > 1:
                findings.append(
                    _finding("LINT-03", "error", _rel(parsed), key, "duplicate active semantic key on page")
                )

        db_block_ids = await _db_page_block_ids(db, str(parsed.row["id"]))
        markdown_block_ids = {block.id for block in parsed.page.blocks if block.status not in {BlockStatus.rejected, BlockStatus.deleted}}
        if db_block_ids != markdown_block_ids:
            findings.append(
                _finding(
                    "LINT-03",
                    "error",
                    _rel(parsed),
                    str(parsed.row["id"]),
                    "page block inventory does not match acw_blocks",
                )
            )
    return findings


async def _check_lint_04_reviews_and_staleness(
    workspace: Path,
    db: aiosqlite.Connection,
    config: ACWConfig,
) -> list[LintFinding]:
    findings: list[LintFinding] = [
        _finding("LINT-04", "error", path, "review", "unresolved review file")
        for path in find_unresolved_review_files(workspace)
    ]
    threshold = datetime.now(UTC) - timedelta(days=config.needs_review_stale_days)
    cursor = await db.execute(
        "SELECT b.id, b.updated_at, p.path FROM acw_blocks b JOIN acw_pages p ON p.id = b.page_id "
        "WHERE b.status = 'needs_review' ORDER BY p.path, b.id",
    )
    for row in await fetch_all(cursor):
        updated = _parse_timestamp(row["updated_at"])
        if updated is not None and updated < threshold:
            findings.append(
                _finding("LINT-04", "warn", str(row["path"]), str(row["id"]), "needs_review block is older than threshold")
            )
    return findings


async def _check_lint_05_conflict_visibility(
    db: aiosqlite.Connection,
    pages: list[ParsedWikiPage],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    open_review_rows = await _open_review_row_ids(db)
    marker_ids: set[str] = set()
    for parsed in pages:
        if parsed.page is None:
            continue
        open_conflicts = _section_body(parsed.text, "Open Conflicts") or ""
        for block in parsed.page.blocks:
            marker_ids.update(block.pending_review_ids)
            if block.status == BlockStatus.conflicted and block.id not in open_conflicts and block.key not in open_conflicts:
                findings.append(
                    _finding(
                        "LINT-05",
                        "error",
                        _rel(parsed),
                        block.id,
                        "conflicted block is missing from Open Conflicts",
                    )
                )
    missing_rows = sorted(marker_ids - open_review_rows)
    for row_id in missing_rows:
        findings.append(_finding("LINT-05", "error", "wiki", row_id, "pending marker has no open review row"))

    cursor = await db.execute(
        "SELECT id FROM acw_chunk_ledger WHERE disposition = 'conflicted_pending' ORDER BY id",
    )
    conflicted_pending = {str(row[0]) for row in await cursor.fetchall()}
    if conflicted_pending and not marker_ids:
        findings.append(
            _finding("LINT-05", "error", "wiki", "conflicted_pending", "ledger has conflicted_pending chunks with no pending markers")
        )
    return findings


def _check_lint_06_links(pages: list[ParsedWikiPage], page_rows: list[Row]) -> list[LintFinding]:
    active, inactive = _link_indexes(page_rows)
    findings: list[LintFinding] = []
    for parsed in pages:
        if parsed.page is None:
            continue
        for target in _wiki_links(parsed.page):
            normalized = _normalize_link_target(target)
            if normalized in active:
                continue
            if normalized in inactive:
                findings.append(
                    _finding("LINT-06", "error", _rel(parsed), target, "link points to merged or archived page")
                )
            else:
                findings.append(_finding("LINT-06", "error", _rel(parsed), target, "broken internal wiki link"))
    return findings


def _check_lint_08_flows(pages: list[ParsedWikiPage]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for parsed in pages:
        if parsed.page is None:
            continue
        for block in parsed.page.blocks:
            if block.type.value != "flow":
                continue
            expected_nodes, expected_edges = _source_flow_counts(block.excerpt)
            actual_nodes, actual_edges = _mermaid_flow_counts(block.content)
            if expected_nodes and actual_nodes < expected_nodes:
                findings.append(
                    _finding(
                        "LINT-08",
                        "error",
                        _rel(parsed),
                        block.id,
                        f"flow block has {actual_nodes} node(s), source has {expected_nodes}",
                    )
                )
            if expected_edges and actual_edges < expected_edges:
                findings.append(
                    _finding(
                        "LINT-08",
                        "error",
                        _rel(parsed),
                        block.id,
                        f"flow block has {actual_edges} edge(s), source has {expected_edges}",
                    )
                )
    return findings


async def _db_page_block_ids(db: aiosqlite.Connection, page_id: str) -> set[str]:
    cursor = await db.execute(
        "SELECT id FROM acw_blocks WHERE page_id = ? AND status NOT IN ('rejected', 'deleted')",
        (page_id,),
    )
    return {str(row[0]) for row in await cursor.fetchall()}


async def _open_review_row_ids(db: aiosqlite.Connection) -> set[str]:
    cursor = await db.execute("SELECT id FROM acw_review_rows WHERE applied_at IS NULL")
    return {str(row[0]) for row in await cursor.fetchall()}


def _link_indexes(pages: list[Row]) -> tuple[set[str], set[str]]:
    active: set[str] = set()
    inactive: set[str] = set()
    for page in pages:
        target = active if page["status"] == "active" else inactive
        values = {str(page["title"]), str(page["path"]), _without_wiki(str(page["path"])), Path(str(page["path"])).stem}
        for alias in page.get("aliases", []):
            values.add(str(alias))
        for value in values:
            target.add(_normalize_link_target(value))
    return active, inactive


def _wiki_links(page: Page) -> list[str]:
    targets: list[str] = []
    for segment in page.segments:
        if isinstance(segment, ProseSegment):
            targets.extend(match.group("target") for match in _WIKI_LINK_RE.finditer(segment.text))
        elif isinstance(segment, BlockSegment):
            targets.extend(match.group("target") for match in _WIKI_LINK_RE.finditer(segment.block.content))
    return targets


def _value_tokens(content: str) -> list[str]:
    tokens = set(_NUMBER_RE.findall(content))
    tokens.update(match.group(1).strip() for match in _CODE_LITERAL_RE.finditer(content) if match.group(1).strip())
    for token in _IDENTIFIER_RE.findall(content):
        if "_" in token or any(character.isdigit() for character in token) or token.isupper():
            tokens.add(token)
    return sorted(tokens, key=str.casefold)


def _section_body(markdown: str, section: str) -> str | None:
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {section}":
            start = index + 1
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith("## ") and line.strip() != f"## {section}":
            end = index
            break
    return "\n".join(lines[start:end]).strip("\n")


def _source_flow_counts(text: str) -> tuple[int, int]:
    nodes = len(re.findall(r"(?m)^\s*-\s*id\s*:", text))
    edges = len(re.findall(r"(?m)^\s*-\s*from\s*:", text))
    if not nodes:
        nodes = len(re.findall(r'"id"\s*:', text))
    if not edges:
        edges = len(re.findall(r'"from"\s*:', text))
    return nodes, edges


def _mermaid_flow_counts(content: str) -> tuple[int, int]:
    node_ids: set[str] = set()
    edges = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```") or stripped == "flowchart TD":
            continue
        if "-->" in stripped:
            edges += 1
            left, right = stripped.split("-->", 1)
            node_ids.add(_node_id_from_mermaid(left))
            node_ids.add(_node_id_from_mermaid(right.split("|")[-1]))
        else:
            node_ids.add(_node_id_from_mermaid(stripped))
    return len({node for node in node_ids if node}), edges


def _node_id_from_mermaid(value: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_]+)", value)
    return match.group(1) if match else ""


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_link_target(target: str) -> str:
    cleaned = target.strip().strip("/")
    cleaned = cleaned.removeprefix("wiki/")
    if cleaned.endswith(".md"):
        cleaned = cleaned[:-3]
    return cleaned.casefold()


def _without_wiki(path: str) -> str:
    return path.removeprefix("wiki/")


def _rel(parsed: ParsedWikiPage) -> str:
    return str(parsed.row["path"])


def _finding(code: str, severity: str, path: str, ref: str, message: str) -> LintFinding:
    return LintFinding(
        code=code,
        severity=LintSeverity(severity),
        path=path,
        ref=ref,
        message=message,
    )


def _stable_unique(findings: list[LintFinding]) -> list[LintFinding]:
    deduped: dict[tuple[str, str, str, str], LintFinding] = {}
    for finding in findings:
        deduped.setdefault((finding.code, finding.path, finding.ref, finding.message), finding)
    severity_order = {"error": 0, "warn": 1}
    return sorted(
        deduped.values(),
        key=lambda item: (item.code, severity_order[item.severity.value], item.path, item.ref, item.message),
    )

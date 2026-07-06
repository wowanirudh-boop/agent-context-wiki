from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blocks.model import ContextBlock
from core.blocks.parser import parse_page
from core.db.dao import Row
from core.models import ReviewDecision, ReviewRowKind
from core.review.parse import parse_review_file

CSV_COLUMNS = [
    "row_id",
    "page",
    "key",
    "conflict_type",
    "existing_content_short",
    "candidate_content_short",
    "existing_source",
    "candidate_source",
    "recommendation",
    "decision",
    "notes",
]


@dataclass(frozen=True, slots=True)
class ImportDecisionsResult:
    csv_path: Path
    review_path: Path
    updated_rows: int


class ImportDecisionError(ValueError):
    pass


def write_review_csv(
    csv_path: Path,
    *,
    workspace: Path,
    rows: list[Row],
    pages: dict[str, Row],
) -> Path:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(workspace, row, pages))
    return csv_path


def import_decisions_from_csv(workspace: str | Path, csv_path: str | Path) -> ImportDecisionsResult:
    del workspace
    path = Path(csv_path)
    review_path = path.with_suffix(".md")
    if not review_path.exists():
        raise ImportDecisionError(f"review markdown not found for CSV: {review_path}")

    text = review_path.read_text(encoding="utf-8")
    parsed = parse_review_file(text)
    known_rows = {row.id for row in parsed.rows}
    updates: dict[str, tuple[str | None, str]] = {}
    errors: list[str] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            row_id = str(row.get("row_id") or "").strip()
            if not row_id:
                errors.append(f"line {line_number}: missing row_id")
                continue
            if row_id not in known_rows:
                errors.append(f"{row_id}: unknown row_id")
                continue
            decision = str(row.get("decision") or "").strip().casefold()
            if decision and decision not in {item.value for item in ReviewDecision}:
                errors.append(f"{row_id}: invalid decision: {decision}")
                continue
            updates[row_id] = (decision or None, str(row.get("notes") or ""))

    if errors:
        raise ImportDecisionError("\n".join(errors))

    review_path.write_text(_update_review_text(text, updates), encoding="utf-8")
    return ImportDecisionsResult(csv_path=path, review_path=review_path, updated_rows=len(updates))


def _csv_row(workspace: Path, row: Row, pages: dict[str, Row]) -> dict[str, str]:
    if row["row_kind"] == ReviewRowKind.exclusion.value:
        return _exclusion_csv_row(row)

    page = pages.get(str(row["page_id"]))
    page_blocks = _page_blocks(workspace, page) if page is not None else {}
    existing = page_blocks.get(str(row["existing_block_id"]))
    candidate = _candidate_block(row)
    key = ""
    if candidate is not None:
        key = candidate.key
    elif existing is not None:
        key = existing.key
    return {
        "row_id": str(row["id"]),
        "page": str(page["path"]) if page is not None else "",
        "key": key,
        "conflict_type": str(row["conflict_type"] or ""),
        "existing_content_short": _short(existing.content if existing is not None else ""),
        "candidate_content_short": _short(candidate.content if candidate is not None else ""),
        "existing_source": existing.source_path if existing is not None else "",
        "candidate_source": candidate.source_path if candidate is not None else "",
        "recommendation": str(row["recommendation"]),
        "decision": str(row["decision"] or ""),
        "notes": str(row["notes"] or ""),
    }


def _exclusion_csv_row(row: Row) -> dict[str, str]:
    data = _candidate_json(row)
    if not isinstance(data, dict):
        data = {}
    samples = "\n".join(str(sample) for sample in data.get("samples", []) if str(sample).strip())
    reason = str(data.get("reason") or "")
    source_path = str(data.get("source_path") or "")
    count_text = f"excluded: {data.get('excluded_count', '')}/{data.get('total_count', '')}; reason: {reason}"
    return {
        "row_id": str(row["id"]),
        "page": source_path,
        "key": "",
        "conflict_type": "bulk_exclusion",
        "existing_content_short": _short(samples),
        "candidate_content_short": _short(count_text),
        "existing_source": source_path,
        "candidate_source": source_path,
        "recommendation": str(row["recommendation"]),
        "decision": str(row["decision"] or ""),
        "notes": str(row["notes"] or ""),
    }


def _page_blocks(workspace: Path, page: Row | None) -> dict[str, ContextBlock]:
    if page is None:
        return {}
    path = workspace / str(page["path"])
    if not path.exists():
        return {}
    return {block.id: block for block in parse_page(path.read_text(encoding="utf-8")).blocks}


def _candidate_block(row: Row) -> ContextBlock | None:
    raw = _candidate_json(row)
    if not isinstance(raw, dict):
        return None
    if "block" in raw and isinstance(raw["block"], dict):
        raw = raw["block"]
    try:
        return ContextBlock(**raw)
    except ValueError:
        return None


def _candidate_json(row: Row) -> Any:
    raw = row.get("candidate_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _short(value: str, limit: int = 150) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _update_review_text(text: str, updates: dict[str, tuple[str | None, str]]) -> str:
    matches = list(_ROW_RE.finditer(text))
    if not matches:
        return text
    pieces: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        row_id = match.group("id")
        section_start = match.end()
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pieces.append(text[cursor:section_start])
        section = text[section_start:section_end]
        if row_id in updates:
            decision, notes = updates[row_id]
            section = _replace_section_fields(section, decision=decision, notes=notes)
        pieces.append(section)
        cursor = section_end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _replace_section_fields(section: str, *, decision: str | None, notes: str) -> str:
    leading_newlines = section[: len(section) - len(section.lstrip("\n"))]
    trailing_newline = "\n" if section.endswith("\n") else ""
    body = section.strip("\n")
    lines = body.split("\n") if body else []
    lines = _replace_field(lines, "decision", decision or "")
    lines = _replace_field(lines, "notes", notes)
    return leading_newlines + "\n".join(lines) + trailing_newline


def _replace_field(lines: list[str], field: str, value: str) -> list[str]:
    prefix = f"- {field}:"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            first, *rest = value.splitlines()
            replacement = [f"{prefix} {first}".rstrip()]
            replacement.extend(rest)
            return [*lines[:index], *replacement]
    return [*lines, f"{prefix} {value}".rstrip()]


_ROW_RE = re.compile(
    r"^### Row (?P<id>\S+) \u00b7 (?P<kind>[a-z_]+)(?: \u00b7 (?P<conflict>[a-z_]+))?\s*$",
    re.MULTILINE,
)

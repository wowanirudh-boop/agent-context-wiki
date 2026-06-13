from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.models import ReviewDecision, ReviewRowKind


@dataclass(frozen=True, slots=True)
class ParsedReviewRow:
    id: str
    row_kind: str
    conflict_type: str | None
    decision: str | None
    notes: str
    validation_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ParsedReviewFile:
    review_id: str
    run_id: str
    rows: list[ParsedReviewRow]
    raw_text: str


def parse_review_file(text: str) -> ParsedReviewFile:
    review_match = re.search(r"^# Review (?P<review_id>\S+)\s*$", text, re.MULTILINE)
    run_match = re.search(r"^Run:\s*(?P<run_id>\S+)", text, re.MULTILINE)
    review_id = review_match.group("review_id") if review_match else ""
    run_id = run_match.group("run_id") if run_match else review_id.removeprefix("RR-")
    rows: list[ParsedReviewRow] = []
    matches = list(_ROW_RE.finditer(text))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[start:end]
        decision = _decision(section)
        notes = _notes(section)
        row_kind = match.group("kind")
        errors = _validate_decision(row_kind, decision)
        rows.append(
            ParsedReviewRow(
                id=match.group("id"),
                row_kind=row_kind,
                conflict_type=match.group("conflict"),
                decision=decision,
                notes=notes,
                validation_errors=errors,
            )
        )
    return ParsedReviewFile(review_id=review_id, run_id=run_id, rows=rows, raw_text=text)


def serialize_review_file(review: ParsedReviewFile) -> str:
    return review.raw_text


def unresolved_review_file(text: str) -> bool:
    parsed = parse_review_file(text)
    if not parsed.rows:
        return "Status: open" in text
    return any(row.decision is None or row.decision == ReviewDecision.needs_more_info.value for row in parsed.rows)


def _decision(section: str) -> str | None:
    match = re.search(r"^- decision:[ \t]*(?P<decision>[^\r\n]*)$", section, re.MULTILINE)
    if match is None:
        return None
    value = match.group("decision").strip().casefold()
    return value or None


def _notes(section: str) -> str:
    match = re.search(r"^- notes:[ \t]*(?P<notes>[^\r\n]*)$", section, re.MULTILINE)
    if match is None:
        return ""
    first_line = match.group("notes").strip()
    continuation = section[match.end() :].strip("\r\n")
    if not continuation.strip():
        return first_line
    if not first_line:
        return continuation.strip()
    return f"{first_line}\n{continuation.strip()}"


def _validate_decision(row_kind: str, decision: str | None) -> list[str]:
    if decision is None:
        return []
    errors: list[str] = []
    if decision not in {item.value for item in ReviewDecision}:
        errors.append(f"unknown decision: {decision}")
        return errors
    if row_kind == ReviewRowKind.taxonomy_merge.value and decision not in {
        ReviewDecision.merge.value,
        ReviewDecision.reject_new.value,
        ReviewDecision.needs_more_info.value,
    }:
        errors.append("taxonomy_merge rows only accept merge, reject_new, or needs_more_info")
    return errors


_ROW_RE = re.compile(
    r"^### Row (?P<id>\S+) \u00b7 (?P<kind>[a-z_]+)(?: \u00b7 (?P<conflict>[a-z_]+))?\s*$",
    re.MULTILINE,
)

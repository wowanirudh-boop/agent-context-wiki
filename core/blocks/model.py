from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.blocks.keys import SemanticKeyError, validate_semantic_key
from core.models import BlockStatus, BlockType

CANONICAL_SECTIONS = [
    "Summary",
    "Flow",
    "Rules",
    "API Details",
    "Requirements",
    "Edge Cases",
    "FAQs",
    "Terms and Conditions",
    "Troubleshooting",
    "Known Issues",
    "Historical Notes",
    "Decisions",
    "Open Questions",
    "Open Conflicts",
    "Deprecated",
    "Related Pages",
    "Source Coverage",
]

PENDING_MARKER_PREFIX = "\u26a0 pending review: "


def close_marker(block_id: str) -> str:
    return f"<!-- /cb {block_id} -->"


class ContextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    key: str
    type: BlockType
    status: BlockStatus
    source_id: str | None = None
    source_path: str
    source_date: str = "unknown"
    chunk_ids: list[str] = Field(default_factory=list)
    user_edited: bool = False
    content: str
    excerpt: str
    needs_review_reason: str | None = None
    pending_review_ids: list[str] = Field(default_factory=list)
    excerpt_source_date: bool | None = None

    @field_validator("key")
    @classmethod
    def _validate_key(cls, key: str) -> str:
        try:
            return validate_semantic_key(key)
        except SemanticKeyError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _validate_block_contract(self) -> ContextBlock:
        marker = close_marker(self.id)
        if marker in self.content:
            raise ValueError("Block content contains its own close marker")
        if marker in self.excerpt:
            raise ValueError("Block excerpt contains its own close marker")
        if self.status != BlockStatus.needs_review and self.needs_review_reason is not None:
            raise ValueError("needs_review_reason is only valid for needs_review blocks")
        return self


@dataclass
class ProseSegment:
    text: str


@dataclass
class BlockSegment:
    block: ContextBlock


Segment: TypeAlias = ProseSegment | BlockSegment


@dataclass
class Page:
    segments: list[Segment]

    @property
    def blocks(self) -> list[ContextBlock]:
        return [segment.block for segment in self.segments if isinstance(segment, BlockSegment)]


@dataclass(frozen=True)
class Position:
    segment_index: int
    offset: int = 0


@dataclass(frozen=True)
class Heading:
    name: str
    start: Position
    line_end: Position

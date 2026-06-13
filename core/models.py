from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ACWBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Disposition(StrEnum):
    pending = "pending"
    placed = "placed"
    duplicate = "duplicate"
    irrelevant = "irrelevant"
    conflicted_pending = "conflicted_pending"
    failed = "failed"
    failed_final = "failed_final"
    superseded = "superseded"


class BlockType(StrEnum):
    fact = "fact"
    rule = "rule"
    flow = "flow"
    api = "api"
    requirement = "requirement"
    faq = "faq"
    term = "term"
    troubleshooting = "troubleshooting"
    issue = "issue"
    decision = "decision"
    note = "note"


class BlockStatus(StrEnum):
    current = "current"
    needs_review = "needs_review"
    conflicted = "conflicted"
    deprecated = "deprecated"
    rejected = "rejected"
    deleted = "deleted"


class ConflictType(StrEnum):
    changed_value = "changed_value"
    changed_scope = "changed_scope"
    newer_disagrees = "newer_disagrees"
    refinement = "refinement"
    deprecated_reappears = "deprecated_reappears"
    missing_evidence = "missing_evidence"
    ambiguous_update = "ambiguous_update"


class ReviewDecision(StrEnum):
    accept_new = "accept_new"
    keep_existing = "keep_existing"
    merge = "merge"
    mark_conflicted = "mark_conflicted"
    deprecate_existing = "deprecate_existing"
    reject_new = "reject_new"
    delete_duplicate = "delete_duplicate"
    needs_more_info = "needs_more_info"


class ReviewRowKind(StrEnum):
    conflict = "conflict"
    needs_review = "needs_review"
    taxonomy_merge = "taxonomy_merge"


class RunStatus(StrEnum):
    running = "running"
    completed = "completed"
    aborted = "aborted"


class EventKind(StrEnum):
    run_started = "run.started"
    run_completed = "run.completed"
    run_aborted = "run.aborted"
    chunk_disposition = "chunk.disposition"
    block_created = "block.created"
    block_status = "block.status"
    review_emitted = "review.emitted"
    review_applied = "review.applied"
    decision_applied = "decision.applied"
    taxonomy_merge = "taxonomy.merge"
    taxonomy_split = "taxonomy.split"
    taxonomy_rename = "taxonomy.rename"
    llm_call = "llm.call"
    llm_validation_failed = "llm.validation_failed"
    git_commit = "git.commit"
    lock_acquired = "lock.acquired"
    lock_stolen = "lock.stolen"
    export_written = "export.written"
    hard_delete_executed = "hard_delete.executed"


class LintSeverity(StrEnum):
    error = "error"
    warn = "warn"


class LintFinding(ACWBaseModel):
    code: str
    severity: LintSeverity
    path: str
    ref: str
    message: str

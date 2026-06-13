from __future__ import annotations

import json
from pathlib import Path

from core.db.dao import ACWDao, Row, utc_now
from core.models import Disposition


class LedgerValidationError(ValueError):
    pass


class InvalidDispositionTransition(ValueError):
    pass


class ChunkLedger:
    def __init__(self, dao: ACWDao, *, max_attempts: int = 3) -> None:
        self.dao = dao
        self.max_attempts = max_attempts

    async def ensure_source_version(
        self,
        *,
        source_id: str,
        version_hash: str,
        source_date: str,
        source_date_origin: str,
        source_version_id: str | None = None,
        seen_at: str | None = None,
    ) -> Row:
        return await self.dao.create_source_version(
            source_id=source_id,
            version_hash=version_hash,
            source_date=source_date,
            source_date_origin=source_date_origin,
            source_version_id=source_version_id,
            seen_at=seen_at,
        )

    async def create_pending_chunk(
        self,
        *,
        source_id: str,
        source_version_id: str,
        content_hash: str,
        ordinal: int,
        document_chunk_id: str | None = None,
        chunk_id: str | None = None,
        updated_at: str | None = None,
    ) -> Row:
        return await self.dao.create_chunk(
            source_id=source_id,
            source_version_id=source_version_id,
            content_hash=content_hash,
            document_chunk_id=document_chunk_id,
            ordinal=ordinal,
            chunk_id=chunk_id,
            updated_at=updated_at,
        )

    async def mark_placed(self, chunk_id: str, *, block_ids: list[str], updated_at: str | None = None) -> Row:
        if not block_ids:
            raise LedgerValidationError("placed chunks require at least one block id")
        row = await self._transition(
            chunk_id,
            Disposition.placed,
            reason=None,
            duplicate_of_block_id=None,
            attempts_delta=0,
            updated_at=updated_at,
        )
        for block_id in block_ids:
            await self.dao.link_block_chunk(block_id, chunk_id)
        return row

    async def mark_duplicate(
        self,
        chunk_id: str,
        *,
        duplicate_of_block_id: str,
        reason: str | None = None,
        updated_at: str | None = None,
    ) -> Row:
        if not duplicate_of_block_id:
            raise LedgerValidationError("duplicate chunks require duplicate_of_block_id")
        return await self._transition(
            chunk_id,
            Disposition.duplicate,
            reason=reason,
            duplicate_of_block_id=duplicate_of_block_id,
            attempts_delta=0,
            updated_at=updated_at,
        )

    async def mark_irrelevant(self, chunk_id: str, *, reason: str, updated_at: str | None = None) -> Row:
        if not reason:
            raise LedgerValidationError("irrelevant chunks require a reason")
        return await self._transition(
            chunk_id,
            Disposition.irrelevant,
            reason=reason,
            duplicate_of_block_id=None,
            attempts_delta=0,
            updated_at=updated_at,
        )

    async def mark_conflicted_pending(self, chunk_id: str, *, reason: str | None = None, updated_at: str | None = None) -> Row:
        return await self._transition(
            chunk_id,
            Disposition.conflicted_pending,
            reason=reason,
            duplicate_of_block_id=None,
            attempts_delta=0,
            updated_at=updated_at,
        )

    async def mark_failed(self, chunk_id: str, *, reason: str, updated_at: str | None = None) -> Row:
        if not reason:
            raise LedgerValidationError("failed chunks require error detail")
        current = await self._get_existing_chunk(chunk_id)
        next_attempts = int(current["attempts"]) + 1
        disposition = Disposition.failed_final if next_attempts >= self.max_attempts else Disposition.failed
        return await self._transition(
            chunk_id,
            disposition,
            reason=reason,
            duplicate_of_block_id=None,
            attempts_delta=1,
            updated_at=updated_at,
        )

    async def mark_pending_for_retry(self, chunk_id: str, *, updated_at: str | None = None) -> Row:
        current = await self._get_existing_chunk(chunk_id)
        if int(current["attempts"]) >= self.max_attempts:
            raise InvalidDispositionTransition("failed_final chunks cannot retry")
        return await self._transition(
            chunk_id,
            Disposition.pending,
            reason=None,
            duplicate_of_block_id=None,
            attempts_delta=0,
            updated_at=updated_at,
        )

    async def mark_superseded(self, chunk_id: str, *, reason: str | None = None, updated_at: str | None = None) -> Row:
        return await self._transition(
            chunk_id,
            Disposition.superseded,
            reason=reason,
            duplicate_of_block_id=None,
            attempts_delta=0,
            updated_at=updated_at,
        )

    async def export_json(self, workspace: str | Path, *, run_id: str, exported_at: str | None = None) -> Path:
        meta_dir = Path(workspace) / "wiki" / "_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        path = meta_dir / "ledger.json"
        payload = {
            "exported_at": exported_at or utc_now(),
            "run_id": run_id,
            "chunks": await self.dao.list_chunks_for_export(),
            "block_chunks": await self.dao.list_block_chunks(),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    async def _transition(
        self,
        chunk_id: str,
        target: Disposition,
        *,
        reason: str | None,
        duplicate_of_block_id: str | None,
        attempts_delta: int,
        updated_at: str | None,
    ) -> Row:
        current = await self._get_existing_chunk(chunk_id)
        source = Disposition(current["disposition"])
        if target not in allowed_transitions(source):
            raise InvalidDispositionTransition(f"{source.value} -> {target.value} is not allowed")

        if target in {Disposition.failed, Disposition.failed_final, Disposition.irrelevant} and not reason:
            raise LedgerValidationError(f"{target.value} chunks require disposition_reason")
        attempts = int(current["attempts"]) + attempts_delta
        return await self.dao.update_chunk(
            chunk_id,
            disposition=target.value,
            disposition_reason=reason,
            duplicate_of_block_id=duplicate_of_block_id,
            attempts=attempts,
            updated_at=updated_at or utc_now(),
        )

    async def _get_existing_chunk(self, chunk_id: str) -> Row:
        row = await self.dao.get_chunk(chunk_id)
        if row is None:
            raise KeyError(chunk_id)
        return row


def allowed_transitions(source: Disposition) -> set[Disposition]:
    if source == Disposition.pending:
        return {
            Disposition.placed,
            Disposition.duplicate,
            Disposition.irrelevant,
            Disposition.conflicted_pending,
            Disposition.failed,
            Disposition.failed_final,
            Disposition.superseded,
        }
    if source == Disposition.failed:
        return {Disposition.pending, Disposition.failed_final, Disposition.superseded}
    if source == Disposition.conflicted_pending:
        return {Disposition.placed, Disposition.duplicate, Disposition.superseded}
    return {Disposition.superseded}

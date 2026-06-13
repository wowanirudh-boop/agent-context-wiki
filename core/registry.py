from __future__ import annotations

import json
from pathlib import Path

from core.db.dao import ACWDao, Row, utc_now
from core.ids import new_id


class RegistryValidationError(ValueError):
    pass


class PageRegistry:
    def __init__(self, dao: ACWDao) -> None:
        self.dao = dao

    async def create_page(
        self,
        *,
        title: str,
        path: str,
        description: str,
        domain: str = "",
        aliases: list[str] | None = None,
        page_id: str | None = None,
        created_at: str | None = None,
    ) -> Row:
        row = await self.dao.create_page(
            page_id=page_id or new_id("pg"),
            path=path,
            title=title,
            description=description,
            status="active",
            domain=domain,
            created_at=created_at or utc_now(),
            aliases=_clean_aliases(aliases or []),
        )
        return await self._with_aliases(row)

    async def get_page(self, page_id: str) -> Row | None:
        row = await self.dao.get_page(page_id)
        if row is None:
            return None
        return await self._with_aliases(row)

    async def update_page(
        self,
        page_id: str,
        *,
        title: str | None = None,
        path: str | None = None,
        description: str | None = None,
        status: str | None = None,
        domain: str | None = None,
        aliases: list[str] | None = None,
    ) -> Row:
        fields: dict[str, str] = {}
        if title is not None:
            fields["title"] = title
        if path is not None:
            fields["path"] = path
        if description is not None:
            fields["description"] = description
        if status is not None:
            _validate_status(status)
            fields["status"] = status
        if domain is not None:
            fields["domain"] = domain

        row = await self.dao.update_page(page_id, fields)
        if aliases is not None:
            await self.dao.replace_aliases(page_id, _clean_aliases(aliases))
            await self.dao.db.commit()
        return await self._with_aliases(row)

    async def archive_page(self, page_id: str) -> Row:
        return await self.update_page(page_id, status="archived")

    async def resolve_page(self, ref: str) -> Row | None:
        needle = ref.casefold()
        for page in await self.list_pages():
            if page["status"] != "active":
                continue
            values = [page["path"], page["title"], *page["aliases"]]
            if any(value.casefold() == needle for value in values):
                return page
        return None

    async def list_pages(self) -> list[Row]:
        return [await self._with_aliases(row) for row in await self.dao.list_pages()]

    async def export_json(self, workspace: str | Path, *, exported_at: str | None = None) -> Path:
        meta_dir = Path(workspace) / "wiki" / "_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        path = meta_dir / "registry.json"
        payload = {
            "exported_at": exported_at or utc_now(),
            "pages": await self.list_pages(),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    async def _with_aliases(self, row: Row) -> Row:
        page = dict(row)
        page["aliases"] = await self.dao.list_aliases(page["id"])
        return page


def _clean_aliases(aliases: list[str]) -> list[str]:
    return sorted({alias.strip() for alias in aliases if alias.strip()}, key=str.lower)


def _validate_status(status: str) -> None:
    if status in {"active", "archived"}:
        return
    if status.startswith("merged_into:pg_"):
        return
    raise RegistryValidationError(f"Invalid page status: {status}")

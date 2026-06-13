from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "shared" / "migrations_local"
_MIGRATION_NAME = re.compile(r"^(?P<version>\d{3})_.+\.sql$")


async def apply_migrations(
    db: aiosqlite.Connection,
    migrations_dir: str | Path = MIGRATIONS_DIR,
) -> list[int]:
    await db.execute("PRAGMA foreign_keys=ON")
    applied = await _applied_versions(db)
    applied_now: list[int] = []

    for path in _migration_files(Path(migrations_dir)):
        version = _migration_version(path)
        if version in applied:
            continue

        await db.executescript(path.read_text(encoding="utf-8"))
        await db.execute(
            "INSERT INTO acw_schema_version (version, applied_at) VALUES (?, ?)",
            (version, _utc_now()),
        )
        await db.commit()
        applied.add(version)
        applied_now.append(version)

    return applied_now


async def migrate_workspace(workspace: str | Path) -> list[int]:
    db_path = Path(workspace) / ".llmwiki" / "index.db"
    async with aiosqlite.connect(db_path) as db:
        return await apply_migrations(db)


async def _applied_versions(db: aiosqlite.Connection) -> set[int]:
    if not await _table_exists(db, "acw_schema_version"):
        return set()

    cursor = await db.execute("SELECT version FROM acw_schema_version")
    return {int(row[0]) for row in await cursor.fetchall()}


async def _table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return await cursor.fetchone() is not None


def _migration_files(migrations_dir: Path) -> list[Path]:
    if not migrations_dir.exists():
        return []
    return sorted(path for path in migrations_dir.iterdir() if _MIGRATION_NAME.match(path.name))


def _migration_version(path: Path) -> int:
    match = _MIGRATION_NAME.match(path.name)
    if match is None:
        raise ValueError(f"Invalid migration filename: {path.name}")
    return int(match.group("version"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

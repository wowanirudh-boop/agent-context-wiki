from __future__ import annotations

from pathlib import Path

import aiosqlite

from core.config import ACWConfig, load_config
from core.db.migrate import apply_migrations
from core.lint.checks import run_checks
from core.models import LintFinding, LintSeverity


async def lint_workspace(
    workspace: str | Path,
    *,
    config: ACWConfig | None = None,
) -> list[LintFinding]:
    ws = Path(workspace)
    db_path = ws / ".llmwiki" / "index.db"
    cfg = config or load_config(ws)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await apply_migrations(db)
        return await run_checks(ws, db, cfg)


def has_errors(findings: list[LintFinding]) -> bool:
    return any(finding.severity == LintSeverity.error for finding in findings)


def findings_to_json_lines(findings: list[LintFinding]) -> str:
    return "".join(f"{finding.model_dump_json()}\n" for finding in findings)


def findings_to_table(findings: list[LintFinding]) -> str:
    if not findings:
        return "Lint passed.\n"
    rows = ["Severity  Code     Path                  Ref                   Message"]
    rows.append("--------  -------  --------------------  --------------------  -------")
    for finding in findings:
        rows.append(
            f"{finding.severity.value:<8}  {finding.code:<7}  "
            f"{_clip(finding.path, 20):<20}  {_clip(finding.ref, 20):<20}  {finding.message}"
        )
    return "\n".join(rows).rstrip() + "\n"


def _clip(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return f"{value[: width - 3]}..."

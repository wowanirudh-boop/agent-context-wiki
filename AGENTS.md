# AGENTS.md — Agent Context Wiki v2 (fork of llm-wiki)

You are building **Agent Context Wiki v2**: a fork of the existing `llm-wiki` repo that turns it
from a *maintained-summary* wiki into a *lossless, conflict-aware, agent-consumable* context system.

This file is your standing instruction set. It applies to every task in this repo.

## 0. Read order (do this at the start of every work session)

1. `AGENTS.md` (this file)
2. `docs/harness/00_OVERVIEW.md` — how the harness fits together
3. `docs/PRD.md` — the product requirements (normative; FR-IDs are traceability anchors)
4. `docs/harness/01_ARCHITECTURE.md` — module map and data flow (normative)
5. `docs/harness/02_DATA_CONTRACTS.md` — schemas, grammars, file formats (normative; do not invent fields)
6. `docs/harness/03_LLM_INTERFACES.md` — the only permitted LLM call sites and their I/O contracts
7. `docs/harness/04_TDD_PLAN.md` — test strategy, fixtures, FakeLLM
8. `docs/harness/05_BUILD_PLAN.md` — the phase you are currently implementing
9. `docs/harness/07_DECISIONS.md` — resolved ambiguities; append your own decisions here

Only read the phase of `05_BUILD_PLAN.md` you were asked to implement, plus its dependencies.

## 1. What already exists (do not rebuild it)

This is a working codebase, not a green field:

- `api/` — FastAPI backend. `api/services/chunker.py` chunks text (~512 tokens, 128 overlap,
  header breadcrumbs). `api/domain/watcher.py` + `api/domain/local_processor.py` index and
  process source files into SQLite. `api/domain/local_index.py` has path/hash helpers.
- `mcp/` — stdio MCP server (FastMCP). Tools: `guide`, `search`, `read`, `create`, `edit`,
  `append`, `delete`, `lint`, plus references helpers. `mcp/vaultfs/` is the storage
  abstraction (`SqliteVaultFS` for local mode).
- `shared/sqlite_schema.sql` — local index schema: `documents`, `document_pages`,
  `document_chunks` (+ FTS5 `chunks_fts`), `document_references`.
- `llmwiki` — CLI entrypoint (`init`, `serve`, `mcp`, `mcp-config`, `reindex`, `open`).
- `tests/` — pytest (`asyncio_mode = auto`), unit + integration. `ruff.toml` for lint.
- `web/` — Next.js UI. **Out of scope for v2 unless a phase explicitly says otherwise.**
- Hosted mode (Postgres/Supabase/S3 paths in `api/`, `mcp/hosted.py`, `mcp/vaultfs/postgres.py`)
  — **out of scope. Never modify hosted-mode code paths. v2 is local-mode only.**

## 2. What you are adding

A new top-level Python package `core/` containing the v2 engine (ledger, registry, block model,
placement pipeline, conflict detection, review workflow, re-ingestion, summaries, lint, eval,
git ops, locking), new CLI subcommands on `llmwiki` (`process`, `apply-decisions`, `lint`,
`coverage`, `eval`, `merge-pages`, `split-page`, `rename-page`), new read-tier MCP tools
(`wiki_index`, `wiki_summary`, `wiki_page`, `wiki_search`), and SQLite migrations adding the
`acw_*` tables. The exact module map is in `01_ARCHITECTURE.md`; the exact schemas in
`02_DATA_CONTRACTS.md`.

## 3. Non-negotiable invariants

Violating any of these is a defect even if tests pass. Several have dedicated invariant tests
(`tests/v2/invariants/`) that must run green after every pipeline operation.

1. **Source files are never modified, moved, or deleted by the system.** (NFR-05)
2. **The ledger is ground truth for coverage.** Any markdown that states coverage
   (Source Coverage sections) is rendered from `acw_chunk_ledger`, never hand-written.
3. **Every chunk reaches a tracked disposition.** A `process` run is not complete while any
   in-scope chunk is `pending`. (FR-LEDGER-04)
4. **Fidelity by construction.** Every block carries a verbatim `excerpt`. Restatements never
   replace evidence. (FR-BLOCK-05)
5. **Deterministic mutation.** All edits to existing page content driven by decisions, status
   changes, or taxonomy ops are mechanical operations on the *parsed page model*. Never
   free-form LLM rewrites of existing content. Never regex edits of raw page text.
   (FR-BLOCK-04; the single exception is the `merge` decision draft, see 03_LLM_INTERFACES.)
6. **LLM calls happen only at the call sites enumerated in `03_LLM_INTERFACES.md`**, through
   `core/llm/provider.py`. No inline prompts anywhere else. All calls logged to `acw_events`.
7. **The system never auto-resolves conflicts** (sole exception: exact duplicate from same
   source path, FR-CONF-03).
8. **Soft delete by default.** `rejected`/`deleted`/`deprecated` are statuses, not file
   removals. Hard delete is the explicit command in FR-GIT-03 only.
9. **User-edited blocks are protected.** Once `user_edited: true`, the system never modifies
   that block's content. (FR-GIT-02)
10. **Idempotency.** Re-running `process` with no source changes performs zero page writes and
    zero LLM calls. (NFR-01 — there is a test for this; it must stay green.)
11. **Round-trip integrity.** `serialize(parse(page)) == page` byte-identical for every page
    the system writes. (FR-BLOCK-04, FR-LINT-07)

## 4. Engineering conventions

- Python 3.11+, full type hints on all new code. `from __future__ import annotations` in every
  new module.
- Async: `aiosqlite` for DB, matching the existing codebase. Reuse the existing connection
  patterns from `api/infra/db/sqlite.py` / `mcp/vaultfs/sqlite.py`; do not introduce an ORM.
- New dependencies allowed for v2: `python-ulid`, `openai` (provider default), `hypothesis`
  (dev/test only). Anything else: justify in `07_DECISIONS.md` first. Pin versions in
  `core/requirements.txt` and add to `api/requirements.txt` + `mcp/requirements.txt` only if
  those processes import `core`.
- Pydantic v2 (already a dependency) for all data contracts in `core/models.py`. The JSON
  schemas in `02_DATA_CONTRACTS.md` are the contract; Pydantic models must match them exactly.
- Lint: `ruff check .` must pass with the existing `ruff.toml`. Format consistently with the
  surrounding code.
- Migrations: numbered SQL files in `shared/migrations_local/NNN_*.sql`, applied by
  `core/db/migrate.py` which tracks versions in `acw_schema_version`. Never edit an applied
  migration; add a new one.
- Logging: stdlib `logging`, logger name `acw.<module>`. No prints in library code.
- Errors that affect a chunk land in the ledger as `failed` dispositions with detail —
  never console-only (NFR-03).

## 5. TDD process (mandatory)

1. For the phase you are implementing, **write or extend the tests listed in
   `04_TDD_PLAN.md` for that phase first**, watch them fail, then implement.
2. Never weaken, skip, or delete an existing test to make your change pass. If a test is
   genuinely wrong, fix it and record why in `07_DECISIONS.md`.
3. All LLM interactions in tests go through `tests/v2/fakes/fake_llm.py` (FakeLLM). Unit and
   integration tests must pass with **no network and no API key**.
4. Use real temp SQLite databases (tmp_path fixtures), not mocks of the DB layer.
5. The invariant suite (`pytest tests/v2/invariants`) is wired to run inside end-to-end tests
   after every pipeline operation via the `assert_invariants(workspace)` helper.

## 6. Commands / Definition of Done

`make check` is the universal gate. It runs, in order:

```
ruff check .
pytest tests/unit tests/v2 -q
pytest tests/integration -q          # skipped automatically if docker/postgres unavailable
```

A phase is done only when:
- `make check` is green,
- the phase's Definition-of-Done commands in `05_BUILD_PLAN.md` all succeed,
- new behavior is traceable to FR-IDs in code comments or test names (e.g.
  `test_fr_ledger_04_run_completes_only_when_no_pending`).

If `make` does not exist yet you are in Phase M0; creating the `Makefile` is part of M0.

## 7. Things you must NOT do

- Do not modify `web/`, `extension/`, `converter/`, `supabase/`, or hosted-mode code.
- Do not change the public behavior of existing MCP tools (`guide`, `search`, `read`,
  `create`, `edit`, `append`, `delete`) except where a phase explicitly routes maintaining-agent
  writes through the block-aware serializer (§10 of PRD, Phase M7).
- Do not rename or restructure existing modules to "clean up". Extend, don't churn.
- Do not invent schema fields, enum values, statuses, decision names, or tool parameters that
  are not in `02_DATA_CONTRACTS.md`. If something is missing, pick the minimal addition,
  implement it, and append the decision to `07_DECISIONS.md`.
- Do not store secrets. LLM credentials come from env (`ACW_LLM_API_KEY`, `ACW_LLM_BASE_URL`,
  `ACW_LLM_MODEL`) loaded in `core/config.py`.
- Do not catch-and-swallow exceptions in the pipeline; convert them into `failed` dispositions
  with detail, or let them abort the run cleanly (the run is resumable from ledger state).

## 8. Handling ambiguity

When the PRD, contracts, and decisions log do not answer a question:
1. Choose the **simplest option that satisfies the FR-ID verbatim**.
2. Implement it behind a small function so it can change later.
3. Append a dated entry to `docs/harness/07_DECISIONS.md` under "Codex additions".
Never silently expand scope. Never ask the user to choose between options you could decide.

## 9. Local development environment

This project is built with the Codex desktop app in **local mode**: you run on the user's
machine, in a clone/worktree of the repo. Therefore:

- All work happens inside a project-local virtualenv at `.venv/`. `make setup` must create it
  (python3.11+; if only a newer python is present, use it) and install
  `api/requirements.txt`, `mcp/requirements.txt`, `core/requirements.txt` (once it exists),
  and the dev/test deps (`ruff`, `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`,
  `markdown-it-py`). Every other make target depends on `setup` having run and invokes tools
  via `.venv/bin/…` (or the Windows equivalent), never globally installed tools.
- If `make setup` fails because no suitable Python exists on the machine, stop and tell the
  user exactly what to install (one sentence + download link); do not work around it with
  system-package hacks.
- Worktrees: the Codex app may run each thread in a fresh worktree. `.venv/` is gitignored, so
  re-run `make setup` whenever it's missing before running checks.
- Tests are hermetic (FakeLLM, tmp dirs) — never read or write outside the repo and pytest tmp
  paths; never require network or API keys for `make check`.
- At the end of a phase, leave the work as a single coherent set of staged/committed changes
  with a clear message (`M<phase>: <summary>`) per the app's normal flow. Do not push unless
  the user asks.

## 10. Workspace vocabulary

Use PRD §2 terms precisely in code, tests, and docs: Source, Chunk, Disposition, Context block,
Semantic key, Page registry, Chunk ledger, Coverage, Fidelity, Processing run. Name things after
these terms (`ChunkLedger`, `PageRegistry`, `ProcessingRun`, `ContextBlock`, …).

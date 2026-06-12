# 00 — Harness Overview

## What this harness is

A complete, self-contained instruction-and-contract pack that lets a coding agent (OpenAI Codex)
build Agent Context Wiki v2 on a fork of `llm-wiki` with minimal human steering. It consists of:

| File | Role | Normative? |
|---|---|---|
| `/AGENTS.md` | Standing rules Codex reads every session | Yes |
| `docs/PRD.md` | Product requirements (FR-IDs) | Yes |
| `01_ARCHITECTURE.md` | Module map, data flow, FR traceability | Yes |
| `02_DATA_CONTRACTS.md` | SQLite DDL, block grammar, review-file format, JSON exports, MCP tool I/O | Yes — single source of truth for every schema |
| `03_LLM_INTERFACES.md` | The 6 permitted LLM call sites, payloads, output JSON schemas, validation/retry | Yes |
| `04_TDD_PLAN.md` | Test inventory per FR, fixtures, FakeLLM, golden workspaces | Yes (process) |
| `05_BUILD_PLAN.md` | Phases M0–M9, each with scope, tests-first list, Definition of Done | Yes (sequence) |
| `06_CODEX_PROMPTS.md` | The exact prompts the human pastes into Codex, one per phase | Operational |
| `07_DECISIONS.md` | Resolved PRD open questions + running decision log | Yes |
| `.github/workflows/ci.yml` | CI gate mirroring `make check` | Operational |

Precedence when documents disagree: **02_DATA_CONTRACTS > PRD > 01_ARCHITECTURE > everything
else.** (The contracts doc is the PRD made executable; if you find a real contradiction with the
PRD, flag it in 07_DECISIONS and follow the contracts doc.)

## How the human uses it (setup, once)

1. Fork `wowanirudh-boop/llm-wiki` on GitHub into a new repo (e.g. `agent-context-wiki`).
   Do **not** open PRs against the original — both versions must keep existing independently.
2. Copy this harness into the fork root: `AGENTS.md`, `docs/PRD.md` (the v2 PRD),
   `docs/harness/*`, `.github/workflows/ci.yml`. Commit as `harness: add Codex build harness`.
3. Point Codex at the fork (Codex cloud: connect the repo; Codex CLI: run inside the clone).
4. Set env for live LLM usage later (not needed for the build itself — tests use FakeLLM):
   `ACW_LLM_API_KEY`, optionally `ACW_LLM_BASE_URL`, `ACW_LLM_MODEL`.
5. Open a fresh Codex task per phase and paste the corresponding prompt from
   `06_CODEX_PROMPTS.md`, starting with Prompt M0. Merge each phase before starting the next.
   Use the Repair prompt template if a phase comes back with failing checks.

## How Codex uses it

Every session: read `/AGENTS.md`, then the files it lists, then implement exactly one phase of
`05_BUILD_PLAN.md` test-first, finish with `make check` green plus the phase DoD commands.
Ambiguities are resolved per AGENTS.md §8 and logged in `07_DECISIONS.md`.

## End state

After M9, a user can: drop heterogeneous sources into a workspace; run `./llmwiki process <ws>`
and get domain pages of source-attributed context blocks with verbatim excerpts and
ledger-rendered Source Coverage; receive one review file per run for conflicts; run
`./llmwiki apply-decisions <ws>`; have agents consume the wiki over MCP via the two-tier read
surface; and verify everything with `./llmwiki lint <ws>` and `./llmwiki eval <ws>` — matching
PRD §13 MVP definition and §14 acceptance scenarios.

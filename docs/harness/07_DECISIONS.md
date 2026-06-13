# 07 — Decisions Log

Resolved ambiguities. Codex: consult before deciding anything; append new entries under
"Codex additions" with date + rationale. This file is normative below 02_DATA_CONTRACTS and
the PRD.

## PRD §16 open questions — resolved

- **Q1 (auto-process):** Default **manual** `llmwiki process`. `ACW_AUTO_PROCESS=1` (or
  `[acw] auto_process = true`) enables watcher-triggered runs (debounced 5s, lock-respecting,
  queued during locks). Built in M8.
- **Q2 (pre-approved merge text):** **Yes.** `notes:` beginning `approved-merge:` applies the
  merged content immediately, skipping the second review round (02_DATA_CONTRACTS §7).
- **Q3 (embeddings):** **No embeddings in MVP.** Conflict-candidate retrieval = semantic-key
  exact/prefix match + FTS5 over target and registry-linked pages. The eval harness measures
  conflict recall; embeddings are a post-MVP decision gated on that measurement.

## Harness architectural decisions (made for the PRD's "decide, don't list options" rule)

| # | Decision | Rationale |
|---|---|---|
| D1 | New v2 engine lives in a single top-level `core/` package imported by CLI, `api/`, and `mcp/` | The repo already duplicated `chunker.py` between api/ and mcp/; a shared package prevents a third copy of v2 logic |
| D2 | New tables are prefixed `acw_` and added by versioned migrations; `documents` doubles as the PRD `sources` table (identity = `relative_path`); `document_chunks` is extended, not replaced | Minimal-churn fork; existing index/watcher keep working; PRD §9 says schema is a minimum sketch, extension allowed |
| D3 | Ledger chunk identity `(source_id, source_version, content_hash, ordinal)` with stable `ch_` ULID; `ordinal` added to the PRD triple | Identical text can legitimately appear twice in one source; without ordinal the UNIQUE constraint would collapse them |
| D4 | LLM provider: OpenAI-compatible (`openai` SDK), base-URL overridable; light/heavy model split via `ACW_LLM_MODEL_LIGHT` | User is building with Codex/OpenAI; base-URL override keeps it vendor-portable |
| D5 | Block delimiter integrity: parser matches close marker by exact block id; serializer rejects content containing the block's own close-marker string | Preserves verbatim excerpts (no escaping mutilation) while making round-trip unambiguous |
| D6 | Excerpt fidelity is enforced *mechanically*: the C1 validator requires `excerpt` to be a whitespace-normalized substring of the chunk text | "Verbatim" must be machine-verifiable (PRD §2 Fidelity), not model-promised |
| D7 | Whimsical MCP JSON → Mermaid is fully deterministic code; only arbitrary YAML/XML flows use an LLM (C2) with node/edge count linting | FR-FLOW-02 fidelity is cheapest to guarantee without a model where the schema is known |
| D8 | Recommendation timestamp basis (`source_date` vs `mtime`) is computed in code and injected into the C3 prompt, never inferred by the model | FR-CONF-04 requires citing the basis truthfully |
| D9 | `keep_existing`/`reject_new` account for candidate chunks as `duplicate` with `duplicate_of_block_id = existing_block_id`, reason `rejected_candidate` | The PRD table's "placed→via rejection record" is ambiguous; `duplicate`-of-existing keeps the coverage invariant clean and auditable |
| D10 | Review decisions are edited in the markdown file; `acw_review_rows` stays authoritative for identity; apply = validate whole file, then apply row-by-row, one git commit | Markdown-as-UI per PRD non-goals, with DB-backed integrity |
| D11 | FakeLLM has scripted (strict fixtures) and rule-based (deterministic heuristics) modes; provider chosen by `ACW_LLM_PROVIDER` | Lets e2e/golden tests run hermetically while still exercising real pipeline paths |
| D12 | Sections are positional spans under H2 headings in a canonical order; "Open Conflicts" added to the PRD's canonical list | FR-CONF-05 requires the section; PRD §7.4 list omitted it |
| D13 | Stale-lock handling: lock JSON carries pid; dead pid → steal with `lock.stolen` event | FR-LOCK gives no crash-recovery rule; fail-forever would violate resumability |
| D14 | `web/`, hosted mode, extension, converter untouched in MVP | Risk containment; PRD is local-first |
| D15 | Coverage threshold 85% on `core/` from M4; CommonMark validation via `markdown-it-py` (dev dep) | NFR-04 needs an objective check |

## Codex additions

*(append below: date · phase · decision · rationale)*

- **2026-06-13 · M3 · Store source-kind classification in `documents.metadata.acw_source_kind`.**
  The `documents.source_kind` column is already constrained to `wiki|source|asset`, while M3
  needs the v2 classification `flow|transcript|doc`; a namespaced metadata key preserves the
  existing index behavior without widening the legacy column.

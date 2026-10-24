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
  exact/prefix match + FTS5, extended to unlinked active pages by the 2026-10-24 decision below. The eval harness measures
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
- **2026-06-13 · M5 · Store taxonomy merge peer snapshots in `acw_review_rows.candidate_json`.**
  The M5 schema has no second-page column for `taxonomy_merge` rows; using the existing JSON
  payload preserves review-row identity and avoids a migration while keeping advisory rows
  renderable.
- **2026-06-14 · M9 · Flow chunks may target an existing semantically overlapping active page before creating a new flow page.**
  AS-UC1-1 requires one workflow page containing both prose-derived blocks and the flow block;
  overlap on source/page words is the minimal deterministic rule that preserves the new-page
  fallback.
- **2026-06-14 · M9 · `nodes/<slug>` MCP read references resolve to existing node pages without changing registry paths.**
  AS-UC2-1 names `wiki_page("nodes/api-call")`; expanding read-side candidates avoids adding
  non-contract fields to placement `new_page` payloads.
- **2026-06-14 · M9 · Same-key changed values in FakeLLM acceptance fixtures produce `changed_value` conflicts.**
  The M9 scenarios need deterministic judgment for node setting updates and support RCA
  contradictions while keeping tests hermetic and API-key free.
- **2026-06-14 · M9 · `deprecate_existing` and same-key `accept_new` move deprecated blocks to the canonical Deprecated section.**
  AS-UC3-1 requires old guidance to remain readable as deprecated history; the move is a parsed
  page-model mutation, not a raw markdown rewrite.
- **2026-06-14 · M9 · The NFR-03 synthetic scale smoke marks generated chunks irrelevant by rule.**
  This keeps the test focused on 50-source/5,000-chunk ledger completion and failure accounting
  instead of measuring thousands of page insertions in the acceptance gate.
- **2026-07-06 · additive · Bulk exclusion rows are source-level review rows with `page_id=NULL`.**
  The checkpoint is about a source's irrelevant-chunk ratio rather than a page conflict; making
  `page_id` nullable for `row_kind=exclusion` avoids inventing placeholder pages while preserving
  DB-backed review-row identity.
- **2026-10-19 · M9 · Replace the invariant scaffold with checks against persisted SQLite and page bytes.**
  The previous helper returned without checking anything. I1–I9 now validate dispositions,
  links, block inventory/status, round trips, evidence, generated coverage, pending-review
  markers, and active-key uniqueness. I10 accepts a source-hash snapshot captured before an
  operation. Seeded-corruption tests prove these checks detect defects.
- **2026-10-19 · M9 · Strict response schemas describe every field at C1–C7.**
  Pydantic response contracts generate closed JSON objects with all properties required;
  optional values are nullable, as required by the OpenAI Responses structured-output API.
  C1/C6 must account for every input ID. The SDK transport closes clients, records usage and
  failed/incomplete responses, and counts calls. Tests use a fake transport, never a live key.
  Reference: https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses
- **2026-10-19 · M9 · Preserve complete excerpts, short tails, and complete flow documents.**
  The local chunking path opts into a minimum of one token; the shared chunker's default
  remains unchanged for existing callers. Full-source flow conversion produces one block
  linked to all participating chunks. Excerpts are no longer truncated in the middle.
  Page writes explicitly preserve LF bytes on Windows for round-trip integrity.
- **2026-10-19 · M9 · Track extraction failures without adding schema fields or dispositions.**
  CLI processing invokes the existing local extractor. A source that cannot produce text
  receives one failure record with nullable `document_chunk_id`, ordinal 0 and a synthetic
  `content_hash` of `extraction:<version_hash>`. It retries through the ordinary pending/
  failed/failed_final states; successful extraction supersedes the failure record. Unchanged
  binary sources retain extracted content and retry history. Original source files stay untouched.
- **2026-10-19 · M9 · Keep chunk disposition pending review until all its topics are decided.**
  Clean placements remain linked when another placement conflicts. After decisions, chunks
  with remaining reviews stay `conflicted_pending`; otherwise any retained placement means
  `placed`. D9's duplicate result applies only when no placement was retained. Invalid model
  batches record failures and exhaust the configured retries instead of leaving silent pending work.
- **2026-10-19 · M9 · Treat competing same-key distinct judgments as ambiguous updates.**
  Two current values for one key violate FR-LINT-03. A distinct judgment on an occupied key
  therefore produces an `ambiguous_update` review. Duplicate judgments from another source
  also require review under FR-CONF-03; same-source duplicates retain the automatic path.
- **2026-10-19 · M9 · Emit actionable reviews for source staleness and merge drafts.**
  A needs-review row without a candidate accepts `keep_existing`, `deprecate_existing`, or
  `needs_more_info`. An unchanged same-source block can regain current status after re-ingestion.
  Merge drafts immediately receive a follow-up review and matching marker. Applying a review
  verifies its database row kind and run, clears resolved markers, and regenerates conflict listings.
- **2026-10-19 · M9 · User-edited content may carry review metadata.**
  Adding/removing a pending marker preserves the content and excerpt exactly, like status
  changes already did. Replacing user-edited content remains forbidden. This lets agents see
  contested user-edited facts without losing the user's writing.
- **2026-10-19 · M9 · Settle transcript supersession before declaring a run complete.**
  The pre-pass sees the whole pending transcript. Temporary survivor references resolve to
  real blocks, inherit a failed survivor's tracked failure, or share its pending review.
  Superseded statements become duplicates of the approved survivor after that review is applied.
- **2026-10-19 · M9 · Taxonomy operations refresh the read surface.**
  Merge/split/rename refresh coverage, summaries, the index and exports. Merges retain manual
  prose and transfer aliases to the active target; moved blocks carry review-row page references.
  A rename that keeps the same path cannot replace its own content with a redirect.
- **2026-10-19 · M9 · Lock before indexing and queue watcher writes.**
  `process` acquires the workspace lock before scanning. Watcher indexing and extraction wait
  for the same lock. A clean placement re-reads and retries one concurrent page write; block
  metadata is persisted only after that write succeeds. Related-page links are now rendered
  through the parsed model. Proposed new page titles also resolve existing aliases.
- **2026-10-19 · M9 · Correct test fixtures rather than relaxing production requirements.**
  The bounded-read fixture includes both the 30s source and a nested 45s overlay; it now
  explicitly approves conflicts and requests history when checking both versions. The old
  test incorrectly expected both versions to be current without review. SDK mocks include
  usage/status/close. Integration fixtures isolate API/MCP module namespaces, use valid PDF
  signatures, authenticate converter requests and mock its current streaming/subprocess
  interfaces. Hosted production code and converter code are unchanged.
- **2026-10-19 · M9 · Use one reproducible release gate and a cross-platform quickstart.**
  `make check` includes integration tests and one coverage run; CI invokes the same target.
  Setup is cached until requirements change. Missing PostgreSQL skips only database-dependent
  tests. The Python quickstart tests process, repeat-run idempotency, change/review/apply,
  invariants, lint, eval, coverage and MCP config. Its Bash entrypoint is preserved; an explicit
  smoke directory is never removed. Live-model smoke remains opt-in with credentials.
- **2026-10-19 · M9 · Validate structural loss and invalid transcript supersession.**
  Flow responses must retain the source node/edge counts. Transcript supersession must point
  forward to a relevant survivor, never to itself or an earlier statement. The whole-source
  transcript pre-pass is followed by placement calls bounded by the configured batch size.
  An invalid summary is logged and skipped after retry, preserving successfully placed blocks;
  a later run can retry that unfinished derived summary.
- **2026-10-19 · M9 · Rebuild binary extraction and retain short pages.**
  An unchanged ready binary source whose extraction rows were removed by `reindex` returns to
  extraction. Local PDF/spreadsheet chunking retains short individual pages/tabs, not only
  short whole documents. Hosted chunking keeps its original minimum by default.
- **2026-10-19 · M9 · Enforce semantic identity after similarity retrieval.**
  The judge still compares retrieved candidates, but different keys cannot duplicate or
  conflict (PRD section 2). Similar wording is not sufficient to discard another attribute.
- **2026-10-19 · M9 · Guard taxonomy merges and complete placement context.**
  Merging pages with competing current/needs-review keys aborts before moving blocks; the
  user must resolve those facts first. This preserves FR-LINT-03 without auto-resolving a
  conflict. C1 now explicitly receives the canonical section list required by FR-PLACE-07.
  The invariant checker and quickstart close SQLite handles explicitly so Windows can
  remove the generated temporary workspace after successful verification.
- **2026-10-19 · M9 · Pass the repository path to the local API subprocess.**
  `serve` starts the backend from `api/`; it now includes the repository root in `PYTHONPATH`
  so local extraction and the shared v2 engine import correctly. A subprocess regression
  checks this without relying on pytest's import paths. Hosted startup is unchanged.
- **2026-10-19 · M9 · Exercise the real MCP launch on Windows.**
  Windows `execvp` loses argument quoting for paths containing spaces. The Windows CLI
  now uses `subprocess.call` with an argument list; POSIX retains `execvp`. A real stdio
  client regression initializes the server and calls all four v2 read tools against a
  generated wiki in a path containing spaces.
- **2026-10-24 · M9 follow-up · Find equivalent facts across pages at the user's request.**
  FR-CONF-01 now adds exact keys from every active page and a separate FTS5 pass over
  unlinked pages. Local and workspace keyword searches each return at most eight additional
  blocks; exact keys are not truncated. This preserves local priority without starving the
  workspace search. Block type names are excluded from search text because words such as
  `rule` do not identify a fact. Comparisons still use C3 only; no embeddings or dependencies.
  Broader retrieval may add model calls for new/changed chunks; unchanged runs still call none.
  The original retrieval test's exclusion of all unlinked pages was intentionally replaced
  with an ordering assertion because that old scope contradicts the requested extension.
- **2026-10-24 · M9 follow-up · Explicit pairwise key resolution, always reviewed.**
  C3 gains nullable `resolved_key`, constrained to the compared existing key, and explicit
  instructions to distinguish entities, attributes and independent scopes. Different keys
  without this proposal still cannot conflict or deduplicate. The unaccepted candidate
  adopts the proposed key in its review row so ordinary decisions, including `accept_new`,
  behave consistently. Content, excerpts, source paths and existing block keys are unchanged.
  No persistent alias is created: a model's pairwise match must not become a general rule.
  Even a same-source duplicate verdict with a newly resolved key requires review. Older C3
  responses missing the field retain literal-key behavior; the current schema requires the
  nullable field. Regression tests cover separate source files, unlinked pages, alternative
  labels, unrelated delivery facts, invalid model keys, three decisions, and unchanged runs.

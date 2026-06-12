# PRD: Agent Context Wiki (v2)

**Status:** Draft for architecture generation
**Consumers of this document:** An automated system that generates a system architecture, a build plan, and a coding-agent harness (Codex). Requirements are therefore written as numbered, testable statements. Where a design decision was needed, this PRD makes the decision rather than listing options. Open questions are isolated in §16.

-----

## 1. Product Summary

Agent Context Wiki is a Markdown-first context management system that organizes diverse source documents into complete, domain-wise wiki pages consumable by humans and AI agents.

It behaves like a generated Wikipedia for private/work knowledge: each topic or domain has one rich page, related topics are linked, and every relevant source chunk for a topic is accounted for on the right page, in the right section, with source attribution.

The system is a fork of an existing local-first LLM wiki (filesystem as source of truth, SQLite derived index, MCP tool surface: `guide`, `search`, `read`, `create`, `edit`, `append`, `delete`). The fork changes the product goal from *maintained summaries* to *lossless, conflict-aware, agent-consumable context*, and adds the machinery that makes “lossless” verifiable rather than aspirational: a chunk-disposition ledger, structured context blocks, a page registry, conflict review workflow, and a two-tier read surface for agents.

-----

## 2. Definitions (Normative)

These terms are used precisely throughout. The architecture generator should treat them as the domain vocabulary.

|Term                              |Definition                                                                                                                                                                           |
|----------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|**Source**                        |A file or external feed (Whimsical MCP output, transcript) registered in the workspace. Identity = workspace-relative path (or feed URI). Versioned by content hash.                 |
|**Chunk**                         |A unit of source content produced by the existing chunking system. Identity = `(source_id, source_version, content_hash)`.                                                           |
|**Disposition**                   |The ledger-recorded outcome of processing one chunk. Enumerated in §7.1. Every chunk must reach a terminal disposition.                                                              |
|**Context block**                 |A structured, source-backed unit of content inside a wiki page. The atomic unit of placement, conflict detection, and decision application. Schema in §7.3.                          |
|**Semantic key**                  |A stable dot-notation identifier (`domain.entity.attribute`) naming *what a block is about*. Two blocks can only duplicate or conflict if they resolve to the same key.              |
|**Page registry**                 |The authoritative list of wiki pages with titles, one-line descriptions, and aliases, consulted before any chunk is placed or any page is created.                                   |
|**Chunk ledger**                  |SQLite tables recording every chunk’s disposition and block linkage. The ground truth for coverage. Markdown coverage sections are *rendered from* the ledger, never hand-maintained.|
|**Coverage (losslessness type A)**|No relevant chunk is silently dropped: every chunk has a terminal disposition, verifiable from the ledger.                                                                           |
|**Fidelity (losslessness type B)**|Placed content is not corrupted by paraphrase: every context block carries a verbatim source excerpt alongside its normalized restatement.                                           |
|**Processing run**                |A single serialized job that ingests pending chunks, places blocks, detects conflicts, and emits one review file.                                                                    |

**“Lossless” in this product means Coverage + Fidelity, both machine-verifiable.** It does not mean the wiki is a verbatim mirror of all sources; irrelevant and duplicate content is excluded, but its exclusion is recorded and auditable.

-----

## 3. Problem

AI agents need complete, organized context drawn from many scattered documents: PDFs, docs, spreadsheets, YAML/XML flow definitions, Whimsical MCP outputs, API docs, FAQs, policies, T&Cs, BRDs, meeting/video transcripts, notes, tickets, RCA documents, troubleshooting guides.

Today this context is fragmented, duplicated, outdated, or conflicting. Plain summaries drop details downstream agents need. Plain retrieval (RAG over raw chunks) returns fragments without organization, dedup, or conflict resolution. The product must organize all relevant source data for a topic into complete, linked, conflict-aware wiki pages, and keep them current as sources change.

-----

## 4. Driving Use Cases (Normative — these define acceptance)

The MVP is built against three concrete use cases. Each contributes acceptance scenarios (§14).

### UC1 — Context for comprehensive test-case generation

- **Sources:** YAML/XML flow definitions, FAQ docs, T&C docs, API docs, BRDs, miscellaneous text context.
- **Behavior:** After processing, each workflow/domain has one wiki page combining its flow, FAQs, T&Cs, API details, BRD specs, and other context. Conflicts between sources (e.g., BRD says retry 3×, API doc says 2×) are surfaced and resolved via review before the page is treated as authoritative.
- **Consumer:** An AI test-generation system reads full pages as its context. Test coverage depends on page completeness; a dropped edge case in a T&C chunk is a product failure.
- **Implication:** Coverage and Fidelity are both load-bearing. Flow structure (nodes, edges, branches) must survive ingestion in machine-usable form, not just prose.

### UC2 — Context for bot building on an Agent Studio platform

- **Sources:** Product documentation for node types, settings, and platform behavior.
- **Behavior:** A planning agent reads a **summary tier** of the wiki (index + per-page summaries) to draft a bot-build plan. During execution, when configuring a specific node or writing a node prompt, it reads that node’s **detail tier** (full page).
- **Consumer:** The platform’s bot-building agent, over MCP.
- **Implication:** The wiki needs a two-tier read surface (§7.10): bounded summaries maintained on every write, and full pages. CRUD on product docs must propagate to the wiki quickly (re-ingestion lifecycle, §7.8).

### UC3 — Context for support RCA / first-level diagnosis

- **Sources:** Product docs, historical tickets, RCAs, troubleshooting docs.
- **Behavior:** A diagnosis system takes issue context, searches the wiki, and produces a first-level diagnosis; complex issues escalate to a tech team. Primary CRUD pattern: continuous addition of new tickets/RCAs/troubleshooting updates.
- **Implication:** High-frequency incremental ingestion; temporal ordering matters (a 2026 RCA supersedes a 2024 troubleshooting note); historical content is deprecated, not deleted, because old incidents remain diagnostic evidence.

-----

## 5. Core Product Principles

1. **Completeness over summarization.** The system maintains complete topic/domain pages, not short summaries. (Summaries exist only as a derived read tier, §7.10.)
1. **Ledger is ground truth for coverage.** Anything the markdown claims about coverage is rendered from the ledger and lint-verified against it.
1. **Fidelity by construction.** Every block carries its verbatim excerpt; restatements never replace evidence.
1. **The user is authoritative on conflicts.** The system detects and recommends; it never auto-resolves.
1. **Deterministic mutation.** All page mutations driven by decisions or status changes are mechanical block operations, never free-form LLM rewrites of existing pages.
1. **Soft delete by default.** History is preserved via git; hard deletion is an explicit, logged exception.
1. **Filesystem remains source of truth for content; SQLite remains a derived-plus-ledger store.** The ledger and registry tables are the only SQLite state not rebuildable from source files alone; they are therefore backed up into `wiki/_meta/` as exportable JSON on every run.

-----

## 6. Goals and Non-Goals

### Goals

1. Preserve raw source files untouched.
1. Process diverse text-based and structured sources, chunk by chunk, using the existing chunking system.
1. Place each relevant chunk’s content into the right section of the right topic/domain page as a context block.
1. Maintain a chunk ledger guaranteeing verifiable coverage.
1. Guarantee fidelity via verbatim excerpts in every block.
1. Maintain a page registry preventing topic drift and page proliferation; support page merge/split/rename.
1. Link related topics with Obsidian-compatible links.
1. Detect conflicts at the semantic-key level; surface them in per-run review files; apply user decisions deterministically.
1. Handle source updates: re-ingestion, staleness detection, supersession.
1. Provide a two-tier agent read surface (summary / detail) over MCP.
1. Preserve direct user edits to wiki pages.
1. Provide lint that verifies the system’s own guarantees.
1. Keep the wiki readable by humans in any Markdown/Obsidian editor.

### Non-Goals for MVP

1. Full knowledge graph or typed relationship metadata.
1. Full truth ledger / per-claim pages / claim database.
1. Stored or dynamic context packs.
1. Dedicated review UI (Markdown review files are the UI).
1. Permissions, multi-user collaboration, role-based access.
1. Fully automated conflict resolution.
1. Hard deletion by default.
1. Guaranteed extraction quality from scanned images / non-text content (best-effort; failures are recorded as `failed` dispositions, never silently skipped).
1. Vector/semantic search beyond what conflict-candidate retrieval requires (FTS5 + key matching is the MVP baseline; embeddings are optional, §16-Q3).

-----

## 7. Functional Requirements

Requirement IDs are stable and intended for traceability in the generated build plan.

### 7.1 Ingestion and the Chunk Ledger

- **FR-LEDGER-01.** Every chunk produced by the chunking system gets a ledger row at creation with disposition `pending`.
- **FR-LEDGER-02.** Disposition enum (terminal unless noted): `pending` (non-terminal), `placed`, `duplicate` (with `duplicate_of_block_id`), `irrelevant` (with required free-text reason), `conflicted_pending` (non-terminal; awaiting review decision), `failed` (with error detail; non-terminal — retried on next run, terminal after N=3 attempts as `failed_final`), `superseded` (chunk belongs to a replaced source version).
- **FR-LEDGER-03.** A `placed` disposition links to one or more block IDs. A block links back to one or more chunk IDs (a block may consolidate adjacent chunks from the same source).
- **FR-LEDGER-04.** A processing run completes only when no chunk in scope remains `pending`. Runs are resumable: an interrupted run continues from ledger state.
- **FR-LEDGER-05.** Ledger and registry tables are exported as JSON to `wiki/_meta/ledger.json` and `wiki/_meta/registry.json` at the end of every run (for git history and disaster recovery).
- **FR-LEDGER-06.** Source metadata includes `source_date`: extracted from document content where possible (document date, meeting date), else user-suppliable, else `unknown`. `source_date` ≠ ingestion time ≠ file mtime; all three are stored distinctly. Conflict recommendations (§7.6) use `source_date` with fallback to file mtime, and state which was used.

### 7.2 Page Registry and Taxonomy Operations

- **FR-REG-01.** The page registry stores, per page: stable page ID, title, workspace-relative path, one-line description, aliases (list), and status (`active`, `merged_into:<page_id>`, `archived`).
- **FR-REG-02.** The placement step (§7.4) must consult the registry (titles + descriptions + aliases) before deciding to create a new page. New-page creation requires the placement model to assert that no active registry entry matches; this assertion is logged.
- **FR-REG-03.** The system supports `merge_pages(a, b)`, `split_page(page, section_spec)`, and `rename_page(page, new_title)` as deterministic operations: blocks are moved with ledger links updated, the merged-away page becomes a redirect stub (Obsidian-compatible alias note), and all inbound `[[links]]` are rewritten.
- **FR-REG-04.** At the end of each run, a taxonomy-review step lists registry entries with high title/description similarity as merge candidates in the run’s review file (advisory only; merging is a user decision).

### 7.3 Context Block Model and Serialization

- **FR-BLOCK-01.** Block schema: `id` (ULID), `key` (semantic key), `type` (enum: `fact`, `rule`, `flow`, `api`, `requirement`, `faq`, `term`, `troubleshooting`, `issue`, `decision`, `note`), `status` (enum below), `source_id`, `source_path`, `source_date`, `chunk_ids`, `user_edited` (bool), `content` (normalized restatement), `excerpt` (verbatim source text).
- **FR-BLOCK-02.** Status enum and meanings:
  - `current` — active, authoritative context.
  - `needs_review` — auto-flagged (e.g., its source version was replaced and the new version no longer contains the backing chunk; or lint detected an integrity issue). Set only by the system, cleared only by a run or a user decision.
  - `conflicted` — user chose `mark_conflicted`; conflict remains visible on the page.
  - `deprecated` — superseded but historically meaningful; remains on the page in a History/Deprecated section. (UC3 relies on this.)
  - `rejected` — candidate that the user declined. **Rejected blocks do not live in page markdown**; they exist only in the ledger/audit (and git history of the review file).
  - `deleted` — soft-deleted; removed from page markdown, retained in ledger and git history.
- **FR-BLOCK-03.** Serialization in Markdown: blocks are delimited by HTML comments containing a single-line JSON metadata object, so pages render cleanly in Obsidian (HTML comments are hidden in reading view) while remaining deterministically parseable:

```markdown
<!-- cb {"id":"cb_01JF8...","key":"refunds.window_days","type":"rule","status":"current","source_path":"docs/tnc_v2.pdf","source_date":"2025-11-02","chunks":["ch_0192"],"user_edited":false} -->
**Refund window:** Refunds are accepted within 30 days of delivery.
> ​Excerpt (docs/tnc_v2.pdf): "Customers may request a refund within thirty (30) days of the delivery date."
<!-- /cb cb_01JF8... -->
```

- **FR-BLOCK-04.** A page parser/serializer round-trips pages losslessly: parse(serialize(page)) == page. All deterministic mutations operate on the parsed model, never on raw text via regex.
- **FR-BLOCK-05.** Excerpt rules: verbatim from the source chunk; if the relevant source text exceeds 1,500 characters, the excerpt may be truncated with `[…]` markers but must include every value-bearing span (numbers, identifiers, enum values, conditions) that the restatement asserts. Lint checks restatement values appear in the excerpt (§7.12).
- **FR-BLOCK-06.** Semantic keys: dot-notation, lowercase snake segments, `domain.entity.attribute` (e.g., `refunds.window_days`, `agent_studio.nodes.api_call.timeout`). Keys are proposed by the placement model **constrained by a per-page key inventory**: before assigning a new key on a page, the model is shown existing keys for that page and must reuse or explicitly justify a new key. Key aliases may be recorded when near-duplicates are later merged.

### 7.4 Chunk Placement Pipeline

For each `pending` chunk, the pipeline executes:

- **FR-PLACE-01.** *Relevance gate:* classify chunk as relevant/irrelevant to the workspace’s domains. Irrelevant → disposition `irrelevant` with reason. (Transcripts get a stricter pre-pass, §7.9.)
- **FR-PLACE-02.** *Page resolution:* select target page from the registry, or create a new page per FR-REG-02. Multi-topic chunks may be split across pages; each placement is a separate block with shared `chunk_ids`.
- **FR-PLACE-03.** *Section resolution:* select or create the target section. Pages have no rigid template, but the following canonical section names are preferred when applicable: Summary, Flow, Rules, API Details, Requirements, Edge Cases, FAQs, Terms and Conditions, Troubleshooting, Known Issues, Historical Notes, Decisions, Open Questions, Deprecated, Related Pages, Source Coverage.
- **FR-PLACE-04.** *Block drafting:* produce key, type, restatement, and excerpt per §7.3.
- **FR-PLACE-05.** *Duplicate/conflict check* (§7.6) before any write. Clean → write block, disposition `placed`. Duplicate → disposition `duplicate`, no write. Conflict → disposition `conflicted_pending`, candidate goes to the review file, no page write (except FR-CONF-05 visibility marker).
- **FR-PLACE-06.** *Linking:* add `[[Obsidian links]]` to related pages where the content references another registry entry.
- **FR-PLACE-07.** Chunks may be batched per source for LLM efficiency, but ledger dispositions remain per-chunk. Each LLM placement call receives: the chunk(s), the registry, the target page’s section outline + key inventory, and the canonical section list.
- **FR-PLACE-08.** Incremental processing: a run processes only chunks with non-terminal dispositions. Cost is O(new/changed chunks), not O(workspace).

### 7.5 Structured-Source Handling (Flows and Diagrams)

- **FR-FLOW-01.** YAML/XML flow definitions and Whimsical MCP diagram outputs are ingested with structure preserved: the system emits a `flow`-type block whose content is a Mermaid representation of the nodes/edges/branches, plus the verbatim source structure (or a path reference to it) as the excerpt.
- **FR-FLOW-02.** Fidelity requirement for flows: every node, edge, and branch condition in the source must appear in the Mermaid representation. Lint samples flows and compares node/edge counts between source and block.
- **FR-FLOW-03.** A prose description block may accompany the flow block but never replaces it. (UC1: the test generator consumes the structural form.)

### 7.6 Conflict and Duplicate Detection

- **FR-CONF-01.** Detection mechanism (normative): for each candidate block, retrieve comparison candidates by (a) exact and prefix semantic-key match on the target page, then (b) FTS5 search over blocks on the target page and registry-linked related pages. Retrieved candidates are compared pairwise by an LLM judge that outputs one of: `distinct`, `duplicate`, or a conflict type.
- **FR-CONF-02.** Conflict types: `changed_value`, `changed_scope`, `newer_disagrees`, `refinement`, `deprecated_reappears`, `missing_evidence`, `ambiguous_update`.
- **FR-CONF-03.** The system never auto-resolves. Exception: exact-duplicate (identical key + semantically identical content from the *same source path*) is auto-recorded as `duplicate` without review, since no information is at stake.
- **FR-CONF-04.** Recommendations are advisory, must cite which timestamp basis was used (`source_date` vs mtime), and must never be auto-applied.
- **FR-CONF-05.** While a conflict is pending, the affected existing block gains a visible inline marker (`⚠ pending review: RR-<run_id>-<row>`) and the page’s Open Conflicts section lists it. Agents reading the page can see contested context is contested.

### 7.7 Review Files and Decision Application

- **FR-REV-01.** One review file per processing run (not per conflict): `wiki/_reviews/RR-<run_id>.md`, grouping conflict rows by affected page. Each row: row ID, source name/path/date, affected page, existing block (id, key, content, excerpt), candidate block (same), conflict type, system recommendation + rationale, empty `decision:` field, empty `notes:` field.
- **FR-REV-02.** Allowed decisions: `accept_new`, `keep_existing`, `merge`, `mark_conflicted`, `deprecate_existing`, `reject_new`, `delete_duplicate`, `needs_more_info`.
- **FR-REV-03.** Decision application is deterministic. Mapping (each is a mechanical operation on the parsed page model + ledger update; no free-form LLM edit of existing page content):

|Decision            |Page operation                                                                                                                                                                |Ledger/status effects                                                           |
|--------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|
|`accept_new`        |Insert candidate block as `current`; existing block → `deprecated` if same key, else untouched                                                                                |candidate chunks → `placed`                                                     |
|`keep_existing`     |No page change; remove pending marker                                                                                                                                         |candidate → `rejected`; chunks remain accounted as `placed`→via rejection record|
|`merge`             |The **only** decision invoking an LLM write: it drafts a merged block shown as a *new candidate in a follow-up review row* unless the user pre-approved merge text in `notes:`|both originals → `deprecated` on apply                                          |
|`mark_conflicted`   |Both blocks remain, statuses `conflicted`, listed in Open Conflicts                                                                                                           |chunks `placed`                                                                 |
|`deprecate_existing`|Existing → `deprecated` (moved to Deprecated/History section); candidate inserted `current`                                                                                   |chunks `placed`                                                                 |
|`reject_new`        |No page change                                                                                                                                                                |candidate → `rejected`                                                          |
|`delete_duplicate`  |Duplicate block removed from page                                                                                                                                             |block → `deleted`; chunks → `duplicate`                                         |
|`needs_more_info`   |Pending marker stays; row carries into next run’s review file                                                                                                                 |unchanged                                                                       |

- **FR-REV-04.** `apply-decisions` is an explicit user-invoked command. It validates the review file (unknown decision values or malformed rows abort with a per-row error report), applies row-by-row, commits to git, and regenerates affected Source Coverage sections and summaries.
- **FR-REV-05.** Unresolved review files are surfaced by lint and at the start of every run.

### 7.8 Source Update / Re-ingestion Lifecycle

- **FR-REING-01.** Source identity is path; version is content hash. On change: re-chunk; chunks with unchanged content hashes keep their dispositions and block links; new/changed chunks enter as `pending`.
- **FR-REING-02.** Chunks present in the old version but absent in the new are marked `superseded`. Blocks whose *every* backing chunk is superseded are flagged `needs_review` with reason `source_no_longer_contains`, listed in the next run’s review file (recommendation: deprecate or delete).
- **FR-REING-03.** Source deletion (file removed): all its chunks → `superseded`; all its blocks → `needs_review` with reason `source_deleted`. Nothing is silently removed from pages.
- **FR-REING-04.** UC2/UC3 latency expectation: the file watcher queues changed sources; processing them requires at most one `process` invocation (or an optional auto-process mode); no full reindex is required for incremental updates.

### 7.9 Transcript Handling

- **FR-TRANS-01.** Meeting/video transcripts receive a pre-pass before normal placement: (a) segment-level relevance classification (most transcript content is chatter → `irrelevant` with reason), (b) intra-document supersession — within one transcript, later statements on the same key override earlier ones, and only the surviving statement proceeds to placement (the overridden segment is recorded `duplicate`-of the survivor with reason `intra_transcript_supersession`), (c) `source_date` = meeting date when extractable.
- **FR-TRANS-02.** Transcript-derived blocks default `type` to `decision` or `note` and always carry the verbatim excerpt, since restatement risk is highest for conversational sources.

### 7.10 Agent Read Surface (Two-Tier)

- **FR-READ-01.** Every page maintains a `## Summary` section, ≤ 300 words, regenerated by the system whenever the page’s `current` blocks change. Summaries are explicitly *derived* and carry no coverage guarantee.
- **FR-READ-02.** The system maintains `wiki/_index.md`: every active page with its one-line registry description and link, grouped by domain.
- **FR-READ-03.** MCP tools added for agent consumers (read-only): `wiki_index()` → index content; `wiki_summary(page)` → summary tier; `wiki_page(page)` → full page (blocks with metadata); `wiki_search(query, tier=summary|full)` → ranked results. Existing read/write tools remain for the maintaining agent.
- **FR-READ-04.** Full-page reads include block metadata so consuming agents can filter by status (e.g., UC3 diagnosis may weigh `deprecated` history; UC1 test generation should use `current` only by default). Tool docs state this.

### 7.11 User Edits, Versioning, and Audit

- **FR-GIT-01.** `wiki/` is a git repository, auto-initialized. The system commits with structured messages at: end of each processing run, each `apply-decisions`, each taxonomy operation. This provides audit, soft-delete history, and rollback.
- **FR-GIT-02.** At run start, the system diffs the working tree against its last commit. Changes inside block delimiters → those blocks get `user_edited: true` and are thereafter **protected**: the system never modifies their content; conflicts against them are detected normally but `accept_new`/`deprecate_existing` recommendations are annotated “edits a user-authored block.” Changes outside block delimiters (free prose) are preserved verbatim by the round-tripping serializer (FR-BLOCK-04).
- **FR-GIT-03.** Hard deletion (sensitive data, accidental ingestion) is an explicit command that removes content from pages, ledger excerpts, and — with a documented warning — requires git history rewrite to be complete. The command performs page+ledger removal and prints the git instructions; it never claims more than it did.

### 7.12 Lint

Lint verifies the system’s own guarantees. Checks (each emits machine-readable findings):

- **FR-LINT-01.** Coverage: every indexed source’s chunks all have terminal or tracked dispositions; ledger vs rendered Source Coverage sections agree.
- **FR-LINT-02.** Every block has `source_path` and non-empty excerpt; every value-bearing token (numbers, enum literals) in a restatement appears in its excerpt (fidelity check; sampled if expensive).
- **FR-LINT-03.** No duplicate active semantic keys on a page without `conflicted` status; key inventory matches blocks present.
- **FR-LINT-04.** Unresolved review files; `needs_review` blocks older than a configurable threshold; stale pages (page untouched after backing source changed).
- **FR-LINT-05.** Every `conflicted` block appears in its page’s Open Conflicts section; pending markers match `conflicted_pending` ledger rows.
- **FR-LINT-06.** Broken `[[internal links]]`; links to merged/archived pages not yet rewritten.
- **FR-LINT-07.** Round-trip integrity: every page parses; serialize(parse(page)) is byte-identical.
- **FR-LINT-08.** Flow fidelity sampling per FR-FLOW-02.

### 7.13 Source Coverage Rendering

- **FR-COV-01.** Every page’s Source Coverage section is generated from the ledger at the end of each run and each `apply-decisions`. It lists, per source touching the page: used fully / used partially (with counts: n of m chunks placed) / had irrelevant chunks / failed chunks / pending or in-review chunks. A footnote links the source’s full chunk-level breakdown (rendered to `wiki/_meta/coverage/<source_id>.md` on demand).
- **FR-COV-02.** Because rendered-from-ledger, manual edits to Source Coverage sections are overwritten; the section carries a generated-content marker saying so.

### 7.14 Concurrency

- **FR-LOCK-01.** A workspace lock file (`.llmwiki/lock`) serializes: processing runs, `apply-decisions`, taxonomy operations, reindex. Concurrent invocation fails fast with a clear message.
- **FR-LOCK-02.** Agent read tools (§7.10) are never blocked by the lock. The file watcher queues changes during a locked period.
- **FR-LOCK-03.** User edits to wiki files during a run are tolerated: the run re-reads pages before each mutation and re-applies FR-GIT-02 protection logic; if a target page changed mid-run, the affected placements are retried once, else deferred to the next run with disposition kept `pending`.

-----

## 8. Page Completeness Rules (Acceptance Definition)

A topic/domain page is complete when:

1. Every relevant processed chunk for that topic is represented as a block, linked away, or recorded in the ledger as duplicate/irrelevant/conflicted/pending/failed — verifiable by FR-LINT-01.
1. Related chunks from different files are organized together on the page.
1. Related topics are linked and links resolve.
1. Every block has source path, source date (or `unknown`), and excerpt.
1. The Source Coverage section is ledger-consistent.
1. Unresolved conflicts are visible in Open Conflicts.
1. Duplicate and irrelevant exclusions are auditable from the ledger.
1. No relevant processed source is silently ignored — “silently” is impossible by construction if FR-LEDGER-01..04 hold.

-----

## 9. Data Model (SQLite — Normative Sketch)

The architecture generator should treat this as the minimum schema; it may extend it.

```
sources(id PK, path UNIQUE, kind, current_version_hash, source_date, ingested_at, mtime, status)
source_versions(id PK, source_id FK, version_hash, seen_at)
chunks(id PK, source_id FK, source_version_id FK, content_hash, ordinal, text_ref, disposition, disposition_reason, duplicate_of_block_id NULL, attempts, updated_at)
pages(id PK, path UNIQUE, title, description, status, created_at)            -- registry
page_aliases(page_id FK, alias)
blocks(id PK, page_id FK, key, type, status, source_id FK, source_date, content_hash, user_edited, created_at, updated_at)
block_chunks(block_id FK, chunk_id FK)
key_aliases(page_id FK, key, alias_of)
runs(id PK, started_at, finished_at, stats_json)
review_rows(id PK, run_id FK, page_id FK, existing_block_id NULL, candidate_json, conflict_type, recommendation, decision NULL, applied_at NULL)
events(id PK, ts, actor, kind, payload_json)                                  -- append-only audit
```

Block *content* lives in markdown files (filesystem is truth for content); SQLite stores block metadata + content hash for drift detection. Ledger/registry tables are exported per FR-LEDGER-05.

-----

## 10. Interfaces

**CLI (extends existing):** `process <ws>` (run pipeline), `apply-decisions <ws> [review-file]`, `lint <ws>`, `merge-pages`, `split-page`, `rename-page`, `coverage <ws> [source]`, `eval <ws>` (§12), plus existing `init/serve/mcp/reindex`.

**MCP (extends existing tool set):** read tier per FR-READ-03; existing `guide` updated to teach consuming agents the two-tier pattern (UC2) and block-status semantics (FR-READ-04). Maintaining-agent tools (`create/edit/append/delete`) route through the block-aware serializer so direct agent edits cannot corrupt block delimiters.

-----

## 11. Non-Functional Requirements

- **NFR-01.** Idempotency: re-running `process` with no source changes performs zero page writes and zero LLM placement calls.
- **NFR-02.** Cost scaling: LLM calls scale with new/changed chunks (FR-PLACE-08), with per-source batching (FR-PLACE-07).
- **NFR-03.** A workspace of ~50 sources / ~5,000 chunks must complete an initial full run without manual intervention; failures land in the ledger, not the console alone.
- **NFR-04.** All generated markdown is valid CommonMark and renders acceptably in Obsidian (block metadata hidden in reading view).
- **NFR-05.** No source file is ever modified or moved.

-----

## 12. Success Metrics and Evaluation Harness

Metrics must be computable from the ledger or the eval harness — no self-graded relevance.

1. **Coverage rate:** chunks with terminal/tracked dispositions ÷ total chunks. Target: 100% at run completion (enforced by FR-LEDGER-04; metric exists to catch regressions).
1. **Attribution rate:** blocks with source path + excerpt ÷ all blocks. Target: 100% (lint-enforced).
1. **Fidelity audit:** sampled blocks where every value-bearing token in the restatement appears in the excerpt. Target ≥ 99% on samples.
1. **Conflict precision (sampled, human-judged):** of surfaced conflicts, fraction that are genuine. Reported, no hard target in MVP.
1. **Conflicts caught before overwrite:** count per run (directional).
1. **Golden-QA eval (`llmwiki eval`):** per workspace, a user-maintained file of N questions answerable from the sources, each with expected answer/key facts. The harness has an agent answer each question using *only* `wiki_*` read tools and scores answers. This is the operational definition of “agents can use the wiki directly” and the regression check for UC1/UC2/UC3. Target: defined per workspace baseline; must not regress run-over-run.
1. **Duplicate reduction:** active blocks per semantic key ≈ 1 outside `conflicted` (lint metric).

-----

## 13. MVP Definition

The MVP is complete when a user can:

1. Add or update diverse source documents (including YAML/XML flows, Whimsical MCP outputs, transcripts).
1. Run `process` and have every chunk reach a tracked disposition.
1. See topic/domain pages combining multi-source content as structured blocks with excerpts, with flows preserved structurally.
1. See ledger-rendered Source Coverage on every page.
1. Receive one review file per run for all detected conflicts, decide each row, and run `apply-decisions` with deterministic effects.
1. Update a source and have staleness/supersession surface automatically.
1. Have a consuming agent complete the UC2 pattern (plan from summaries, execute from detail pages) over MCP.
1. Run `lint` and `eval` and get machine-readable results.
1. See full history of every change in git.

## 14. Acceptance Scenarios (Per Use Case)

- **AS-UC1-1.** Given a workspace with a YAML flow, an FAQ doc, a T&C doc, an API doc, and a BRD all describing one workflow, after `process` there is exactly one page for that workflow containing a `flow` block (Mermaid, node/edge complete), FAQ blocks, rule blocks from T&C, API blocks, and requirement blocks from the BRD — and the ledger shows zero `pending`.
- **AS-UC1-2.** Given the BRD says “retry 3 times” and the API doc says “maxRetries: 2,” `process` produces a `changed_value` conflict row, neither value silently overwrites the other, and the page shows a pending-review marker until the user decides.
- **AS-UC2-1.** Given product docs for 10 node types, `wiki_index` + `wiki_summary` for the relevant pages total a bounded read (< ~4k words) sufficient for a planning agent; `wiki_page("nodes/api-call")` returns full configuration detail including every setting present in the source doc (golden-QA verified).
- **AS-UC2-2.** Given a product-doc update changing one node’s default timeout, one `process` run later the node’s page reflects the new value, the old value is `deprecated` (visible in history) or replaced per user decision if conflicting, and no other page changed (git diff scoped).
- **AS-UC3-1.** Given a new RCA contradicting an older troubleshooting doc, the conflict recommendation cites `source_date` recency; after `deprecate_existing`, the old guidance remains readable in the Deprecated section and the diagnosis agent can retrieve both, status-tagged.

## 15. Later Scope

Dynamic context packs; dedicated review UI; per-block granular source references (page/row/timestamp/YAML path); improved OCR/table extraction; source-level audit pages; typed relationships/knowledge graph; permissions and team workflows; embedding/semantic search; automated completeness regression in CI; richer agent APIs (scoped context assembly).

## 16. Open Questions (Deliberately Few)

- **Q1.** Auto-process mode (watcher-triggered runs) on by default for UC2/UC3 workspaces, or always manual `process`? (MVP default: manual; flag exists.)
- **Q2.** Should `merge` decisions allow pre-approved merge text in the review file’s `notes:` to skip the second review round? (MVP: yes, as specified in FR-REV-03 — confirm.)
- **Q3.** Are embeddings needed for conflict-candidate retrieval, or is key-match + FTS5 sufficient for MVP corpora? (MVP: FTS5 only; measure conflict recall in eval before adding embeddings.)
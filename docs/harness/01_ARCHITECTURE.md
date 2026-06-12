# 01 — System Architecture (Normative)

## 1. Position in the existing system

```
                         ┌────────────────────────────────────────────┐
                         │                 Workspace                   │
                         │  sources (untouched)   wiki/*.md   .llmwiki/│
                         └──────┬──────────────────────┬───────────────┘
                                │                      │
              existing ingestion│                      │ v2 engine writes
   ┌────────────────────────────▼───────┐   ┌──────────▼──────────────────────┐
   │ api/domain/watcher + local_processor│   │            core/                │
   │ + api/services/chunker             │   │ ledger · registry · blocks ·     │
   │ (extended: chunk content_hash,     │──▶│ placement · conflicts · review · │
   │  source_versions, ledger seeding)  │   │ reingest · summary · lint · eval │
   └────────────────────────────────────┘   │ gitops · lock · llm provider     │
                                            └───────┬───────────────┬──────────┘
                                                    │               │
                                  ┌─────────────────▼──┐   ┌────────▼─────────┐
                                  │ llmwiki CLI (v2     │   │ mcp/ (v2 read    │
                                  │ subcommands)        │   │ tier + block-    │
                                  └─────────────────────┘   │ aware writes)    │
                                                            └──────────────────┘
SQLite `.llmwiki/index.db`: existing derived tables + new `acw_*` tables
(`acw_chunk_ledger` and `acw_pages`/registry/`acw_review_rows` are *persistent*, exported to
wiki/_meta/*.json each run; everything else remains rebuildable).
```

The filesystem stays source of truth for content. The DB holds (a) the existing derived index
and (b) the new ledger/registry state, which is the *only* SQLite state not rebuildable from
source files (PRD §5.7) and is therefore exported to `wiki/_meta/` JSON every run.

## 2. New package layout

```
core/
  __init__.py
  config.py            # ACWConfig: env + .llmwiki/config.toml (LLM creds, thresholds, flags)
  models.py            # Pydantic models for every contract in 02_DATA_CONTRACTS
  ids.py               # ULID generation: cb_/ch_/run_/rr_/pg_ prefixes
  db/
    migrate.py         # applies shared/migrations_local/NNN_*.sql, tracks acw_schema_version
    dao.py             # typed async accessors for acw_* tables (no ORM)
  ledger.py            # ChunkLedger: dispositions, transitions, completion check, JSON export
  registry.py          # PageRegistry: CRUD, alias resolution, similarity candidates, export
  blocks/
    model.py           # ContextBlock, Page (parsed model: prose segments + blocks + sections)
    parser.py          # markdown -> Page (FR-BLOCK-04)
    serializer.py      # Page -> markdown, byte-identical round-trip
    keys.py            # semantic key grammar, validation, per-page key inventory
  pipeline/
    run.py             # ProcessingRun orchestrator (resumable, lock-guarded)
    relevance.py       # FR-PLACE-01 (folded into placement LLM call; transcripts pre-pass here)
    placement.py       # FR-PLACE-02..07: page/section resolution, block drafting, linking
    flows.py           # FR-FLOW: Whimsical-JSON deterministic converter + LLM-assisted YAML/XML
    transcripts.py     # FR-TRANS pre-pass
    batching.py        # per-source chunk batching (FR-PLACE-07)
  conflicts/
    detect.py          # candidate retrieval (key match + FTS5) + LLM judge (FR-CONF-01..02)
    markers.py         # pending-review inline markers + Open Conflicts section (FR-CONF-05)
  review/
    emit.py            # one review file per run (FR-REV-01)
    parse.py           # review-file parser + validation (FR-REV-04)
    apply.py           # deterministic decision application table (FR-REV-03)
  reingest.py          # source version diffing, supersession, needs_review flagging (FR-REING)
  coverage.py          # Source Coverage rendering from ledger (FR-COV)
  summary.py           # ## Summary regeneration + wiki/_index.md (FR-READ-01..02)
  taxonomy.py          # merge_pages / split_page / rename_page (FR-REG-03..04)
  gitops.py            # auto-init, structured commits, working-tree diff, user-edit detection
  lock.py              # .llmwiki/lock workspace lock (FR-LOCK)
  lint/
    checks.py          # FR-LINT-01..08, machine-readable findings
    runner.py
  eval/
    harness.py         # golden-QA runner over wiki_* tools only (§12.6)
  llm/
    provider.py        # LLMProvider protocol; OpenAIProvider; structured-output enforcement
    calls.py           # the six call sites (03_LLM_INTERFACES), prompt templates live here
  mcp_tools.py         # wiki_index/wiki_summary/wiki_page/wiki_search registration helpers
```

CLI: extend the root `llmwiki` script with subcommands that import `core` (`process`,
`apply-decisions`, `lint`, `coverage`, `eval`, `merge-pages`, `split-page`, `rename-page`).
MCP: `mcp/tools/wiki_read.py` registers the four read tools; `mcp/tools/write.py` gains routing
through `core.blocks` serializer for pages containing `cb` delimiters (Phase M7).

## 3. FR → module traceability

| FR group | Module(s) | Phase |
|---|---|---|
| FR-LEDGER-01..06 | `core/ledger.py`, watcher/processor extension, `core/db` | M1, M3 |
| FR-REG-01..04 | `core/registry.py`, `core/taxonomy.py` | M1, M8 |
| FR-BLOCK-01..06 | `core/blocks/*` | M2 |
| FR-PLACE-01..08 | `core/pipeline/*`, `core/llm/calls.py` | M4 |
| FR-FLOW-01..03 | `core/pipeline/flows.py` | M4 |
| FR-CONF-01..05 | `core/conflicts/*` | M5 |
| FR-REV-01..05 | `core/review/*` | M5 (emit), M6 (apply) |
| FR-REING-01..04 | `core/reingest.py`, watcher extension | M3 |
| FR-TRANS-01..02 | `core/pipeline/transcripts.py` | M4 |
| FR-READ-01..04 | `core/summary.py`, `core/mcp_tools.py`, `mcp/tools/wiki_read.py` | M7 |
| FR-GIT-01..03 | `core/gitops.py` | M6 |
| FR-LINT-01..08 | `core/lint/*` | M8 |
| FR-COV-01..02 | `core/coverage.py` | M7 |
| FR-LOCK-01..03 | `core/lock.py` | M8 |
| NFR-01..05, §12 metrics | `core/eval/*`, idempotency tests | M8, M9 |

## 4. The processing run (state machine)

`llmwiki process <ws>` →

1. **Acquire lock** (`core/lock`). Fail fast if held.
2. **Run row** created in `acw_runs` (or resume the latest unfinished run — resumability is
   purely ledger-driven: the run re-derives its worklist as "chunks with non-terminal
   dispositions", FR-LEDGER-04/FR-PLACE-08).
3. **Git pre-scan** (`core/gitops`): diff working tree vs last commit; mark user-edited blocks
   `user_edited=true` in DB + page metadata (FR-GIT-02). Surface unresolved review files
   (FR-REV-05).
4. **Source sync**: ensure every indexed source has a `acw_source_versions` row for its current
   content hash; run `core/reingest` diffing for changed sources (supersession, needs_review).
5. **Worklist**: all chunks with disposition `pending` or `failed` (attempts < 3).
6. Per source (batched, FR-PLACE-07):
   a. transcript pre-pass if `kind=transcript` (FR-TRANS-01) — may mark chunks
      `irrelevant`/`duplicate` before placement;
   b. placement call (relevance gate + page/section/key/block drafting in one structured LLM
      call per batch — see 03_LLM_INTERFACES C1);
   c. flow chunks routed through `pipeline/flows.py` instead (C2 only for non-Whimsical);
   d. per candidate block: duplicate/conflict detection (`conflicts/detect`, LLM judge C3);
   e. outcome: write block via parsed-page mutation (`placed`), or record `duplicate` /
      `irrelevant`, or queue review row + pending marker (`conflicted_pending`), or `failed`.
7. **Taxonomy review pass**: registry similarity candidates appended to the review file
   (FR-REG-04, advisory).
8. **Render**: Source Coverage sections (ledger-derived), summaries for pages whose `current`
   blocks changed (C5), `wiki/_index.md`.
9. **Emit** `wiki/_reviews/RR-<run_id>.md` if any rows; export `wiki/_meta/ledger.json` +
   `registry.json`; write run stats; **assert completion**: zero `pending` in scope.
10. **Git commit** (structured message), release lock.

Every step is restartable: state lives in the ledger/DB, page writes are atomic
(write-temp + rename), and step 9's completion assertion is the gate.

`apply-decisions` is the same lock → validate → apply rows deterministically → re-render
coverage/summaries → export → commit sequence (FR-REV-04).

## 5. Page write discipline

All page mutations go through one choke point: `core/blocks/serializer.write_page(page_model)`.
It (a) serializes the parsed model, (b) verifies round-trip (`parse(serialize(m)) == m`),
(c) refuses to write if the on-disk file changed since parse (re-read & retry once per
FR-LOCK-03), (d) writes atomically. Free prose outside block delimiters always survives
verbatim because mutations operate on the model's segment list, never on raw text.

## 6. Concurrency & failure model

- One workspace lock file (`.llmwiki/lock`, JSON: pid, op, started_at) serializes `process`,
  `apply-decisions`, taxonomy ops, `reindex`. Stale-lock detection: pid not alive → steal with
  warning. Read tools never take the lock.
- LLM failures: retried with backoff (provider-level, max 3); structured-output validation
  failures retried once with the validation error appended; then the chunk → `failed`
  (attempts incremented; `failed_final` at attempts == 3).
- Interrupted runs: next `process` resumes; no state is kept outside DB + filesystem.

## 7. Storage layout (workspace)

```
<workspace>/
  ...source files...                # never touched
  wiki/                             # git repo (auto-init by core/gitops)
    _index.md                       # FR-READ-02
    _reviews/RR-<run_id>.md         # FR-REV-01
    _meta/ledger.json registry.json # FR-LEDGER-05
    _meta/coverage/<source_id>.md   # FR-COV-01 footnote target (on demand)
    <domain>/<page>.md              # pages with context blocks
  .llmwiki/
    index.db                        # existing + acw_* tables
    lock                            # FR-LOCK-01
    config.toml                     # optional overrides (core/config.py)
    cache/
```

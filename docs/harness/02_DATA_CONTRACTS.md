# 02 — Data Contracts (Normative — single source of truth)

Codex: implement these exactly. Pydantic models in `core/models.py` mirror every JSON object
here. Do not add, rename, or repurpose fields without a 07_DECISIONS entry.

## 1. Identifiers

ULIDs (python-ulid), lowercase, with type prefixes: blocks `cb_`, ledger chunk ids `ch_`
(distinct from the existing `document_chunks.id` hex ids — see §2), runs `run_`, review rows are
`RR-<run_id>-<row_number>` (row_number 1-based within the file), pages `pg_`, source versions
`sv_`, events `ev_`.

## 2. SQLite schema additions (`shared/migrations_local/001_acw_core.sql` …)

Existing tables are untouched except: `document_chunks` gains columns
`content_hash TEXT` and `source_version_id TEXT` (nullable for legacy rows; backfilled by
`reindex`). Chunk *ledger identity* per PRD = `(source_id, source_version, content_hash)`; the
ledger row id `ch_…` is minted when the ledger row is created and is stable for the life of
that identity.

```sql
CREATE TABLE acw_schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE acw_source_versions (
  id TEXT PRIMARY KEY,                       -- sv_<ulid>
  source_id TEXT NOT NULL REFERENCES documents(id),
  version_hash TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  source_date TEXT,                          -- ISO date or 'unknown' (FR-LEDGER-06)
  source_date_origin TEXT CHECK (source_date_origin IN ('content','user','unknown')),
  UNIQUE(source_id, version_hash)
);

CREATE TABLE acw_chunk_ledger (
  id TEXT PRIMARY KEY,                       -- ch_<ulid>
  source_id TEXT NOT NULL REFERENCES documents(id),
  source_version_id TEXT NOT NULL REFERENCES acw_source_versions(id),
  content_hash TEXT NOT NULL,
  document_chunk_id TEXT,                    -- FK into document_chunks.id (current version)
  ordinal INTEGER NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN
    ('pending','placed','duplicate','irrelevant','conflicted_pending',
     'failed','failed_final','superseded')),
  disposition_reason TEXT,                   -- REQUIRED when 'irrelevant' or 'failed*'
  duplicate_of_block_id TEXT,                -- REQUIRED when 'duplicate'
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE(source_id, source_version_id, content_hash, ordinal)
);

CREATE TABLE acw_pages (                     -- the page registry (FR-REG-01)
  id TEXT PRIMARY KEY,                       -- pg_<ulid>
  path TEXT NOT NULL UNIQUE,                 -- workspace-relative, e.g. wiki/refunds.md
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',      -- one line
  status TEXT NOT NULL DEFAULT 'active',     -- 'active' | 'merged_into:<page_id>' | 'archived'
  domain TEXT NOT NULL DEFAULT '',           -- grouping key for _index.md
  created_at TEXT NOT NULL
);
CREATE TABLE acw_page_aliases (page_id TEXT NOT NULL REFERENCES acw_pages(id),
  alias TEXT NOT NULL, UNIQUE(page_id, alias));

CREATE TABLE acw_blocks (
  id TEXT PRIMARY KEY,                       -- cb_<ulid>
  page_id TEXT NOT NULL REFERENCES acw_pages(id),
  key TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('fact','rule','flow','api','requirement','faq',
    'term','troubleshooting','issue','decision','note')),
  status TEXT NOT NULL CHECK (status IN
    ('current','needs_review','conflicted','deprecated','rejected','deleted')),
  needs_review_reason TEXT,                  -- 'source_no_longer_contains'|'source_deleted'|'lint:<code>'
  source_id TEXT NOT NULL REFERENCES documents(id),
  source_path TEXT NOT NULL,
  source_date TEXT NOT NULL DEFAULT 'unknown',
  content_hash TEXT NOT NULL,                -- hash of serialized block body, drift detection
  user_edited INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE acw_block_chunks (block_id TEXT NOT NULL REFERENCES acw_blocks(id),
  chunk_id TEXT NOT NULL REFERENCES acw_chunk_ledger(id), UNIQUE(block_id, chunk_id));
CREATE TABLE acw_key_aliases (page_id TEXT NOT NULL, key TEXT NOT NULL, alias_of TEXT NOT NULL,
  UNIQUE(page_id, key));

CREATE TABLE acw_runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','completed','aborted')),
  stats_json TEXT NOT NULL DEFAULT '{}');

CREATE TABLE acw_review_rows (
  id TEXT PRIMARY KEY,                       -- 'RR-<run_id>-<n>'
  run_id TEXT NOT NULL REFERENCES acw_runs(id),
  page_id TEXT NOT NULL REFERENCES acw_pages(id),
  row_kind TEXT NOT NULL CHECK (row_kind IN ('conflict','needs_review','taxonomy_merge')),
  existing_block_id TEXT,                    -- NULL for pure-new candidates / taxonomy rows
  candidate_json TEXT,                       -- serialized CandidateBlock (NULL for taxonomy)
  conflict_type TEXT,                        -- §6 enum; NULL for non-conflict rows
  recommendation TEXT NOT NULL,
  recommendation_basis TEXT,                 -- 'source_date' | 'mtime' (FR-CONF-04)
  decision TEXT CHECK (decision IN ('accept_new','keep_existing','merge','mark_conflicted',
    'deprecate_existing','reject_new','delete_duplicate','needs_more_info')),
  notes TEXT, applied_at TEXT
);

CREATE TABLE acw_events (id TEXT PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}');
```

Block **content/excerpt text lives only in the markdown files** (filesystem is truth);
`acw_blocks.content_hash` exists for drift detection (lint).

## 3. Semantic key grammar (FR-BLOCK-06)

```
key      := segment "." segment ("." segment)*        ; 2–6 segments
segment  := [a-z][a-z0-9_]*                            ; lowercase snake
```
Convention: `domain.entity.attribute` (e.g. `refunds.window_days`,
`agent_studio.nodes.api_call.timeout`). Validation in `core/blocks/keys.py`; invalid keys from
the LLM are a validation failure → retry once → `failed` disposition.

## 4. Context block serialization (FR-BLOCK-03/04/05)

Grammar within a page:

```
block       := open_marker "\n" body "\n" close_marker
open_marker := "<!-- cb " json_meta " -->"           ; json_meta single-line, no inner newlines
body        := content_md "\n" excerpt_quote
excerpt_quote := "> Excerpt (" source_path [", " source_date] "): " excerpt_text
close_marker:= "<!-- /cb " block_id " -->"
```

`json_meta` object (exact keys, this order when serializing):
`{"id","key","type","status","source_path","source_date","chunks":[ch_ids],"user_edited"}`
(`needs_review_reason` included only when status is `needs_review`).

Rules:
- The parser locates blocks by exact `<!-- cb {` open and exact `<!-- /cb <id> -->` close for
  the same id. Content between markers is opaque body; nested HTML comments are legal *unless*
  they equal that exact close marker. The serializer must reject (ValidationError) any
  content/excerpt containing the block's own close-marker string.
- Multi-line excerpts: each continuation line is also `> `-prefixed (standard md blockquote).
- Truncation: excerpts over 1,500 chars may use `[…]`, but every value-bearing span asserted by
  the restatement (numbers, identifiers, enum literals, conditions) must remain (lint FR-LINT-02).
- Everything in a page **outside** block markers is a `ProseSegment` and round-trips verbatim.
- Page model: ordered list of `Segment = ProseSegment(text) | BlockSegment(ContextBlock)`,
  plus derived section index (H2 headings). Sections are positional, not containers — inserting
  a block "into section Rules" means inserting the BlockSegment after the last segment belonging
  to the `## Rules` heading span (creating the heading if absent, in canonical order: Summary,
  Flow, Rules, API Details, Requirements, Edge Cases, FAQs, Terms and Conditions,
  Troubleshooting, Known Issues, Historical Notes, Decisions, Open Questions, Open Conflicts,
  Deprecated, Related Pages, Source Coverage).
- Pending-review marker (FR-CONF-05): the line `⚠ pending review: RR-<run_id>-<n>` appended
  inside the affected block's body, last line before the excerpt quote. Removed on apply.

Canonical example (tests must use this exact shape):

```markdown
<!-- cb {"id":"cb_01jf8z…","key":"refunds.window_days","type":"rule","status":"current","source_path":"docs/tnc_v2.pdf","source_date":"2025-11-02","chunks":["ch_01jf8y…"],"user_edited":false} -->
**Refund window:** Refunds are accepted within 30 days of delivery.
> Excerpt (docs/tnc_v2.pdf, 2025-11-02): "Customers may request a refund within thirty (30) days of the delivery date."
<!-- /cb cb_01jf8z… -->
```

Generated-section markers (Source Coverage, Summary, Open Conflicts, `_index.md`):
`<!-- acw:generated <section> run=<run_id> — manual edits will be overwritten -->` as the first
line under the heading (FR-COV-02).

## 5. Review file format (FR-REV-01/02)

Path `wiki/_reviews/RR-<run_id>.md`. Structure:

```markdown
# Review RR-<run_id>
Run: <run_id> · Started: <iso> · Rows: <n> · Status: open

## Page: [[<page title>]] (<page path>)

### Row RR-<run_id>-1 · conflict · changed_value
- source: docs/api_v3.yaml (source_date: 2026-01-10)
- existing block: cb_… · key `refunds.retry_count` · status current
  - content: Retries are attempted 3 times.
  - excerpt: "retry up to three (3) times" (docs/brd.docx)
- candidate block:
  - content: Retries are attempted 2 times.
  - excerpt: "maxRetries: 2" (docs/api_v3.yaml)
- recommendation: accept_new — newer source_date (basis: source_date)
- decision:
- notes:
```

Parser rules (`core/review/parse.py`): rows are `### Row <id> · <row_kind> · <conflict_type?>`
sections; `decision:` accepts exactly the eight enum values (case-insensitive, surrounding
whitespace ignored); anything else → per-row validation error; `apply-decisions` aborts with a
report listing every invalid row and applies nothing (validate-all-then-apply-all per file is
NOT used — application is row-by-row but only after the whole file validates, FR-REV-04).
Taxonomy rows (`row_kind=taxonomy_merge`) accept only `merge` (interpreted as merge_pages) /
`reject_new` (ignore) / `needs_more_info`. The DB (`acw_review_rows`) is authoritative for row
identity; the markdown file is the editing surface — at apply time, decisions/notes are read
from the file and written back to the DB.

## 6. Conflict types (FR-CONF-02)

`changed_value | changed_scope | newer_disagrees | refinement | deprecated_reappears |
missing_evidence | ambiguous_update`

## 7. Decision application table (FR-REV-03)

Implement exactly the PRD table. Additional precision:
- `accept_new` deprecates the existing block only when keys are equal (after alias resolution).
- `merge`: if `notes:` begins with `approved-merge:` the remainder (verbatim markdown body) is
  used as the merged block content and applied immediately; otherwise LLM call C4 drafts a
  merged candidate emitted as a `needs_review`-kind row in the *next* run's review file.
- `keep_existing` / `reject_new`: candidate's chunks get disposition `placed` with
  `duplicate_of_block_id`-style linkage replaced by a ledger event recording rejection (chunks
  remain accounted-for: disposition `placed` is wrong — use disposition `duplicate` with
  `duplicate_of_block_id = existing_block_id` and reason `rejected_candidate`). ← normative.
- Every application appends an `acw_events` row and is part of one git commit per
  `apply-decisions` invocation.

## 8. JSON exports (FR-LEDGER-05)

`wiki/_meta/ledger.json`: `{"exported_at", "run_id", "chunks":[{every acw_chunk_ledger column}],
"block_chunks":[…]}`. `wiki/_meta/registry.json`: `{"exported_at","pages":[{…, "aliases":[…]}]}`.
Stable key order, 2-space indent, sorted by id — diffs must be meaningful in git.

## 9. MCP read-tier tool contracts (FR-READ-03/04)

| Tool | Params | Returns |
|---|---|---|
| `wiki_index()` | – | `{markdown: str}` — content of `wiki/_index.md` |
| `wiki_summary(page)` | page path or title/alias | `{page, title, summary_markdown}` |
| `wiki_page(page, statuses?)` | `statuses` default `["current","conflicted","needs_review"]`; pass `["*"]` for all | `{page, title, markdown, blocks:[{id,key,type,status,source_path,source_date,section}]}` (markdown filtered to requested statuses; metadata list always complete) |
| `wiki_search(query, tier="summary"\|"full", limit=10)` | – | `{results:[{page, title, score, snippet, tier}]}` — summary tier searches `## Summary` + descriptions; full tier searches block FTS |
| `wiki_coverage(source?)` | optional source path | ledger-derived coverage report (CLI parity) |

Tool docstrings must teach the two-tier pattern and status semantics (FR-READ-04); update
`guide` accordingly. Read tools never acquire the workspace lock.

## 10. Config (`core/config.py`)

Env (prefix `ACW_`): `ACW_LLM_API_KEY`, `ACW_LLM_BASE_URL` (default OpenAI),
`ACW_LLM_MODEL` (default `gpt-5`; overridable), `ACW_LLM_MODEL_LIGHT` (relevance/judge),
`ACW_AUTO_PROCESS` (default false — PRD Q1), `ACW_MAX_ATTEMPTS=3`,
`ACW_NEEDS_REVIEW_STALE_DAYS=14`, `ACW_SUMMARY_MAX_WORDS=300`, `ACW_BATCH_MAX_CHUNKS=8`.
`.llmwiki/config.toml` `[acw]` section overrides env; precedence: toml > env > defaults.

## 11. Events taxonomy (`acw_events.kind`)

`run.started run.completed run.aborted chunk.disposition block.created block.status
review.emitted review.applied decision.applied taxonomy.merge taxonomy.split taxonomy.rename
llm.call llm.validation_failed git.commit lock.acquired lock.stolen export.written
hard_delete.executed`
`llm.call` payload: `{call_site, model, input_tokens, output_tokens, latency_ms, ok}` — never
the raw prompt (may contain sensitive source text); raw I/O goes to `.llmwiki/cache/llm/` only
when `ACW_LLM_TRACE=1`.

## 12. Lint findings format

`{"code":"LINT-01".."LINT-08","severity":"error"|"warn","path","ref","message"}` — JSON lines on
stdout with `--json`, human table otherwise. Exit code 1 iff any `error`.

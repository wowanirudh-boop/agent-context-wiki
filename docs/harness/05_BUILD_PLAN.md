# 05 — Build Plan (Phases M0–M9)

Each phase: implement test-first per 04_TDD_PLAN, finish with `make check` green + the phase
DoD. Phases are strictly sequential; do not start work from a later phase.

---

## M0 — Bootstrap
**Scope:** `Makefile` (`setup` creates a gitignored project-local `.venv` and installs all
requirements; `check`/`test`/`lint` run tools from it — see AGENTS.md §9); `core/` package
skeleton with `config.py`, `ids.py`, `models.py` stubs; `core/requirements.txt`
(`python-ulid`, `openai`) and dev requirements (`hypothesis`, `pytest-cov`); `tests/v2/`
scaffold with conftest (tmp workspace + db fixtures), empty invariant module, FakeLLM skeleton
(Protocol + scripted/rule-based modes); verify the pre-existing `.github/workflows/ci.yml`
agrees with the Makefile; verify existing suite still passes untouched.
**DoD:** `make setup` then `make check` green on the local machine;
`.venv/bin/python -c "import core"` works.

## M1 — Data layer: migrations, ledger, registry, runs, events, exports
**Scope:** `shared/migrations_local/001_acw_core.sql` per 02§2; `core/db/migrate.py` + `dao.py`;
`core/ledger.py` (disposition transition rules: pending→{placed,duplicate,irrelevant,
conflicted_pending,failed}; failed→{pending-retry path via attempts,failed_final};
any→superseded on version change; conflicted_pending→{placed,duplicate} via decisions);
`core/registry.py`; `acw_runs`/`acw_events` writers; JSON exports (02§8). Golden workspace
fixture trees created (04§3) as static files.
**Tests first:** LEDGER-01/02/03/05 unit tests, REG-01, export stability, migration
idempotence (apply twice = no-op), transition-matrix test.
**DoD:** `pytest tests/v2/unit/test_ledger.py tests/v2/unit/test_registry.py -q` green;
`llmwiki reindex` on a fixture workspace applies migrations without touching existing tables'
data.

## M2 — Block model: parser/serializer, keys, page mutations
**Scope:** `core/blocks/*` per 02§4: Page model, parser, serializer with round-trip check,
atomic write, section resolution in canonical order, key grammar + per-page inventory,
pending-marker insert/remove, generated-section markers, mutation API
(`insert_block`, `set_status`, `replace_block_content` — refused when `user_edited`),
close-marker injection rejection.
**Tests first:** BLOCK-01..06 incl. hypothesis round-trip properties; mutation ops preserve
all untouched bytes.
**DoD:** property suite ≥ 200 examples green; canonical example in 02§4 parses to the
documented model and re-serializes byte-identically.

## M3 — Ingestion integration & re-ingestion lifecycle
**Scope:** extend chunking path (`api/services/chunker.py` output + `api/domain/watcher.py`/
`local_processor.py` + `llmwiki reindex`) to compute per-chunk `content_hash`, maintain
`acw_source_versions`, seed `acw_chunk_ledger` rows `pending` (LEDGER-01); `source_date`
extraction (regex/front-matter/meeting-date heuristics; fallback chain per FR-LEDGER-06);
`core/reingest.py` (REING-01..03); source `kind` classification (`flow`, `transcript`,
`doc`) by extension + path hints, stored in `documents.metadata`.
**Tests first:** REING matrix (changed/added/removed chunks; deleted source), hash-stable
chunk keeps disposition, LEDGER-06 three-timestamps-distinct.
**DoD:** indexing `uc1_minimal` yields a fully-`pending` ledger; modifying one file and
re-indexing supersedes exactly the removed chunks.

## M4 — Placement pipeline (the core run)
**Scope:** `core/pipeline/*` + `core/llm/{provider,calls}.py` (C1, C2, C6 + OpenAIProvider with
structured outputs, retries, event logging); run orchestrator per 01§4 steps 1–6 + 9 partial
(exports, completion gate); flows deterministic Whimsical converter; transcripts pre-pass;
batching; incremental worklist; `llmwiki process` CLI.
**Tests first:** PLACE-01..08, FLOW-01..03, TRANS-01..02, LEDGER-04 resume test, NFR-01
idempotency (FakeLLM call counter = 0 on second run).
**DoD:** `./llmwiki process tests/v2/fixtures/workspaces/uc1_minimal` (FakeLLM via
`ACW_LLM_PROVIDER=fake-rules`) completes: zero pending, pages created with blocks+excerpts,
invariants pass.

## M5 — Conflicts, duplicates, review emission
**Scope:** `core/conflicts/*` (retrieval, exact-dup short-circuit, C3 judge, recommendation
basis), pending markers + Open Conflicts section, `core/review/emit.py` + `parse.py`,
`acw_review_rows`, taxonomy-merge advisory rows (REG-04 emission only), unresolved-review
surfacing (REV-05).
**Tests first:** CONF-01..05, REV-01 format + parser round-trip, scripted-fixture conflict for
uc1 retry 3× vs 2× (AS-UC1-2 precursor).
**DoD:** processing uc1_minimal emits exactly one review file with the retry conflict as
`changed_value`, page shows marker, ledger row `conflicted_pending`.

## M6 — Decision application, git, user-edit protection
**Scope:** `core/review/apply.py` (8-decision table per 02§7), C4 merge drafting,
`apply-decisions` CLI, `core/gitops.py` (auto-init, three commit points, pre-run diff →
`user_edited` protection, annotation of recommendations against user blocks), hard-delete
command (GIT-03), coverage/summary re-render hooks stubbed to no-op until M7.
**Tests first:** 8 parametrized decision tests, GIT-01..03, validation-abort test, pre-approved
merge text path (Q2).
**DoD:** full loop on uc1_minimal: process → edit review file (`accept_new`) →
apply-decisions → page updated deterministically, old block deprecated, git log shows
structured commits, invariants pass.

## M7 — Read surface: coverage, summaries, index, MCP tools
**Scope:** `core/coverage.py` (+ `wiki/_meta/coverage/<source_id>.md` on demand),
`core/summary.py` (C5) + `_index.md`, `mcp/tools/wiki_read.py` registering the five tools of
02§9, `guide` text update, block-aware routing of existing `edit`/`append`/`create` for pages
containing `cb` delimiters (writes go through parse→mutate→serialize; malformed delimiter edits
rejected with a helpful error), `llmwiki coverage` CLI.
**Tests first:** READ-01..04, COV-01..02, NFR-04 CommonMark check, MCP tool contract tests
(reuse `tests/integration/mcp` patterns with the local fixtures).
**DoD:** AS-UC2-1 bounded-read test passes on uc2_nodes (index + summaries < 4k words;
`wiki_page` returns every setting — golden-QA verified with FakeLLM-built wiki).

## M8 — Lint, eval, taxonomy ops, locking
**Scope:** `core/lint/*` (LINT-01..08, JSON findings, CLI `llmwiki lint`), `core/eval/harness.py`
+ `llmwiki eval` (golden-QA over wiki_* tools, baseline file, regression exit code),
`core/taxonomy.py` (merge/split/rename + CLI), `core/lock.py` (LOCK-01..03) wired into all
mutating entrypoints, `ACW_AUTO_PROCESS` watcher hook (Q1: off by default).
**Tests first:** per-check lint fixtures, LOCK matrix, taxonomy link-rewrite tests, eval
scoring unit tests.
**DoD:** `llmwiki lint` clean on processed golden workspaces and detects each seeded defect
fixture; `llmwiki eval uc1_minimal` ≥ baseline.

## M9 — Acceptance, hardening, docs
**Scope:** the five PRD §14 scenarios as e2e tests; NFR-03 scale smoke (synthetic 50-source/
5,000-chunk workspace generated in-test, rule-based FakeLLM, must complete + all failures in
ledger); re-assert NFR-01; `README_V2.md` (user-facing: setup, workflow, CLI, MCP config,
live-LLM configuration); `docs/harness/07_DECISIONS.md` finalized; live smoke test (gated);
changelog of deviations.
**DoD:** `pytest tests/v2/acceptance -q` green; `make check` green; fresh-clone quickstart in
README_V2 verified by a script `scripts/smoke_quickstart.sh`.

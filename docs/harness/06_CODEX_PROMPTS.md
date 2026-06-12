# 06 — Codex Prompts (paste one per fresh thread, in order)

Rules for the human (Codex desktop app, local mode): one prompt = one new thread = one phase,
with Full access on. After Codex finishes: ask it to run `make check` and show the summary;
if green, tell it to commit/merge the phase into master and push (CI on GitHub re-verifies);
then start the next phase in a new thread. If a phase fails, use the Repair prompt; if a
thread was cut off mid-phase, use the Resume prompt. Never run two phases in parallel — they
share files and local worktrees will conflict.

---

## Prompt M0 — Bootstrap

```
Read AGENTS.md (especially §9 Local development environment), then docs/harness/00_OVERVIEW.md,
01_ARCHITECTURE.md, 04_TDD_PLAN.md, and phase M0 of docs/harness/05_BUILD_PLAN.md.

First, verify this machine has Python 3.11+ available. If it does not, STOP and tell me in
one sentence what to install and where to download it — do not attempt system-level
workarounds.

Then implement phase M0 exactly as specified: Makefile with setup/check/test/lint targets
where `make setup` creates a project-local .venv (gitignored) and installs api/, mcp/, core/
and dev/test requirements, and all other targets run tools from that .venv; core/ package
skeleton (config.py, ids.py, models.py); core/requirements.txt and dev test deps; tests/v2
scaffold (conftest with tmp-workspace and tmp-sqlite fixtures, fakes/fake_llm.py skeleton
implementing the LLMProvider protocol from docs/harness/03_LLM_INTERFACES.md with scripted
and rule-based modes, invariants package with an assert_invariants stub); and
.github/workflows/ci.yml is already present — verify it matches the Makefile's check target
and adjust the Makefile (not the CI file) if they disagree.

Constraints: do not modify any existing module behavior; the pre-existing test suite must
still pass. Definition of done: `make setup` then `make check` both succeed on this machine,
and `.venv/bin/python -c "import core"` works.
```

## Prompt M1 — Data layer

```
Read AGENTS.md, docs/PRD.md §7.1 §7.2 §9, docs/harness/02_DATA_CONTRACTS.md §1 §2 §8 §11,
docs/harness/04_TDD_PLAN.md, and phase M1 of docs/harness/05_BUILD_PLAN.md.

TDD: first write the M1 tests listed in the TDD plan (ledger transitions, registry CRUD,
migration idempotence, export stability), watch them fail, then implement:
shared/migrations_local/001_acw_core.sql exactly per the contracts DDL, core/db/migrate.py and
dao.py, core/ledger.py with the disposition transition matrix, core/registry.py, run/event
writers, and JSON exports. Also create the three golden workspace fixture trees described in
04_TDD_PLAN §3 as static files under tests/v2/fixtures/workspaces/ (uc1_minimal, uc2_nodes
with its v2 overlay, uc3_support) plus eval/questions.yaml for each.

Definition of done: phase M1 DoD in the build plan, `make check` green.
```

## Prompt M2 — Block model

```
Read AGENTS.md, docs/PRD.md §7.3, docs/harness/02_DATA_CONTRACTS.md §3 §4, and phase M2 of
docs/harness/05_BUILD_PLAN.md.

TDD: write the FR-BLOCK test suite first, including hypothesis property tests for byte-identical
round-tripping with adversarial content (nested HTML comments, blockquote lines, code fences,
unicode), then implement core/blocks/ (model, parser, serializer with round-trip verification
and atomic writes, keys.py grammar + per-page inventory, canonical-section insertion, pending
markers, generated-section markers, the mutation API with user_edited protection, and rejection
of content containing a block's own close marker).

The canonical example in 02_DATA_CONTRACTS §4 must parse and re-serialize byte-identically —
make that an explicit test. Definition of done: phase M2 DoD, `make check` green.
```

## Prompt M3 — Ingestion & re-ingestion

```
Read AGENTS.md, docs/PRD.md §7.1 §7.8, docs/harness/02_DATA_CONTRACTS.md §2,
docs/harness/01_ARCHITECTURE.md §1 §4 step 4, and phase M3 of docs/harness/05_BUILD_PLAN.md.

TDD first (FR-REING matrix, hash-stable dispositions, FR-LEDGER-06 timestamps), then: extend
the existing chunk-indexing path (api/services/chunker.py callers, api/domain/watcher.py,
api/domain/local_processor.py, llmwiki reindex) to compute per-chunk content_hash, maintain
acw_source_versions, and seed acw_chunk_ledger rows as pending; implement source_date
extraction with the content→user→unknown fallback and distinct storage of source_date /
ingestion time / mtime; implement core/reingest.py supersession and needs_review flagging;
classify source kind (flow/transcript/doc).

Keep all existing indexing behavior working (existing tests must pass). Definition of done:
phase M3 DoD, `make check` green.
```

## Prompt M4 — Placement pipeline

```
Read AGENTS.md, docs/PRD.md §7.4 §7.5 §7.9 §11, docs/harness/03_LLM_INTERFACES.md (C1, C2, C6,
FakeLLM), docs/harness/01_ARCHITECTURE.md §4, and phase M4 of docs/harness/05_BUILD_PLAN.md.

TDD first (FR-PLACE, FR-FLOW, FR-TRANS, FR-LEDGER-04 resumability, NFR-01 zero-call
idempotency), then implement: core/llm/provider.py (Protocol + OpenAIProvider with structured
outputs, one validation retry, event logging, ACW_LLM_PROVIDER=fake-rules|fake-scripted|openai
selection), core/llm/calls.py (C1/C2/C6 templates + validators including the
excerpt-substring-of-chunk check), core/pipeline/ (run orchestrator per architecture §4 steps
1–6 and 9's exports + completion gate; batching; flows with deterministic Whimsical→Mermaid;
transcript pre-pass), and the `llmwiki process` subcommand. Conflict detection is NOT in this
phase: every candidate placement writes directly (the conflicts module lands in M5); leave a
clearly-marked seam where detection will be inserted.

Definition of done: phase M4 DoD (process uc1_minimal end-to-end with fake-rules provider,
zero pending, invariants pass, second run makes zero LLM calls and zero writes),
`make check` green.
```

## Prompt M5 — Conflicts & review emission

```
Read AGENTS.md, docs/PRD.md §7.6 §7.7 (emission side) §7.2 FR-REG-04,
docs/harness/02_DATA_CONTRACTS.md §5 §6, docs/harness/03_LLM_INTERFACES.md C3, and phase M5 of
docs/harness/05_BUILD_PLAN.md.

TDD first (FR-CONF-01..05, FR-REV-01 format + parser round-trip, the scripted uc1 retry-count
conflict), then implement core/conflicts/ (candidate retrieval via key match + FTS5 over target
and related pages, exact-duplicate code-level short-circuit, C3 judge, code-computed
recommendation basis), pending markers + Open Conflicts section via the M2 mutation API,
core/review/emit.py and parse.py, acw_review_rows persistence, advisory taxonomy-merge rows,
and unresolved-review surfacing at run start. Wire detection into the M4 seam so conflicted
candidates never overwrite pages.

Definition of done: phase M5 DoD, `make check` green.
```

## Prompt M6 — Decisions, git, protection

```
Read AGENTS.md, docs/PRD.md §7.7 §7.11, docs/harness/02_DATA_CONTRACTS.md §7,
docs/harness/03_LLM_INTERFACES.md C4, and phase M6 of docs/harness/05_BUILD_PLAN.md.

TDD first (eight parametrized decision tests asserting exactly the PRD FR-REV-03 table effects,
validation-abort, FR-GIT-01..03, pre-approved merge text), then implement
core/review/apply.py, the apply-decisions CLI (validate whole file, then apply row-by-row,
commit once), C4 merge drafting emitting a follow-up review row, core/gitops.py (auto-init,
structured commits at run end / apply-decisions / taxonomy ops, pre-run working-tree diff
setting user_edited and protecting those blocks, annotated recommendations), and the explicit
hard-delete command per FR-GIT-03.

Definition of done: phase M6 DoD (full process → decide → apply loop on uc1_minimal),
`make check` green.
```

## Prompt M7 — Read surface

```
Read AGENTS.md, docs/PRD.md §7.10 §7.13 §10, docs/harness/02_DATA_CONTRACTS.md §4 (generated
markers) §9, docs/harness/03_LLM_INTERFACES.md C5, and phase M7 of docs/harness/05_BUILD_PLAN.md.

TDD first (FR-READ, FR-COV, NFR-04, MCP contract tests), then implement core/coverage.py with
per-source breakdown files, core/summary.py (C5, ≤300 words, regenerate only when current
blocks changed) and wiki/_index.md, mcp/tools/wiki_read.py registering wiki_index /
wiki_summary / wiki_page / wiki_search / wiki_coverage exactly per the contracts table, the
guide-tool text update teaching the two-tier pattern and status semantics, block-aware routing
of existing create/edit/append for pages containing cb delimiters, and the `llmwiki coverage`
subcommand. Hook coverage+summary rendering into process and apply-decisions.

Definition of done: phase M7 DoD incl. the AS-UC2-1 bounded-read test, `make check` green.
```

## Prompt M8 — Lint, eval, taxonomy, locking

```
Read AGENTS.md, docs/PRD.md §7.12 §7.14 §12 §7.2 FR-REG-03, docs/harness/02_DATA_CONTRACTS.md
§12, and phase M8 of docs/harness/05_BUILD_PLAN.md.

TDD first (one seeded-defect fixture per lint check, lock matrix, taxonomy link-rewrite, eval
scoring), then implement core/lint/ (LINT-01..08, JSON findings, exit codes, llmwiki lint),
core/eval/harness.py + llmwiki eval (answers golden-QA using only the wiki_* read tools with
the configured provider — fake in tests; scoring per TDD plan; baseline + regression exit),
core/taxonomy.py merge/split/rename with redirect stubs and inbound-link rewriting + CLI
subcommands, core/lock.py wired into every mutating entrypoint with stale-lock stealing, and
the ACW_AUTO_PROCESS watcher hook defaulting off.

Definition of done: phase M8 DoD, `make check` green.
```

## Prompt M9 — Acceptance & hardening

```
Read AGENTS.md, docs/PRD.md §13 §14 §11, and phase M9 of docs/harness/05_BUILD_PLAN.md.

Write the five acceptance scenarios AS-UC1-1, AS-UC1-2, AS-UC2-1, AS-UC2-2, AS-UC3-1 as
end-to-end tests under tests/v2/acceptance (FakeLLM; scripted fixtures where judgment matters),
plus the NFR-03 synthetic-scale smoke (50 sources / ~5,000 chunks generated in-test) and a
re-assertion of NFR-01. Fix whatever they uncover. Then write README_V2.md (quickstart, full
workflow, CLI reference, MCP config for consuming agents, live-LLM env configuration,
troubleshooting), scripts/smoke_quickstart.sh, the gated live smoke test, and finalize
docs/harness/07_DECISIONS.md with every decision logged during the build.

Definition of done: pytest tests/v2/acceptance -q green, make check green,
scripts/smoke_quickstart.sh succeeds on a fresh clone.
```

## Repair prompt (template)

```
Read AGENTS.md. Phase <Mx> of docs/harness/05_BUILD_PLAN.md was implemented but has problems:

<paste failing command output / review comments>

Fix these while preserving the phase's tests (do not weaken or delete tests to pass; if a test
itself is wrong per the contracts in docs/harness/02_DATA_CONTRACTS.md, fix the test and log
why in docs/harness/07_DECISIONS.md). Definition of done: the pasted commands pass and
`make check` is green.
```

## Resume prompt (template, for interrupted phases)

```
Read AGENTS.md and phase <Mx> of docs/harness/05_BUILD_PLAN.md. This phase is partially
implemented on the current branch. Audit what exists against the phase scope and TDD list,
complete the remainder test-first, and finish with the phase Definition of Done plus
`make check` green. Log any decisions in docs/harness/07_DECISIONS.md.
```

# 04 — TDD Plan (Normative process)

## 1. Principles

1. **Tests first, per phase.** Each Build Plan phase lists its tests; write them, see them
   fail, implement, see them pass. Test names encode FR-IDs:
   `test_fr_block_04_round_trip_byte_identical`.
2. **No network, no API keys** in unit/integration tests — FakeLLM only. One optional live
   smoke test (`tests/v2/live/test_smoke_live.py`) runs only when `ACW_LIVE_TESTS=1`.
3. **Real SQLite** in tmp dirs; real filesystem; real git (tmp repos). Mock nothing that is
   cheap to run for real.
4. **Invariant suite as a library.** `tests/v2/invariants/` exposes
   `assert_invariants(workspace)` checking, against any workspace state:
   - I1 every ledger chunk has a valid disposition; reasons present where required
   - I2 no `pending` chunks after a completed run
   - I3 every `placed` chunk links ≥1 block; every block links ≥1 chunk
   - I4 every block in markdown exists in `acw_blocks` and vice-versa (status≠rejected/deleted)
   - I5 every page parses; serialize(parse(p)) == p byte-identical
   - I6 every block has non-empty excerpt + source_path
   - I7 rendered Source Coverage equals ledger-derived expectation
   - I8 pending markers ⟷ `conflicted_pending` ledger rows, 1:1
   - I9 no duplicate active keys on a page outside `conflicted`
   - I10 source files' bytes unchanged since fixture creation (NFR-05)
   End-to-end tests call it after **every** pipeline operation.
5. **Property-based tests** (hypothesis) for the parser/serializer: generate pages with random
   prose, headings, N blocks with adversarial content (lines starting `> `, HTML comments,
   unicode, code fences) → round trip must hold; mutation ops preserve untouched segments.

## 2. Layout

```
tests/v2/
  conftest.py            # tmp workspace factory, db, FakeLLM, git fixtures
  fakes/fake_llm.py
  invariants/__init__.py
  unit/                  # per-module, phase-tagged
  pipeline/              # integration of process/apply over golden workspaces
  acceptance/            # AS-UC1-1 … AS-UC3-1 verbatim from PRD §14
  live/                  # gated live smoke
  fixtures/workspaces/
    uc1_minimal/  uc2_nodes/  uc3_support/
  fixtures/llm/          # scripted FakeLLM response fixtures (JSON)
```

## 3. Golden workspaces (built in M1, grown over phases)

- **uc1_minimal**: `flows/order_refund.yaml` (6 nodes, 7 edges, 2 branch conditions),
  `docs/faq.md` (4 Q&As), `docs/tnc.md` (refund window 30 days; retry 3×),
  `docs/api.md` (maxRetries: 2 ← deliberate conflict), `docs/brd.md` (3 requirements),
  `notes/misc.txt` (1 relevant para + 1 irrelevant recipe para).
- **uc2_nodes**: `docs/nodes/*.md` — 10 node-type docs each with settings tables incl.
  `api_call.md` (`default_timeout: 30s`), plus `uc2_nodes_v2/` overlay changing the timeout to
  45s for re-ingestion tests.
- **uc3_support**: `docs/troubleshooting_2024.md`, `rca/RCA-2026-014.md` (contradicts the 2024
  guidance), `tickets/T-1001.md`, `transcripts/standup_2026-05-02.txt` (chatter + 2 decisions,
  one superseding the other mid-transcript).
- Golden-QA files `eval/questions.yaml` per workspace: `{question, expected_keys:[…],
  expected_substrings:[…]}` (≥6 per workspace) for the eval harness.

FakeLLM rule-based mode must be sufficient to drive these to deterministic outcomes; scripted
fixtures cover the tricky judgments (the retry conflict, transcript supersession).

## 4. Test inventory by FR (write in the phase shown)

| FR / NFR | Key tests | Phase |
|---|---|---|
| LEDGER-01..03 | row created `pending` on chunking; enum transitions enforced (illegal transition raises); placed↔block linkage | M1/M3 |
| LEDGER-04 | interrupted run (kill after N chunks) resumes and completes; completion gate fails if a chunk is left pending | M4 |
| LEDGER-05/06 | exports written, stable ordering; source_date extraction/fallback recorded with origin | M1/M3 |
| REG-01..02 | registry CRUD; placement with matching alias does NOT create page; new-page assertion logged | M1/M4 |
| REG-03..04 | merge/split/rename move blocks, rewrite `[[links]]`, leave redirect stub; similarity candidates emitted advisory | M8 |
| BLOCK-01..06 | schema validation; status semantics; round-trip property tests; close-marker-injection rejected; key grammar; truncation keeps value spans | M2 |
| PLACE-01..08 | irrelevant chunk → reasoned disposition; multi-topic split; section creation in canonical order; batching ≤ max; incremental worklist O(changed) — second run zero LLM calls (also NFR-01) | M4 |
| FLOW-01..03 | Whimsical JSON → Mermaid node/edge-complete (deterministic, exact); YAML via C2 with count check; prose block never replaces flow block | M4 |
| CONF-01..05 | retrieval = key match + FTS5 (no embeddings); exact-dup short-circuit no-LLM; conflict → no page overwrite + pending marker + Open Conflicts entry; recommendation cites timestamp basis | M5 |
| REV-01..05 | one file per run grouped by page; parser round-trip; invalid decision aborts all with per-row report; each of the 8 decisions produces exactly the PRD table effects (8 parametrized tests); unresolved file surfaced at next run | M5/M6 |
| REING-01..04 | unchanged-hash chunks keep dispositions; removed chunks → superseded; fully-superseded block → needs_review with reason; deleted source path; one process call propagates a doc edit (AS-UC2-2) | M3 |
| TRANS-01..02 | chatter dropped with reasons; intra-transcript supersession; meeting date extracted; default types decision/note; excerpt always present | M4 |
| READ-01..04 | summary ≤300 words regenerated only when current blocks change; `_index.md` complete; four MCP tools' contracts; status filtering returns metadata-complete block list | M7 |
| GIT-01..03 | auto-init; structured commits at the 3 trigger points; user edit inside delimiters → protected (subsequent decision cannot modify content, recommendation annotated); free prose survives runs verbatim; hard-delete removes page+ledger excerpt and prints git rewrite instructions without claiming completion | M6 |
| LINT-01..08 | one failing fixture per check proving detection; clean golden workspace passes | M8 |
| COV-01..02 | counts (n of m) match ledger; manual edit to coverage section overwritten next run | M7 |
| LOCK-01..03 | second process fails fast; read tools unblocked; mid-run page edit → retry once then deferred pending | M8 |
| NFR-01 | second `process` run: 0 page writes, 0 FakeLLM calls (call counter) | M4, re-asserted M9 |
| NFR-04 | generated md passes CommonMark parse (use markdown-it-py already transitively available, else add dev dep) | M7 |
| §12.6 eval | harness answers golden-QA via wiki_* tools only; scoring = expected_keys ⊆ retrieved block keys ∧ substrings present; regression baseline stored in `eval/baseline.json` | M8 |
| AS-UC1-1..AS-UC3-1 | one e2e test each, named after the scenario, run on golden workspaces with FakeLLM | M9 |

## 5. Gates

`make check` = ruff + `pytest tests/unit tests/v2 -q` (+ existing integration when available).
CI (`.github/workflows/ci.yml`) runs the same on push. Coverage of `core/` measured with
`pytest --cov=core`; fail CI under 85% from M4 onward.

# 03 — LLM Interfaces (Normative)

LLM calls happen **only** at these six call sites, all routed through
`core/llm/provider.py::LLMProvider.complete_structured(call_site, payload, schema)`.
Every call: structured JSON output enforced against the schema below (provider-native
structured outputs when available, else JSON-mode + local validation); on validation failure
retry once with the validator error appended; then fail the unit of work (→ `failed`
disposition or skipped summary). Every call logs an `llm.call` event.

`LLMProvider` is a Protocol; `OpenAIProvider` is the default; `FakeLLM` (tests) implements the
same Protocol. Prompt templates live in `core/llm/calls.py` as module constants — nowhere else.

## C1 — Placement (FR-PLACE-01..04, FR-PLACE-07)

One call per chunk batch (same source, ≤ `ACW_BATCH_MAX_CHUNKS`).

Input payload: workspace domain hint, the chunks (id, text, breadcrumb), the **full registry**
(id, title, description, aliases of active pages), the target-page candidates' section outlines
and **per-page key inventories**, the canonical section list.

Output schema (per chunk):
```json
{"chunks":[{"chunk_id":"ch_…","relevant":true,
  "irrelevant_reason":null,
  "placements":[{"page": {"existing_page_id":"pg_…"} ,
                 "new_page": null,
                 "section":"Rules",
                 "block":{"key":"refunds.window_days","type":"rule",
                          "content":"…restatement…","excerpt":"…verbatim…",
                          "new_key_justification":null},
                 "links":["pg_…"]}]}]}
```
Constraints enforced by the validator (not trust): `relevant=false` ⇒ non-empty
`irrelevant_reason` and empty placements; `new_page` ⇒ `{title, description, domain, path_slug}`
plus a non-null `no_registry_match_assertion` string (FR-REG-02, logged); a key not in the
page's inventory ⇒ non-null `new_key_justification` (FR-BLOCK-06); `excerpt` must be a
substring-modulo-whitespace of the chunk text (validator check — guarantees verbatim fidelity
mechanically); multi-topic chunks ⇒ multiple placements sharing the chunk id.

## C2 — Flow extraction (FR-FLOW-01/02) — *only for non-Whimsical structured sources*

Whimsical MCP JSON is converted to Mermaid **deterministically** in `pipeline/flows.py`
(no LLM). Arbitrary YAML/XML flow definitions use C2: input = raw structure text; output =
`{"mermaid": "...", "nodes": [ids], "edges": [{"from","to","condition"}]}`. The validator parses
the source with yaml/lxml, counts candidate nodes/edges heuristically, and lint FR-LINT-08
compares counts. The flow block's excerpt = verbatim source structure (or path reference if
> 1,500 chars).

## C3 — Conflict/duplicate judge (FR-CONF-01)

Pairwise: candidate block vs one retrieved comparison block (same payload fields: key, type,
content, excerpt, source_path, source_date). Output:
`{"verdict":"distinct"|"duplicate"|"conflict","conflict_type":<§6 enum|null>,
"recommendation":<decision enum>,"rationale":"…"}`.
The *recommendation basis* (`source_date` vs `mtime`) is computed by code, not the model
(FR-CONF-04): code picks the timestamp pair, passes it in, and records which was used.
Exact-duplicate short-circuit (same key + same source path + normalized-equal content) is
decided in code before any C3 call (FR-CONF-03).

## C4 — Merge drafting (FR-REV-03 `merge` without pre-approved text)

Input: both blocks. Output: `{"content":"…","excerpt_policy":"keep_both"}` — the merged block
keeps **both** excerpts concatenated (fidelity); result enters the next run's review file as a
new candidate row, never applied directly.

## C5 — Summary regeneration (FR-READ-01)

Input: page title + all `current` blocks (key, type, content). Output:
`{"summary_markdown":"…"}`, ≤ `ACW_SUMMARY_MAX_WORDS` (validator counts words; retry once with
"shorten" instruction). Summaries carry the generated-content marker and no coverage guarantee.

## C6 — Transcript pre-pass (FR-TRANS-01)

Input: ordered transcript segments (chunk ids + text + speaker/timestamps if present).
Output per segment: `{"chunk_id","relevant":bool,"reason":str|null,
"superseded_by_chunk_id":str|null,"key_hint":str|null,"source_date_extracted":str|null}`.
Code applies: irrelevant → `irrelevant`; superseded → `duplicate` of the survivor's eventual
block with reason `intra_transcript_supersession`; extracted date → `acw_source_versions.source_date`
(origin `content`). Surviving segments proceed to C1 with `type` defaulting to
`decision`/`note` (FR-TRANS-02).

## FakeLLM (tests)

`tests/v2/fakes/fake_llm.py`: same Protocol; responses keyed by `(call_site, fingerprint)`
where fingerprint is a stable hash of selected payload fields (e.g. chunk ids). Two modes:
**scripted** (fixtures provide exact responses; unknown fingerprint raises — strict) and
**rule-based** (simple deterministic heuristics for golden-workspace e2e tests, e.g. "place
chunk in the page whose title shares the most words; excerpt = first sentence containing a
digit"). Both modes must be deterministic. No test may hit the network.

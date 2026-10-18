# Agent Context Wiki v2

Agent Context Wiki v2 turns a local source folder into a lossless, source-attributed wiki for
agents. Source files stay untouched; generated context lives under `wiki/`, with coverage,
review rows, and read-tier MCP tools backed by `.llmwiki/index.db`.

## Quickstart

Run from this repository. Setup requires Python 3.11 or newer, Git, and Make; dependencies
are installed into the project's `.venv`. Re-run `make setup` when requirements change.

Windows (PowerShell):

```powershell
make setup
$env:ACW_LLM_PROVIDER = 'fake-rules'
.venv/Scripts/python.exe scripts/smoke_quickstart.py
```

Linux/macOS:

```bash
make setup
export ACW_LLM_PROVIDER=fake-rules
.venv/bin/python scripts/smoke_quickstart.py
```

FakeLLM is a deterministic test/demo provider. Use a real model or the connected-agent
provider below to interpret your own documents.

The same smoke check is available through Bash:

```bash
scripts/smoke_quickstart.sh
```

The smoke creates a workspace under `.tmp`, processes a short source, proves an unchanged
run makes zero page writes and zero model calls, changes a policy, approves its review,
and checks invariants, `lint`, `coverage`, `eval`, and MCP configuration. It checks source
files remain unchanged by each operation. Set `ACW_SMOKE_KEEP=1` to retain the demo.
`ACW_SMOKE_WORKSPACE` may name a new directory; the script never deletes that directory.

For the commands below, replace `.venv/bin/python` with `.venv/Scripts/python.exe` on Windows.

## Full Workflow

1. Add source files anywhere outside `.llmwiki/` and `wiki/`.
2. Run `.venv/bin/python llmwiki process <workspace>`.
3. Read generated pages under `wiki/`; every context block includes a verbatim excerpt.
4. If `wiki/_reviews/RR-*.md` exists, fill `decision:` for each row.
5. Run `.venv/bin/python llmwiki apply-decisions <workspace> [review_file]`.
6. Run `.venv/bin/python llmwiki lint <workspace>` and `.venv/bin/python llmwiki eval <workspace>`.
7. Inspect git history inside `wiki/` for structured process and decision commits.

## CLI Reference

- `init <workspace>`: create `.llmwiki/`, `wiki/`, SQLite schema, and initial wiki files.
- `reindex <workspace>`: rescan sources and seed the v2 Chunk ledger.
- `process <workspace>`: scan sources, extract pending documents, run placement, conflict detection,
  coverage, summaries, exports, and a local git commit.
- `apply-decisions <workspace> [review_file]`: apply review-file decisions deterministically.
- `import-decisions <workspace> <csv_path>`: import edits to a review's CSV companion;
  then run `apply-decisions`. Only the `decision` and `notes` columns are editable.
- `reset-dispositions <workspace> --source <path-substring> --from irrelevant --to pending`:
  reconsider excluded content on the next processing run.
- `prune-ledger <workspace>`: remove excluded or orphaned ledger records.
- `lint <workspace> [--json]`: run v2 lint checks; exits nonzero on errors.
- `coverage <workspace> [source]`: print ledger-derived coverage.
- `eval <workspace> [--json] [--update-baseline]`: run golden-QA over the wiki read tools.
- `mcp <workspace>`: serve the local MCP server over stdio.
- `mcp-config <workspace> [--name NAME]`: print a client config snippet.
- `merge-pages <workspace> <source-page> <target-page>`: merge pages, preserving notes and old names.
  Resolve competing current blocks with the same key before merging; the command refuses such collisions.
- `split-page <workspace> <page> <section> --new-title <title>`: move a section's blocks to a new page.
- `rename-page <workspace> <page> <new-title>`: rename a page and update links and the index.
- `hard-delete <workspace> --block-id <id>`: explicit destructive delete path for a block;
  this does not rewrite existing git history or remove the original source file.

## MCP For Consuming Agents

Generate config:

```bash
.venv/bin/python llmwiki mcp-config /path/to/workspace --name acw-local
```

Typical read pattern:

1. `wiki_index()`
2. `wiki_summary(page)` for candidate pages
3. `wiki_page(page)` only for pages needing full evidence
4. `wiki_search(query, tier="summary")`, then `tier="full"` when evidence is needed

`wiki_page` defaults to `current`, `conflicted`, and `needs_review` blocks. Pass
`statuses=["*"]` to include deprecated history.

## Live LLM Configuration

Tests and quickstart use FakeLLM. For live processing:

```bash
export ACW_LLM_PROVIDER=openai
export ACW_LLM_API_KEY=...
export ACW_LLM_MODEL=gpt-5
export ACW_LLM_MODEL_LIGHT=gpt-5
.venv/bin/python llmwiki process /path/to/workspace
```

Optional:

- `ACW_LLM_BASE_URL`: OpenAI-compatible endpoint override.
- `ACW_BATCH_MAX_CHUNKS`: placement batch size.
- `ACW_SUMMARY_MAX_WORDS`: summary budget.
- `ACW_LIVE_TESTS=1`: enables `tests/v2/live` smoke tests.

PowerShell uses `$env:NAME = 'value'` instead of `export NAME=value`. The real provider uses
the Responses API with strict JSON schemas. The live smoke is opt-in and requires your key;
the normal check never needs a key or makes model API calls.

To use a connected reasoning agent instead, set `ACW_LLM_PROVIDER=agent` and run `process`.
It waits for answers in `.llmwiki/agent_queue`. In the MCP client, the agent repeatedly calls
`acw_next_request()`, follows that request's instructions and schema, and submits a JSON
object with `acw_answer_request(request_id, response_json)`.

The first run creates `.llmwiki/domain_hint.md` if absent. You can edit it to describe the
workspace. If more than half of a source would be excluded, a source-level review asks you
to choose `approve_exclusion` or `reinstate`; exclusion is not silently finalized. The
threshold is configurable with `ACW_EXCLUSION_REVIEW_THRESHOLD`.

## Extraction and source changes

Text sources and spreadsheets are read locally. PDFs use the configured existing PDF
backend; Office documents require LibreOffice. Missing extractors, unsupported formats,
and unreadable files produce ledger errors rather than disappearing from coverage.
Failed work retries up to `ACW_MAX_ATTEMPTS` (default 3). Images without extracted text need
a text export. A modified or deleted source marks unsupported old blocks `needs_review`
and creates review rows. Staleness-only rows accept `keep_existing`, `deprecate_existing`,
or `needs_more_info`.

Source files belong outside `wiki/` and `.llmwiki/`. Evaluation questions belong in `eval/`.
Manual `process` is the default. With `ACW_AUTO_PROCESS=1`, the running local file watcher
requests processing after changes; it queues writes while another operation holds the lock.

## Checks

```bash
make check
make mcp-import-smoke
```

`make check` runs lint, existing unit tests, v2 tests with at least 85% coverage of `core/`,
the 50-source/5,000-chunk scale case, and integration tests. PostgreSQL-dependent integration
tests skip when the local test database is unavailable; local SQLite tests still run.

## Troubleshooting

- Missing dependencies: run `make setup`; it installs into `.venv/`.
- No API key: use `ACW_LLM_PROVIDER=fake-rules` for local smoke tests.
- Unresolved reviews: edit `wiki/_reviews/RR-*.md`, fill `decision:`, then run `apply-decisions`.
- Lock errors: another write operation is active; retry after it exits.
- Lint errors: run `.venv/bin/python llmwiki lint <workspace> --json` for machine-readable findings.
- Unexpected page changes: inspect `git -C <workspace>/wiki log --stat`.
- Source safety: v2 never modifies, moves, or deletes source files.

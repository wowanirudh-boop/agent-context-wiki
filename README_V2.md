# Agent Context Wiki v2

Agent Context Wiki v2 turns a local source folder into a lossless, source-attributed wiki for
agents. Source files stay untouched; generated context lives under `wiki/`, with coverage,
review rows, and read-tier MCP tools backed by `.llmwiki/index.db`.

## Quickstart

```bash
make setup
export ACW_LLM_PROVIDER=fake-rules
python llmwiki process /path/to/workspace
python llmwiki lint /path/to/workspace
python llmwiki coverage /path/to/workspace
```

For a self-contained check from this clone:

```bash
scripts/smoke_quickstart.sh
```

The quickstart smoke creates a temporary workspace, processes a small source, runs `lint`,
`coverage`, `eval`, and writes an MCP config snippet.

## Full Workflow

1. Add source files anywhere outside `.llmwiki/` and `wiki/`.
2. Run `python llmwiki process <workspace>`.
3. Read generated pages under `wiki/`; every context block includes a verbatim excerpt.
4. If `wiki/_reviews/RR-*.md` exists, fill `decision:` for each row.
5. Run `python llmwiki apply-decisions <workspace> [review_file]`.
6. Run `python llmwiki lint <workspace>` and `python llmwiki eval <workspace>`.
7. Inspect git history inside `wiki/` for structured process and decision commits.

## CLI Reference

- `init <workspace>`: create `.llmwiki/`, `wiki/`, SQLite schema, and initial wiki files.
- `reindex <workspace>`: rescan sources and seed the v2 Chunk ledger.
- `process <workspace>`: run placement, conflict detection, coverage, summaries, exports, and git commit.
- `apply-decisions <workspace> [review_file]`: apply review-file decisions deterministically.
- `lint <workspace> [--json]`: run v2 lint checks; exits nonzero on errors.
- `coverage <workspace> [source]`: print ledger-derived coverage.
- `eval <workspace> [--json] [--update-baseline]`: run golden-QA over the wiki read tools.
- `mcp <workspace>`: serve the local MCP server over stdio.
- `mcp-config <workspace> [--name NAME]`: print a client config snippet.
- `merge-pages`, `split-page`, `rename-page`: taxonomy operations over parsed page models.
- `hard-delete --block-id <id>`: explicit destructive delete path for a block.

## MCP For Consuming Agents

Generate config:

```bash
python llmwiki mcp-config /path/to/workspace --name acw-local
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
python llmwiki process /path/to/workspace
```

Optional:

- `ACW_LLM_BASE_URL`: OpenAI-compatible endpoint override.
- `ACW_BATCH_MAX_CHUNKS`: placement batch size.
- `ACW_SUMMARY_MAX_WORDS`: summary budget.
- `ACW_LIVE_TESTS=1`: enables `tests/v2/live` smoke tests.

## Troubleshooting

- Missing dependencies: run `make setup`; it installs into `.venv/`.
- No API key: use `ACW_LLM_PROVIDER=fake-rules` for local smoke tests.
- Unresolved reviews: edit `wiki/_reviews/RR-*.md`, fill `decision:`, then run `apply-decisions`.
- Lock errors: another write operation is active; retry after it exits.
- Lint errors: run `python llmwiki lint <workspace> --json` for machine-readable findings.
- Unexpected page changes: inspect `git -C <workspace>/wiki log --stat`.
- Source safety: v2 never modifies, moves, or deletes source files.

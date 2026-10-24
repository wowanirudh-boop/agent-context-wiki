# Agent Context Wiki

Turn a folder of documents into a wiki that you and your AI agents can use.

The wiki keeps source excerpts with each fact, flags disagreements for you to review,
and updates when your files change. Your original files stay untouched.

## What it does

- Organizes notes, policies, transcripts, spreadsheets, and other documents into pages.
- Keeps the original evidence alongside generated text.
- Asks you to review conflicting information before replacing it.
- Tracks what was used, excluded, or could not be processed.
- Lets connected AI agents search the wiki and read summaries or full pages.

Conflict checks also look across pages and separate documents. Different labels for the
same fact can be matched, but disagreements still need your review. Detection can miss
matches; it is not a guarantee that every contradiction will be found.

This version runs on your computer. Building a wiki from your own files requires an
AI model or a connected agent. The included demo works without an API key.

## Try the demo

You need **Python 3.11 or newer, Git, and Make**. Run these commands from this repository's folder.

**Windows — PowerShell**

```powershell
make setup
.venv/Scripts/python.exe scripts/smoke_quickstart.py
```

**macOS / Linux**

```bash
make setup
.venv/bin/python scripts/smoke_quickstart.py
```

The demo builds a temporary wiki, changes a policy, applies a review decision, and
checks the result. It uses built-in sample responses, not a live AI model.

## Use your own files

First, [choose an AI model or connect an agent](README_V2.md#live-llm-configuration).
Then replace `path/to/your-folder` below with the folder containing your documents.
Use quotes if the path contains spaces.

```bash
.venv/bin/python llmwiki process "path/to/your-folder"
```

On Windows, use `.venv/Scripts/python.exe` instead of `.venv/bin/python`.

1. Read the generated pages in `wiki/` inside your folder.
2. If there are review files in `wiki/_reviews/`, fill in their `decision:` fields.
3. Apply your decisions:

```bash
.venv/bin/python llmwiki apply-decisions "path/to/your-folder"
```

Run `process` again after changing your documents. An unchanged, fully processed
workspace makes no new model calls or page writes.

Keep both `wiki/` and `.llmwiki/` when backing up your workspace. They contain your
pages, processing records, and review history.

## Connect an AI agent

Generate connection settings with:

```bash
.venv/bin/python llmwiki mcp-config "path/to/your-folder"
```

Add the output to an MCP-compatible client. MCP is the connection that lets an AI
agent use this project's tools. Ask the agent to start with `wiki_index`, then read
page summaries and the source evidence it needs.

See the [full guide](README_V2.md) for model settings, review decisions, supported
file handling, and all commands. Some file formats need extra software; Word and
PowerPoint extraction require LibreOffice.

## Run the checks

```bash
make check
```

Tests use sample model responses and do not need an API key. Database tests that
require PostgreSQL are skipped when it is unavailable.

## Credits and license

Built on [LLM Wiki](https://github.com/lucasastorian/llmwiki). Licensed under
[Apache 2.0](LICENSE).

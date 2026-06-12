# SETUP — Using this harness with the Codex desktop app (local mode)

You'll work entirely out of the Codex desktop app on your own machine. No terminal skills
needed — Codex does the git and environment work; you review and approve.

## One-time setup (~15 minutes)

### 1. Create your independent copy of the repo on GitHub
(Already done if you followed the Import step.) You should have
`github.com/<you>/agent-context-wiki` containing the full llm-wiki code.

### 2. Get the repo onto your machine
In the Codex desktop app, when creating/opening the project, choose to **clone from GitHub**
and pick `agent-context-wiki`. If your existing project already shows the repo files and the
`master` branch in the bottom bar, this is done — skip ahead.
If the app only lets you pick an existing local folder, ask Codex itself (any chat in the app
with Full access) to do it:

```
Clone https://github.com/<your-username>/agent-context-wiki into a folder called
agent-context-wiki in my home directory, then tell me the full path.
```

Then point the project at that folder.

### 3. Put the harness files into the project
Unzip `codex-harness.zip`. Copy everything into the repo folder on your machine, preserving
paths — easiest way: drag the unzipped contents into the project folder in your file
explorer/Finder so you end up with:

```
agent-context-wiki/
  AGENTS.md          ← from the zip (root)
  SETUP.md           ← from the zip (root)
  docs/PRD.md        ← from the zip
  docs/harness/      ← 8 files from the zip
  .github/workflows/ci.yml
  api/  mcp/  ...    ← existing repo files
```

(If your OS hides the `.github` folder, that's fine — it copied.)

### 4. Have Codex commit and push the harness
New thread in the project, paste:

```
The repo root now contains new harness files: AGENTS.md, SETUP.md, docs/PRD.md,
docs/harness/ (8 files), and .github/workflows/ci.yml. Verify all of them are present,
commit them with message "harness: add Codex build harness for Agent Context Wiki v2",
and push to origin master.
```

Check on github.com that the files appeared. CI will start running on pushes from now on —
that's your independent safety net.

### 5. Python
The build runs tests on YOUR machine, so Python 3.11 or newer must be installed. Don't worry
about checking — Prompt M0 makes Codex verify this and tells you exactly what to install if
it's missing (python.org download, one click).

No API keys are needed for the build — all tests use a fake LLM. Keys come in only when you
*use* the finished system (step "After M9").

## Build loop (repeat 10 times, M0 → M9)

1. **New thread** in the Codex desktop project (this keeps each phase's context clean —
   important).
2. Paste the next prompt from `docs/harness/06_CODEX_PROMPTS.md` (start with M0). Keep
   "Full access" on so Codex can run tests and git commands.
3. When Codex finishes, verify before accepting:
   - Ask in the same thread: `Run make check and show me the final summary lines.`
     You want to see all green / 0 failed.
   - Skim Codex's summary against the phase's Definition of Done in
     `docs/harness/05_BUILD_PLAN.md`.
4. Green → tell Codex: `Commit this phase to master and push to origin.` (If the app put the
   work on a codex/ branch or worktree, say instead: `Merge this work into master and push.`)
   The push triggers CI on GitHub — confirm the green ✓ appears on the latest commit.
5. Not green → new thread, paste the **Repair prompt** template with the failing output.
   Thread got cut off mid-phase → **Resume prompt** template.
6. Next phase. One prompt per thread, strictly in order, never two phases at once (they share
   files, and local worktrees would conflict).

## After M9 — using the system

Everything runs from the same project folder. Codex will have written `README_V2.md` with the
exact commands; the short version:

```
export ACW_LLM_API_KEY=sk-...            # your OpenAI key, real provider
./llmwiki init <path-to-your-workspace>
./llmwiki process <workspace>            # builds the wiki, emits wiki/_reviews/RR-<run>.md
# open the review file, fill in decision: fields, then
./llmwiki apply-decisions <workspace>
./llmwiki lint <workspace>
./llmwiki mcp-config <workspace>         # connect consuming agents (wiki_* tools)
```

You can also just ask Codex in the project to run these for you.

## Why this harness keeps Codex on the rails

- **AGENTS.md** carries the invariants into every thread automatically (the app reads it).
- **02_DATA_CONTRACTS.md** removes schema/format improvisation — the #1 way agent builds drift.
- **Phased prompts + tests-first** keep each thread's surface small and verifiable; the
  invariant suite and idempotency test catch cross-phase regressions.
- **FakeLLM** keeps `make check` hermetic — green means the code is right, locally and in CI.
- **07_DECISIONS.md** gives Codex a sanctioned place to resolve ambiguity instead of silently
  inventing behavior.

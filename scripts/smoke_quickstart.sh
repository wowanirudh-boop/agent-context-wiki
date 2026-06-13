#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON="$ROOT/.venv/Scripts/python.exe"
else
  make -C "$ROOT" setup
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON="$ROOT/.venv/Scripts/python.exe"
  fi
fi

WORKSPACE="${ACW_SMOKE_WORKSPACE:-$(mktemp -d)}"
if [[ "${ACW_SMOKE_KEEP:-0}" != "1" ]]; then
  trap 'rm -rf "$WORKSPACE"' EXIT
fi

mkdir -p "$WORKSPACE/docs" "$WORKSPACE/eval"

cat > "$WORKSPACE/docs/tnc.md" <<'EOF'
# Refund Terms

Source date: 2026-01-15

Refund requests must be opened within 30 days of the original purchase date.
Support agents must use this refund window when planning test cases, answering
customer questions, and checking whether a refund workflow branch should approve
or reject the request. This extra context makes the quickstart document large
enough for the local chunker while keeping the expected answer simple.
EOF

cat > "$WORKSPACE/eval/questions.yaml" <<'EOF'
- question: What is the refund request window?
  expected_keys: [refunds.window_days]
  expected_substrings: ["30 days"]
EOF

export ACW_LLM_PROVIDER=fake-rules

"$PYTHON" "$ROOT/llmwiki" process "$WORKSPACE"
"$PYTHON" "$ROOT/llmwiki" lint "$WORKSPACE"
"$PYTHON" "$ROOT/llmwiki" coverage "$WORKSPACE" > "$WORKSPACE/coverage.md"
"$PYTHON" "$ROOT/llmwiki" eval "$WORKSPACE" --json > "$WORKSPACE/eval-result.json"
"$PYTHON" "$ROOT/llmwiki" mcp-config "$WORKSPACE" > "$WORKSPACE/mcp-config.json"

test -f "$WORKSPACE/wiki/_index.md"
test -f "$WORKSPACE/wiki/_meta/ledger.json"
grep -R "refunds.window_days" "$WORKSPACE/wiki" >/dev/null
grep '"score": 1.0' "$WORKSPACE/eval-result.json" >/dev/null

echo "ACW v2 quickstart smoke succeeded: $WORKSPACE"

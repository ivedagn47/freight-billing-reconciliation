#!/usr/bin/env bash
# smoke-branch worker_b: writes a deterministic note_b JSON output.
# Inputs:  none (deterministic)
# Output:  $FLOWSTATE_VAR_note_b (path populated by flowstate from sets_variables)
set -euo pipefail

OUTPUT_PATH="${FLOWSTATE_VAR_note_b:?FLOWSTATE_VAR_note_b not set}"

cat > "$OUTPUT_PATH" <<'EOF'
{
  "_session_id": "smoke-branch-worker-b",
  "note": "hello from branch B"
}
EOF

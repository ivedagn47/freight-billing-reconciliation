#!/usr/bin/env bash
# smoke-branch worker_a: writes a deterministic note_a JSON output.
# Inputs:  none (deterministic)
# Output:  $FLOWSTATE_VAR_note_a (path populated by flowstate from sets_variables)
set -euo pipefail

OUTPUT_PATH="${FLOWSTATE_VAR_note_a:?FLOWSTATE_VAR_note_a not set}"

cat > "$OUTPUT_PATH" <<'EOF'
{
  "_session_id": "smoke-branch-worker-a",
  "note": "hello from branch A"
}
EOF

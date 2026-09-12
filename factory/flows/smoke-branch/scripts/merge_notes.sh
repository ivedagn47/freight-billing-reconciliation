#!/usr/bin/env bash
# smoke-branch merge_notes: reads merged branch scope and writes a combined note.
# Inputs:  $FLOWSTATE_VAR_note_a (path to worker_a's note JSON; set by flowstate scope merge)
#          $FLOWSTATE_VAR_note_b (path to worker_b's note JSON; set by flowstate scope merge)
# Output:  $FLOWSTATE_VAR_merged_note (path populated by flowstate from sets_variables)
set -euo pipefail

OUTPUT_PATH="${FLOWSTATE_VAR_merged_note:?FLOWSTATE_VAR_merged_note not set}"
NOTE_A="${FLOWSTATE_VAR_note_a:?merge_notes: FLOWSTATE_VAR_note_a is unset}"
NOTE_B="${FLOWSTATE_VAR_note_b:?merge_notes: FLOWSTATE_VAR_note_b is unset}"

cat > "$OUTPUT_PATH" <<EOF
{
  "_session_id": "smoke-branch-merge",
  "merged": "${NOTE_A} + ${NOTE_B}"
}
EOF

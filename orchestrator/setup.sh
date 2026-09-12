#!/usr/bin/env bash
# One-time setup for the orchestrator CLIs: creates a local venv and
# installs the three Python dependencies. Requires python3, git, tmux, jq.
set -euo pipefail
ORCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for tool in python3 git tmux jq; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool" >&2; exit 1; }
done
python3 -m venv "$ORCH_ROOT/.venv"
"$ORCH_ROOT/.venv/bin/pip" install --quiet -r "$ORCH_ROOT/requirements.txt"
echo "OK. CLIs ready: $ORCH_ROOT/bin/flowstate, $ORCH_ROOT/bin/agentctl"

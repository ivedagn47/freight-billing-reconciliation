#!/bin/bash
# Gate: verify the research phase produced a non-empty brief file.
set -euo pipefail
BRIEF="${FLOWSTATE_VAR_research_brief:-}"
[ -n "$BRIEF" ] || { echo "research_brief env var empty" >&2; exit 1; }
[ -s "$BRIEF" ] || { echo "research brief missing or empty at $BRIEF" >&2; exit 1; }
exit 0

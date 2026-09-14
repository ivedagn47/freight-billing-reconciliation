# Freight Billing Reconciliation

An agent system that reconciles carrier freight invoices (Alpine Express,
Falcon Freight, Sagar Roadlines — JSON, free-text, and CSV respectively)
against BlueFin Commerce's shipment records and prose rate contracts,
producing a schema-validated `reconciliation-report.json` and a `memos/`
folder of carrier-relations write-ups for every disputed or escalated
line.

This was built for the take-home exercise in `PROBLEM.md`. See
`DESIGN.md` for the full design rationale, judgment calls, and
post-implementation notes — this README is the practical "what is this
and how do I run it" companion.

## What's here

The `orchestrator/lib` package and the `graph-orchestrator` skill that
`PROBLEM.md` describes were missing from the checkout (see `CLAUDE.md`
§"Known gap"). Rather than reconstruct flowstate/agentctl from two
example flows, this submission keeps the same ideas — externalized
control flow, schema-gated transitions between steps, a fresh small
context per agent-judgment step, an inspectable run-state file — in a
~250-line hand-rolled graph runner (`pipeline/runner.py`) that calls
`claude -p` directly (`pipeline/claude_worker.py`) instead of DOT files,
YAML, and tmux-managed workers. Why, in more detail, is `DESIGN.md` §3.

## Architecture at a glance

Deterministic Python owns every number and every accounting relationship.
Agents are used in exactly two places, and neither can ever emit a
monetary value:

1. **Contract extraction** (Phase 2) — one agent turn per carrier,
   turning a prose rate contract into a structured rate card.
2. **Adjudication** (Phase 5) — one agent turn per finding, deciding
   `accept` / `dispute` / `escalate`, citing governing clauses, and
   drafting memo prose. The finding's own amounts are computed upstream
   by deterministic code; the agent's monetary fields are structurally
   disallowed by schema (`schemas/adjudication_output.schema.json`).

```
discover_invoices → parse_invoices → load_shipments → match_and_normalise
    → extract_ratecard_{alpine,falcon,sagar}  (agent turn, one per carrier)
    → price_lines        (deterministic pricing engine, no LLM)
    → detect_findings    (invoice-level findings + dispute-unit attribution)
    → adjudicate_findings (agent turn, one per finding)
    → write_memos        (deterministic assembly of agent prose + own amounts)
    → assemble_report → validate_report
```

Every node's output is validated against a JSON Schema
(`schemas/*.schema.json`) before the graph advances, and several nodes
carry an additional gate (a Python function that raises on structural or
invariant violations — e.g. `line_conservation_gate` ensures no invoice
line is dropped or duplicated by matching, `validate_report`
independently re-derives every report invariant rather than trusting
`assemble_report`'s own bookkeeping). Full node-by-node rationale is
`DESIGN.md` §5–§14.

## Requirements

- `python3` (3.9+; developed against 3.12) — the pipeline itself has
  **zero third-party dependencies** (see `pipeline/schema_lite.py`, a
  hand-rolled draft-07 JSON Schema subset validator, chosen specifically
  so the pipeline never needs network pip access at grading time).
- The `claude` CLI on `PATH`, authenticated — agent nodes shell out to
  `claude -p` directly.
- `orchestrator/setup.sh` (python3, git, tmux, jq) is only needed if you
  want the `flowstate`/`agentctl` CLIs for their own sake; the
  reconciliation pipeline does not use them.

## Running it

```bash
# Full pipeline: discovery → parsing → matching → rate-card extraction
# → pricing → findings → adjudication → memos → report assembly/validation
python3 -m pipeline.reconciliation_graph
```

This writes `reconciliation-report.json` at the repo root and populates
`memos/` — but only after every gate up through `validate_report` has
passed; the deliverable file is written once, in `__main__`, never inside
a node function, so a unit test can never clobber it.

Every run also leaves evidence under `runs/reconciliation/<timestamp>-<id>/`:
per-node output JSON, the run's `state.json`, and — for agent nodes — the
full `claude -p` argv, prompt, and response envelope (session id, cost,
timing). That's the reproducibility trail `DESIGN.md` §21–22 refers to.

A 2-node smoke test (script → agent) proving the runner's variable
passing and gating works end to end, independent of the real pipeline:

```bash
python3 -m pipeline.phase0_smoke_test
```

Unit tests (standard library `unittest`, no test runner dependency):

```bash
python3 -m unittest discover -s pipeline/tests -v
```

## Repo layout

```
data/                   Input: shipments.json, per-carrier contracts (data/contracts/),
                         per-carrier invoices in three formats (data/invoices/)
report.schema.json       Fixed report shape — the one non-negotiable design constraint
pipeline/
  runner.py               The graph runner (Context, NodeSpec, RunState, gating)
  claude_worker.py         One real, fresh, tool-less `claude -p` turn per agent node
  schema_lite.py            Hand-rolled JSON Schema (draft-07 subset) validator
  reconciliation_graph.py    The real graph: wires all nodes in order, runs it, writes the report
  phase0_smoke_test.py       Minimal script→agent proof of the runner mechanics
  parsers/                   Alpine (JSON), Falcon (free-text), Sagar (CSV) → canonical lines
  nodes/                      One module per pipeline phase (discover, parse, match,
                               ratecards, pricing, findings, adjudication, report)
  tests/                      unittest suite per phase
schemas/                 Output schema for every node (input/output contract, not just the report)
ratecards/               Structured rate cards extracted from data/contracts/ (Phase 2 output)
memos/                   Carrier-relations memos, one per disputed/escalated finding
runs/                    Run evidence: state.json + per-node output + agent transcripts, per run
.claude/skills/adjudication/  The disposition policy an adjudication agent turn is briefed with
orchestrator/, factory/  The provided (partially present) flowstate/agentctl kit — not used
                         by the pipeline; see "What's here" above
DESIGN.md                Full design rationale, judgment calls, and post-implementation notes
CLAUDE.md                Repo orientation for Claude Code sessions working in this repo
PROBLEM.md, brief.md     The original task statement and background reading
```

## Current output

The report in this repo (`reconciliation-report.json`) is from the most
recent real run: 126 invoice lines across all three carriers, 120
accepted, 5 disputed, 1 escalated, ₹9,328.80 in dispute
(`summary.total_in_dispute`, counted exactly once per rupee — see
`DESIGN.md` §13 for how line-level deltas and invoice-level findings are
kept disjoint). Corresponding memos are in `memos/`.

Re-running the pipeline will hit the two agent stages again — Phase 2
extraction is content-hash cached (re-extracting only if a contract
file's content changes), Phase 5 adjudication is not, so expect fresh
memo prose (and possibly, rarely, a different disposition — see
`DESIGN.md` §22's repeatability experiment) but the same underlying
dollar amounts on every run, since those come only from deterministic
code.

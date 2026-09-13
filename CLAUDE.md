# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is the take-home exercise described in `PROBLEM.md`: build an AI agent
system that reconciles carrier freight invoices against BlueFin Commerce's
shipment records and rate contracts, producing `reconciliation-report.json`
(schema-validated against `report.schema.json`) and a `memos/` folder. No
solution has been implemented yet — the repo currently contains only the
problem statement, background reading, sample data, and a partially-present
orchestrator kit.

Read `PROBLEM.md` (task and ground rules) and `brief.md` (§4–6 especially —
the ReAct/skills/graph/flowstate tradeoff space) before proposing an
architecture. The report schema (`report.schema.json`) is the **only fixed
design decision**; everything upstream is open.

## Known gap in the provided kit

`PROBLEM.md` references two things that are **not present** in this
checkout:

- `orchestrator/lib` — the Python package `bin/agentctl` and `bin/flowstate`
  both `PYTHONPATH`-import (`orchestrator/bin/agentctl` and
  `orchestrator/bin/flowstate` are thin wrappers that `exec python -m
  agentctl` / `-m flowstate` with `PYTHONPATH="$ORCH_ROOT/lib"`). Without
  this directory, `orchestrator/setup.sh` will install dependencies fine but
  the CLIs themselves will fail to import.
- `.claude/skills/graph-orchestrator/SKILL.md` — the orchestrator-agent
  skill that's supposed to drive a flowstate graph.

If you intend to use the flowstate/graph path, you (or a prior session)
need to either locate/restore these from the original kit distribution or
build equivalent machinery from scratch, per PROBLEM.md's "all of the
provided machinery is yours to modify ... if your design calls for it."
Whatever you do here, note it in `DESIGN.md` as instructed. Don't assume
these files exist — check before relying on `orchestrator/setup.sh` or the
graph-orchestrator skill.

## Commands

```bash
orchestrator/setup.sh          # one-time: creates orchestrator/.venv, installs
                                # pyyaml/jsonschema/pydot (requires python3, git, tmux, jq)
orchestrator/bin/flowstate ...  # flow CLI (state, rendering, schema validation)
orchestrator/bin/agentctl ...   # worker lifecycle: spawn/wait/send/kill
```

Both CLIs are argparse-style Python entry points invoked via wrapper
scripts that pick `orchestrator/.venv/bin/python` if present, else
`python3` on `PATH`. There is no separate lint/test/build tooling in this
repo — whatever test/validation story exists is whatever the submitted
system defines for itself (schema validation, gate scripts, etc.).

## Architecture of the provided kit

### Data (`data/`) — the reconciliation inputs

- `data/shipments.json` — BlueFin's ground-truth shipment records. Each
  record has `shipment_id`, `carrier_consignment_ref`, `carrier`,
  `ship_date`, origin/destination (city + pincode), `distance_km`,
  `billed_weight_kg`, `declared_value_inr`, `service_level`,
  `special_handling` (array), `delivery_status`, `delivered_at`. This is
  the join target for every invoice line, via `carrier_consignment_ref`.
- `data/contracts/*.md` — one prose rate contract per carrier
  (alpine-express, falcon-freight, sagar-roadlines). **These are the sole
  authority on correct pricing** — numbered clauses covering chargeable
  weight rules, per-kg rates by weight band, minimums, surcharges,
  service-level and special-handling premiums, etc. Any `expected_amount`
  computed in the report must trace back to a clause here
  (`contract_clause` field in the schema).
- `data/invoices/` — three carrier billing systems, three different
  formats, by design (the task explicitly wants format heterogeneity
  handled):
  - Alpine: JSON (`ALPINE-*.json`) — `lines[]` with
    `consignment_no`, `actual_weight_kg`, `chargeable_weight_kg`,
    `rate_per_kg`, `handling_fee`, `line_amount`.
  - Falcon: free-text invoice (`FALCON-*.txt`) — numbered consignment
    blocks with origin/destination, distance, weight, service level, and a
    `LINE TOTAL: Rs ...` per consignment. Needs parsing, not just loading.
  - Sagar: CSV (`SAGAR-*.csv`) — `cnote_no,booking_dt,wt_kg,dist_km,
    freight_rs,chill_prem_rs,total_rs`.
  - Only the `*-07*`/`JUL` files are July invoices (the ones the task asks
    you to reconcile); Aug/Sep files and `*-CN-*` (credit-note-shaped)
    files exist as noise/edge cases for a system meant to scale to "many
    times" the sample — don't assume the invoices dir is July-only.

### `report.schema.json`

Defines `reconciliation-report.json`'s shape: every invoice line must
appear exactly once in `lines[]` (unmatched lines keep `shipment_id: null`
and can leave `expected_amount`/`delta` null), each line gets a
`disposition` of `accept`/`dispute`/`escalate` plus a `justification` and
optional `contract_clause`; `invoice_findings[]` holds invoice-level (not
per-line) issues; `invoice_totals[]` and `summary` roll everything up.
Critically, `summary.total_in_dispute` must count each disputed rupee
**exactly once** — a line-level delta and an invoice-level finding must
never double-count the same amount. Validate output against this schema
mechanically (`jsonschema` is already in `orchestrator/requirements.txt`),
don't rely on a model to self-check it.

### Orchestrator kit (`orchestrator/`, `factory/`)

Two pieces work together:

1. **flowstate** — flows are a Graphviz DOT file (nodes = units of work,
   edges = transitions) plus a companion `<flow>.flow.yml` declaring
   variables and per-node `output_schemas` (JSON Schema files that node
   outputs are validated against before the flow advances). Node
   attributes in the DOT: `prompt_template` (path to a markdown prompt,
   variable-substituted), `working_dir`, `output_schema`, `model`,
   `permission_mode`, `pause_at`, `description`. Edges can carry `gates`
   (a shell script that must exit 0 to proceed) and `condition`
   expressions; `fork`/`dynamic_fanout`/`join` node shapes run branches in
   parallel with a reducer. All run state lives in one YAML file the CLI
   owns — see `factory/flows/smoke-test/` (linear: research → summarise,
   with a gate) and `factory/flows/smoke-branch/` (fork/join with a merge
   script) as the reference examples; run these first if you go this
   route, per PROBLEM.md.
2. **agentctl** — spawns/waits/messages/kills the actual worker sessions
   (each flow node gets a fresh, small-context worker) that flowstate's
   graph steps invoke.
3. **The graph-orchestrator skill** (referenced but currently absent —
   see "Known gap" above) is meant to be the agent that *drives* a flow:
   the CLIs handle mechanics, but a human-or-agent orchestrator interprets
   ambiguous situations (a condition that doesn't resolve, a validation
   failure, a stalled worker) rather than the engine hard-failing.

`factory/factory-prefs-example.yml` is a template for
`factory/factory-prefs.yml` (gitignored) — local runtime preferences for
the CLIs.

## Design constraints to keep in mind

- **The reconciliation must come from a real run of the agent system you
  build** — not you computing the answers by hand and writing the JSON
  yourself. Whatever you submit should be reproducible by re-running the
  system, and you should keep run evidence (state files, transcripts,
  intermediate artifacts) proving that.
- **Design for run-to-run variance and much larger volume than 5
  invoices.** Validation between steps should be mechanical (schemas, gate
  scripts, invariant checks), not "ask the model to double check." See
  `brief.md` §3–4 for why.
- `DESIGN.md` (to be written) should cover: architecture chosen and why,
  what's validated where and why there, judgement calls made in the
  reconciliation logic itself, and post-implementation notes — this is a
  deliverable, not optional documentation.

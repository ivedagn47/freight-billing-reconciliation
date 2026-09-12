# Take-home: freight billing reconciliation

## Background

BlueFin Commerce ships with three contracted road carriers. Each month the
carriers send invoices; finance reconciles them against BlueFin's own
shipment records and the rate contracts before paying. It is slow, fiddly
work: the contracts are prose, the invoices arrive in whatever format each
carrier's billing system emits, and the mistakes hide in the details.

BlueFin wants this pipeline run by an AI agent system — and because money
moves on the output, it has to hold up on *every* run, not just a good one.

## Your task

Build an agent system that reconciles the July invoices end to end and
produces:

1. `reconciliation-report.json` — conforming to [report.schema.json](report.schema.json).
   This schema is the only fixed design decision; everything upstream of it
   is yours.
2. `memos/` — one short, human-readable memo for every line or finding your
   system judges as anything other than `accept`, written for the
   carrier-relations colleague who has to act on it.

The data:

- [data/shipments.json](data/shipments.json) — BlueFin's shipment records (ground truth)
- [data/invoices/](data/invoices/) — five carrier invoices, July 2026
- [data/contracts/](data/contracts/) — the three rate contracts. **The contracts
  are the authority on what anything should cost.**

How you architect the system is entirely your call: skills, a flowstate
graph, a hybrid of the two — and within that, how you decompose the work,
what runs as an agent vs. a script, what gets validated and how. These
choices, and your reasons for them, are a large part of what we read.

## What's in the kit

- `orchestrator/` — the flowstate + agentctl CLIs (run `orchestrator/setup.sh`
  once; needs python3, git, tmux, jq). Flows live under `factory/flows/`.
- [.claude/skills/graph-orchestrator/SKILL.md](.claude/skills/graph-orchestrator/SKILL.md)
  — a minimal orchestrator skill with just enough machinery to progress a
  flowstate graph; it likely needs additional improvements.
- All of the provided machinery is yours to modify — the orchestrator
  skill, and the flowstate/agentctl code itself, if your design calls for
  it. Note in DESIGN.md what you changed and why.
- Two runnable demo flows: `factory/flows/smoke-test/` and
  `factory/flows/smoke-branch/`. If you use the graph machinery, run these
  first — they are the fastest way to understand it.
- [brief.md](brief.md) — background reading on agents, skills, graphs, and
  why any of this is hard.

## Ground rules

- The reconciliation must be performed by the agent system you build — not
  by you working the data in your own session and pasting answers into
  files.
- Your submitted report and memos must come from a real, complete run of
  that system.
- LLMs are nondeterministic and this pipeline touches money. We will run
  your system ourselves. Design accordingly.
- **Design for much larger volumes.** This month's sample is five invoices;
  the system you build should scale to many times that — more carriers,
  more invoices, more lines — while retaining accuracy. Your design will be
  read at that scale, not at the sample's.

## Deliverables

1. The system: your skills and/or flow definitions, with whatever prompts,
   schemas, scripts, and checks they involve.
2. `reconciliation-report.json` + `memos/` from a real run.
3. Run evidence appropriate to your architecture — whatever lets us verify
   the submission came from a real run of your system (state files, working
   artifacts, logs).
4. `DESIGN.md` — brief notes on: the architecture you chose and why;
   what you validated, where, and why there; any judgement calls in the
   reconciliation itself; and a **post-implementation notes** section with
   any observations from building and running it that you want to share.

## Time expectation

Around eight hours, and it's the harder of our two exercises. A thin
pipeline that runs beats an elaborate one that doesn't.

# Brief: coding agents, skills, graphs, and why none of this is deterministic

Background reading for the take-home. Conceptual only — nothing here is a
template for the solution.

## 1. Chat interfaces vs ReAct coding agents

A chat LLM produces one thing: text. You ask, it answers, and acting on the
answer is your job. A **coding agent** is the same model placed in a loop
with tools: it reasons about what to do next, takes an action (run a
command, read a file, edit code), observes the result, and feeds that
observation back into its next step of reasoning. This
reason → act → observe cycle — named **ReAct** by the paper that
popularised it — is what lets an agent pursue a goal across many steps
rather than emit a single reply.

Two consequences matter for this exercise:

- **The context window is the agent's working memory.** Everything the
  agent "knows" mid-task — your instructions, files it has read, results of
  commands it ran — lives in one finite context. Nothing else persists
  unless it is written to disk.
- **The agent decides its own next step.** Between your instruction and the
  final result sit dozens of small decisions you never see. The quality of
  the outcome depends on how well those decisions are steered.

Claude Code, which you'll use here, is a ReAct-style coding agent: it works
in your terminal, reads and writes real files, and runs real commands.

Reading:
- ReAct: Synergizing Reasoning and Acting in Language Models (Yao et al., 2022) — https://arxiv.org/abs/2210.03629
- Building Effective Agents (Anthropic) — https://www.anthropic.com/engineering/building-effective-agents
- Claude Code documentation — https://code.claude.com/docs

## 2. Skills

A **skill** is a procedure written in markdown that an agent loads into
context and follows — a recipe for a kind of task, versioned in the repo
next to the code it operates on. Instead of re-explaining a workflow in
every session, you write it down once; the agent reads the file and executes
the procedure against the current situation. Skills can invoke other
skills, chaining focused procedures into multi-step workflows. In this kit,
the orchestrator protocol itself is a skill:
[.claude/skills/graph-orchestrator/SKILL.md](.claude/skills/graph-orchestrator/SKILL.md).

Reading:
- Agent Skills (Anthropic engineering) — https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Skills in Claude Code — https://code.claude.com/docs/en/skills

## 3. Probabilistic behaviour — the part that makes this hard

An LLM is a probability distribution over next tokens. Run the same agent
on the same input twice and you can get different actions, different
orderings, different results. This is not a bug to be configured away; it
is the substrate you are designing on.

Three phenomena to internalise:

- **Run-to-run variance.** Identical inputs do not produce identical
  behaviour. Anything you need to be true of *every* run has to be made
  true by design, not observed once and assumed.
- **Instruction decay.** As the context fills with files, outputs, and
  intermediate chatter, early instructions lose influence. An agent that
  followed rule 3 perfectly on step 2 may quietly ignore it on step 14 —
  not because the rule was unclear, but because it is now thousands of
  tokens behind the agent's attention.
- **Degradation with context length.** This is measured, not folklore.
  Models retrieve and use information less reliably as relevant material
  sits deeper in a long context ("lost in the middle"), and task
  performance degrades as input length grows even when the task itself is
  unchanged.

Reading:
- Lost in the Middle: How Language Models Use Long Contexts (Liu et al., 2023) — https://arxiv.org/abs/2307.03172
- Context Rot: How Increasing Input Tokens Impacts LLM Performance (Chroma, 2025) — https://www.trychroma.com/research/context-rot

## 4. Graphs for LLM process flows

If section 3 is the disease, externalised control flow is the strongest
medicine. Instead of asking one agent to hold a 15-step procedure in its
head — where step 14 is at the mercy of attention decay — you write the
procedure down as a **graph**: nodes are units of work, edges are
transitions, and a piece of ordinary software (not the model) owns the
state: what has run, what's next, what was produced.

The division of labour is the whole idea:

- **The graph** guarantees the things that must be true every run: order,
  gating, "this output exists and parses before we continue."
- **The model** is invoked *inside* nodes, where its judgement is the
  value — and each worker starts with a fresh, small context containing
  exactly what its node needs, sidestepping decay entirely.
- **Validation between nodes** is code, not vibes: schemas, scripts,
  invariant checks. A model can't skip a checkpoint that isn't an
  instruction but a state machine.

The public reference point for this pattern is LangGraph; the trade-off
space (when a freely-acting agent beats a structured workflow and vice
versa) is laid out well in Anthropic's *Building Effective Agents*.

Reading:
- LangGraph documentation — https://langchain-ai.github.io/langgraph/
- Workflows vs agents, in Building Effective Agents (link in §1)

## 5. Flowstate, briefly

The classic weakness of workflow engines is rigidity: encode a process as
code and it executes beautifully right up until reality deviates from what
the author anticipated — then it errors out, because a state machine has no
judgement. Flowstate's design premise is that you can keep the graph's
guarantees without that brittleness by **putting an agent in charge of
driving the flow**.

A flow is a Graphviz **DOT file** (nodes and edges, readable by humans and
renderable as a picture) plus a companion **flow.yml** declaring output
schemas and typed variables. Agent nodes name a prompt template; flowstate
renders it by substituting variables, a fresh worker session executes it,
and the worker's declared output files are validated against JSON Schemas.
Edges can carry **gates** (shell scripts that must exit 0) and
**conditions** (expressions over variables); `fork`/`dynamic_fanout`/`join`
nodes run branches in parallel. All run state lives in one YAML file owned
by the CLI.

But the engine doesn't *run* the flow — it scaffolds an **orchestrator
agent** who does. The CLIs handle the mechanics (state, rendering,
validation, worker lifecycle via `agentctl`: spawn/wait/send/kill) and hand
each situation back to the orchestrator, who decides what it means and what
to do: which branch to take when a condition doesn't resolve cleanly, what
a validation failure or a stalled worker calls for, when to pause for a
human. The graph guarantees what must be true of every run; the agent
absorbs what the graph's author didn't anticipate.

The fastest way to understand it: run the two demo flows under
`factory/flows/` and read their DOT files. `smoke-test` shows agent nodes,
schema validation, and a gate; `smoke-branch` shows fork/join and a
reducer.

## 6. Choosing a harness: steered vs skills vs graphs vs flowstate

| | Fully-steered | Skill-chain | Graph framework (e.g. LangGraph) | Flowstate |
|---|---|---|---|---|
| Control flow lives in | The human, live | Written procedures the model follows | Application code | A declarative DOT file, driven by an orchestrator agent on engine scaffolding |
| Consistency across runs | Low | Higher, still model-enforced | High — code owns the sequence | High — the graph can't advance past failed validation |
| Step-skipping possible? | Yes | Yes — instructions decay | No | No — but how to *respond* to a failed step is the orchestrator's judgement, not a hardcoded path |
| Handles the unanticipated | Fully — that's the human | The model improvises within its instructions | Only what the author coded for | The orchestrator agent absorbs it within the graph's rails |
| Worker context | One long, growing context | One long, growing context | Per-node, if you design it so | Per-node by construction (fresh worker per node) |
| Authoring cost | None | Low — markdown | High — it's software | Medium — DOT + YAML + prompts |
| Debuggability | Ask the human | Read the skill + transcript | Logs/debugger | Run-state file, event log, per-worker transcripts |
| Best for | One-off, exploratory work | Recurring workflows where a wrong step is cheap | Product features with LLM steps | Recurring multi-step work where a skipped or unvalidated step is expensive |

The question that picks the column: **what does a bad run cost?** When the
answer is "annoyance", steering or a skill-chain is cheaper and more
flexible. When the answer is "money, data, or trust", you want the
sequence, the validation, and the audit trail owned by something that
cannot get distracted.

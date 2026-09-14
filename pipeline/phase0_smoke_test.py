"""Phase 0 proof: a 2-node graph (script -> agent) run through the runner.

    prepare (script, deterministic)
        -> writes {"topic": ...} into the run's variable scope
    hello_agent (agent, one real `claude -p` turn)
        -> reads {topic} from the script node's output (proving variable
           passing between nodes works, the same job flowstate's
           {var} substitution does)
        -> answers with JSON validated against
           schemas/phase0_greeting.schema.json
        -> a gate then re-checks the node identifies itself correctly,
           proving gates run as a distinct step after schema validation

Run with: python3 -m pipeline.phase0_smoke_test
"""
import json
from pathlib import Path

from pipeline.runner import Context, GateFailure, NodeSpec, run_graph

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
GREETING_SCHEMA = json.loads((SCHEMA_DIR / "phase0_greeting.schema.json").read_text())


def prepare(ctx: Context) -> dict:
    return {"topic": "the flowstate/agentctl gap in this repository"}


def hello_agent_prompt(ctx: Context) -> str:
    topic = ctx.vars["prepare"]["topic"]
    return (
        "You are a single, isolated worker node in a small pipeline runner. "
        "You have no tools and no memory of any other node.\n\n"
        f"The topic passed to you by the previous node is: \"{topic}\"\n\n"
        "Reply with ONLY a raw JSON object (no markdown, no code fences, no "
        "commentary) with exactly these fields:\n"
        '  "node": the literal string "hello_agent"\n'
        '  "greeting": a short one-sentence greeting of your choosing\n'
        '  "topic_echo": the exact topic string given to you above, verbatim\n'
    )


def hello_agent_gate(ctx: Context, output: dict) -> None:
    if output["node"] != "hello_agent":
        raise GateFailure(f"expected node='hello_agent', got {output['node']!r}")
    expected_topic = ctx.vars["prepare"]["topic"]
    if output["topic_echo"] != expected_topic:
        raise GateFailure(
            f"agent did not echo the topic it was given: "
            f"expected {expected_topic!r}, got {output['topic_echo']!r}"
        )


NODES = [
    NodeSpec(
        name="prepare",
        kind="script",
        description="Deterministically produce a topic variable for the agent node.",
        fn=prepare,
    ),
    NodeSpec(
        name="hello_agent",
        kind="agent",
        description="One real claude -p turn; proves agent invocation, schema validation, and gating.",
        prompt=hello_agent_prompt,
        model="sonnet",
        output_schema=GREETING_SCHEMA,
        gate=hello_agent_gate,
    ),
]


if __name__ == "__main__":
    ctx = run_graph(NODES, graph_name="phase0-smoke-test")
    print(json.dumps(ctx.vars, indent=2))

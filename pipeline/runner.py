"""Minimal node-graph runner.

This is the whole orchestration layer: no DOT files, no YAML, no tmux, no
reimplementation of flowstate/agentctl. A "graph" is just an ordered list of
NodeSpec objects; a node is either a deterministic Python function ("script")
or one real Claude Code agent turn ("agent"). The runner's job is exactly
what brief.md says a graph should guarantee: fixed order, "this output
exists and validates before we continue", and a run-state file a human (or a
grader) can read after the fact.

Money never flows through an agent node's output schema in this pipeline —
see the reconciliation graph — so validating a node's output here is about
shape and traceability, not about re-deriving the numbers.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pipeline import schema_lite
from pipeline.claude_worker import run_agent_turn

RUNS_ROOT = Path(__file__).resolve().parent.parent / "runs"


class GateFailure(Exception):
    pass


@dataclass
class Context:
    run_dir: Path
    vars: dict = field(default_factory=dict)  # node name -> that node's output dict
    node_dir: Optional[Path] = None  # set by run_graph before each node runs; lets a
                                      # node write extra evidence files (e.g. residue
                                      # dumps) without hardcoding its own name


@dataclass
class NodeSpec:
    name: str
    kind: str  # "script" | "agent"
    description: str = ""

    # script nodes
    fn: Optional[Callable[[Context], dict]] = None

    # agent nodes
    prompt: Optional[Callable[[Context], str]] = None
    model: str = "sonnet"
    append_system_prompt: Optional[str] = None

    # both
    output_schema: Optional[dict] = None
    gate: Optional[Callable[[Context, dict], None]] = None  # raise GateFailure to halt


class RunState:
    """Owns runs/<run_id>/state.json — the single source of truth for what
    has run, what its status was, and where its evidence lives."""

    def __init__(self, run_dir: Path, graph_name: str):
        self.run_dir = run_dir
        self.path = run_dir / "state.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {
                "graph": graph_name,
                "started_at": _now(),
                "nodes": {},
            }
            self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))

    def node_status(self, name: str) -> Optional[str]:
        entry = self.data["nodes"].get(name)
        return entry["status"] if entry else None

    def start_node(self, name: str) -> None:
        self.data["nodes"][name] = {"status": "running", "started_at": _now()}
        self._save()

    def finish_node(self, name: str, *, output_path: str, session_id: Optional[str]) -> None:
        self.data["nodes"][name].update({
            "status": "done",
            "finished_at": _now(),
            "output_path": output_path,
            "session_id": session_id,
        })
        self._save()

    def fail_node(self, name: str, error: str) -> None:
        self.data["nodes"][name].update({
            "status": "failed",
            "finished_at": _now(),
            "error": error,
        })
        self._save()

    def finish_run(self) -> None:
        self.data["finished_at"] = _now()
        self._save()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_graph(nodes: list[NodeSpec], *, graph_name: str, run_id: Optional[str] = None,
              resume: bool = False) -> Context:
    """Runs every node in order. Halts immediately (raises) on the first
    node whose output fails schema validation or whose gate fails — a
    failed gate means no downstream node runs, and in particular
    assemble_report never runs, so a bad run never produces a report."""
    if run_id is None:
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    run_dir = RUNS_ROOT / graph_name / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_dir, graph_name)
    ctx = Context(run_dir=run_dir)

    print(f"[runner] run_id={run_id} run_dir={run_dir}")

    for node in nodes:
        node_dir = run_dir / node.name
        status = state.node_status(node.name)

        if resume and status == "done":
            print(f"[runner] {node.name}: SKIP (already done)")
            ctx.vars[node.name] = json.loads((node_dir / "output.json").read_text())
            continue

        print(f"[runner] {node.name}: START ({node.kind}) — {node.description}")
        state.start_node(node.name)
        node_dir.mkdir(parents=True, exist_ok=True)
        ctx.node_dir = node_dir

        try:
            session_id = None
            if node.kind == "script":
                output = node.fn(ctx)
            elif node.kind == "agent":
                prompt_text = node.prompt(ctx)
                output = run_agent_turn(
                    prompt=prompt_text,
                    output_schema=node.output_schema,
                    node_dir=node_dir,
                    model=node.model,
                    append_system_prompt=node.append_system_prompt,
                )
                session_id = (node_dir / "session_id.txt").read_text().strip()
            else:
                raise ValueError(f"unknown node kind {node.kind!r}")

            if node.output_schema is not None:
                schema_lite.validate(output, node.output_schema)

            output_path = node_dir / "output.json"
            output_path.write_text(json.dumps(output, indent=2))

            if node.gate is not None:
                node.gate(ctx, output)

        except (schema_lite.SchemaError, GateFailure, Exception) as e:
            state.fail_node(node.name, f"{type(e).__name__}: {e}")
            print(f"[runner] {node.name}: FAILED — {e}")
            raise

        ctx.vars[node.name] = output
        state.finish_node(node.name, output_path=str(output_path), session_id=session_id)
        print(f"[runner] {node.name}: DONE")

    state.finish_run()
    print(f"[runner] run complete: {run_dir}")
    return ctx

"""Invokes one real, fresh Claude Code agent turn via `claude -p`.

This replaces the missing orchestrator/agentctl. Instead of spawning a
tmux-managed worker session, each agent node is a single non-interactive
`claude -p` call with:
  - a fresh session id (uuid4) so every node is independently addressable
  - --tools "" so the agent has no tool access at all: it reads only what
    is in the prompt and answers with one JSON object. This removes an
    entire class of failure (wrong file path, missed write, permission
    prompt hanging headless) and makes agent turns trivially sandboxed.
  - --output-format json, giving one JSON envelope containing session_id,
    cost, timing, and the model's raw text answer (`result`)
  - --json-schema, so the CLI itself constrains the model's structured
    output to the node's schema (validated a second time on our side,
    since a node's output is never trusted on the strength of a single
    check)

The full envelope, the exact prompt, and the exact argv are all written to
disk under the node's run directory — that is this pipeline's run evidence
for "a real agent turn happened here."
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

from pipeline import schema_lite


class AgentTurnError(Exception):
    pass


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def run_agent_turn(
    *,
    prompt: str,
    output_schema: dict,
    node_dir: Path,
    model: str = "sonnet",
    append_system_prompt: str | None = None,
    max_retries: int = 2,
    extra_validate: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run one agent turn, validate its JSON answer, and persist evidence.

    Returns the parsed, schema-valid JSON object. Raises AgentTurnError if
    the model's answer still fails validation after `max_retries` retries
    (each retry is a brand-new call with the validator's error appended to
    the prompt — the agent gets one chance to see exactly what was wrong).

    `extra_validate`, if given, runs after schema validation passes and
    must raise (any exception, message used verbatim) on a semantic
    problem the JSON Schema can't express -- e.g. a clause citation that's
    structurally a string but references a section number that doesn't
    exist in the actual contract. It shares the same retry-with-feedback
    loop as schema validation, so a semantic failure gets fed back to the
    model exactly like a structural one.
    """
    node_dir.mkdir(parents=True, exist_ok=True)
    attempt_prompt = prompt

    for attempt in range(1, max_retries + 2):  # first try + retries
        session_id = str(uuid.uuid4())
        argv = [
            "claude", "-p",
            "--output-format", "json",
            "--model", model,
            "--tools", "",
            "--session-id", session_id,
            "--no-session-persistence",
            "--json-schema", json.dumps(output_schema),
        ]
        if append_system_prompt:
            argv += ["--append-system-prompt", append_system_prompt]
        # The prompt goes last, after a bare "--": some prompts legitimately
        # start with "-" (e.g. one embedding a markdown file with YAML
        # frontmatter, which opens with "---"), and without this separator
        # the CLI's option parser mistakes such a prompt for an unknown flag
        # instead of the positional [prompt] argument.
        argv += ["--", attempt_prompt]

        attempt_dir = node_dir / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "prompt.txt").write_text(attempt_prompt)
        (attempt_dir / "command.json").write_text(json.dumps(argv, indent=2))

        proc = subprocess.run(argv, capture_output=True, text=True)
        (attempt_dir / "stderr.txt").write_text(proc.stderr)

        if proc.returncode != 0:
            (attempt_dir / "stdout.txt").write_text(proc.stdout)
            if attempt <= max_retries:
                attempt_prompt = (
                    f"{prompt}\n\n---\nYour previous attempt failed to run "
                    f"(exit code {proc.returncode}): {proc.stderr.strip()[:500]}\n"
                    f"Try again."
                )
                continue
            raise AgentTurnError(f"claude -p exited {proc.returncode}: {proc.stderr}")

        envelope = json.loads(proc.stdout)
        (attempt_dir / "agent_result.json").write_text(json.dumps(envelope, indent=2))

        if envelope.get("is_error"):
            error_detail = envelope.get("result", "unknown error")
            if attempt <= max_retries:
                attempt_prompt = (
                    f"{prompt}\n\n---\nYour previous attempt errored: "
                    f"{error_detail}\nTry again."
                )
                continue
            raise AgentTurnError(f"agent turn reported is_error: {error_detail}")

        # --json-schema makes the CLI itself parse+validate the model's
        # structured answer into `structured_output`; prefer that over
        # hand-parsing `result` text, and only fall back if it's absent
        # (older CLI, or the model's answer didn't shape into the schema).
        if isinstance(envelope.get("structured_output"), dict):
            parsed = envelope["structured_output"]
        else:
            raw_answer = _strip_code_fence(envelope.get("result", ""))
            try:
                parsed = json.loads(raw_answer)
            except json.JSONDecodeError as e:
                if attempt <= max_retries:
                    attempt_prompt = (
                        f"{prompt}\n\n---\nYour previous answer was not valid JSON "
                        f"({e}). Reply with ONLY the raw JSON object, no markdown, "
                        f"no commentary. Previous answer was:\n{raw_answer[:1000]}"
                    )
                    continue
                raise AgentTurnError(f"answer is not valid JSON after retries: {e}\nraw: {raw_answer}")

        try:
            schema_lite.validate(parsed, output_schema)
        except schema_lite.SchemaError as e:
            if attempt <= max_retries:
                attempt_prompt = (
                    f"{prompt}\n\n---\nYour previous answer failed schema "
                    f"validation: {e}\nPrevious answer was:\n{json.dumps(parsed)}\n"
                    f"Fix it and reply with ONLY the corrected raw JSON object."
                )
                continue
            raise AgentTurnError(f"answer failed schema validation after retries: {e}")

        if extra_validate is not None:
            try:
                extra_validate(parsed)
            except Exception as e:
                if attempt <= max_retries:
                    attempt_prompt = (
                        f"{prompt}\n\n---\nYour previous answer passed schema validation but failed "
                        f"a semantic check: {e}\nPrevious answer was:\n{json.dumps(parsed)}\n"
                        f"Fix it and reply with ONLY the corrected raw JSON object."
                    )
                    continue
                raise AgentTurnError(f"answer failed semantic validation after retries: {e}")

        (attempt_dir / "output.json").write_text(json.dumps(parsed, indent=2))
        (node_dir / "session_id.txt").write_text(session_id)
        (node_dir / "final_output.json").write_text(json.dumps(parsed, indent=2))
        return parsed

    raise AgentTurnError("unreachable")

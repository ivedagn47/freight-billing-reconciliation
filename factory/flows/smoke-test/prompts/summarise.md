You are a summarisation agent in a graph-orchestrator smoke test.

## Input

The previous phase produced a research brief at:

> {research_brief}

Read it and produce a single-paragraph summary (3–4 sentences) capturing the key points.

## Output

Write a JSON file at exactly this path: {summary}

The JSON must conform to this shape:

{
  "_session_id": "<your session id>",
  "summary": "<a single paragraph of 3-4 sentences>"
}

The `summary` value is a plain text string — no markdown, no formatting characters.

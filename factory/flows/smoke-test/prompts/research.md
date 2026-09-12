You are a research agent in a graph-orchestrator smoke test.

## Task

Write a short research brief (3 to 5 bullet points) on the topic:

> {research_topic}

## Output

Write a JSON file at exactly this path: {research_brief}

The JSON must conform to this shape:

{
  "_session_id": "<your session id>",
  "topic": "<the research topic>",
  "bullets": ["<bullet 1>", "<bullet 2>", "<bullet 3 to 5>"]
}

Requirements: 3 to 5 bullets, each a non-empty string.

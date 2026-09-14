"""parse_invoices: deterministically parse every in-scope invoice file
into canonical lines, using the classification discover_invoices already
made (so the parser dispatch here mirrors it exactly rather than
re-deriving it)."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.errors import ParseResidueError
from pipeline.parsers import alpine, falcon, sagar

_PARSERS = {".json": alpine, ".csv": sagar, ".txt": falcon}


def parse_invoices(ctx) -> dict:
    manifest = ctx.vars["discover_invoices"]["files"]
    invoices = []
    lines = []
    residue = []

    for entry in manifest:
        if not entry["in_scope"]:
            continue
        path = Path(entry["file"])
        parser = _PARSERS[path.suffix]
        canonical_lines, header, file_residue = parser.parse_lines(path)
        invoices.append(header)
        lines.extend(canonical_lines)
        residue.extend(file_residue)

    if residue:
        residue_path = ctx.node_dir / "residue.json"
        residue_path.write_text(json.dumps(residue, indent=2))
        raise ParseResidueError(
            f"{len(residue)} unparseable line(s)/block(s) found across in-scope invoices; "
            f"see {residue_path} for full detail. First: {residue[0]}"
        )

    return {"invoices": invoices, "lines": lines}

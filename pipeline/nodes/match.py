"""load_shipments, match_and_normalise, and the line-conservation gate.

Matching is purely by carrier_consignment_ref — a canonical line's own
"carrier" label (however each parser derived it) is never part of the join
key. This means the match still works correctly even if a parser's carrier
label were ever wrong or missing; carrier is cross-checked against the
matched shipment afterwards as a consistency signal, not relied upon
beforehand.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.runner import GateFailure

SHIPMENTS_PATH = Path("data/shipments.json")


def load_shipments(ctx, path: Path = SHIPMENTS_PATH) -> dict:
    shipments = json.loads(path.read_text())
    by_ref = {}
    duplicate_refs = []
    for s in shipments:
        ref = s["carrier_consignment_ref"]
        if ref in by_ref:
            duplicate_refs.append(ref)
        by_ref[ref] = s
    if duplicate_refs:
        # A collision here means BlueFin's own ground truth is ambiguous for
        # this ref — silently keeping "the last one seen" would make a
        # matching decision no one actually made. Fail loudly instead.
        raise RuntimeError(
            f"{path} has {len(duplicate_refs)} duplicate "
            f"carrier_consignment_ref value(s): {duplicate_refs}"
        )
    return {"count": len(shipments), "by_ref": by_ref}


def match_and_normalise(ctx) -> dict:
    parsed = ctx.vars["parse_invoices"]
    shipments_by_ref = ctx.vars["load_shipments"]["by_ref"]

    matched_lines = []
    ref_to_occurrences: dict[str, list[dict]] = {}

    for line in parsed["lines"]:
        ref = line["consignment_ref"]
        shipment = shipments_by_ref.get(ref)
        matched_line = dict(line)
        matched_line["shipment_id"] = shipment["shipment_id"] if shipment else None
        matched_line["matched"] = shipment is not None
        matched_line["shipment_carrier"] = shipment["carrier"] if shipment else None
        matched_line["is_duplicate"] = False
        matched_line["duplicate_group_id"] = None
        matched_lines.append(matched_line)
        ref_to_occurrences.setdefault(ref, []).append(matched_line)

    duplicate_groups = []
    for ref, occurrences in ref_to_occurrences.items():
        if len(occurrences) <= 1:
            continue
        group_id = f"dup-{ref}"
        duplicate_groups.append({
            "group_id": group_id,
            "consignment_ref": ref,
            "occurrences": [
                {"invoice": o["invoice"], "provenance": o["provenance"]}
                for o in occurrences
            ],
        })
        for o in occurrences:
            o["is_duplicate"] = True
            o["duplicate_group_id"] = group_id

    unmatched_count = sum(1 for l in matched_lines if not l["matched"])

    return {
        "matched_lines": matched_lines,
        "duplicate_groups": duplicate_groups,
        "summary": {
            "invoice_count": len(parsed["invoices"]),
            "line_count": len(matched_lines),
            "matched_count": len(matched_lines) - unmatched_count,
            "unmatched_count": unmatched_count,
            "duplicate_group_count": len(duplicate_groups),
        },
    }


def line_conservation_gate(ctx, output: dict) -> None:
    """Fails closed on any of: a line appearing or vanishing during
    matching, an unmatched line missing its null shipment_id, a
    matched/shipment_id inconsistency, duplicate bookkeeping that doesn't
    agree with the per-line flags, or two canonical lines that are
    literally the same source record (a parser bug, not a business
    duplicate)."""
    parsed_count = len(ctx.vars["parse_invoices"]["lines"])
    matched_lines = output["matched_lines"]

    if len(matched_lines) != parsed_count:
        raise GateFailure(
            f"line count changed during matching: parsed {parsed_count}, "
            f"matched {len(matched_lines)}"
        )

    required_keys = ("invoice", "consignment_ref", "billed_amount", "shipment_id", "matched")
    seen_source_keys = set()
    for i, line in enumerate(matched_lines):
        missing = [k for k in required_keys if k not in line]
        if missing:
            raise GateFailure(f"matched_lines[{i}] missing required key(s) {missing}")

        if line["matched"] and line["shipment_id"] is None:
            raise GateFailure(f"matched_lines[{i}] ({line['consignment_ref']}): matched=True but shipment_id is null")
        if not line["matched"] and line["shipment_id"] is not None:
            raise GateFailure(f"matched_lines[{i}] ({line['consignment_ref']}): matched=False but shipment_id is set")

        source_key = (line["invoice"], line["consignment_ref"], line["provenance"]["locator"])
        if source_key in seen_source_keys:
            raise GateFailure(f"matched_lines[{i}] duplicates source record {source_key} — parser emitted the same line twice")
        seen_source_keys.add(source_key)

    flagged_dup_count = sum(1 for l in matched_lines if l["is_duplicate"])
    group_occurrence_count = sum(len(g["occurrences"]) for g in output["duplicate_groups"])
    if flagged_dup_count != group_occurrence_count:
        raise GateFailure(
            f"duplicate bookkeeping mismatch: {flagged_dup_count} line(s) flagged "
            f"is_duplicate, but duplicate_groups lists {group_occurrence_count} occurrence(s)"
        )

    unmatched_count = sum(1 for l in matched_lines if not l["matched"])
    if unmatched_count != output["summary"]["unmatched_count"]:
        raise GateFailure(
            f"summary.unmatched_count ({output['summary']['unmatched_count']}) does not "
            f"match actual unmatched line count ({unmatched_count})"
        )

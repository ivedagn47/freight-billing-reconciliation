"""Parser for Alpine Express's JSON invoice format.

Dispatched to any *.json file under data/invoices/ purely on extension —
the format itself (this exact set of top-level/line fields) is what's
actually validated; a JSON file that doesn't match it fails loudly rather
than being silently coerced.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.errors import ParseError
from pipeline.parsers.common import first_word_lower

REQUIRED_INVOICE_FIELDS = {
    "carrier", "invoice_no", "billing_period", "consignment_count",
    "lines", "discount", "invoice_total",
}
REQUIRED_LINE_FIELDS = {
    "sl", "consignment_no", "booking_date", "actual_weight_kg",
    "chargeable_weight_kg", "rate_per_kg", "handling_fee", "line_amount",
}


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ParseError(f"{path}: not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ParseError(f"{path}: expected a JSON object at the top level")
    missing = REQUIRED_INVOICE_FIELDS - data.keys()
    if missing:
        raise ParseError(f"{path}: missing top-level field(s) {sorted(missing)}")
    return data


def sniff(path: Path) -> dict:
    """Cheap classification read for the discovery node: carrier, doc
    type, invoice id, billing period. No alternate (e.g. credit-note)
    shape is defined for this format in the data, so any Alpine JSON file
    that doesn't match the standard invoice shape fails classification
    outright rather than being guessed at."""
    data = _load(path)
    return {
        "carrier": first_word_lower(str(data["carrier"])),
        "doc_type": "invoice",
        "invoice_id": data["invoice_no"],
        "period": data["billing_period"],
        "period_ambiguous": False,
    }


def parse_lines(path: Path) -> tuple[list[dict], dict, list[dict]]:
    """Returns (canonical_lines, invoice_header, residue). Every entry in
    data["lines"] produces exactly one canonical line or exactly one
    residue record — never both, never neither."""
    data = _load(path)
    invoice_id = data["invoice_no"]
    carrier = first_word_lower(str(data["carrier"]))
    file_str = str(path)

    header = {
        "invoice": invoice_id,
        "carrier": carrier,
        "file": file_str,
        "declared_line_count": data["consignment_count"],
        "declared_total": data["invoice_total"],
        "declared_discount": data["discount"],
    }

    canonical_lines = []
    residue = []
    for idx, line in enumerate(data.get("lines", [])):
        if not isinstance(line, dict):
            residue.append({"file": file_str, "locator": f"lines[{idx}]",
                             "reason": "line is not a JSON object", "raw": line})
            continue
        missing = REQUIRED_LINE_FIELDS - line.keys()
        if missing:
            residue.append({"file": file_str, "locator": f"lines[{idx}]",
                             "reason": f"missing field(s) {sorted(missing)}", "raw": line})
            continue

        components = []
        if line["handling_fee"]:
            components.append({"label": "handling_fee", "amount": float(line["handling_fee"])})

        canonical_lines.append({
            "invoice": invoice_id,
            "carrier": carrier,
            "consignment_ref": str(line["consignment_no"]),
            "billed_amount": float(line["line_amount"]),
            "billed_components": components,
            "stated_attributes": {
                "weight_kg": float(line["actual_weight_kg"]),
                "stated_chargeable_weight_kg": float(line["chargeable_weight_kg"]),
                "stated_rate_per_kg": float(line["rate_per_kg"]),
            },
            "provenance": {"file": file_str, "locator": f"lines[{idx}] sl={line['sl']}"},
            "raw": line,
        })

    return canonical_lines, header, residue

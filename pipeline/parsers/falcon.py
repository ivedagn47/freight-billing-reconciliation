"""Parser for Falcon Freight's free-text invoice format.

This is the format PROBLEM.md calls out as needing real parsing rather
than a load. The approach is a small state machine over the file's lines
rather than a single regex over the whole text, specifically so the parser
is exhaustive by construction: every line in the file is accounted for as
either header boilerplate, a separator, part of a recognized consignment
block, or footer content, and any line that fits none of those buckets
becomes an explicit residue record instead of being silently skipped.
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline.errors import ParseError
from pipeline.parsers.common import first_word_lower, parse_money

TAX_INVOICE_RE = re.compile(r"^TAX INVOICE\s+(\S+)\s+Period:\s*[\d\-]+\s+(\d{4}-\d{2})\s*$", re.MULTILINE)
CREDIT_NOTE_RE = re.compile(r"^CREDIT NOTE\s+(\S+)\s+Date:\s*(\S+)\s*$", re.MULTILINE)
AGAINST_RE = re.compile(r"^Against:\s*TAX INVOICE\s+(\S+)\s*$", re.MULTILINE)
SEPARATOR_RE = re.compile(r"^=+$")
BLOCK_START_RE = re.compile(r"^(\d+)\.\s*Consignment\s+(\S+)\s*$")
LOCATION_RE = re.compile(r"^(.+?)\s+to\s+(.+?),\s*(\d+)\s*km,\s*(\d+)\s*kg,\s*(\w+)\s*$")
CHARGE_LINE_RE = re.compile(r"^(.+?):\s*Rs\s*([\-\d,\.]+)\s*$")
TOTAL_RE = re.compile(r"^(?:INVOICE|CREDIT NOTE) TOTAL:\s*Rs\s*([\-\d,\.]+)\s*$", re.MULTILINE)


def _sections(path: Path, text: str) -> tuple[list[str], list[str], list[str]]:
    lines = [l.rstrip() for l in text.splitlines()]
    sep_idxs = [i for i, l in enumerate(lines) if SEPARATOR_RE.match(l.strip())]
    if len(sep_idxs) != 2:
        raise ParseError(f"{path}: expected exactly 2 '====' separator lines, found {len(sep_idxs)}")
    header_lines = lines[:sep_idxs[0]]
    body_lines = lines[sep_idxs[0] + 1:sep_idxs[1]]
    footer_lines = lines[sep_idxs[1] + 1:]
    return header_lines, body_lines, footer_lines


def sniff(path: Path) -> dict:
    text = path.read_text()
    header_lines, _, _ = _sections(path, text)
    header_text = "\n".join(l.strip() for l in header_lines)
    carrier = first_word_lower(header_lines[0]) if header_lines and header_lines[0].strip() else None

    m = TAX_INVOICE_RE.search(header_text)
    if m:
        return {"carrier": carrier, "doc_type": "invoice", "invoice_id": m.group(1),
                "period": m.group(2), "period_ambiguous": False}

    m = CREDIT_NOTE_RE.search(header_text)
    if m:
        return {"carrier": carrier, "doc_type": "credit_note", "invoice_id": m.group(1),
                "period": None, "period_ambiguous": False}

    raise ParseError(f"{path}: header contains neither a TAX INVOICE nor a CREDIT NOTE line")


def _parse_block(path: Path, invoice_id: str, carrier: str, block_lines: list[str]) -> tuple[dict | None, dict | None]:
    """Returns (canonical_line, residue_record) — exactly one is non-None."""
    raw_lines = list(block_lines)
    start_m = BLOCK_START_RE.match(block_lines[0])
    if not start_m:
        return None, {"file": str(path), "locator": "unrecognized block",
                       "reason": "block does not start with 'N. Consignment REF'", "raw": {"lines": raw_lines}}
    block_no, ref = start_m.group(1), start_m.group(2)
    locator = f"consignment block {block_no} (ref={ref})"

    if len(block_lines) < 2:
        return None, {"file": str(path), "locator": locator,
                       "reason": "block has no location/weight/service line", "raw": {"lines": raw_lines}}
    loc_m = LOCATION_RE.match(block_lines[1])
    if not loc_m:
        return None, {"file": str(path), "locator": locator,
                       "reason": f"unrecognized location line: {block_lines[1]!r}", "raw": {"lines": raw_lines}}
    origin, destination, distance_km, weight_kg, service_level = loc_m.groups()

    components = []
    billed_amount = None
    for line in block_lines[2:]:
        m = CHARGE_LINE_RE.match(line)
        if not m:
            return None, {"file": str(path), "locator": locator,
                           "reason": f"unrecognized charge line: {line!r}", "raw": {"lines": raw_lines}}
        label = m.group(1).strip()
        try:
            amount = parse_money(m.group(2))
        except ValueError as e:
            return None, {"file": str(path), "locator": locator,
                           "reason": f"unparseable amount on {label!r} line: {e}", "raw": {"lines": raw_lines}}
        if label == "LINE TOTAL":
            billed_amount = amount
        else:
            components.append({"label": label, "amount": amount})

    if billed_amount is None:
        return None, {"file": str(path), "locator": locator,
                       "reason": "block has no LINE TOTAL line", "raw": {"lines": raw_lines}}

    canonical_line = {
        "invoice": invoice_id,
        "carrier": carrier,
        "consignment_ref": ref,
        "billed_amount": billed_amount,
        "billed_components": components,
        "stated_attributes": {
            "origin": origin, "destination": destination,
            "distance_km": float(distance_km), "weight_kg": float(weight_kg),
            "service_level": service_level,
        },
        "provenance": {"file": str(path), "locator": locator},
        "raw": {"lines": raw_lines},
    }
    return canonical_line, None


def parse_credit_note(path: Path) -> dict:
    """Parses a credit-note-shaped file (doc_type == 'credit_note' per
    sniff()) into its relational facts: which invoice it's against, and
    per consignment, the credited amount. Deliberately minimal -- the
    free-text "Correction: ..." explanation inside each block is kept
    verbatim as evidence but not further parsed, since Phase 4 only needs
    the (against_invoice, consignment_ref, amount) relationship, not a
    re-derivation of the correction's own reasoning."""
    text = path.read_text()
    header_lines, body_lines, footer_lines = _sections(path, text)
    header_text = "\n".join(l.strip() for l in header_lines)

    m = CREDIT_NOTE_RE.search(header_text)
    if not m:
        raise ParseError(f"{path}: parse_credit_note called on a non-credit-note-shaped file")
    credit_note_id = m.group(1)

    against_m = AGAINST_RE.search(header_text)
    against_invoice = against_m.group(1) if against_m else None

    block_start_idxs = [i for i, l in enumerate(body_lines) if BLOCK_START_RE.match(l.strip())]
    entries = []
    residue = []
    for k, start in enumerate(block_start_idxs):
        end = block_start_idxs[k + 1] if k + 1 < len(block_start_idxs) else len(body_lines)
        block_lines = [l.strip() for l in body_lines[start:end] if l.strip()]
        start_m = BLOCK_START_RE.match(block_lines[0])
        ref = start_m.group(2)
        total_line = next((l for l in block_lines if CHARGE_LINE_RE.match(l) and CHARGE_LINE_RE.match(l).group(1).strip() == "LINE TOTAL"), None)
        if total_line is None:
            residue.append({"file": str(path), "locator": f"credit note block (ref={ref})",
                             "reason": "block has no LINE TOTAL line", "raw": {"lines": block_lines}})
            continue
        amount = parse_money(CHARGE_LINE_RE.match(total_line).group(2))
        entries.append({
            "consignment_ref": ref, "amount": amount, "against_invoice": against_invoice,
            "explanation_lines": [l for l in block_lines if l != block_lines[0] and l != total_line],
            "provenance": {"file": str(path), "locator": f"credit note block (ref={ref})"},
        })

    footer_text = "\n".join(l.strip() for l in footer_lines)
    total_m = TOTAL_RE.search(footer_text)
    declared_total = parse_money(total_m.group(1)) if total_m else None

    return {
        "credit_note_id": credit_note_id, "carrier": first_word_lower(header_lines[0]) if header_lines else "unknown",
        "file": str(path), "against_invoice": against_invoice,
        "entries": entries, "declared_total": declared_total, "residue": residue,
    }


def parse_lines(path: Path) -> tuple[list[dict], dict, list[dict]]:
    """Only ever called for doc_type == 'invoice'; see sagar.parse_lines
    docstring for why that guard matters."""
    text = path.read_text()
    header_lines, body_lines, footer_lines = _sections(path, text)
    header_text = "\n".join(l.strip() for l in header_lines)

    m = TAX_INVOICE_RE.search(header_text)
    if not m:
        raise ParseError(f"{path}: parse_lines called on a non-invoice-shaped file")
    invoice_id, period = m.group(1), m.group(2)
    carrier = first_word_lower(header_lines[0]) if header_lines else "unknown"
    file_str = str(path)

    block_start_idxs = [i for i, l in enumerate(body_lines) if BLOCK_START_RE.match(l.strip())]
    residue = []

    leading = [l for l in body_lines[:block_start_idxs[0]]] if block_start_idxs else body_lines
    if any(l.strip() for l in leading):
        residue.append({"file": file_str, "locator": "body preamble",
                         "reason": "non-blank content before the first consignment block",
                         "raw": {"lines": [l for l in leading if l.strip()]}})

    canonical_lines = []
    for k, start in enumerate(block_start_idxs):
        end = block_start_idxs[k + 1] if k + 1 < len(block_start_idxs) else len(body_lines)
        block_lines = [l.strip() for l in body_lines[start:end] if l.strip()]
        line, res = _parse_block(path, invoice_id, carrier, block_lines)
        if line is not None:
            canonical_lines.append(line)
        else:
            residue.append(res)

    footer_text = "\n".join(l.strip() for l in footer_lines)
    total_m = TOTAL_RE.search(footer_text)
    if not total_m:
        residue.append({"file": file_str, "locator": "footer",
                         "reason": "no INVOICE TOTAL line found in footer",
                         "raw": {"lines": [l for l in footer_lines if l.strip()]}})
        declared_total = None
    else:
        declared_total = parse_money(total_m.group(1))

    header_out = {
        "invoice": invoice_id,
        "carrier": carrier,
        "file": file_str,
        "declared_line_count": None,  # Falcon invoices never state a consignment count
        "declared_total": declared_total,
        "declared_discount": None,  # Falcon's contract has no volume discount clause
    }
    return canonical_lines, header_out, residue

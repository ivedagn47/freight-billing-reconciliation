"""Parser for Sagar Roadlines' CSV format.

Sagar's CSVs carry no carrier name and no invoice-level period field
anywhere in their content — unlike Alpine (billing_period field) and
Falcon (a "Period: ..." header line), there is nothing in the bytes of the
file itself to read a carrier or period from. Two, and only two, pieces of
filename-derived information are used here, and both are structural
fallbacks for information the format genuinely does not carry in-content,
not shortcuts around parsing the data:
  - carrier: the filename's leading token (SAGAR-JUL-1.csv -> "sagar")
  - invoice id: the filename stem, used as-is (there's no invoice number
    printed anywhere in the file to use instead)
Billing period is still derived from content — the booking_dt column of
the data rows — never from the filename's JUL/AUG/SEP token.
"""
from __future__ import annotations

import csv
from pathlib import Path

from pipeline.errors import ParseError
from pipeline.parsers.common import parse_money

INVOICE_HEADER = ["cnote_no", "booking_dt", "wt_kg", "dist_km", "freight_rs", "chill_prem_rs", "total_rs"]
CREDIT_HEADER = ["credit_note", "against_invoice", "cnote_no", "credit_rs"]


def _carrier_from_filename(path: Path) -> str:
    return path.stem.split("-")[0].lower()


def _read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ParseError(f"{path}: empty CSV file")
        rows = [dict(zip(header, row)) for row in reader if any(cell.strip() for cell in row)]
    return header, rows


def sniff(path: Path) -> dict:
    header, rows = _read_rows(path)
    carrier = _carrier_from_filename(path)
    invoice_id = path.stem

    if header == INVOICE_HEADER:
        data_rows = [r for r in rows if r["cnote_no"] != "TOTAL"]
        periods = sorted({r["booking_dt"][:7] for r in data_rows if r.get("booking_dt")})
        ambiguous = len(periods) != 1
        return {
            "carrier": carrier, "doc_type": "invoice", "invoice_id": invoice_id,
            "period": periods[0] if not ambiguous else None,
            "period_ambiguous": ambiguous,
        }
    if header == CREDIT_HEADER:
        return {
            "carrier": carrier, "doc_type": "credit_note", "invoice_id": invoice_id,
            "period": None, "period_ambiguous": False,
        }
    raise ParseError(f"{path}: unrecognized CSV header {header}")


def parse_credit_note(path: Path) -> dict:
    """Parses a credit-note-shaped CSV (doc_type == 'credit_note' per
    sniff()) into its relational facts. Sagar's credit-note format is
    already almost entirely structured (unlike Falcon's free text), so
    this is mostly a direct field read plus the same TOTAL-row handling
    used for regular invoices."""
    header, rows = _read_rows(path)
    if header != CREDIT_HEADER:
        raise ParseError(f"{path}: parse_credit_note called on a non-credit-note-shaped CSV (header {header})")

    file_str = str(path)
    carrier = _carrier_from_filename(path)
    entries = []
    residue = []
    declared_total = None
    against_invoices = set()
    row_number = 1
    credit_note_id = None

    for row in rows:
        row_number += 1
        if row["credit_note"] == "TOTAL":
            try:
                declared_total = parse_money(row["credit_rs"])
            except ValueError as e:
                residue.append({"file": file_str, "locator": f"csv row {row_number}",
                                 "reason": f"unparseable TOTAL row: {e}", "raw": row})
            continue
        try:
            amount = parse_money(row["credit_rs"])
            ref = row["cnote_no"].strip()
            if not ref:
                raise ValueError("empty cnote_no")
        except (ValueError, KeyError) as e:
            residue.append({"file": file_str, "locator": f"csv row {row_number}",
                             "reason": f"unparseable row: {e}", "raw": row})
            continue
        credit_note_id = row["credit_note"]
        against_invoices.add(row["against_invoice"])
        entries.append({
            "consignment_ref": ref, "amount": amount, "explanation_lines": [],
            "against_invoice": row["against_invoice"],  # tracked per-entry: Sagar's format
                                                           # allows one credit note to reference
                                                           # different invoices per row in
                                                           # principle, though the real data never does
            "provenance": {"file": file_str, "locator": f"csv row {row_number} (cnote_no={ref})"},
        })

    return {
        "credit_note_id": credit_note_id, "carrier": carrier, "file": file_str,
        "against_invoice": next(iter(against_invoices)) if len(against_invoices) == 1 else None,
        "entries": entries, "declared_total": declared_total, "residue": residue,
    }


def parse_lines(path: Path) -> tuple[list[dict], dict, list[dict]]:
    """Only ever called for doc_type == 'invoice' (credit notes are always
    out of scope for line-level reconciliation in Phase 1 — see
    nodes/discover.py). Guards against being misapplied to a credit-note
    shaped file rather than silently producing nonsense output."""
    header, rows = _read_rows(path)
    if header != INVOICE_HEADER:
        raise ParseError(f"{path}: parse_lines called on a non-invoice-shaped CSV (header {header})")

    file_str = str(path)
    carrier = _carrier_from_filename(path)
    invoice_id = path.stem

    canonical_lines = []
    residue = []
    declared_total = None
    row_number = 1  # header consumed line 1

    for row in rows:
        row_number += 1
        if row["cnote_no"] == "TOTAL":
            try:
                declared_total = parse_money(row["total_rs"])
            except ValueError as e:
                residue.append({"file": file_str, "locator": f"csv row {row_number}",
                                 "reason": f"unparseable TOTAL row: {e}", "raw": row})
            continue

        try:
            wt_kg = float(row["wt_kg"])
            dist_km = float(row["dist_km"])
            freight_rs = parse_money(row["freight_rs"])
            chill_prem_rs = parse_money(row["chill_prem_rs"])
            total_rs = parse_money(row["total_rs"])
            cnote_no = row["cnote_no"].strip()
            if not cnote_no:
                raise ValueError("empty cnote_no")
        except (ValueError, KeyError) as e:
            residue.append({"file": file_str, "locator": f"csv row {row_number}",
                             "reason": f"unparseable row: {e}", "raw": row})
            continue

        components = [{"label": "freight_rs", "amount": freight_rs}]
        if chill_prem_rs:
            components.append({"label": "chill_prem_rs", "amount": chill_prem_rs})

        canonical_lines.append({
            "invoice": invoice_id,
            "carrier": carrier,
            "consignment_ref": cnote_no,
            "billed_amount": total_rs,
            "billed_components": components,
            "stated_attributes": {"weight_kg": wt_kg, "distance_km": dist_km},
            "provenance": {"file": file_str, "locator": f"csv row {row_number} (cnote_no={cnote_no})"},
            "raw": row,
        })

    header_out = {
        "invoice": invoice_id,
        "carrier": carrier,
        "file": file_str,
        "declared_line_count": None,  # Sagar invoices never state a line count
        "declared_total": declared_total,
        "declared_discount": None,  # Sagar's contract has no volume discount clause
    }
    return canonical_lines, header_out, residue

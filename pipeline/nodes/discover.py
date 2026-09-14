"""discover_invoices: classify every file under data/invoices/ by carrier,
document type, and billing period, and decide which are in scope for this
reconciliation run.

TARGET_PERIOD is the one and only place "July 2026" is named anywhere in
this pipeline — it is PROBLEM.md's explicit scope instruction ("reconcile
the July invoices"), not a discrepancy shortcut. Nothing about which lines
are correct or incorrect is decided here or anywhere near here.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.parsers import alpine, falcon, sagar

INVOICES_DIR = Path("data/invoices")
TARGET_PERIOD = "2026-07"

_PARSERS = {".json": alpine, ".csv": sagar, ".txt": falcon}


def discover_invoices(ctx) -> dict:
    entries = [_classify(path) for path in sorted(INVOICES_DIR.iterdir()) if path.is_file()]
    return {"target_period": TARGET_PERIOD, "files": entries}


def _classify(path: Path) -> dict:
    parser = _PARSERS.get(path.suffix)
    if parser is None:
        return _entry(path, None, None, None, None, False,
                      f"unrecognized file extension {path.suffix!r}")

    try:
        info = parser.sniff(path)
    except Exception as e:
        return _entry(path, None, None, None, None, False,
                      f"failed to classify: {type(e).__name__}: {e}")

    in_scope, reason = _scope_decision(info)
    return _entry(path, info["carrier"], info["doc_type"], info["invoice_id"],
                  info["period"], in_scope, reason, info.get("period_ambiguous", False))


def _scope_decision(info: dict) -> tuple[bool, str]:
    if info["doc_type"] == "credit_note":
        return False, "credit_note: not part of line-level reconciliation scope"
    if info.get("period_ambiguous"):
        return False, "file's own records span more than one billing period; cannot assign a single period"
    if info["period"] is None:
        return False, "no billing period could be determined from file content"
    if info["period"] != TARGET_PERIOD:
        return False, f"period {info['period']} does not match target period {TARGET_PERIOD}"
    return True, f"period {info['period']} matches target period {TARGET_PERIOD}"


def _entry(path, carrier, doc_type, invoice_id, period, in_scope, reason, period_ambiguous=False) -> dict:
    return {
        "file": str(path),
        "carrier": carrier,
        "doc_type": doc_type,
        "invoice_id": invoice_id,
        "period": period,
        "period_ambiguous": period_ambiguous,
        "in_scope": in_scope,
        "reason": reason,
    }

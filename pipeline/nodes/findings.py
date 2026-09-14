"""detect_findings: everything that cannot be decided by pricing a single
line in isolation -- invoice-total self-consistency, rate-card-driven
invoice-level rules (e.g. a volume discount), duplicate billing across
invoices, and credit-note relationships to July invoices. Deterministic,
no LLM. Disposition (accept/dispute/escalate) is not decided here -- only
whether a finding exists, what money (if any) it puts in dispute, and
whether it needs the later adjudication step's judgment.

Dispute-unit rule: every distinct piece of money that might end up
disputed gets exactly one dispute_unit_id. A finding that only explains or
corroborates money already counted elsewhere (e.g. a credit note that
matches an already-detected line-level delta) carries dispute_unit_id =
None. This is what lets a later total_in_dispute sum every dispute unit
exactly once without re-deriving which findings overlap.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.parsers import falcon, sagar
from pipeline.runner import GateFailure

_OPS = {
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
}


def _round2(x: float) -> float:
    return round(x + 0.0, 2)


def _month_of(date_str) -> str | None:
    return date_str[:7] if date_str else None


def _booking_month(line: dict) -> str | None:
    raw = line.get("raw", {})
    return _month_of(raw.get("booking_date") or raw.get("booking_dt"))


# --- 1. invoice-total self-consistency ---------------------------------------

def _invoice_total_findings(invoices: list[dict], lines_by_invoice: dict) -> list[dict]:
    findings = []
    for inv in invoices:
        declared = inv["declared_total"]
        if declared is None:
            continue  # never infer a stated total the source document doesn't provide
        lines_here = lines_by_invoice[inv["invoice"]]
        recomputed = _round2(sum(l["billed_amount"] for l in lines_here))
        diff = _round2(declared - recomputed)
        if abs(diff) < 0.005:
            continue
        findings.append({
            "finding_id": f"invoice_total_mismatch:{inv['invoice']}",
            "finding_type": "invoice_total_mismatch",
            "affected_invoices": [inv["invoice"]], "affected_consignments": [],
            "related_shipments": [], "billed_amounts": [declared],
            "expected_amount": recomputed, "delta": diff,
            "dispute_unit_id": f"du:invoice_total:{inv['invoice']}", "dispute_amount": abs(diff),
            "related_dispute_unit_id": None, "governing_clauses": [],
            "evidence": {"declared_total": declared, "recomputed_from_own_lines": recomputed,
                         "line_count": len(lines_here)},
            "requires_adjudication": True,
            "adjudication_reason": "the invoice's own declared total does not equal the sum of its own billed line amounts",
        })
    return findings


# --- 2. rate-card-driven invoice-level rules (e.g. volume discount) ----------

def _consignment_count_bases(invoice_id: str, carrier: str, lines_here: list[dict],
                              all_lines: list[dict], shipments_by_ref: dict) -> dict:
    """Every independently computable candidate for 'how many consignments
    count toward this invoice-level threshold', keyed by the date basis it
    corresponds to. None where that basis can't be computed at all. This
    exists specifically so a genuine disagreement between candidates (a
    real date-basis ambiguity) is detected mechanically, not assumed away."""
    by_document = len(lines_here)

    booking_months = {_booking_month(l) for l in lines_here}
    booking_months.discard(None)
    by_booking_date = None
    if len(booking_months) == 1:
        month = next(iter(booking_months))
        by_booking_date = sum(1 for l in all_lines if l["carrier"] == carrier and _booking_month(l) == month)

    ship_months = set()
    for l in lines_here:
        shipment = shipments_by_ref.get(l["consignment_ref"])
        if shipment:
            ship_months.add(_month_of(shipment.get("ship_date")))
    ship_months.discard(None)
    by_ship_date = None
    if len(ship_months) == 1:
        month = next(iter(ship_months))
        by_ship_date = 0
        for l in all_lines:
            if l["carrier"] != carrier:
                continue
            shipment = shipments_by_ref.get(l["consignment_ref"])
            if shipment and _month_of(shipment.get("ship_date")) == month:
                by_ship_date += 1

    return {
        "by_invoice_document": by_document,
        "by_booking_date_calendar_month": by_booking_date,
        "by_ship_date_calendar_month": by_ship_date,
    }


def _discount_findings(invoices: list[dict], lines_by_invoice: dict, priced_by_key: dict,
                        ratecards: dict, all_lines: list[dict], shipments_by_ref: dict) -> list[dict]:
    findings = []
    for inv in invoices:
        ratecard = ratecards.get(inv["carrier"])
        if not ratecard or not ratecard["discounts"]:
            continue
        lines_here = lines_by_invoice[inv["invoice"]]

        for discount in ratecard["discounts"]:
            threshold = discount["threshold"]
            if threshold is None or threshold["metric"] != "consignment_count":
                continue  # no numeric mechanism this engine can evaluate generically

            bases = _consignment_count_bases(inv["invoice"], inv["carrier"], lines_here, all_lines, shipments_by_ref)
            computed = {k: v for k, v in bases.items() if v is not None}
            distinct_values = set(computed.values())
            finding_id = f"invoice_discount:{inv['invoice']}:{discount['name']}"

            if len(distinct_values) > 1:
                findings.append({
                    "finding_id": finding_id, "finding_type": "invoice_level_discount",
                    "affected_invoices": [inv["invoice"]],
                    "affected_consignments": [l["consignment_ref"] for l in lines_here],
                    "related_shipments": [], "billed_amounts": [], "expected_amount": None, "delta": None,
                    "dispute_unit_id": None, "dispute_amount": None, "related_dispute_unit_id": None,
                    "governing_clauses": [discount["clause"]],
                    "evidence": {"candidate_consignment_count_bases": bases},
                    "requires_adjudication": True,
                    "adjudication_reason": (
                        f"candidate consignment-count bases disagree ({computed}) -- the contract does not "
                        f"specify whether 'calendar month' for {discount['name']!r} is measured by ship date, "
                        f"booking date, or invoice/document grouping, and the choice changes the outcome here, "
                        f"so this is not resolved automatically"
                    ),
                })
                continue

            if not distinct_values:
                continue  # no basis computable at all -- nothing to say
            count = next(iter(distinct_values))
            op_fn = _OPS[threshold["op"]]
            if not op_fn(count, threshold["value"]):
                continue  # threshold not met under the (unanimous) computable basis -- no finding

            priced_here = [priced_by_key[(inv["invoice"], l["consignment_ref"])] for l in lines_here]
            undetermined = [p for p in priced_here if p["pricing_outcome"] != "determined"]
            declared_discount_amount = inv["declared_discount"] or 0.0

            if undetermined:
                expected_discount_amount = None
                dispute_amount = None
                evidence_note = (
                    f"{len(undetermined)} line(s) on this invoice are not yet pricing-determined; "
                    f"the exact discount shortfall cannot be computed until they are resolved"
                )
            else:
                pre_discount_total = sum(p["expected_amount"] for p in priced_here)
                expected_discount_amount = _round2(pre_discount_total * discount["rate"])
                dispute_amount = _round2(expected_discount_amount - declared_discount_amount)
                evidence_note = None

            has_dispute = dispute_amount is not None and abs(dispute_amount) > 0.005
            findings.append({
                "finding_id": finding_id, "finding_type": "invoice_level_discount",
                "affected_invoices": [inv["invoice"]],
                "affected_consignments": [l["consignment_ref"] for l in lines_here],
                "related_shipments": [], "billed_amounts": [],
                "expected_amount": expected_discount_amount, "delta": dispute_amount,
                "dispute_unit_id": f"du:discount:{inv['invoice']}:{discount['name']}" if has_dispute else None,
                "dispute_amount": abs(dispute_amount) if has_dispute else None,
                "related_dispute_unit_id": None,
                "governing_clauses": [discount["clause"]],
                "evidence": {
                    "consignment_count": count, "candidate_consignment_count_bases": bases,
                    "threshold": threshold, "declared_discount_amount": declared_discount_amount,
                    "expected_discount_amount": expected_discount_amount, "note": evidence_note,
                },
                "requires_adjudication": True,
                "adjudication_reason": (
                    f"invoice qualifies for {discount['name']!r} under every computable date basis "
                    f"({count} consignments, threshold {threshold['op']} {threshold['value']}), but the "
                    f"carrier's declared discount is {declared_discount_amount}"
                ),
            })
    return findings


# --- 3. duplicate billing (from Phase 1's duplicate_groups) ------------------

def _duplicate_findings(duplicate_groups: list[dict], priced_by_key: dict) -> list[dict]:
    findings = []
    for group in duplicate_groups:
        occurrences = []
        for occ in group["occurrences"]:
            key = (occ["invoice"], group["consignment_ref"])
            priced = priced_by_key[key]
            occurrences.append({
                "invoice": occ["invoice"], "consignment_ref": group["consignment_ref"],
                "shipment_id": priced["shipment_id"], "billed_amount": priced["billed_amount"],
                "expected_amount": priced["expected_amount"], "delta": priced["delta"],
                "pricing_outcome": priced["pricing_outcome"], "provenance": occ["provenance"],
            })
        billed_amounts = [o["billed_amount"] for o in occurrences]
        # Only one occurrence should ever be paid; the rest is the dispute-worthy
        # excess. Using max() (not the first/last chronologically) is a deliberate,
        # documented convention -- the contracts don't say which occurrence to
        # honor, so this doesn't overstate the excess if amounts ever differ.
        amount_impact = _round2(sum(billed_amounts) - max(billed_amounts))
        shipment_ids = sorted({o["shipment_id"] for o in occurrences if o["shipment_id"]})
        has_dispute = amount_impact > 0.005

        findings.append({
            "finding_id": f"duplicate:{group['group_id']}",
            "finding_type": "duplicate_billing",
            "affected_invoices": [o["invoice"] for o in occurrences],
            "affected_consignments": [group["consignment_ref"]],
            "related_shipments": shipment_ids,
            "billed_amounts": billed_amounts, "expected_amount": None, "delta": None,
            "dispute_unit_id": f"du:duplicate:{group['group_id']}" if has_dispute else None,
            "dispute_amount": amount_impact if has_dispute else None,
            "related_dispute_unit_id": None,
            "governing_clauses": [],
            "evidence": {"occurrences": occurrences, "formula": "sum(billed_amounts) - max(billed_amounts)"},
            "requires_adjudication": True,
            "adjudication_reason": (
                f"consignment {group['consignment_ref']!r} is billed on {len(occurrences)} separate "
                f"invoices for what Phase 1 matched to a single shipment; only one occurrence should be payable"
            ),
        })
    return findings


# --- 4. credit-note relationships to July invoices/lines ---------------------

_CREDIT_NOTE_PARSERS = {".txt": falcon.parse_credit_note, ".csv": sagar.parse_credit_note}


def _credit_note_findings(manifest: dict, priced_by_key: dict, in_scope_invoice_ids: set) -> list[dict]:
    findings = []
    for entry in manifest["files"]:
        if entry["doc_type"] != "credit_note":
            continue
        path = Path(entry["file"])
        parser = _CREDIT_NOTE_PARSERS.get(path.suffix)
        if parser is None:
            continue
        cn = parser(path)

        for e in cn["entries"]:
            against_invoice = e.get("against_invoice") or cn.get("against_invoice")
            key = (against_invoice, e["consignment_ref"])
            related = priced_by_key.get(key)
            is_july_related = against_invoice in in_scope_invoice_ids and related is not None
            finding_id = f"credit_note:{cn['credit_note_id']}:{e['consignment_ref']}"

            if not is_july_related:
                findings.append({
                    "finding_id": finding_id, "finding_type": "credit_note_unrelated",
                    "affected_invoices": [against_invoice] if against_invoice else [],
                    "affected_consignments": [e["consignment_ref"]], "related_shipments": [],
                    "billed_amounts": [], "expected_amount": None, "delta": None,
                    "dispute_unit_id": None, "dispute_amount": None, "related_dispute_unit_id": None,
                    "governing_clauses": [],
                    "evidence": {"credit_note_id": cn["credit_note_id"], "credit_note_file": cn["file"],
                                 "against_invoice": against_invoice, "credit_amount": e["amount"],
                                 "explanation_lines": e["explanation_lines"]},
                    "requires_adjudication": False,
                    "adjudication_reason": (
                        f"references {against_invoice!r}/{e['consignment_ref']!r}, which is not an "
                        f"in-scope July invoice/consignment -- recorded for completeness, does not "
                        f"affect this report's July dispute accounting"
                    ),
                })
                continue

            line_delta = related["delta"]
            corroborates = line_delta is not None and abs(abs(line_delta) - abs(e["amount"])) < 0.02
            evidence = {"credit_note_id": cn["credit_note_id"], "credit_note_file": cn["file"],
                        "against_invoice": against_invoice, "credit_amount": e["amount"],
                        "explanation_lines": e["explanation_lines"],
                        "related_line_delta": line_delta, "related_line_pricing_outcome": related["pricing_outcome"]}

            if corroborates:
                findings.append({
                    "finding_id": finding_id, "finding_type": "credit_note_corroboration",
                    "affected_invoices": [against_invoice], "affected_consignments": [e["consignment_ref"]],
                    "related_shipments": [related["shipment_id"]] if related["shipment_id"] else [],
                    "billed_amounts": [], "expected_amount": None, "delta": None,
                    "dispute_unit_id": None, "dispute_amount": None,
                    "related_dispute_unit_id": f"du:line:{against_invoice}:{e['consignment_ref']}",
                    "governing_clauses": [],
                    "evidence": evidence,
                    "requires_adjudication": True,
                    "adjudication_reason": (
                        f"credit note amount ({e['amount']}) matches the already independently-detected "
                        f"line-level pricing delta ({line_delta}) -- corroborating evidence only, no new "
                        f"dispute amount (already counted at the line level)"
                    ),
                })
            else:
                findings.append({
                    "finding_id": finding_id, "finding_type": "credit_note_novel",
                    "affected_invoices": [against_invoice], "affected_consignments": [e["consignment_ref"]],
                    "related_shipments": [related["shipment_id"]] if related["shipment_id"] else [],
                    "billed_amounts": [], "expected_amount": None, "delta": None,
                    "dispute_unit_id": f"du:creditnote:{cn['credit_note_id']}:{e['consignment_ref']}",
                    "dispute_amount": abs(e["amount"]), "related_dispute_unit_id": None,
                    "governing_clauses": [],
                    "evidence": evidence,
                    "requires_adjudication": True,
                    "adjudication_reason": (
                        f"credit note identifies a correction ({e['amount']}) not matched by the "
                        f"independently-computed line-level delta ({line_delta}) -- treated as new evidence, "
                        f"not already captured elsewhere"
                    ),
                })
    return findings


# --- 5. line-level findings carried forward from Phase 3 ---------------------

def _line_level_findings(priced_lines: list[dict]) -> list[dict]:
    findings = []
    for l in priced_lines:
        if l["pricing_outcome"] == "determined":
            if l["delta"] is None or abs(l["delta"]) < 0.005:
                continue
            fid = f"line_delta:{l['invoice']}:{l['consignment_ref']}"
            findings.append({
                "finding_id": fid, "finding_type": "line_pricing_delta",
                "affected_invoices": [l["invoice"]], "affected_consignments": [l["consignment_ref"]],
                "related_shipments": [l["shipment_id"]] if l["shipment_id"] else [],
                "billed_amounts": [l["billed_amount"]], "expected_amount": l["expected_amount"], "delta": l["delta"],
                "dispute_unit_id": f"du:line:{l['invoice']}:{l['consignment_ref']}", "dispute_amount": abs(l["delta"]),
                "related_dispute_unit_id": None,
                "governing_clauses": l["clauses"],
                "evidence": {"pricing_trace": l["trace"]},
                "requires_adjudication": True,
                "adjudication_reason": f"billed amount differs from the contract-governed expected amount by {l['delta']}",
            })
        else:
            fid = f"ambiguous_line:{l['invoice']}:{l['consignment_ref']}"
            findings.append({
                "finding_id": fid, "finding_type": "ambiguous_line",
                "affected_invoices": [l["invoice"]], "affected_consignments": [l["consignment_ref"]],
                "related_shipments": [l["shipment_id"]] if l["shipment_id"] else [],
                "billed_amounts": [l["billed_amount"]], "expected_amount": None, "delta": None,
                "dispute_unit_id": None, "dispute_amount": None, "related_dispute_unit_id": None,
                "governing_clauses": l["clauses"],
                "evidence": {"pricing_trace": l["trace"]},
                "requires_adjudication": True,
                "adjudication_reason": l["reason"],
            })
    return findings


# --- node entry point ---------------------------------------------------------

def detect_findings(ctx) -> dict:
    parsed = ctx.vars["parse_invoices"]
    priced = ctx.vars["price_lines"]["priced_lines"]
    duplicate_groups = ctx.vars["match_and_normalise"]["duplicate_groups"]
    manifest = ctx.vars["discover_invoices"]
    shipments_by_ref = ctx.vars["load_shipments"]["by_ref"]
    ratecards = {c: ctx.vars[f"extract_ratecard_{c}"] for c in ("alpine", "falcon", "sagar")}

    lines_by_invoice: dict[str, list[dict]] = {}
    for l in parsed["lines"]:
        lines_by_invoice.setdefault(l["invoice"], []).append(l)
    priced_by_key = {(p["invoice"], p["consignment_ref"]): p for p in priced}
    in_scope_invoice_ids = {inv["invoice"] for inv in parsed["invoices"]}

    findings = []
    findings += _invoice_total_findings(parsed["invoices"], lines_by_invoice)
    findings += _discount_findings(parsed["invoices"], lines_by_invoice, priced_by_key,
                                    ratecards, parsed["lines"], shipments_by_ref)
    findings += _duplicate_findings(duplicate_groups, priced_by_key)
    findings += _credit_note_findings(manifest, priced_by_key, in_scope_invoice_ids)
    findings += _line_level_findings(priced)

    dispute_units = []
    for f in findings:
        if f["dispute_unit_id"] is not None:
            dispute_units.append({
                "dispute_unit_id": f["dispute_unit_id"], "amount": f["dispute_amount"],
                "finding_id": f["finding_id"], "finding_type": f["finding_type"],
            })

    return {
        "findings": findings,
        "dispute_units": dispute_units,
        "summary": {
            "finding_count": len(findings),
            "dispute_unit_count": len(dispute_units),
            "total_dispute_unit_amount_if_all_disputed": _round2(sum(u["amount"] for u in dispute_units)),
            "requires_adjudication_count": sum(1 for f in findings if f["requires_adjudication"]),
        },
    }


def findings_gate(ctx, output: dict) -> None:
    """Fails closed on: duplicate IDs, dispute units that don't sum to
    what the findings themselves claim, a corroboration finding whose
    linked unit doesn't exist, any finding referencing an invoice/line
    Phase 1 never produced, and -- the check that most directly proves no
    finding was hand-injected -- the count of line-level findings must
    exactly match Phase 3's own independently-computed pricing_outcome
    counts, recomputed here from price_lines, not from findings.py's own
    bookkeeping."""
    errors = []
    findings = output["findings"]
    dispute_units = output["dispute_units"]

    parsed = ctx.vars["parse_invoices"]
    valid_invoice_ids = {inv["invoice"] for inv in parsed["invoices"]}
    valid_line_keys = {(l["invoice"], l["consignment_ref"]) for l in parsed["lines"]}

    finding_ids = [f["finding_id"] for f in findings]
    if len(finding_ids) != len(set(finding_ids)):
        dupes = [fid for fid in set(finding_ids) if finding_ids.count(fid) > 1]
        errors.append(f"duplicate finding_id(s): {dupes}")

    du_ids_from_units = [u["dispute_unit_id"] for u in dispute_units]
    if len(du_ids_from_units) != len(set(du_ids_from_units)):
        dupes = [d for d in set(du_ids_from_units) if du_ids_from_units.count(d) > 1]
        errors.append(f"duplicate dispute_unit_id(s) in dispute_units: {dupes}")

    du_ids_from_findings = {f["dispute_unit_id"] for f in findings if f["dispute_unit_id"] is not None}
    if du_ids_from_findings != set(du_ids_from_units):
        errors.append(
            f"dispute_units list does not match findings' own dispute_unit_id fields: "
            f"only-in-findings={du_ids_from_findings - set(du_ids_from_units)}, "
            f"only-in-units={set(du_ids_from_units) - du_ids_from_findings}"
        )

    for f in findings:
        label = f["finding_id"]
        if not f["evidence"]:
            errors.append(f"{label}: no evidence recorded")
        if not isinstance(f["requires_adjudication"], bool):
            errors.append(f"{label}: requires_adjudication is not a boolean")
        if not f["adjudication_reason"]:
            errors.append(f"{label}: missing adjudication_reason")

        if (f["dispute_unit_id"] is None) != (f["dispute_amount"] is None):
            errors.append(f"{label}: dispute_unit_id and dispute_amount must both be set or both be null")
        if f["dispute_amount"] is not None and f["dispute_amount"] <= 0:
            errors.append(f"{label}: dispute_amount {f['dispute_amount']} is not positive")

        if f["finding_type"] != "credit_note_unrelated":
            bad_invoices = set(f["affected_invoices"]) - valid_invoice_ids
            if bad_invoices:
                errors.append(f"{label}: references invoice(s) not produced by Phase 1: {bad_invoices}")
            for ref in f["affected_consignments"]:
                for inv in f["affected_invoices"]:
                    if (inv, ref) not in valid_line_keys:
                        errors.append(f"{label}: references ({inv}, {ref}) which is not a real canonical line")

        if f["finding_type"] == "credit_note_corroboration":
            if f["related_dispute_unit_id"] not in du_ids_from_units:
                errors.append(f"{label}: related_dispute_unit_id {f['related_dispute_unit_id']!r} does not exist in dispute_units")
            if f["dispute_unit_id"] is not None:
                errors.append(
                    f"{label}: a corroboration finding must never carry its own dispute_unit_id "
                    f"(got {f['dispute_unit_id']!r}) -- its money is already counted via "
                    f"related_dispute_unit_id {f['related_dispute_unit_id']!r}, so this would double-count it"
                )

        if f["finding_type"] == "duplicate_billing":
            occ = f["evidence"]["occurrences"]
            billed = [o["billed_amount"] for o in occ]
            expected_impact = _round2(sum(billed) - max(billed)) if billed else 0.0
            if f["dispute_amount"] is not None and abs(f["dispute_amount"] - expected_impact) > 0.005:
                errors.append(f"{label}: dispute_amount {f['dispute_amount']} != recomputed sum(billed)-max(billed) = {expected_impact}")

    recomputed_total = _round2(sum(u["amount"] for u in dispute_units))
    if abs(recomputed_total - output["summary"]["total_dispute_unit_amount_if_all_disputed"]) > 0.005:
        errors.append(
            f"summary.total_dispute_unit_amount_if_all_disputed "
            f"({output['summary']['total_dispute_unit_amount_if_all_disputed']}) != recomputed sum of "
            f"dispute_units ({recomputed_total})"
        )

    # Cross-check against Phase 3's OWN independent counts -- this is the
    # check that most directly proves findings weren't hand-tuned: it
    # never looks at what the findings claim about themselves, only at
    # whether their aggregate count matches price_lines, computed fresh.
    priced = ctx.vars["price_lines"]["priced_lines"]
    expected_delta_findings = sum(1 for p in priced if p["pricing_outcome"] == "determined"
                                   and p["delta"] is not None and abs(p["delta"]) >= 0.005)
    expected_ambiguous_findings = sum(1 for p in priced if p["pricing_outcome"] != "determined")
    actual_delta_findings = sum(1 for f in findings if f["finding_type"] == "line_pricing_delta")
    actual_ambiguous_findings = sum(1 for f in findings if f["finding_type"] == "ambiguous_line")
    if actual_delta_findings != expected_delta_findings:
        errors.append(
            f"line_pricing_delta finding count ({actual_delta_findings}) does not match "
            f"price_lines' own nonzero-delta count ({expected_delta_findings})"
        )
    if actual_ambiguous_findings != expected_ambiguous_findings:
        errors.append(
            f"ambiguous_line finding count ({actual_ambiguous_findings}) does not match "
            f"price_lines' own non-determined count ({expected_ambiguous_findings})"
        )

    if errors:
        raise GateFailure("findings_gate failed:\n  - " + "\n  - ".join(errors))

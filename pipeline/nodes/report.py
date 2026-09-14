"""assemble_report / validate_report: the final deliverable.

Every number here comes from Phase 1 (parsed/matched lines), Phase 3
(priced lines), or Phase 4 (findings/dispute_units) -- never from Phase 5's
adjudication output, which has no field a number could occupy in the
first place (see adjudication_output.schema.json). This module only ever
reads `disposition`, `justification`, and `governing_clauses` off an
adjudication.

Line/invoice-finding construction is a small, fixed set of DETERMINISTIC
allocation rules over Phase 4's finding types -- nothing here branches on
carrier name, invoice id, or consignment ref. The only per-finding-type
logic is:
  - line_pricing_delta / ambiguous_line: the finding's own adjudication
    governs that one line directly (delta already correct from Phase 3).
  - duplicate_billing: exactly one occurrence (the largest billed amount,
    ties broken by first occurrence) is left as the shipment's one
    legitimate charge; every other occurrence has its expected_amount
    reset to billed_amount minus the finding's own dispute_amount (an
    already-validated Phase 4 number, never invented here) so its delta
    equals its share of the duplicate excess.
  - credit_note_novel: same override shape as duplicate_billing, applied
    only when no more specific finding already governs the line.
  - credit_note_corroboration: never changes a line's disposition --
    recorded only as a note, since its money is already counted via the
    line finding it corroborates.
  - invoice_total_mismatch / invoice_level_discount: become
    invoice_findings, never a per-line allocation (the discount in
    particular is deliberately never spread across lines -- see the
    Phase 6 instructions on not inventing an allocation while AE-3005
    remains unresolved).
Every line with no finding attached at all is `accept` by construction --
Phase 4 never sent a clean line to adjudication, and Phase 6 doesn't
invent a reason to second-guess that.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline.runner import GateFailure
from pipeline import schema_lite

REPORT_SCHEMA_PATH = Path("report.schema.json")
MEMOS_DIR = Path("memos")


def _round2(x: float) -> float:
    return round(x + 0.0, 2)


def _clause_string(clauses: list[str]) -> str | None:
    return ", ".join(clauses) if clauses else None


def _sanitize(finding_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "__", finding_id)


# --- classify Phase 4 findings by how they affect report construction -------

def _classify_findings(findings: list[dict]) -> dict:
    """One pass over Phase 4's findings, splitting them by the role each
    plays in report assembly. Independent of any specific finding_id --
    driven entirely by finding_type and each finding's own affected_*
    lists, so a run with different findings classifies the same way."""
    line_level = {}            # (invoice, ref) -> finding [line_pricing_delta | ambiguous_line]
    duplicate_occurrence = {}  # (invoice, ref) -> (finding, is_kept)
    credit_novel = {}          # (invoice, ref) -> finding [credit_note_novel]
    credit_corroboration = {}  # (invoice, ref) -> list[finding]
    invoice_level = []         # [finding, ...] [invoice_total_mismatch | invoice_level_discount]

    for f in findings:
        if f["finding_type"] in ("line_pricing_delta", "ambiguous_line"):
            key = (f["affected_invoices"][0], f["affected_consignments"][0])
            line_level[key] = f
        elif f["finding_type"] == "duplicate_billing":
            occurrences = f["evidence"]["occurrences"]
            kept_idx = max(range(len(occurrences)), key=lambda i: (occurrences[i]["billed_amount"], -i))
            for i, occ in enumerate(occurrences):
                key = (occ["invoice"], occ["consignment_ref"])
                duplicate_occurrence[key] = (f, i == kept_idx)
        elif f["finding_type"] == "credit_note_novel":
            key = (f["affected_invoices"][0], f["affected_consignments"][0])
            credit_novel[key] = f
        elif f["finding_type"] == "credit_note_corroboration":
            key = (f["affected_invoices"][0], f["affected_consignments"][0])
            credit_corroboration.setdefault(key, []).append(f)
        elif f["finding_type"] in ("invoice_total_mismatch", "invoice_level_discount"):
            invoice_level.append(f)
        # credit_note_unrelated: deliberately excluded -- out of July scope entirely

    return {
        "line_level": line_level, "duplicate_occurrence": duplicate_occurrence,
        "credit_novel": credit_novel, "credit_corroboration": credit_corroboration,
        "invoice_level": invoice_level,
    }


def _derive_line(matched: dict, priced: dict, classified: dict, adjudications_by_id: dict) -> dict:
    key = (matched["invoice"], matched["consignment_ref"])
    billed_amount = matched["billed_amount"]
    expected_amount = priced["expected_amount"]
    delta = priced["delta"]
    disposition = None
    justification = None
    contract_clause = _clause_string(priced["clauses"])
    notes_parts = []

    line_finding = classified["line_level"].get(key)
    if line_finding is not None:
        adj = adjudications_by_id[line_finding["finding_id"]]
        disposition = adj["disposition"]
        justification = adj["justification"]
        contract_clause = _clause_string(adj["governing_clauses"]) or contract_clause

    dup = classified["duplicate_occurrence"].get(key)
    if dup is not None:
        dup_finding, is_kept = dup
        if not is_kept:
            adj = adjudications_by_id[dup_finding["finding_id"]]
            disposition = adj["disposition"]
            justification = adj["justification"]
            contract_clause = _clause_string(adj["governing_clauses"]) or contract_clause
            if dup_finding["dispute_amount"] is not None:
                expected_amount = _round2(billed_amount - dup_finding["dispute_amount"])
                delta = _round2(billed_amount - expected_amount)
            notes_parts.append(f"Duplicate billing: see finding {dup_finding['finding_id']}.")
        else:
            notes_parts.append(
                f"Also billed on another invoice as part of duplicate billing finding "
                f"{dup_finding['finding_id']}; this occurrence is treated as the payable one."
            )

    novel = classified["credit_novel"].get(key)
    if novel is not None and line_finding is None and dup is None:
        adj = adjudications_by_id[novel["finding_id"]]
        disposition = adj["disposition"]
        justification = adj["justification"]
        contract_clause = _clause_string(adj["governing_clauses"]) or contract_clause
        if novel["dispute_amount"] is not None:
            expected_amount = _round2(billed_amount - novel["dispute_amount"])
            delta = _round2(billed_amount - expected_amount)

    for c in classified["credit_corroboration"].get(key, []):
        notes_parts.append(f"Corroborated by credit note (finding {c['finding_id']}); no additional amount counted.")

    if disposition is None:
        disposition = "accept"
        justification = "Billed amount matches the amount determined by the governing rate card; no discrepancy found."

    line = {
        "invoice": matched["invoice"], "consignment_ref": matched["consignment_ref"],
        "shipment_id": matched["shipment_id"], "billed_amount": billed_amount,
        "expected_amount": expected_amount, "delta": delta,
        "disposition": disposition, "justification": justification,
        "contract_clause": contract_clause,
    }
    if notes_parts:
        line["notes"] = " ".join(notes_parts)
    return line


def _build_report_lines(ctx, classified: dict, adjudications_by_id: dict) -> list[dict]:
    matched_lines = ctx.vars["match_and_normalise"]["matched_lines"]
    priced_lines = ctx.vars["price_lines"]["priced_lines"]
    if len(matched_lines) != len(priced_lines):
        raise RuntimeError("matched_lines and priced_lines have different lengths -- cannot assemble report")

    report_lines = []
    for matched, priced in zip(matched_lines, priced_lines):
        if matched["invoice"] != priced["invoice"] or matched["consignment_ref"] != priced["consignment_ref"]:
            raise RuntimeError(
                f"positional misalignment between matched_lines and priced_lines: "
                f"{(matched['invoice'], matched['consignment_ref'])} != {(priced['invoice'], priced['consignment_ref'])}"
            )
        report_lines.append(_derive_line(matched, priced, classified, adjudications_by_id))
    return report_lines


def _build_invoice_findings(classified: dict, adjudications_by_id: dict) -> list[dict]:
    invoice_findings = []
    for f in classified["invoice_level"]:
        adj = adjudications_by_id[f["finding_id"]]
        for inv in f["affected_invoices"]:
            invoice_findings.append({
                "invoice": inv,
                "description": f["adjudication_reason"],
                "amount_impact": f["dispute_amount"],
                "disposition": adj["disposition"],
                "justification": adj["justification"],
                "contract_clause": _clause_string(adj["governing_clauses"]),
            })
    return invoice_findings


def _build_invoice_totals(invoices: list[dict], report_lines: list[dict], classified: dict) -> list[dict]:
    lines_by_invoice: dict[str, list[dict]] = {}
    for l in report_lines:
        lines_by_invoice.setdefault(l["invoice"], []).append(l)

    discount_by_invoice = {}
    for f in classified["invoice_level"]:
        if f["finding_type"] == "invoice_level_discount":
            for inv in f["affected_invoices"]:
                discount_by_invoice[inv] = f

    invoice_totals = []
    for inv in invoices:
        lines_here = lines_by_invoice.get(inv["invoice"], [])
        billed_total = _round2(sum(l["billed_amount"] for l in lines_here))

        discount_finding = discount_by_invoice.get(inv["invoice"])
        if any(l["expected_amount"] is None for l in lines_here):
            expected_total = None
        elif discount_finding is not None and discount_finding["expected_amount"] is None:
            expected_total = None  # discount entitlement established but its amount isn't yet -- don't invent one
        else:
            base = sum(l["expected_amount"] for l in lines_here)
            if discount_finding is not None:
                base -= discount_finding["expected_amount"]
            expected_total = _round2(base)

        entry = {"invoice": inv["invoice"], "billed_total": billed_total, "expected_total": expected_total}
        if discount_finding is not None:
            entry["notes"] = f"Volume discount finding {discount_finding['finding_id']} applies; see invoice_findings."
        invoice_totals.append(entry)
    return invoice_totals


def _compute_total_in_dispute(dispute_units: list[dict], adjudications_by_id: dict) -> tuple[float, list[str]]:
    """Sums exactly the dispute units whose finding was actually
    adjudicated 'dispute' -- reusing Phase 4's already-proven-disjoint
    dispute_unit amounts directly, so a disputed rupee is counted exactly
    once by construction, not by a new summation this phase invents."""
    total = 0.0
    counted = []
    for unit in dispute_units:
        adj = adjudications_by_id.get(unit["finding_id"])
        if adj is not None and adj["disposition"] == "dispute":
            total += unit["amount"]
            counted.append(unit["dispute_unit_id"])
    return _round2(total), counted


def _build_summary(report_lines: list[dict], invoice_totals: list[dict], total_in_dispute: float) -> dict:
    total_billed = _round2(sum(it["billed_total"] for it in invoice_totals))
    if any(it["expected_total"] is None for it in invoice_totals):
        total_expected = None
    else:
        total_expected = _round2(sum(it["expected_total"] for it in invoice_totals))

    counts = {"accept": 0, "dispute": 0, "escalate": 0}
    for l in report_lines:
        counts[l["disposition"]] += 1

    return {
        "total_billed": total_billed,
        "total_expected": total_expected,
        "total_in_dispute": total_in_dispute,
        "line_count": len(report_lines),
        "counts_by_disposition": counts,
    }


def assemble_report(ctx) -> dict:
    findings = ctx.vars["detect_findings"]["findings"]
    dispute_units = ctx.vars["detect_findings"]["dispute_units"]
    adjudications_by_id = {a["finding_id"]: a for a in ctx.vars["adjudicate_findings"]["adjudications"]}
    invoices = ctx.vars["parse_invoices"]["invoices"]

    classified = _classify_findings(findings)
    report_lines = _build_report_lines(ctx, classified, adjudications_by_id)
    invoice_findings = _build_invoice_findings(classified, adjudications_by_id)
    invoice_totals = _build_invoice_totals(invoices, report_lines, classified)
    total_in_dispute, _counted_units = _compute_total_in_dispute(dispute_units, adjudications_by_id)
    summary = _build_summary(report_lines, invoice_totals, total_in_dispute)

    return {
        "lines": report_lines,
        "invoice_findings": invoice_findings,
        "invoice_totals": invoice_totals,
        "summary": summary,
    }


# --- validation: JSON Schema + independently re-derived semantic invariants --

_CLAUSE_FILE_RE = re.compile(r"\S+\.md")


def _check_clause_format(errors: list, label: str, clause: str | None) -> None:
    """report.schema.json only requires contract_clause to be a string; this
    is a looser sanity check (not a strict reparse) since the field is built
    by joining one or more already-validated citations (each independently
    checked against the real contract during Phase 5) with ", " -- a
    per-source citation may itself be single- or multi-clause, so this only
    confirms the joined string still contains at least one real-looking
    "<file>.md" reference and no empty citation slipped through."""
    if clause is None:
        return
    if not clause.strip():
        errors.append(f"{label}: contract_clause is an empty string (use null instead)")
    elif not _CLAUSE_FILE_RE.search(clause):
        errors.append(f"{label}: contract_clause {clause!r} contains no recognizable '<file>.md' reference")


def validate_report(ctx, report: dict) -> None:
    """Fails closed. Never trusts assemble_report's own working variables --
    every check here re-derives its expectation from Phase 1-5 outputs in
    ctx.vars directly, the same standard every earlier phase's gate holds
    itself to."""
    errors = []

    schema = json.loads(REPORT_SCHEMA_PATH.read_text())
    try:
        schema_lite.validate(report, schema)
    except schema_lite.SchemaError as e:
        raise GateFailure(f"validate_report: report.schema.json validation failed: {e}")

    matched_lines = ctx.vars["match_and_normalise"]["matched_lines"]
    priced_lines = ctx.vars["price_lines"]["priced_lines"]
    parsed_lines = ctx.vars["parse_invoices"]["lines"]
    findings = ctx.vars["detect_findings"]["findings"]
    dispute_units = ctx.vars["detect_findings"]["dispute_units"]
    adjudications = ctx.vars["adjudicate_findings"]["adjudications"]
    adjudications_by_id = {a["finding_id"]: a for a in adjudications}
    invoices = ctx.vars["parse_invoices"]["invoices"]

    report_lines = report["lines"]

    # 1-2. every invoice line exactly once; report line count == parsed line count
    if len(report_lines) != len(parsed_lines):
        errors.append(f"report has {len(report_lines)} line(s), parse_invoices produced {len(parsed_lines)}")
    parsed_multiset = sorted((l["invoice"], l["consignment_ref"]) for l in parsed_lines)
    report_multiset = sorted((l["invoice"], l["consignment_ref"]) for l in report_lines)
    if parsed_multiset != report_multiset:
        only_parsed = set(parsed_multiset) - set(report_multiset)
        only_report = set(report_multiset) - set(parsed_multiset)
        errors.append(f"report lines don't match parsed lines: missing {only_parsed}, extra {only_report}")

    # cross-check against matched_lines/priced_lines too (independent of assemble_report's zip)
    priced_by_key = {(p["invoice"], p["consignment_ref"]): p for p in priced_lines}
    matched_by_key = {(m["invoice"], m["consignment_ref"]): m for m in matched_lines}

    # re-derive the same finding classification independently (not by calling
    # assemble_report's helpers on trust -- by re-reading raw finding data)
    line_level = {}
    duplicate_kept = {}   # (invoice, ref) -> finding_id of the duplicate group, if this occurrence is the kept one
    duplicate_excess = {}  # (invoice, ref) -> finding
    credit_novel = {}
    credit_corroboration_keys = set()
    invoice_level_findings = []
    for f in findings:
        if f["finding_type"] in ("line_pricing_delta", "ambiguous_line"):
            line_level[(f["affected_invoices"][0], f["affected_consignments"][0])] = f
        elif f["finding_type"] == "duplicate_billing":
            occs = f["evidence"]["occurrences"]
            kept_idx = max(range(len(occs)), key=lambda i: (occs[i]["billed_amount"], -i))
            for i, occ in enumerate(occs):
                k = (occ["invoice"], occ["consignment_ref"])
                if i == kept_idx:
                    duplicate_kept[k] = f
                else:
                    duplicate_excess[k] = f
        elif f["finding_type"] == "credit_note_novel":
            credit_novel[(f["affected_invoices"][0], f["affected_consignments"][0])] = f
        elif f["finding_type"] == "credit_note_corroboration":
            credit_corroboration_keys.add((f["affected_invoices"][0], f["affected_consignments"][0]))
        elif f["finding_type"] in ("invoice_total_mismatch", "invoice_level_discount"):
            invoice_level_findings.append(f)

    memo_required_finding_ids = set()

    for rl in report_lines:
        key = (rl["invoice"], rl["consignment_ref"])
        label = f"line {key}"

        matched = matched_by_key.get(key)
        priced = priced_by_key.get(key)
        if matched is None or priced is None:
            errors.append(f"{label}: not found in matched_lines/priced_lines")
            continue

        # 3. billed_amount matches deterministic parsed/matched data
        if abs(rl["billed_amount"] - matched["billed_amount"]) > 0.005:
            errors.append(f"{label}: billed_amount {rl['billed_amount']} != matched line's {matched['billed_amount']}")

        override_finding = None
        if key in duplicate_excess:
            override_finding = duplicate_excess[key]
        elif key in credit_novel and key not in line_level:
            override_finding = credit_novel[key]

        if override_finding is not None:
            if override_finding["dispute_amount"] is not None:
                expected_should_be = _round2(matched["billed_amount"] - override_finding["dispute_amount"])
                if rl["expected_amount"] is None or abs(rl["expected_amount"] - expected_should_be) > 0.005:
                    errors.append(f"{label}: overridden expected_amount {rl['expected_amount']} != {expected_should_be}")
            adj = adjudications_by_id.get(override_finding["finding_id"])
            if adj is None:
                errors.append(f"{label}: overriding finding {override_finding['finding_id']} has no adjudication")
            elif rl["disposition"] != adj["disposition"]:
                errors.append(f"{label}: disposition {rl['disposition']!r} != overriding adjudication's {adj['disposition']!r}")
            if rl["disposition"] != "accept":
                memo_required_finding_ids.add(override_finding["finding_id"])
        elif key in line_level:
            # 4. expected_amount matches deterministic pricing output exactly (no override)
            if rl["expected_amount"] != priced["expected_amount"]:
                errors.append(f"{label}: expected_amount {rl['expected_amount']} != priced {priced['expected_amount']}")
            f = line_level[key]
            adj = adjudications_by_id.get(f["finding_id"])
            if adj is None:
                errors.append(f"{label}: line finding {f['finding_id']} has no adjudication")
            elif rl["disposition"] != adj["disposition"]:
                errors.append(f"{label}: disposition {rl['disposition']!r} != adjudication's {adj['disposition']!r}")
            if rl["disposition"] != "accept":
                memo_required_finding_ids.add(f["finding_id"])
        else:
            # no finding at all (including a "kept" duplicate occurrence) -> must match priced exactly and be accept
            if rl["expected_amount"] != priced["expected_amount"]:
                errors.append(f"{label}: expected_amount {rl['expected_amount']} != priced {priced['expected_amount']} (no finding governs this line)")
            if rl["disposition"] != "accept":
                errors.append(f"{label}: disposition {rl['disposition']!r} but no finding governs this line -- should be accept")

        # 5-6. delta consistency, both directions
        if rl["expected_amount"] is None:
            if rl["delta"] is not None:
                errors.append(f"{label}: expected_amount is null but delta is {rl['delta']} (must also be null)")
        else:
            if rl["delta"] is None:
                errors.append(f"{label}: expected_amount is set but delta is null")
            else:
                expected_delta = _round2(rl["billed_amount"] - rl["expected_amount"])
                if abs(rl["delta"] - expected_delta) > 0.005:
                    errors.append(f"{label}: delta {rl['delta']} != billed-expected ({expected_delta})")

        # 7. valid disposition
        if rl["disposition"] not in ("accept", "dispute", "escalate"):
            errors.append(f"{label}: invalid disposition {rl['disposition']!r}")

        _check_clause_format(errors, label, rl.get("contract_clause"))

    # invoice_findings: adjudication IDs map to real findings, clauses validated, memo coverage
    expected_invoice_finding_count = sum(len(f["affected_invoices"]) for f in invoice_level_findings)
    if len(report["invoice_findings"]) != expected_invoice_finding_count:
        errors.append(
            f"invoice_findings has {len(report['invoice_findings'])} entries, expected "
            f"{expected_invoice_finding_count} from {len(invoice_level_findings)} invoice-level finding(s)"
        )
    invoice_level_by_id = {f["finding_id"]: f for f in invoice_level_findings}
    for inv_finding in report["invoice_findings"]:
        _check_clause_format(errors, f"invoice_finding[{inv_finding['invoice']}]", inv_finding.get("contract_clause"))
        matches = [f for f in invoice_level_findings if inv_finding["invoice"] in f["affected_invoices"]
                   and f["adjudication_reason"] == inv_finding["description"]]
        if not matches:
            errors.append(f"invoice_finding[{inv_finding['invoice']}] description does not match any real finding")
            continue
        f = matches[0]
        adj = adjudications_by_id.get(f["finding_id"])
        if adj is None or inv_finding["disposition"] != adj["disposition"]:
            errors.append(f"invoice_finding[{inv_finding['invoice']}] disposition does not match its finding's adjudication")
        if abs((inv_finding["amount_impact"] or 0) - (f["dispute_amount"] or 0)) > 0.005 or \
           (inv_finding["amount_impact"] is None) != (f["dispute_amount"] is None):
            errors.append(f"invoice_finding[{inv_finding['invoice']}] amount_impact {inv_finding['amount_impact']} != finding's dispute_amount {f['dispute_amount']}")
        if inv_finding["disposition"] != "accept":
            memo_required_finding_ids.add(f["finding_id"])

    # 8. memo coverage: every non-accept report item backed by a real finding has a memo
    for finding_id in memo_required_finding_ids:
        path = MEMOS_DIR / f"{_sanitize(finding_id)}.md"
        if not path.exists():
            errors.append(f"finding {finding_id!r} requires a memo (non-accept) but {path} does not exist")

    # 9. invoice totals reconcile
    report_lines_by_invoice: dict[str, list[dict]] = {}
    for rl in report_lines:
        report_lines_by_invoice.setdefault(rl["invoice"], []).append(rl)
    invoice_totals_by_id = {it["invoice"]: it for it in report["invoice_totals"]}
    if set(invoice_totals_by_id) != {inv["invoice"] for inv in invoices}:
        errors.append("invoice_totals does not cover exactly the in-scope invoices")
    for inv in invoices:
        it = invoice_totals_by_id.get(inv["invoice"])
        if it is None:
            continue
        lines_here = report_lines_by_invoice.get(inv["invoice"], [])
        billed_should_be = _round2(sum(l["billed_amount"] for l in lines_here))
        if abs(it["billed_total"] - billed_should_be) > 0.005:
            errors.append(f"invoice_totals[{inv['invoice']}].billed_total {it['billed_total']} != recomputed {billed_should_be}")
        if any(l["expected_amount"] is None for l in lines_here):
            if it["expected_total"] is not None:
                errors.append(f"invoice_totals[{inv['invoice']}].expected_total should be null (an undetermined line exists)")

    # 10. summary.total_billed reconciles
    summary = report["summary"]
    total_billed_should_be = _round2(sum(it["billed_total"] for it in report["invoice_totals"]))
    if abs(summary["total_billed"] - total_billed_should_be) > 0.005:
        errors.append(f"summary.total_billed {summary['total_billed']} != recomputed {total_billed_should_be}")

    # 11. summary.total_expected reconciles
    if any(it["expected_total"] is None for it in report["invoice_totals"]):
        if summary["total_expected"] is not None:
            errors.append("summary.total_expected should be null (an invoice_totals entry is null)")
    else:
        total_expected_should_be = _round2(sum(it["expected_total"] for it in report["invoice_totals"]))
        if summary["total_expected"] is None or abs(summary["total_expected"] - total_expected_should_be) > 0.005:
            errors.append(f"summary.total_expected {summary['total_expected']} != recomputed {total_expected_should_be}")

    # 12. summary.total_in_dispute is the sum of unique dispute units, not duplicated findings
    total_in_dispute_should_be, counted_units = _compute_total_in_dispute(dispute_units, adjudications_by_id)
    if abs(summary["total_in_dispute"] - total_in_dispute_should_be) > 0.005:
        errors.append(f"summary.total_in_dispute {summary['total_in_dispute']} != recomputed {total_in_dispute_should_be} from dispute_units {counted_units}")

    # 13. disposition counts reconcile
    recount = {"accept": 0, "dispute": 0, "escalate": 0}
    for rl in report_lines:
        if rl["disposition"] in recount:
            recount[rl["disposition"]] += 1
    if recount != summary["counts_by_disposition"]:
        errors.append(f"summary.counts_by_disposition {summary['counts_by_disposition']} != recomputed {recount}")
    if summary["line_count"] != len(report_lines):
        errors.append(f"summary.line_count {summary['line_count']} != len(lines) {len(report_lines)}")

    # 14. no agent-provided monetary value anywhere -- redundant with Phase 5's own
    # gate, but re-checked here against the actual adjudications this run produced
    allowed_adjudication_fields = {"finding_id", "disposition", "justification", "governing_clauses", "memo_narrative"}
    for a in adjudications:
        extra = set(a.keys()) - allowed_adjudication_fields
        if extra:
            errors.append(f"adjudication {a.get('finding_id')}: unexpected field(s) {extra} (possible monetary injection)")

    if errors:
        raise GateFailure("validate_report failed:\n  - " + "\n  - ".join(errors))

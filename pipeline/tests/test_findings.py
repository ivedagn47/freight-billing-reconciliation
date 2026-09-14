"""Tests for Phase 4: invoice-level findings and dispute-unit attribution.
All fixtures are synthetic (small, hand-built in the same shape Phases
1-3 produce) -- no real consignment reference from the actual data
appears anywhere in this file, so these tests exercise the mechanism
generically rather than the specific known discrepancies.

Run with: python3 -m unittest pipeline.tests.test_findings -v
"""
import unittest

from pipeline.nodes.findings import (
    _consignment_count_bases, _credit_note_findings, _discount_findings,
    _duplicate_findings, _invoice_total_findings, _line_level_findings,
    detect_findings, findings_gate,
)
from pipeline.runner import Context, GateFailure


def _inv(invoice, carrier, declared_total=None, declared_discount=None):
    return {"invoice": invoice, "carrier": carrier, "file": f"{invoice}.x",
            "declared_line_count": None, "declared_total": declared_total, "declared_discount": declared_discount}


def _line(invoice, ref, billed_amount, raw=None, carrier="test"):
    return {"invoice": invoice, "carrier": carrier, "consignment_ref": ref, "billed_amount": billed_amount,
            "billed_components": [], "stated_attributes": {}, "raw": raw or {},
            "provenance": {"file": "x", "locator": ref}}


def _priced(invoice, ref, billed, expected, delta, outcome="determined", shipment_id="SH-1", reason=None, clauses=None):
    return {"invoice": invoice, "consignment_ref": ref, "carrier": "test", "shipment_id": shipment_id,
            "billed_amount": billed, "delta": delta, "pricing_outcome": outcome,
            "expected_amount": expected, "reason": reason, "clauses": clauses or ["x.md §1"], "trace": {}}


def _empty_ratecard():
    return {"discounts": []}


class InvoiceTotalTests(unittest.TestCase):
    def test_mismatch_creates_finding(self):
        inv = [_inv("INV-1", "test", declared_total=100.0)]
        lines_by_invoice = {"INV-1": [_line("INV-1", "R1", 40.0), _line("INV-1", "R2", 50.0)]}  # sums to 90
        findings = _invoice_total_findings(inv, lines_by_invoice)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["finding_type"], "invoice_total_mismatch")
        self.assertAlmostEqual(f["delta"], 10.0)
        self.assertEqual(f["dispute_unit_id"], "du:invoice_total:INV-1")
        self.assertAlmostEqual(f["dispute_amount"], 10.0)

    def test_matching_total_creates_no_finding(self):
        inv = [_inv("INV-1", "test", declared_total=90.0)]
        lines_by_invoice = {"INV-1": [_line("INV-1", "R1", 40.0), _line("INV-1", "R2", 50.0)]}
        findings = _invoice_total_findings(inv, lines_by_invoice)
        self.assertEqual(findings, [])

    def test_no_declared_total_is_never_inferred(self):
        inv = [_inv("INV-1", "test", declared_total=None)]
        lines_by_invoice = {"INV-1": [_line("INV-1", "R1", 40.0)]}
        findings = _invoice_total_findings(inv, lines_by_invoice)
        self.assertEqual(findings, [])  # no finding fabricated from a total the document never stated


class DiscountThresholdTests(unittest.TestCase):
    def _ratecard(self, op="gt", value=12):
        return {"discounts": [{"name": "Volume discount", "type": "percentage_of_invoice_total", "rate": 0.05,
                                "amount": None, "condition": "x", "clause": "x.md §5",
                                "threshold": {"metric": "consignment_count", "period": "calendar_month", "op": op, "value": value}}]}

    def _setup(self, n_lines, declared_discount=0.0):
        inv = _inv("INV-1", "alpine", declared_total=None, declared_discount=declared_discount)
        lines = [_line("INV-1", f"R{i}", 100.0, raw={"booking_date": "2026-07-01"}, carrier="alpine") for i in range(n_lines)]
        lines_by_invoice = {"INV-1": lines}
        priced_by_key = {("INV-1", l["consignment_ref"]): _priced("INV-1", l["consignment_ref"], 100.0, 100.0, 0.0)
                          for l in lines}
        return [inv], lines_by_invoice, priced_by_key, lines

    def test_below_threshold_no_finding(self):
        invoices, lines_by_invoice, priced_by_key, all_lines = self._setup(10)
        ratecards = {"alpine": self._ratecard(op="gt", value=12)}
        findings = _discount_findings(invoices, lines_by_invoice, priced_by_key, ratecards, all_lines, {})
        self.assertEqual(findings, [])

    def test_equal_to_threshold_gt_is_not_met(self):
        invoices, lines_by_invoice, priced_by_key, all_lines = self._setup(12)
        ratecards = {"alpine": self._ratecard(op="gt", value=12)}
        findings = _discount_findings(invoices, lines_by_invoice, priced_by_key, ratecards, all_lines, {})
        self.assertEqual(findings, [])  # "more than 12" (gt) excludes exactly 12

    def test_above_threshold_creates_finding_when_discount_not_applied(self):
        invoices, lines_by_invoice, priced_by_key, all_lines = self._setup(13, declared_discount=0.0)
        ratecards = {"alpine": self._ratecard(op="gt", value=12)}
        findings = _discount_findings(invoices, lines_by_invoice, priced_by_key, ratecards, all_lines, {})
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["finding_type"], "invoice_level_discount")
        # 13 lines * 100 = 1300 pre-discount; 5% = 65.0; declared discount 0 -> shortfall 65.0
        self.assertAlmostEqual(f["expected_amount"], 65.0)
        self.assertAlmostEqual(f["dispute_amount"], 65.0)
        self.assertIsNotNone(f["dispute_unit_id"])

    def test_discount_already_applied_creates_no_dispute(self):
        invoices, lines_by_invoice, priced_by_key, all_lines = self._setup(13, declared_discount=65.0)
        ratecards = {"alpine": self._ratecard(op="gt", value=12)}
        findings = _discount_findings(invoices, lines_by_invoice, priced_by_key, ratecards, all_lines, {})
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0]["dispute_unit_id"])  # discount matches what was applied -- nothing to dispute

    def test_undetermined_line_blocks_amount_but_still_finds(self):
        invoices, lines_by_invoice, priced_by_key, all_lines = self._setup(13)
        priced_by_key[("INV-1", "R0")] = _priced("INV-1", "R0", 100.0, None, None, outcome="ambiguous", reason="gap")
        ratecards = {"alpine": self._ratecard(op="gt", value=12)}
        findings = _discount_findings(invoices, lines_by_invoice, priced_by_key, ratecards, all_lines, {})
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0]["dispute_unit_id"])  # can't compute exact shortfall yet
        self.assertIsNone(findings[0]["expected_amount"])


class AmbiguousCalendarMonthBasisTests(unittest.TestCase):
    def test_disagreeing_bases_produce_no_dispute_amount(self):
        """If booking-date and ship-date bases would give a DIFFERENT
        threshold outcome, the ambiguity is preserved, not resolved."""
        ratecard = {"discounts": [{"name": "Volume discount", "type": "percentage_of_invoice_total", "rate": 0.05,
                                    "amount": None, "condition": "x", "clause": "x.md §5",
                                    "threshold": {"metric": "consignment_count", "period": "calendar_month", "op": "gt", "value": 12}}]}
        invoices = [_inv("INV-1", "alpine", declared_discount=0.0)]
        # INV-1 has 13 lines, all booked in July (so its own booking-date
        # month IS computable) -- but a second invoice (INV-2, same
        # carrier, also July) contributes more lines to the calendar-month
        # aggregate, so the by-document count (13) and the by-calendar-month
        # count (18) genuinely disagree. This is what the ambiguity is
        # actually about: not an uncomputable basis, but two computable
        # bases that answer differently.
        lines_inv1 = [_line("INV-1", f"R{i}", 100.0, raw={"booking_date": "2026-07-01"}, carrier="alpine") for i in range(13)]
        lines_inv2 = [_line("INV-2", f"S{i}", 100.0, raw={"booking_date": "2026-07-15"}, carrier="alpine") for i in range(5)]
        all_lines = lines_inv1 + lines_inv2
        lines_by_invoice = {"INV-1": lines_inv1}
        priced_by_key = {("INV-1", l["consignment_ref"]): _priced("INV-1", l["consignment_ref"], 100.0, 100.0, 0.0)
                          for l in lines_inv1}
        findings = _discount_findings(invoices, lines_by_invoice, priced_by_key, {"alpine": ratecard}, all_lines, {})
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertIsNone(f["dispute_unit_id"])
        self.assertTrue(f["requires_adjudication"])
        self.assertIn("disagree", f["adjudication_reason"])

    def test_consignment_count_bases_agree_when_single_month(self):
        lines = [_line("INV-1", f"R{i}", 100.0, raw={"booking_date": "2026-07-01"}, carrier="alpine") for i in range(5)]
        bases = _consignment_count_bases("INV-1", "alpine", lines, lines, {})
        self.assertEqual(bases["by_invoice_document"], 5)
        self.assertEqual(bases["by_booking_date_calendar_month"], 5)


class DuplicateFindingTests(unittest.TestCase):
    def test_duplicate_occurrences_recorded(self):
        groups = [{"group_id": "dup-R1", "consignment_ref": "R1", "occurrences": [
            {"invoice": "INV-A", "provenance": {"file": "a", "locator": "l1"}},
            {"invoice": "INV-B", "provenance": {"file": "b", "locator": "l2"}},
        ]}]
        priced_by_key = {
            ("INV-A", "R1"): _priced("INV-A", "R1", 500.0, 500.0, 0.0, shipment_id="SH-9"),
            ("INV-B", "R1"): _priced("INV-B", "R1", 500.0, 500.0, 0.0, shipment_id="SH-9"),
        }
        findings = _duplicate_findings(groups, priced_by_key)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["finding_type"], "duplicate_billing")
        self.assertEqual(set(f["affected_invoices"]), {"INV-A", "INV-B"})
        self.assertEqual(f["related_shipments"], ["SH-9"])

    def test_duplicate_monetary_attribution_uses_sum_minus_max(self):
        groups = [{"group_id": "dup-R1", "consignment_ref": "R1", "occurrences": [
            {"invoice": "INV-A", "provenance": {"file": "a", "locator": "l1"}},
            {"invoice": "INV-B", "provenance": {"file": "b", "locator": "l2"}},
            {"invoice": "INV-C", "provenance": {"file": "c", "locator": "l3"}},
        ]}]
        priced_by_key = {
            ("INV-A", "R1"): _priced("INV-A", "R1", 500.0, 500.0, 0.0),
            ("INV-B", "R1"): _priced("INV-B", "R1", 500.0, 500.0, 0.0),
            ("INV-C", "R1"): _priced("INV-C", "R1", 600.0, 500.0, 100.0),  # differs, and has its own pricing delta too
        }
        findings = _duplicate_findings(groups, priced_by_key)
        f = findings[0]
        # sum(500,500,600) - max(600) = 1000 -- the two extra payments
        self.assertAlmostEqual(f["dispute_amount"], 1000.0)


class CreditNoteRelationshipTests(unittest.TestCase):
    def _manifest_and_priced(self, credit_amount, related_delta):
        priced_by_key = {("JULY-1", "R1"): _priced("JULY-1", "R1", 200.0, 200.0 - related_delta, related_delta,
                                                     shipment_id="SH-5")}
        return priced_by_key

    def test_credit_note_linked_to_july_line_corroborates(self):
        priced_by_key = self._manifest_and_priced(credit_amount=-50.0, related_delta=50.0)

        class FakeCN:
            pass

        import pipeline.nodes.findings as findings_mod
        original = findings_mod._CREDIT_NOTE_PARSERS
        try:
            findings_mod._CREDIT_NOTE_PARSERS = {".txt": lambda path: {
                "credit_note_id": "CN-1", "carrier": "test", "file": str(path), "against_invoice": "JULY-1",
                "entries": [{"consignment_ref": "R1", "amount": -50.0, "against_invoice": "JULY-1", "explanation_lines": []}],
                "declared_total": -50.0, "residue": [],
            }}
            manifest = {"files": [{"doc_type": "credit_note", "file": "cn.txt"}]}
            findings = _credit_note_findings(manifest, priced_by_key, {"JULY-1"})
        finally:
            findings_mod._CREDIT_NOTE_PARSERS = original

        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["finding_type"], "credit_note_corroboration")
        self.assertIsNone(f["dispute_unit_id"])  # no new money -- already counted at line level
        self.assertEqual(f["related_dispute_unit_id"], "du:line:JULY-1:R1")

    def test_credit_note_unrelated_does_not_affect_july(self):
        import pipeline.nodes.findings as findings_mod
        original = findings_mod._CREDIT_NOTE_PARSERS
        try:
            findings_mod._CREDIT_NOTE_PARSERS = {".csv": lambda path: {
                "credit_note_id": "CN-2", "carrier": "test", "file": str(path), "against_invoice": "AUG-1",
                "entries": [{"consignment_ref": "R99", "amount": -10.0, "against_invoice": "AUG-1", "explanation_lines": []}],
                "declared_total": -10.0, "residue": [],
            }}
            manifest = {"files": [{"doc_type": "credit_note", "file": "cn.csv"}]}
            findings = _credit_note_findings(manifest, {}, {"JULY-1"})  # AUG-1 not in scope
        finally:
            findings_mod._CREDIT_NOTE_PARSERS = original

        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["finding_type"], "credit_note_unrelated")
        self.assertIsNone(f["dispute_unit_id"])
        self.assertFalse(f["requires_adjudication"])

    def test_credit_note_novel_amount_gets_own_dispute_unit(self):
        priced_by_key = {("JULY-1", "R1"): _priced("JULY-1", "R1", 200.0, 200.0, 0.0)}  # zero delta -- new info
        import pipeline.nodes.findings as findings_mod
        original = findings_mod._CREDIT_NOTE_PARSERS
        try:
            findings_mod._CREDIT_NOTE_PARSERS = {".txt": lambda path: {
                "credit_note_id": "CN-3", "carrier": "test", "file": str(path), "against_invoice": "JULY-1",
                "entries": [{"consignment_ref": "R1", "amount": -30.0, "against_invoice": "JULY-1", "explanation_lines": []}],
                "declared_total": -30.0, "residue": [],
            }}
            manifest = {"files": [{"doc_type": "credit_note", "file": "cn.txt"}]}
            findings = _credit_note_findings(manifest, priced_by_key, {"JULY-1"})
        finally:
            findings_mod._CREDIT_NOTE_PARSERS = original

        f = findings[0]
        self.assertEqual(f["finding_type"], "credit_note_novel")
        self.assertEqual(f["dispute_unit_id"], "du:creditnote:CN-3:R1")
        self.assertAlmostEqual(f["dispute_amount"], 30.0)


class LineLevelFindingsTests(unittest.TestCase):
    def test_nonzero_delta_line_becomes_finding(self):
        priced = [_priced("INV", "R1", 110.0, 100.0, 10.0)]
        findings = _line_level_findings(priced)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_type"], "line_pricing_delta")
        self.assertEqual(findings[0]["dispute_amount"], 10.0)

    def test_zero_delta_line_no_finding(self):
        priced = [_priced("INV", "R1", 100.0, 100.0, 0.0)]
        self.assertEqual(_line_level_findings(priced), [])

    def test_ambiguous_line_becomes_finding_without_dispute_unit(self):
        priced = [_priced("INV", "R1", 100.0, None, None, outcome="ambiguous", reason="gap at boundary")]
        findings = _line_level_findings(priced)
        self.assertEqual(findings[0]["finding_type"], "ambiguous_line")
        self.assertIsNone(findings[0]["dispute_unit_id"])
        self.assertEqual(findings[0]["adjudication_reason"], "gap at boundary")


class FindingsGateTests(unittest.TestCase):
    def _ctx(self, invoices, lines, priced_lines):
        ctx = Context(run_dir=None)
        ctx.vars["parse_invoices"] = {"invoices": invoices, "lines": lines}
        ctx.vars["price_lines"] = {"priced_lines": priced_lines}
        return ctx

    def test_valid_output_passes(self):
        ctx = self._ctx([_inv("INV", "test")], [_line("INV", "R1", 110.0)],
                         [_priced("INV", "R1", 110.0, 100.0, 10.0)])
        ctx.vars["match_and_normalise"] = {"duplicate_groups": []}
        ctx.vars["discover_invoices"] = {"files": []}
        ctx.vars["load_shipments"] = {"by_ref": {}}
        ctx.vars["extract_ratecard_alpine"] = _empty_ratecard()
        ctx.vars["extract_ratecard_falcon"] = _empty_ratecard()
        ctx.vars["extract_ratecard_sagar"] = _empty_ratecard()
        output = detect_findings(ctx)
        findings_gate(ctx, output)  # should not raise

    def test_gate_catches_duplicate_finding_id(self):
        ctx = self._ctx([_inv("INV", "test")], [_line("INV", "R1", 110.0)],
                         [_priced("INV", "R1", 110.0, 100.0, 10.0)])
        output = {
            "findings": [
                {"finding_id": "same", "finding_type": "line_pricing_delta", "affected_invoices": ["INV"],
                 "affected_consignments": ["R1"], "related_shipments": [], "billed_amounts": [110.0],
                 "expected_amount": 100.0, "delta": 10.0, "dispute_unit_id": "du:1", "dispute_amount": 10.0,
                 "related_dispute_unit_id": None, "governing_clauses": [], "evidence": {"x": 1},
                 "requires_adjudication": True, "adjudication_reason": "r"},
                {"finding_id": "same", "finding_type": "ambiguous_line", "affected_invoices": ["INV"],
                 "affected_consignments": ["R1"], "related_shipments": [], "billed_amounts": [110.0],
                 "expected_amount": None, "delta": None, "dispute_unit_id": None, "dispute_amount": None,
                 "related_dispute_unit_id": None, "governing_clauses": [], "evidence": {"x": 1},
                 "requires_adjudication": True, "adjudication_reason": "r"},
            ],
            "dispute_units": [{"dispute_unit_id": "du:1", "amount": 10.0, "finding_id": "same", "finding_type": "line_pricing_delta"}],
            "summary": {"finding_count": 2, "dispute_unit_count": 1, "total_dispute_unit_amount_if_all_disputed": 10.0, "requires_adjudication_count": 2},
        }
        with self.assertRaises(GateFailure):
            findings_gate(ctx, output)

    def test_gate_catches_amount_double_counted_across_two_dispute_units(self):
        """Two different dispute units both claiming credit for the same
        underlying rupees would inflate total_dispute_unit_amount beyond
        what findings can justify -- simulate via a corroboration finding
        that wrongly also sets its own dispute_unit_id."""
        ctx = self._ctx([_inv("INV", "test")], [_line("INV", "R1", 110.0)],
                         [_priced("INV", "R1", 110.0, 100.0, 10.0)])
        output = {
            "findings": [
                {"finding_id": "line_delta:INV:R1", "finding_type": "line_pricing_delta", "affected_invoices": ["INV"],
                 "affected_consignments": ["R1"], "related_shipments": [], "billed_amounts": [110.0],
                 "expected_amount": 100.0, "delta": 10.0, "dispute_unit_id": "du:line:INV:R1", "dispute_amount": 10.0,
                 "related_dispute_unit_id": None, "governing_clauses": [], "evidence": {"x": 1},
                 "requires_adjudication": True, "adjudication_reason": "r"},
                {"finding_id": "credit_note:CN:R1", "finding_type": "credit_note_corroboration", "affected_invoices": ["INV"],
                 "affected_consignments": ["R1"], "related_shipments": [], "billed_amounts": [],
                 "expected_amount": None, "delta": None,
                 "dispute_unit_id": "du:creditnote:CN:R1",  # WRONG: corroboration must never set its own unit
                 "dispute_amount": 10.0,
                 "related_dispute_unit_id": "du:line:INV:R1", "governing_clauses": [], "evidence": {"x": 1},
                 "requires_adjudication": True, "adjudication_reason": "r"},
            ],
            "dispute_units": [
                {"dispute_unit_id": "du:line:INV:R1", "amount": 10.0, "finding_id": "line_delta:INV:R1", "finding_type": "line_pricing_delta"},
                {"dispute_unit_id": "du:creditnote:CN:R1", "amount": 10.0, "finding_id": "credit_note:CN:R1", "finding_type": "credit_note_corroboration"},
            ],
            "summary": {"finding_count": 2, "dispute_unit_count": 2, "total_dispute_unit_amount_if_all_disputed": 20.0, "requires_adjudication_count": 2},
        }
        # This specific double-count shape isn't caught by a single named
        # check -- it's caught because price_lines only produced ONE
        # nonzero-delta line but findings claims 2 dispute-carrying entries
        # tied to the same (invoice, ref); assert the gate still fails
        # findings_gate for a real, present reason (not asserting *which*).
        with self.assertRaises(GateFailure):
            findings_gate(ctx, output)

    def test_gate_catches_dangling_related_dispute_unit(self):
        ctx = self._ctx([_inv("INV", "test")], [_line("INV", "R1", 110.0)], [])
        output = {
            "findings": [
                {"finding_id": "credit_note:CN:R1", "finding_type": "credit_note_corroboration", "affected_invoices": ["INV"],
                 "affected_consignments": ["R1"], "related_shipments": [], "billed_amounts": [],
                 "expected_amount": None, "delta": None, "dispute_unit_id": None, "dispute_amount": None,
                 "related_dispute_unit_id": "du:line:INV:R1",  # points at a unit that doesn't exist
                 "governing_clauses": [], "evidence": {"x": 1}, "requires_adjudication": True, "adjudication_reason": "r"},
            ],
            "dispute_units": [],
            "summary": {"finding_count": 1, "dispute_unit_count": 0, "total_dispute_unit_amount_if_all_disputed": 0.0, "requires_adjudication_count": 1},
        }
        with self.assertRaises(GateFailure):
            findings_gate(ctx, output)

    def test_gate_catches_finding_referencing_unknown_line(self):
        ctx = self._ctx([_inv("INV", "test")], [_line("INV", "R1", 110.0)], [])
        output = {
            "findings": [
                {"finding_id": "line_delta:INV:R99", "finding_type": "line_pricing_delta", "affected_invoices": ["INV"],
                 "affected_consignments": ["R99"], "related_shipments": [], "billed_amounts": [1.0],
                 "expected_amount": 1.0, "delta": 1.0, "dispute_unit_id": "du:1", "dispute_amount": 1.0,
                 "related_dispute_unit_id": None, "governing_clauses": [], "evidence": {"x": 1},
                 "requires_adjudication": True, "adjudication_reason": "r"},
            ],
            "dispute_units": [{"dispute_unit_id": "du:1", "amount": 1.0, "finding_id": "line_delta:INV:R99", "finding_type": "line_pricing_delta"}],
            "summary": {"finding_count": 1, "dispute_unit_count": 1, "total_dispute_unit_amount_if_all_disputed": 1.0, "requires_adjudication_count": 1},
        }
        with self.assertRaises(GateFailure):
            findings_gate(ctx, output)


if __name__ == "__main__":
    unittest.main()

"""Tests for Phase 6: final report assembly and validation.

Builds one small, internally-consistent synthetic "world" (2 invoices, 3
lines: one clean, one with a line-level pricing delta, one ambiguous) that
passes validate_report cleanly, then corrupts one aspect at a time to
prove the gate actually catches it. No real consignment reference from
the actual data appears here.

Run with: python3 -m unittest pipeline.tests.test_report -v
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import pipeline.nodes.report as report_mod
from pipeline.nodes.report import (
    _build_invoice_findings, _build_invoice_totals, _build_report_lines,
    _build_summary, _classify_findings, _compute_total_in_dispute,
    assemble_report, validate_report,
)
from pipeline.runner import Context, GateFailure


def _matched(invoice, ref, billed, shipment_id="SH-1"):
    return {"invoice": invoice, "carrier": "test", "consignment_ref": ref, "billed_amount": billed,
            "billed_components": [], "stated_attributes": {}, "raw": {},
            "provenance": {"file": "x", "locator": ref},
            "shipment_id": shipment_id, "matched": True, "shipment_carrier": "test",
            "is_duplicate": False, "duplicate_group_id": None}


def _priced(invoice, ref, billed, expected, delta, outcome="determined", clauses=None):
    return {"invoice": invoice, "consignment_ref": ref, "carrier": "test", "shipment_id": "SH-1",
            "billed_amount": billed, "delta": delta, "pricing_outcome": outcome,
            "expected_amount": expected, "reason": None if outcome == "determined" else "gap",
            "clauses": clauses if clauses is not None else (["x.md §1"] if outcome == "determined" else []),
            "trace": {}}


def _finding(finding_id, finding_type, invoices, refs, dispute_unit_id=None, dispute_amount=None,
             related_dispute_unit_id=None, evidence=None, requires_adjudication=True):
    return {
        "finding_id": finding_id, "finding_type": finding_type,
        "affected_invoices": invoices, "affected_consignments": refs, "related_shipments": ["SH-1"],
        "billed_amounts": [], "expected_amount": None, "delta": None,
        "dispute_unit_id": dispute_unit_id, "dispute_amount": dispute_amount,
        "related_dispute_unit_id": related_dispute_unit_id,
        "governing_clauses": ["x.md §1"], "evidence": evidence or {},
        "requires_adjudication": requires_adjudication, "adjudication_reason": "reason",
    }


def _adjudication(finding_id, disposition, clauses=None):
    return {
        "finding_id": finding_id, "disposition": disposition,
        "justification": "because of the evidence", "governing_clauses": clauses or ["x.md §1"],
        "memo_narrative": {"what_happened": "w", "why": "y", "recommended_action": "r"},
    }


def _good_world(tmp_memos_dir: Path):
    """Two invoices, three lines: L1 clean (accept), L2 has a
    line_pricing_delta finding (dispute), L3 is ambiguous (escalate)."""
    matched_lines = [
        _matched("INV-A", "L1", 100.0),
        _matched("INV-A", "L2", 150.0),
        _matched("INV-B", "L3", 80.0),
    ]
    priced_lines = [
        _priced("INV-A", "L1", 100.0, 100.0, 0.0),
        _priced("INV-A", "L2", 150.0, 120.0, 30.0),
        _priced("INV-B", "L3", 80.0, None, None, outcome="ambiguous"),
    ]
    findings = [
        _finding("line_delta:INV-A:L2", "line_pricing_delta", ["INV-A"], ["L2"],
                  dispute_unit_id="du:line:INV-A:L2", dispute_amount=30.0),
        _finding("ambiguous_line:INV-B:L3", "ambiguous_line", ["INV-B"], ["L3"]),
    ]
    dispute_units = [{"dispute_unit_id": "du:line:INV-A:L2", "amount": 30.0,
                       "finding_id": "line_delta:INV-A:L2", "finding_type": "line_pricing_delta"}]
    adjudications = [
        _adjudication("line_delta:INV-A:L2", "dispute", clauses=["x.md §2"]),
        _adjudication("ambiguous_line:INV-B:L3", "escalate", clauses=["x.md §3"]),
    ]

    ctx = Context(run_dir=None)
    ctx.vars["match_and_normalise"] = {"matched_lines": matched_lines, "duplicate_groups": []}
    ctx.vars["price_lines"] = {"priced_lines": priced_lines}
    ctx.vars["parse_invoices"] = {
        "invoices": [{"invoice": "INV-A", "carrier": "test", "file": "a", "declared_line_count": None,
                       "declared_total": None, "declared_discount": None},
                      {"invoice": "INV-B", "carrier": "test", "file": "b", "declared_line_count": None,
                       "declared_total": None, "declared_discount": None}],
        "lines": [{"invoice": m["invoice"], "consignment_ref": m["consignment_ref"], "billed_amount": m["billed_amount"]}
                  for m in matched_lines],
    }
    ctx.vars["detect_findings"] = {"findings": findings, "dispute_units": dispute_units}
    ctx.vars["adjudicate_findings"] = {"adjudications": adjudications}

    # write real memo files for the two non-accept findings
    tmp_memos_dir.mkdir(parents=True, exist_ok=True)
    (tmp_memos_dir / "line_delta__INV-A__L2.md").write_text("memo for L2")
    (tmp_memos_dir / "ambiguous_line__INV-B__L3.md").write_text("memo for L3")

    report = assemble_report(ctx)
    return ctx, report


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_memos_dir = report_mod.MEMOS_DIR
        report_mod.MEMOS_DIR = Path(self._tmpdir.name)
        self.ctx, self.report = _good_world(report_mod.MEMOS_DIR)

    def tearDown(self):
        report_mod.MEMOS_DIR = self._orig_memos_dir
        self._tmpdir.cleanup()


class GoodWorldPassesTests(ReportTestCase):
    def test_schema_valid_and_gate_passes(self):
        schema = json.loads(Path("report.schema.json").read_text())
        import pipeline.schema_lite as schema_lite
        schema_lite.validate(self.report, schema)  # should not raise
        validate_report(self.ctx, self.report)  # should not raise

    def test_line_count_and_dispositions(self):
        self.assertEqual(len(self.report["lines"]), 3)
        by_ref = {l["consignment_ref"]: l for l in self.report["lines"]}
        self.assertEqual(by_ref["L1"]["disposition"], "accept")
        self.assertEqual(by_ref["L2"]["disposition"], "dispute")
        self.assertEqual(by_ref["L3"]["disposition"], "escalate")

    def test_escalated_line_has_null_expected_and_delta(self):
        by_ref = {l["consignment_ref"]: l for l in self.report["lines"]}
        self.assertIsNone(by_ref["L3"]["expected_amount"])
        self.assertIsNone(by_ref["L3"]["delta"])

    def test_total_in_dispute_is_disjoint_sum(self):
        self.assertAlmostEqual(self.report["summary"]["total_in_dispute"], 30.0)

    def test_total_expected_null_when_a_line_is_undetermined(self):
        # INV-B's only line is ambiguous -> its invoice_total.expected_total is null
        # -> summary.total_expected must also be null
        self.assertIsNone(self.report["summary"]["total_expected"])


class NegativeCaseTests(ReportTestCase):
    def test_missing_line_rejected(self):
        bad = copy.deepcopy(self.report)
        bad["lines"].pop()
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_duplicate_line_rejected(self):
        bad = copy.deepcopy(self.report)
        bad["lines"].append(copy.deepcopy(bad["lines"][0]))
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_incorrect_billed_amount_rejected(self):
        bad = copy.deepcopy(self.report)
        bad["lines"][0]["billed_amount"] += 1.0
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_incorrect_expected_amount_rejected(self):
        bad = copy.deepcopy(self.report)
        for l in bad["lines"]:
            if l["consignment_ref"] == "L2":
                l["expected_amount"] = 999.0
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_incorrect_delta_rejected(self):
        bad = copy.deepcopy(self.report)
        for l in bad["lines"]:
            if l["consignment_ref"] == "L2":
                l["delta"] = 999.0
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_incorrect_invoice_total_rejected(self):
        bad = copy.deepcopy(self.report)
        bad["invoice_totals"][0]["billed_total"] += 50.0
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_incorrect_summary_total_billed_rejected(self):
        bad = copy.deepcopy(self.report)
        bad["summary"]["total_billed"] += 1.0
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_duplicate_dispute_unit_counting_rejected(self):
        """Simulate an attempt to count the same dispute unit's money twice
        by inflating total_in_dispute beyond what the disjoint dispute_units
        list actually justifies."""
        bad = copy.deepcopy(self.report)
        bad["summary"]["total_in_dispute"] = 60.0  # should be 30.0
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_credit_note_double_count_attempt_rejected(self):
        """A credit_note_novel finding's money must show up exactly once
        (via its target line's override), never also folded into
        total_in_dispute a second time through a fabricated extra unit."""
        ctx = copy.deepcopy(self.ctx)
        ctx.vars["detect_findings"]["dispute_units"].append(
            {"dispute_unit_id": "du:creditnote:CN:L1", "amount": 15.0,
             "finding_id": "credit_note:CN:L1", "finding_type": "credit_note_novel"}
        )
        ctx.vars["adjudicate_findings"]["adjudications"].append(
            _adjudication("credit_note:CN:L1", "dispute")
        )
        bad = copy.deepcopy(self.report)
        bad["summary"]["total_in_dispute"] = 30.0  # doesn't include the new unit -> now inconsistent
        with self.assertRaises(GateFailure):
            validate_report(ctx, bad)

    def test_escalation_with_null_expected_delta_passes(self):
        # this is the GOOD-world L3 case already, verified as a positive
        # test above; here we additionally verify it's rejected if someone
        # tries to give it a fabricated non-null amount instead of null.
        bad = copy.deepcopy(self.report)
        for l in bad["lines"]:
            if l["consignment_ref"] == "L3":
                l["expected_amount"] = 999.0
                l["delta"] = 20.0
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, bad)

    def test_memo_coverage_failure_rejected(self):
        (report_mod.MEMOS_DIR / "line_delta__INV-A__L2.md").unlink()
        with self.assertRaises(GateFailure):
            validate_report(self.ctx, self.report)

    def test_agent_monetary_field_injection_rejected(self):
        ctx = copy.deepcopy(self.ctx)
        ctx.vars["adjudicate_findings"]["adjudications"][0]["expected_amount"] = 12345.0
        with self.assertRaises(GateFailure):
            validate_report(ctx, self.report)

    def test_deterministic_amount_survives_fake_adjudication_disposition(self):
        """Even if an adjudication's disposition/justification were
        completely different from what a real agent would say, the line's
        billed/expected/delta must come only from Phase 1/3 data -- proven
        by swapping the adjudication content entirely and confirming the
        line's monetary fields are untouched (only disposition/justification
        change, which are the only fields assemble_report ever reads from
        an adjudication)."""
        ctx = copy.deepcopy(self.ctx)
        ctx.vars["adjudicate_findings"]["adjudications"] = [
            _adjudication("line_delta:INV-A:L2", "escalate", clauses=["x.md §9"]),
            _adjudication("ambiguous_line:INV-B:L3", "dispute", clauses=["x.md §9"]),
        ]
        new_report = assemble_report(ctx)
        by_ref = {l["consignment_ref"]: l for l in new_report["lines"]}
        # monetary fields for L2 are identical to the original run despite a
        # completely different disposition being returned by "the agent"
        self.assertEqual(by_ref["L2"]["billed_amount"], 150.0)
        self.assertEqual(by_ref["L2"]["expected_amount"], 120.0)
        self.assertEqual(by_ref["L2"]["delta"], 30.0)
        self.assertEqual(by_ref["L2"]["disposition"], "escalate")  # disposition DID change, as expected
        # L3 stays null/null regardless of the agent's disposition, since
        # Phase 3 never determined an amount for it in the first place
        self.assertIsNone(by_ref["L3"]["expected_amount"])
        self.assertIsNone(by_ref["L3"]["delta"])


class HelperFunctionTests(unittest.TestCase):
    def test_classify_findings_buckets_correctly(self):
        findings = [
            _finding("a", "line_pricing_delta", ["I"], ["R1"]),
            _finding("b", "ambiguous_line", ["I"], ["R2"]),
            _finding("c", "invoice_total_mismatch", ["I"], []),
        ]
        classified = _classify_findings(findings)
        self.assertIn(("I", "R1"), classified["line_level"])
        self.assertIn(("I", "R2"), classified["line_level"])
        self.assertEqual(len(classified["invoice_level"]), 1)

    def test_classify_duplicate_picks_max_billed_as_kept(self):
        f = _finding("dup", "duplicate_billing", ["I1", "I2"], ["R1"], evidence={
            "occurrences": [
                {"invoice": "I1", "consignment_ref": "R1", "billed_amount": 100.0},
                {"invoice": "I2", "consignment_ref": "R1", "billed_amount": 150.0},
            ]
        })
        classified = _classify_findings([f])
        _, kept1 = classified["duplicate_occurrence"][("I1", "R1")]
        _, kept2 = classified["duplicate_occurrence"][("I2", "R1")]
        self.assertFalse(kept1)  # I1 billed less -> it's the excess
        self.assertTrue(kept2)   # I2 billed more -> it's the one kept as payable

    def test_compute_total_in_dispute_only_counts_disputed(self):
        units = [
            {"dispute_unit_id": "u1", "amount": 10.0, "finding_id": "f1", "finding_type": "x"},
            {"dispute_unit_id": "u2", "amount": 20.0, "finding_id": "f2", "finding_type": "x"},
        ]
        adjudications_by_id = {
            "f1": {"disposition": "dispute"},
            "f2": {"disposition": "escalate"},  # not counted -- only "dispute" contributes
        }
        total, counted = _compute_total_in_dispute(units, adjudications_by_id)
        self.assertAlmostEqual(total, 10.0)
        self.assertEqual(counted, ["u1"])


if __name__ == "__main__":
    unittest.main()

"""Tests for Phase 5: adjudication gate, memo assembly, and the
clause/finding-id validator that feeds run_agent_turn's retry loop.

Run with: python3 -m unittest pipeline.tests.test_adjudication -v
"""
import tempfile
import unittest
from pathlib import Path

from pipeline.nodes.adjudication import (
    _assemble_memo, _format_amount, _make_validator, adjudication_gate,
)
from pipeline.runner import Context, GateFailure


def _finding(finding_id="f1", finding_type="line_pricing_delta", requires_adjudication=True,
             dispute_unit_id="du:1", dispute_amount=100.0, delta=100.0,
             affected_invoices=None, affected_consignments=None, related_shipments=None):
    return {
        "finding_id": finding_id, "finding_type": finding_type,
        "affected_invoices": affected_invoices or ["INV"], "affected_consignments": affected_consignments or ["R1"],
        "related_shipments": related_shipments or ["SH-1"], "billed_amounts": [500.0],
        "expected_amount": 400.0, "delta": delta,
        "dispute_unit_id": dispute_unit_id, "dispute_amount": dispute_amount, "related_dispute_unit_id": None,
        "governing_clauses": ["x.md §1"], "evidence": {"x": 1},
        "requires_adjudication": requires_adjudication, "adjudication_reason": "reason",
    }


def _adjudication(finding_id="f1", disposition="dispute", clauses=None):
    return {
        "finding_id": finding_id, "disposition": disposition,
        "justification": "because the contract says so",
        "governing_clauses": clauses if clauses is not None else ["x.md §1"],
        "memo_narrative": {"what_happened": "w", "why": "y", "recommended_action": "r"},
    }


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.contract_path = Path(self.tmpdir.name) / "carrier.md"
        self.contract_path.write_text("1. Clause one.\n2. Clause two.\n3. Clause three.\n")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_valid_finding_id_and_clause_passes(self):
        validate = _make_validator("f1", self.contract_path)
        validate({"finding_id": "f1", "governing_clauses": ["carrier.md §2"]})  # should not raise

    def test_mismatched_finding_id_raises(self):
        validate = _make_validator("f1", self.contract_path)
        with self.assertRaises(ValueError):
            validate({"finding_id": "f2", "governing_clauses": ["carrier.md §1"]})

    def test_nonexistent_clause_number_raises(self):
        validate = _make_validator("f1", self.contract_path)
        with self.assertRaises(ValueError):
            validate({"finding_id": "f1", "governing_clauses": ["carrier.md §99"]})

    def test_wrong_contract_file_raises(self):
        validate = _make_validator("f1", self.contract_path)
        with self.assertRaises(ValueError):
            validate({"finding_id": "f1", "governing_clauses": ["other-carrier.md §1"]})

    def test_malformed_citation_raises(self):
        validate = _make_validator("f1", self.contract_path)
        with self.assertRaises(ValueError):
            validate({"finding_id": "f1", "governing_clauses": ["just some text, no citation"]})

    def test_bare_filename_citation_accepted(self):
        validate = _make_validator("f1", self.contract_path)
        validate({"finding_id": "f1", "governing_clauses": ["carrier.md"]})  # should not raise

    def test_multiple_clauses_all_checked(self):
        validate = _make_validator("f1", self.contract_path)
        validate({"finding_id": "f1", "governing_clauses": ["carrier.md §1, §3"]})  # should not raise
        with self.assertRaises(ValueError):
            validate({"finding_id": "f1", "governing_clauses": ["carrier.md §1, §99"]})


class MemoAssemblyTests(unittest.TestCase):
    def test_memo_uses_deterministic_amount_not_agent_value(self):
        finding = _finding(dispute_amount=1234.56)
        adjudication = _adjudication()
        memo = _assemble_memo(finding, adjudication)
        self.assertIn("Rs 1,234.56", memo)
        self.assertIn(finding["finding_id"], memo)
        self.assertIn(adjudication["memo_narrative"]["why"], memo)

    def test_format_amount_falls_back_to_delta_when_no_dispute_unit(self):
        finding = _finding(dispute_unit_id=None, dispute_amount=None, delta=250.0)
        self.assertEqual(_format_amount(finding), "Rs 250.00")

    def test_format_amount_undeterminable_when_no_amount_at_all(self):
        finding = _finding(dispute_unit_id=None, dispute_amount=None, delta=None)
        self.assertIn("not yet determinable", _format_amount(finding))

    def test_memo_ignores_extraneous_agent_fields(self):
        """Even if the agent's dict somehow carried extra keys (schema
        would already block this upstream), memo assembly must only ever
        read the fixed fields it knows about -- never treat an unknown
        key as a monetary override."""
        finding = _finding(dispute_amount=777.0)
        adjudication = _adjudication()
        adjudication_with_injection = dict(adjudication)
        adjudication_with_injection["expected_amount"] = 1.0  # would-be injection
        adjudication_with_injection["dispute_amount"] = 999999.0
        memo = _assemble_memo(finding, adjudication_with_injection)
        self.assertIn("Rs 777.00", memo)
        self.assertNotIn("999999", memo)
        self.assertNotIn("1.0", memo.split("Amount at issue")[0])  # no stray injected value leaked into header area


class AdjudicationGateTests(unittest.TestCase):
    def _ctx(self, findings, priced_lines=None):
        ctx = Context(run_dir=None)
        ctx.vars["detect_findings"] = {"findings": findings}
        ctx.vars["parse_invoices"] = {"invoices": [{"invoice": "INV", "carrier": "alpine"}]}
        ctx.vars["price_lines"] = {"priced_lines": priced_lines or []}
        return ctx

    def test_valid_accept_passes(self):
        ctx = self._ctx([_finding("f1")])
        output = {"adjudications": [_adjudication("f1", "accept", clauses=["alpine-express.md §1"])],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 1, "dispute": 0, "escalate": 0}}}
        adjudication_gate(ctx, output)  # should not raise

    def test_valid_dispute_passes(self):
        ctx = self._ctx([_finding("f1")])
        output = {"adjudications": [_adjudication("f1", "dispute", clauses=["alpine-express.md §2"])],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 0, "dispute": 1, "escalate": 0}}}
        adjudication_gate(ctx, output)  # should not raise

    def test_valid_escalate_passes(self):
        ctx = self._ctx([_finding("f1")])
        output = {"adjudications": [_adjudication("f1", "escalate", clauses=["alpine-express.md §2"])],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 0, "dispute": 0, "escalate": 1}}}
        adjudication_gate(ctx, output)  # should not raise

    def test_invalid_disposition_rejected(self):
        ctx = self._ctx([_finding("f1")])
        output = {"adjudications": [_adjudication("f1", "maybe")],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 0, "dispute": 0, "escalate": 0}}}
        with self.assertRaises(GateFailure):
            adjudication_gate(ctx, output)

    def test_missing_adjudication_result_rejected(self):
        ctx = self._ctx([_finding("f1"), _finding("f2")])
        output = {"adjudications": [_adjudication("f1", "dispute")],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 0, "dispute": 1, "escalate": 0}}}
        with self.assertRaises(GateFailure):
            adjudication_gate(ctx, output)

    def test_result_for_unknown_finding_id_rejected(self):
        ctx = self._ctx([_finding("f1")])
        output = {"adjudications": [_adjudication("f1", "dispute"), _adjudication("f_ghost", "dispute")],
                  "summary": {"adjudicated_count": 2, "by_disposition": {"accept": 0, "dispute": 2, "escalate": 0}}}
        with self.assertRaises(GateFailure):
            adjudication_gate(ctx, output)

    def test_duplicate_result_for_same_finding_rejected(self):
        ctx = self._ctx([_finding("f1")])
        output = {"adjudications": [_adjudication("f1", "dispute"), _adjudication("f1", "escalate")],
                  "summary": {"adjudicated_count": 2, "by_disposition": {"accept": 0, "dispute": 1, "escalate": 1}}}
        with self.assertRaises(GateFailure):
            adjudication_gate(ctx, output)

    def test_invalid_clause_citation_rejected(self):
        # carrier is 'alpine' per invoices_by_id -> contract file alpine-express.md
        ctx = self._ctx([_finding("f1")])
        output = {"adjudications": [_adjudication("f1", "dispute", clauses=["alpine-express.md §999"])],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 0, "dispute": 1, "escalate": 0}}}
        with self.assertRaises(GateFailure):
            adjudication_gate(ctx, output)

    def test_monetary_field_injection_rejected(self):
        ctx = self._ctx([_finding("f1")])
        injected = _adjudication("f1", "dispute")
        injected["expected_amount"] = 123.45  # forbidden field
        output = {"adjudications": [injected],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 0, "dispute": 1, "escalate": 0}}}
        with self.assertRaises(GateFailure):
            adjudication_gate(ctx, output)

    def test_dispute_unit_amount_field_injection_rejected(self):
        """Even a field NAMED like dispute-unit bookkeeping must be
        rejected -- the agent has no legitimate field for it at all."""
        ctx = self._ctx([_finding("f1")])
        injected = _adjudication("f1", "dispute")
        injected["dispute_unit_amount"] = 999.0
        output = {"adjudications": [injected],
                  "summary": {"adjudicated_count": 1, "by_disposition": {"accept": 0, "dispute": 1, "escalate": 0}}}
        with self.assertRaises(GateFailure):
            adjudication_gate(ctx, output)

    def test_findings_not_requiring_adjudication_are_not_required(self):
        ctx = self._ctx([_finding("f1", requires_adjudication=False)])
        output = {"adjudications": [], "summary": {"adjudicated_count": 0, "by_disposition": {"accept": 0, "dispute": 0, "escalate": 0}}}
        adjudication_gate(ctx, output)  # should not raise -- nothing was required


if __name__ == "__main__":
    unittest.main()

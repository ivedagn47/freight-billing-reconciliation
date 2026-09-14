"""Tests for the Phase 1 parsers, matching, duplicate detection, and the
line-conservation gate. Plain stdlib unittest -- no external test
dependency, consistent with the rest of this pipeline.

Run with: python3 -m unittest pipeline.tests.test_phase1 -v
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.errors import ParseError
from pipeline.nodes.discover import _scope_decision
from pipeline.nodes.match import GateFailure, line_conservation_gate, load_shipments, match_and_normalise
from pipeline.parsers import alpine, falcon, sagar
from pipeline.runner import Context


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content)
    return p


class AlpineParserTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_happy_path(self):
        path = _write(self.tmp, "ALPINE-TEST.json", json.dumps({
            "carrier": "Alpine Express Logistics",
            "customer": "BlueFin Commerce",
            "invoice_no": "ALPINE-TEST",
            "billing_period": "2026-07",
            "consignment_count": 1,
            "lines": [{
                "sl": 1, "consignment_no": "AE-9001", "booking_date": "2026-07-01",
                "actual_weight_kg": 30, "chargeable_weight_kg": 30, "rate_per_kg": 9.5,
                "handling_fee": 150, "line_amount": 435.0,
            }],
            "discount": 0.0, "invoice_total": 435.0,
        }))
        info = alpine.sniff(path)
        self.assertEqual(info, {"carrier": "alpine", "doc_type": "invoice",
                                 "invoice_id": "ALPINE-TEST", "period": "2026-07",
                                 "period_ambiguous": False})

        lines, header, residue = alpine.parse_lines(path)
        self.assertEqual(residue, [])
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["consignment_ref"], "AE-9001")
        self.assertEqual(line["billed_amount"], 435.0)
        self.assertEqual(line["billed_components"], [{"label": "handling_fee", "amount": 150.0}])
        self.assertEqual(header["declared_line_count"], 1)
        self.assertEqual(header["declared_total"], 435.0)

    def test_missing_top_level_field_raises(self):
        path = _write(self.tmp, "bad.json", json.dumps({"carrier": "Alpine Express", "lines": []}))
        with self.assertRaises(ParseError):
            alpine.sniff(path)

    def test_line_missing_field_becomes_residue_not_dropped(self):
        path = _write(self.tmp, "ALPINE-BAD.json", json.dumps({
            "carrier": "Alpine Express Logistics", "invoice_no": "ALPINE-BAD",
            "billing_period": "2026-07", "consignment_count": 1,
            "lines": [{"sl": 1, "consignment_no": "AE-9002"}],  # missing required fields
            "discount": 0.0, "invoice_total": 0.0,
        }))
        lines, header, residue = alpine.parse_lines(path)
        self.assertEqual(lines, [])
        self.assertEqual(len(residue), 1)
        self.assertIn("AE-9002", str(residue[0]["raw"]))


class FalconParserTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_happy_path_with_accessorial(self):
        text = (
            "FALCON FREIGHT PVT LTD\n"
            "Servicing BlueFin Commerce under agreement FF/BFC/2024-11\n"
            "TAX INVOICE FALCON-TEST    Period: 1-14 2026-07\n"
            "================================================================\n"
            "\n"
            "1. Consignment FF-9001\n"
            "   Mumbai to Pune, 150 km, 900 kg, standard\n"
            "   Residential delivery: Rs 250.00\n"
            "   Freight incl. fuel surcharge: Rs 4,032.00\n"
            "   LINE TOTAL: Rs 4,282.00\n"
            "\n"
            "================================================================\n"
            "INVOICE TOTAL: Rs 4,282.00\n"
            "Payment due 30 days from invoice date. E&OE.\n"
        )
        path = _write(self.tmp, "FALCON-TEST.txt", text)
        info = falcon.sniff(path)
        self.assertEqual(info["doc_type"], "invoice")
        self.assertEqual(info["invoice_id"], "FALCON-TEST")
        self.assertEqual(info["period"], "2026-07")
        self.assertEqual(info["carrier"], "falcon")

        lines, header, residue = falcon.parse_lines(path)
        self.assertEqual(residue, [])
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["consignment_ref"], "FF-9001")
        self.assertEqual(line["billed_amount"], 4282.00)
        labels = {c["label"] for c in line["billed_components"]}
        self.assertEqual(labels, {"Residential delivery", "Freight incl. fuel surcharge"})
        self.assertEqual(line["stated_attributes"]["service_level"], "standard")
        self.assertEqual(header["declared_total"], 4282.00)

    def test_credit_note_classified_and_excluded(self):
        text = (
            "FALCON FREIGHT PVT LTD\n"
            "CREDIT NOTE FALCON-CN-TEST    Date: 2026-08-01\n"
            "Against: TAX INVOICE FALCON-TEST\n"
            "================================================================\n"
            "\n"
            "1. Consignment FF-9001\n"
            "   Correction: some note.\n"
            "   LINE TOTAL: Rs -100.00\n"
            "\n"
            "================================================================\n"
            "CREDIT NOTE TOTAL: Rs -100.00\n"
        )
        path = _write(self.tmp, "FALCON-CN-TEST.txt", text)
        info = falcon.sniff(path)
        self.assertEqual(info["doc_type"], "credit_note")
        in_scope, reason = _scope_decision(info)
        self.assertFalse(in_scope)
        self.assertIn("credit_note", reason)

    def test_unrecognized_charge_line_becomes_residue(self):
        text = (
            "FALCON FREIGHT PVT LTD\n"
            "TAX INVOICE FALCON-TEST2    Period: 1-14 2026-07\n"
            "================================================================\n"
            "\n"
            "1. Consignment FF-9002\n"
            "   Mumbai to Pune, 150 km, 900 kg, standard\n"
            "   Some totally unexpected line with no colon-amount shape\n"
            "   LINE TOTAL: Rs 100.00\n"
            "\n"
            "================================================================\n"
            "INVOICE TOTAL: Rs 100.00\n"
        )
        path = _write(self.tmp, "FALCON-TEST2.txt", text)
        lines, header, residue = falcon.parse_lines(path)
        self.assertEqual(lines, [])
        self.assertEqual(len(residue), 1)
        self.assertIn("FF-9002", residue[0]["locator"])

    def test_missing_line_total_becomes_residue(self):
        text = (
            "FALCON FREIGHT PVT LTD\n"
            "TAX INVOICE FALCON-TEST3    Period: 1-14 2026-07\n"
            "================================================================\n"
            "\n"
            "1. Consignment FF-9003\n"
            "   Mumbai to Pune, 150 km, 900 kg, standard\n"
            "\n"
            "================================================================\n"
            "INVOICE TOTAL: Rs 0.00\n"
        )
        path = _write(self.tmp, "FALCON-TEST3.txt", text)
        lines, header, residue = falcon.parse_lines(path)
        self.assertEqual(lines, [])
        self.assertEqual(len(residue), 1)
        self.assertIn("no LINE TOTAL", residue[0]["reason"])


class SagarParserTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_happy_path_invoice(self):
        csv_text = (
            "cnote_no,booking_dt,wt_kg,dist_km,freight_rs,chill_prem_rs,total_rs\n"
            "SG-9001,2026-07-02,260,60,1680.00,0.00,1680.00\n"
            "SG-9002,2026-07-03,200,110,1420.00,284.00,1704.00\n"
            "TOTAL,,,,,,3384.00\n"
        )
        path = _write(self.tmp, "SAGAR-JUL-TEST.csv", csv_text)
        info = sagar.sniff(path)
        self.assertEqual(info, {"carrier": "sagar", "doc_type": "invoice",
                                 "invoice_id": "SAGAR-JUL-TEST", "period": "2026-07",
                                 "period_ambiguous": False})
        lines, header, residue = sagar.parse_lines(path)
        self.assertEqual(residue, [])
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["billed_components"], [{"label": "freight_rs", "amount": 1680.0}])
        self.assertEqual(
            lines[1]["billed_components"],
            [{"label": "freight_rs", "amount": 1420.0}, {"label": "chill_prem_rs", "amount": 284.0}],
        )
        self.assertEqual(header["declared_total"], 3384.00)

    def test_credit_note_header_classified(self):
        csv_text = "credit_note,against_invoice,cnote_no,credit_rs\nSAGAR-CN-TEST,SAGAR-JUL-TEST,SG-9001,-50.00\nTOTAL,,,-50.00\n"
        path = _write(self.tmp, "SAGAR-CN-TEST.csv", csv_text)
        info = sagar.sniff(path)
        self.assertEqual(info["doc_type"], "credit_note")

    def test_unrecognized_header_raises(self):
        path = _write(self.tmp, "SAGAR-BAD.csv", "totally,wrong,columns\n1,2,3\n")
        with self.assertRaises(ParseError):
            sagar.sniff(path)

    def test_ambiguous_period_across_two_months(self):
        csv_text = (
            "cnote_no,booking_dt,wt_kg,dist_km,freight_rs,chill_prem_rs,total_rs\n"
            "SG-9001,2026-07-30,260,60,1680.00,0.00,1680.00\n"
            "SG-9002,2026-08-01,200,110,1420.00,0.00,1420.00\n"
            "TOTAL,,,,,,3100.00\n"
        )
        path = _write(self.tmp, "SAGAR-STRADDLE.csv", csv_text)
        info = sagar.sniff(path)
        self.assertTrue(info["period_ambiguous"])
        self.assertIsNone(info["period"])
        in_scope, reason = _scope_decision(info)
        self.assertFalse(in_scope)

    def test_malformed_row_becomes_residue(self):
        csv_text = (
            "cnote_no,booking_dt,wt_kg,dist_km,freight_rs,chill_prem_rs,total_rs\n"
            "SG-9001,2026-07-02,not-a-number,60,1680.00,0.00,1680.00\n"
            "TOTAL,,,,,,1680.00\n"
        )
        path = _write(self.tmp, "SAGAR-BADROW.csv", csv_text)
        lines, header, residue = sagar.parse_lines(path)
        self.assertEqual(lines, [])
        self.assertEqual(len(residue), 1)


class ScopeDecisionTests(unittest.TestCase):
    def test_matching_period_in_scope(self):
        in_scope, _ = _scope_decision({"doc_type": "invoice", "period": "2026-07", "period_ambiguous": False})
        self.assertTrue(in_scope)

    def test_different_period_excluded(self):
        in_scope, reason = _scope_decision({"doc_type": "invoice", "period": "2026-08", "period_ambiguous": False})
        self.assertFalse(in_scope)
        self.assertIn("2026-08", reason)

    def test_credit_note_always_excluded_even_if_period_matches(self):
        in_scope, reason = _scope_decision({"doc_type": "credit_note", "period": "2026-07", "period_ambiguous": False})
        self.assertFalse(in_scope)


def _fake_line(invoice, ref, amount=100.0, locator="loc"):
    return {
        "invoice": invoice, "carrier": "testcarrier", "consignment_ref": ref,
        "billed_amount": amount, "billed_components": [],
        "stated_attributes": {}, "provenance": {"file": "f.txt", "locator": locator},
        "raw": {},
    }


class MatchingTests(unittest.TestCase):
    def _ctx(self, lines):
        ctx = Context(run_dir=Path("/tmp"))
        ctx.vars["parse_invoices"] = {"invoices": [], "lines": lines}
        ctx.vars["load_shipments"] = {"by_ref": {
            "REF-1": {"shipment_id": "SH-1", "carrier": "testcarrier"},
        }}
        return ctx

    def test_matched_and_unmatched(self):
        ctx = self._ctx([_fake_line("INV-A", "REF-1"), _fake_line("INV-A", "REF-2")])
        out = match_and_normalise(ctx)
        by_ref = {l["consignment_ref"]: l for l in out["matched_lines"]}
        self.assertEqual(by_ref["REF-1"]["shipment_id"], "SH-1")
        self.assertTrue(by_ref["REF-1"]["matched"])
        self.assertIsNone(by_ref["REF-2"]["shipment_id"])
        self.assertFalse(by_ref["REF-2"]["matched"])
        self.assertEqual(out["summary"]["matched_count"], 1)
        self.assertEqual(out["summary"]["unmatched_count"], 1)
        line_conservation_gate(ctx, out)  # should not raise

    def test_cross_invoice_duplicate_detected(self):
        ctx = self._ctx([
            _fake_line("INV-A", "REF-1", locator="a-loc"),
            _fake_line("INV-B", "REF-1", locator="b-loc"),
        ])
        out = match_and_normalise(ctx)
        self.assertEqual(len(out["duplicate_groups"]), 1)
        group = out["duplicate_groups"][0]
        self.assertEqual(group["consignment_ref"], "REF-1")
        self.assertEqual(len(group["occurrences"]), 2)
        self.assertTrue(all(l["is_duplicate"] for l in out["matched_lines"]))
        self.assertEqual(len({l["duplicate_group_id"] for l in out["matched_lines"]}), 1)
        # both duplicate lines are still individually matched -- duplication
        # is a separate concern from whether a line matches a real shipment
        for l in out["matched_lines"]:
            self.assertEqual(l["shipment_id"], "SH-1")
        line_conservation_gate(ctx, out)  # should not raise

    def test_gate_catches_vanished_line(self):
        ctx = self._ctx([_fake_line("INV-A", "REF-1"), _fake_line("INV-A", "REF-2")])
        out = match_and_normalise(ctx)
        out["matched_lines"].pop()  # simulate a line silently disappearing
        with self.assertRaises(GateFailure):
            line_conservation_gate(ctx, out)

    def test_gate_catches_matched_shipment_id_inconsistency(self):
        ctx = self._ctx([_fake_line("INV-A", "REF-1")])
        out = match_and_normalise(ctx)
        out["matched_lines"][0]["shipment_id"] = None  # matched=True but no shipment_id
        with self.assertRaises(GateFailure):
            line_conservation_gate(ctx, out)

    def test_gate_catches_literal_source_duplicate(self):
        ctx = self._ctx([_fake_line("INV-A", "REF-1", locator="same")])
        out = match_and_normalise(ctx)
        out["matched_lines"].append(dict(out["matched_lines"][0]))  # same source record twice
        with self.assertRaises(GateFailure):
            line_conservation_gate(ctx, out)

    def test_duplicate_shipment_refs_raise(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "shipments.json"
            path.write_text(json.dumps([
                {"shipment_id": "SH-1", "carrier_consignment_ref": "REF-1"},
                {"shipment_id": "SH-2", "carrier_consignment_ref": "REF-1"},
            ]))
            ctx = Context(run_dir=Path(d))
            with self.assertRaises(RuntimeError):
                load_shipments(ctx, path=path)


if __name__ == "__main__":
    unittest.main()

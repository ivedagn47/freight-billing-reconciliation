"""Tests that verify_ratecard actually rejects bad rate cards, not just
accepts good ones. Uses the real committed rate cards + real contracts as
the happy-path fixtures (this pipeline's own Phase 2 output), and
deliberately corrupted copies for the negative cases.

Run with: python3 -m unittest pipeline.tests.test_ratecard_verify -v
"""
import copy
import json
import unittest
from pathlib import Path

from pipeline.nodes.ratecard_verify import verify_ratecard
from pipeline.runner import GateFailure

RATECARDS_DIR = Path("ratecards")
CONTRACTS = {
    "alpine": Path("data/contracts/alpine-express.md"),
    "falcon": Path("data/contracts/falcon-freight.md"),
    "sagar": Path("data/contracts/sagar-roadlines.md"),
}


def _load(carrier: str) -> dict:
    return json.loads((RATECARDS_DIR / f"{carrier}.json").read_text())


@unittest.skipUnless(all((RATECARDS_DIR / f"{c}.json").exists() for c in CONTRACTS), "Phase 2 not yet run")
class RealRatecardsPassTests(unittest.TestCase):
    def test_all_three_real_ratecards_pass_verification(self):
        for carrier, contract_path in CONTRACTS.items():
            with self.subTest(carrier=carrier):
                ratecard = _load(carrier)
                verify_ratecard(ratecard, contract_path, expected_carrier=carrier)  # must not raise


@unittest.skipUnless((RATECARDS_DIR / "alpine.json").exists(), "Phase 2 not yet run")
class NegativeCaseTests(unittest.TestCase):
    def setUp(self):
        self.alpine = _load("alpine")
        self.alpine_path = CONTRACTS["alpine"]

    def test_hash_mismatch_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["source_contract"]["sha256"] = "0" * 64
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_wrong_carrier_label_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["carrier"] = "falcon"
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_nonexistent_clause_number_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["pricing_basis"]["clause"] = "alpine-express.md §99"
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_clause_citing_wrong_file_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["pricing_basis"]["clause"] = "falcon-freight.md §1"
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_overlapping_bands_rejected(self):
        rc = copy.deepcopy(self.alpine)
        # widen both bands so they overlap between 40 and 50 -- a real
        # contradiction, unlike the genuine strict-inequality gap
        rc["rate_bands"][0]["max"], rc["rate_bands"][0]["max_op"] = 60, "<"
        rc["rate_bands"][1]["min"], rc["rate_bands"][1]["min_op"] = 40, ">"
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_undeclared_gap_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["ambiguities"] = []  # strip the agent's own gap disclosure
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_percentage_rate_out_of_range_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["discounts"][0]["rate"] = 5.0  # 500%, not a fraction
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_flat_fee_with_rate_also_set_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["accessorial_charges"][0]["rate"] = 0.5  # flat_fee shouldn't also carry a rate
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_negative_flat_fee_rejected(self):
        rc = copy.deepcopy(self.alpine)
        rc["accessorial_charges"][0]["amount"] = -150.0
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, self.alpine_path, expected_carrier="alpine")

    def test_unknown_compounds_on_name_rejected(self):
        falcon = _load("falcon")
        rc = copy.deepcopy(falcon)
        rc["surcharges"][0]["compounds_on"] = ["base_freight", "some_nonexistent_rule"]
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, CONTRACTS["falcon"], expected_carrier="falcon")

    def test_empty_whitelist_rejected(self):
        falcon = _load("falcon")
        rc = copy.deepcopy(falcon)
        rc["accessorial_policy"]["allowed_names"] = []
        with self.assertRaises(GateFailure):
            verify_ratecard(rc, CONTRACTS["falcon"], expected_carrier="falcon")


if __name__ == "__main__":
    unittest.main()

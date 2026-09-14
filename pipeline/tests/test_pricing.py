"""Unit tests for the deterministic pricing engine: contract boundaries,
compounding order, decimal precision, and the ambiguous/determined/
out_of_scope trichotomy. All fixtures are small synthetic rate cards
built in the same shape Phase 2 produces (not the real committed rate
cards -- these tests must keep passing even if a future contract edit
changes the real cards), so no specific consignment from the real data
is referenced here.

Run with: python3 -m unittest pipeline.tests.test_pricing -v
"""
import unittest

from pipeline.nodes.pricing import price_line, pricing_gate
from pipeline.runner import Context, GateFailure


def _empty_ratecard(**overrides) -> dict:
    base = {
        "chargeable_weight_floor": None,
        "rate_bands": [], "flat_rates": [],
        "service_level_rules": [], "premiums": [], "surcharges": [],
        "accessorial_charges": [], "exclusions": [],
        "pricing_basis": {"description": "test", "clause": "test.md §1"},
    }
    base.update(overrides)
    return base


def _shipment(**overrides) -> dict:
    base = {
        "billed_weight_kg": 100, "distance_km": 100,
        "service_level": "standard", "special_handling": [],
    }
    base.update(overrides)
    return base


def _band(name, banded_on, min_=None, min_op=None, max_=None, max_op=None, rate=1, rate_unit="per_kg", clause="test.md §1"):
    return {"name": name, "banded_on": banded_on, "min": min_, "min_op": min_op,
            "max": max_, "max_op": max_op, "rate": rate, "rate_unit": rate_unit, "clause": clause}


def _flat(name, basis, rate, rate_unit, clause="test.md §1"):
    return {"name": name, "basis": basis, "rate": rate, "rate_unit": rate_unit, "clause": clause}


def _rule(name, type_, rate=None, amount=None, trigger=None, compounds_on=None, clause="test.md §1"):
    return {"name": name, "type": type_, "rate": rate, "amount": amount,
            "condition": "test", "trigger": trigger,
            "compounds_on": compounds_on if compounds_on is not None else ["base_freight"], "clause": clause}


def _accessorial(name, type_, rate=None, amount=None, trigger=None, clause="test.md §1"):
    return {"name": name, "type": type_, "rate": rate, "amount": amount,
            "condition": "test", "trigger": trigger, "clause": clause}


class AlpineStyleTests(unittest.TestCase):
    """Weight-banded per-kg with a chargeable-weight floor."""

    def _alpine_ratecard(self):
        return _empty_ratecard(
            chargeable_weight_floor={"minimum_kg": 25, "clause": "alpine.md §1"},
            rate_bands=[
                _band("under_50", "weight_kg", max_=50, max_op="<", rate="9.5", clause="alpine.md §2"),
                _band("over_50", "weight_kg", min_=50, min_op=">", rate="8.25", clause="alpine.md §2"),
            ],
            accessorial_charges=[
                _accessorial("fragile", "flat_fee", amount="150",
                              trigger={"attribute": "special_handling", "op": "contains", "value": "fragile"},
                              clause="alpine.md §4"),
            ],
        )

    def test_minimum_chargeable_weight_floor(self):
        rc = self._alpine_ratecard()
        result = price_line(_shipment(billed_weight_kg=18), rc)
        self.assertEqual(result["pricing_outcome"], "determined")
        # floor to 25kg, under-50 band @ 9.5/kg -> 237.50
        self.assertAlmostEqual(result["expected_amount"], 237.50)
        self.assertEqual(result["trace"]["chargeable_weight_floor_applied"]["chargeable_kg"], "25")

    def test_boundary_exactly_50kg_is_ambiguous_not_guessed(self):
        rc = self._alpine_ratecard()
        result = price_line(_shipment(billed_weight_kg=50), rc)
        self.assertEqual(result["pricing_outcome"], "ambiguous")
        self.assertIsNone(result["expected_amount"])
        self.assertIn("50", result["reason"])

    def test_just_under_50_uses_under_band(self):
        rc = self._alpine_ratecard()
        result = price_line(_shipment(billed_weight_kg=49.99), rc)
        self.assertEqual(result["pricing_outcome"], "determined")
        self.assertAlmostEqual(result["expected_amount"], round(49.99 * 9.5, 2))

    def test_just_over_50_uses_over_band(self):
        rc = self._alpine_ratecard()
        result = price_line(_shipment(billed_weight_kg=50.01), rc)
        self.assertEqual(result["pricing_outcome"], "determined")
        self.assertAlmostEqual(result["expected_amount"], round(50.01 * 8.25, 2))

    def test_fragile_accessorial_added(self):
        rc = self._alpine_ratecard()
        result = price_line(_shipment(billed_weight_kg=30, special_handling=["fragile"]), rc)
        # floor doesn't apply (30 > 25), under-50 band: 30*9.5=285, + 150 fragile = 435
        self.assertAlmostEqual(result["expected_amount"], 435.0)
        fired = [a for a in result["trace"]["accessorials_applied"] if a["fired"]]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["name"], "fragile")

    def test_fragile_accessorial_not_added_when_absent(self):
        rc = self._alpine_ratecard()
        result = price_line(_shipment(billed_weight_kg=30, special_handling=[]), rc)
        self.assertAlmostEqual(result["expected_amount"], 285.0)

    def test_zero_weight_floors_to_minimum(self):
        rc = self._alpine_ratecard()
        result = price_line(_shipment(billed_weight_kg=0), rc)
        self.assertEqual(result["pricing_outcome"], "determined")
        self.assertAlmostEqual(result["expected_amount"], 25 * 9.5)


class FalconStyleTests(unittest.TestCase):
    """Weight-banded per-km, with a compounding express premium + fuel surcharge."""

    def _falcon_ratecard(self):
        return _empty_ratecard(
            rate_bands=[
                _band("le_500", "weight_kg", max_=500, max_op="<=", rate="18", rate_unit="per_km", clause="falcon.md §1"),
                _band("ge_501", "weight_kg", min_=501, min_op=">=", max_=2000, max_op="<=", rate="24", rate_unit="per_km", clause="falcon.md §1"),
            ],
            premiums=[
                _rule("express_premium", "percentage", rate="0.15",
                      trigger={"attribute": "service_level", "op": "eq", "value": "express"},
                      compounds_on=["base_freight"], clause="falcon.md §2"),
            ],
            surcharges=[
                _rule("fuel_surcharge", "percentage", rate="0.12", trigger=None,
                      compounds_on=["base_freight", "express_premium"], clause="falcon.md §3"),
            ],
            accessorial_charges=[
                _accessorial("residential", "flat_fee", amount="250",
                              trigger={"attribute": "special_handling", "op": "contains", "value": "residential"},
                              clause="falcon.md §4"),
            ],
            exclusions=[
                {"condition": "weight>2000", "description": "outside rate card", "clause": "falcon.md §1",
                 "trigger": {"attribute": "billed_weight_kg", "op": "gt", "value": 2000}},
            ],
        )

    def test_le_500_and_ge_501_boundaries(self):
        rc = self._falcon_ratecard()
        r500 = price_line(_shipment(billed_weight_kg=500, distance_km=100), rc)
        r501 = price_line(_shipment(billed_weight_kg=501, distance_km=100), rc)
        self.assertEqual(r500["pricing_outcome"], "determined")
        self.assertEqual(r501["pricing_outcome"], "determined")
        # both bands have a fuel surcharge (12%, unconditional), no express premium
        self.assertAlmostEqual(r500["expected_amount"], round(100 * 18 * 1.12, 2))
        self.assertAlmostEqual(r501["expected_amount"], round(100 * 24 * 1.12, 2))

    def test_fractional_weight_between_bands_is_ambiguous(self):
        rc = self._falcon_ratecard()
        result = price_line(_shipment(billed_weight_kg=500.5, distance_km=100), rc)
        self.assertEqual(result["pricing_outcome"], "ambiguous")
        self.assertIsNone(result["expected_amount"])

    def test_express_premium_then_fuel_surcharge_compounding_order(self):
        rc = self._falcon_ratecard()
        result = price_line(_shipment(billed_weight_kg=120, distance_km=610, service_level="express"), rc)
        self.assertEqual(result["pricing_outcome"], "determined")
        base = 120 * 0 + 610 * 18  # le_500 band, rate 18/km
        premium = base * 0.15
        surcharge = (base + premium) * 0.12  # surcharge compounds on base+premium, NOT base alone
        expected = round(base + premium + surcharge, 2)
        self.assertAlmostEqual(result["expected_amount"], expected)
        # sanity: surcharge must differ from a (wrongly) base-only surcharge
        wrong_surcharge_total = round(base + premium + base * 0.12, 2)
        self.assertNotAlmostEqual(result["expected_amount"], wrong_surcharge_total)

    def test_surcharge_applies_even_without_express(self):
        rc = self._falcon_ratecard()
        result = price_line(_shipment(billed_weight_kg=120, distance_km=610, service_level="standard"), rc)
        base = 610 * 18
        expected = round(base * 1.12, 2)
        self.assertAlmostEqual(result["expected_amount"], expected)
        premiums = result["trace"]["premiums_and_surcharges_applied"]
        express_entry = next(p for p in premiums if p["name"] == "express_premium")
        self.assertFalse(express_entry["fired"])
        surcharge_entry = next(p for p in premiums if p["name"] == "fuel_surcharge")
        self.assertTrue(surcharge_entry["fired"])

    def test_residential_accessorial(self):
        rc = self._falcon_ratecard()
        result = price_line(_shipment(billed_weight_kg=120, distance_km=100, special_handling=["residential"]), rc)
        base = 100 * 18
        expected = round(base * 1.12 + 250, 2)
        self.assertAlmostEqual(result["expected_amount"], expected)

    def test_exclusion_above_2000kg_is_out_of_scope(self):
        rc = self._falcon_ratecard()
        result = price_line(_shipment(billed_weight_kg=2500, distance_km=100), rc)
        self.assertEqual(result["pricing_outcome"], "out_of_scope")
        self.assertIsNone(result["expected_amount"])

    def test_zero_distance_gives_zero_base_freight_not_a_crash(self):
        rc = self._falcon_ratecard()
        result = price_line(_shipment(billed_weight_kg=120, distance_km=0), rc)
        self.assertEqual(result["pricing_outcome"], "determined")
        self.assertAlmostEqual(result["expected_amount"], 0.0)


class SagarStyleTests(unittest.TestCase):
    """Flat per-kg + per-km summed, with a compounding cold-chain premium."""

    def _sagar_ratecard(self):
        return _empty_ratecard(
            flat_rates=[
                _flat("weight_component", "weight_kg", "6", "per_kg", clause="sagar.md §1"),
                _flat("distance_component", "distance_km", "2", "per_km", clause="sagar.md §1"),
            ],
            premiums=[
                _rule("cold_chain_premium", "percentage", rate="0.2",
                      trigger={"attribute": "special_handling", "op": "contains", "value": "cold_chain"},
                      compounds_on=["base_freight"], clause="sagar.md §3"),
            ],
        )

    def test_base_freight_is_weight_plus_distance_component(self):
        rc = self._sagar_ratecard()
        result = price_line(_shipment(billed_weight_kg=260, distance_km=60), rc)
        expected = round(260 * 6 + 60 * 2, 2)
        self.assertAlmostEqual(result["expected_amount"], expected)
        self.assertEqual(len(result["trace"]["base_freight"]["components"]), 2)

    def test_cold_chain_premium(self):
        rc = self._sagar_ratecard()
        result = price_line(_shipment(billed_weight_kg=200, distance_km=110, special_handling=["cold_chain"]), rc)
        base = 200 * 6 + 110 * 2
        expected = round(base * 1.2, 2)
        self.assertAlmostEqual(result["expected_amount"], expected)

    def test_no_cold_chain_no_premium(self):
        rc = self._sagar_ratecard()
        result = price_line(_shipment(billed_weight_kg=200, distance_km=110, special_handling=[]), rc)
        base = 200 * 6 + 110 * 2
        self.assertAlmostEqual(result["expected_amount"], base)


class GenericEngineTests(unittest.TestCase):
    def test_no_rate_bands_or_flat_rates_is_ambiguous(self):
        rc = _empty_ratecard()  # neither mechanism defined
        result = price_line(_shipment(), rc)
        self.assertEqual(result["pricing_outcome"], "ambiguous")
        self.assertIsNone(result["expected_amount"])
        self.assertIn("no mechanism", result["reason"])

    def test_service_level_explicit_denial_is_ambiguous(self):
        rc = _empty_ratecard(
            rate_bands=[_band("only_band", "weight_kg", rate="1")],
            service_level_rules=[
                {"service_level": "express", "allowed": False, "clause": "alpine.md §3", "note": "not offered"},
            ],
        )
        result = price_line(_shipment(service_level="express"), rc)
        self.assertEqual(result["pricing_outcome"], "ambiguous")
        self.assertIsNone(result["expected_amount"])

    def test_service_level_not_mentioned_is_priced_normally(self):
        rc = _empty_ratecard(
            rate_bands=[_band("only_band", "weight_kg", rate="1")],
            service_level_rules=[
                {"service_level": "standard", "allowed": True, "clause": "x.md §1", "note": ""},
            ],
        )
        result = price_line(_shipment(billed_weight_kg=100, service_level="express"), rc)
        self.assertEqual(result["pricing_outcome"], "determined")

    def test_exclusion_with_null_trigger_never_auto_fires(self):
        """An exclusion the agent couldn't express as a trigger (e.g. a
        matching-outcome-dependent rule) must never silently exclude
        every line -- only explicitly-triggered exclusions fire."""
        rc = _empty_ratecard(
            rate_bands=[_band("only_band", "weight_kg", rate="1")],
            exclusions=[{"condition": "cannot be matched to a booking", "description": "not payable",
                         "clause": "sagar.md §5", "trigger": None}],
        )
        result = price_line(_shipment(billed_weight_kg=100), rc)
        self.assertEqual(result["pricing_outcome"], "determined")

    def test_rounding_half_up_to_nearest_paisa(self):
        rc = _empty_ratecard(rate_bands=[_band("b", "weight_kg", rate="8.25")])
        result = price_line(_shipment(billed_weight_kg=71.5), rc)
        # 71.5 * 8.25 = 589.875 exactly -- a genuine half-paisa tie
        self.assertEqual(result["trace"]["total_before_rounding"], "589.875")
        self.assertAlmostEqual(result["expected_amount"], 589.88)

    def test_decimal_precision_no_float_drift_across_many_lines(self):
        """0.1 + 0.2 != 0.3 in binary float; the engine must not
        accumulate that kind of error across repeated percentage
        application."""
        rc = _empty_ratecard(
            rate_bands=[_band("b", "weight_kg", rate="0.1")],
            surcharges=[_rule("s1", "percentage", rate="0.1", trigger=None, compounds_on=["base_freight"])],
        )
        result = price_line(_shipment(billed_weight_kg=3), rc)
        # base = 0.3 exactly; surcharge = 0.3*0.1 = 0.03; total = 0.33 exactly
        self.assertEqual(result["trace"]["total_before_rounding"], "0.33")
        self.assertAlmostEqual(result["expected_amount"], 0.33)

    def test_determined_line_carries_governing_clauses(self):
        rc = _empty_ratecard(rate_bands=[_band("b", "weight_kg", rate="1", clause="x.md §7")])
        result = price_line(_shipment(billed_weight_kg=10), rc)
        self.assertIn("x.md §7", result["clauses"])

    def test_unmatched_dependency_raises_hard_error_not_silent_skip(self):
        rc = _empty_ratecard(
            rate_bands=[_band("b", "weight_kg", rate="1")],
            surcharges=[_rule("orphan", "percentage", rate="0.1", trigger=None, compounds_on=["nonexistent_rule"])],
        )
        with self.assertRaises(RuntimeError):
            price_line(_shipment(billed_weight_kg=10), rc)


class PricingGateTests(unittest.TestCase):
    def _ctx_with(self, matched_count: int):
        ctx = Context(run_dir=None)
        ctx.vars["match_and_normalise"] = {"matched_lines": [{}] * matched_count}
        return ctx

    def _line(self, **overrides):
        base = {"invoice": "INV", "consignment_ref": "R1", "billed_amount": 100.0,
                "delta": 0.0, "pricing_outcome": "determined", "expected_amount": 100.0,
                "reason": None, "clauses": ["x.md §1"]}
        base.update(overrides)
        return base

    def test_passes_on_consistent_determined_line(self):
        ctx = self._ctx_with(1)
        pricing_gate(ctx, {"priced_lines": [self._line()]})  # should not raise

    def test_passes_on_consistent_ambiguous_line(self):
        ctx = self._ctx_with(1)
        line = self._line(pricing_outcome="ambiguous", expected_amount=None, delta=None, reason="some reason", clauses=[])
        pricing_gate(ctx, {"priced_lines": [line]})  # should not raise

    def test_catches_vanished_line(self):
        ctx = self._ctx_with(2)
        with self.assertRaises(GateFailure):
            pricing_gate(ctx, {"priced_lines": [self._line()]})

    def test_catches_fabricated_amount_on_ambiguous_line(self):
        ctx = self._ctx_with(1)
        line = self._line(pricing_outcome="ambiguous", expected_amount=99.0, reason="x")
        with self.assertRaises(GateFailure):
            pricing_gate(ctx, {"priced_lines": [line]})

    def test_catches_missing_reason_on_ambiguous_line(self):
        ctx = self._ctx_with(1)
        line = self._line(pricing_outcome="ambiguous", expected_amount=None, delta=None, reason=None, clauses=[])
        with self.assertRaises(GateFailure):
            pricing_gate(ctx, {"priced_lines": [line]})

    def test_catches_inconsistent_delta(self):
        ctx = self._ctx_with(1)
        line = self._line(billed_amount=100.0, expected_amount=90.0, delta=5.0)  # should be 10.0
        with self.assertRaises(GateFailure):
            pricing_gate(ctx, {"priced_lines": [line]})

    def test_catches_determined_without_clauses(self):
        ctx = self._ctx_with(1)
        line = self._line(clauses=[])
        with self.assertRaises(GateFailure):
            pricing_gate(ctx, {"priced_lines": [line]})


if __name__ == "__main__":
    unittest.main()

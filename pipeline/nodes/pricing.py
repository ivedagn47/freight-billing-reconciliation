"""The deterministic pricing engine: a generic rate-card interpreter.

price_line(shipment, ratecard) never branches on which carrier it's
pricing -- it only reads rate_bands / flat_rates / premiums / surcharges /
accessorial_charges / exclusions / service_level_rules out of whichever
rate card it's given, and evaluates each rule's structured `trigger`
against the shipment's own attributes. The same function prices Alpine
(weight-banded per-kg), Falcon (weight-banded per-km, with compounding
premiums/surcharges), and Sagar (flat per-kg + per-km, with a compounding
premium) without a single carrier name appearing in this module's control
flow.

No LLM is involved anywhere in this file. Every amount is Decimal from
the moment it's read out of a rate card or shipment record until the
single final rounding step (see money.py).
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from pipeline.money import D, ROUNDING_POLICY, round_money
from pipeline.nodes.ratecard_verify import _iter_clause_bearing_dicts
from pipeline.runner import GateFailure

NUMERIC_OPS = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
               "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b, "eq": lambda a, b: a == b}


def _trigger_matches(trigger: dict, shipment: dict) -> bool:
    attribute, op, value = trigger["attribute"], trigger["op"], trigger["value"]
    actual = shipment[attribute]
    if op == "contains":
        return value in actual
    if op == "eq" and isinstance(actual, str):
        # string equality (e.g. service_level == "express") -- must not
        # fall through to Decimal(), which would raise on a non-numeric string
        return actual == value
    if op in NUMERIC_OPS:
        return NUMERIC_OPS[op](D(actual), D(value))
    raise ValueError(f"unknown trigger op {op!r} for attribute {attribute!r}")


def _band_contains(band: dict, value: Decimal) -> bool:
    if band["min"] is not None:
        bound = D(band["min"])
        if band["min_op"] == ">" and not (value > bound):
            return False
        if band["min_op"] == ">=" and not (value >= bound):
            return False
    if band["max"] is not None:
        bound = D(band["max"])
        if band["max_op"] == "<" and not (value < bound):
            return False
        if band["max_op"] == "<=" and not (value <= bound):
            return False
    return True


def _match_band(rate_bands: list[dict], banded_on: str, value: Decimal) -> dict | None:
    candidates = [b for b in rate_bands if b["banded_on"] == banded_on]
    matches = [b for b in candidates if _band_contains(b, value)]
    if len(matches) > 1:
        # verify_ratecards' overlap check should make this unreachable; a hard
        # error here (not a silent pick) is the correct response if it ever
        # isn't -- a rate-card integrity bug must never resolve to a guess.
        raise RuntimeError(
            f"rate card integrity error: {banded_on}={value} matches multiple "
            f"bands {[m['name'] for m in matches]} -- this should have been "
            f"caught by verify_ratecards"
        )
    return matches[0] if matches else None


def _compute_base_freight(shipment: dict, ratecard: dict) -> tuple[Decimal | None, dict, str | None]:
    weight = D(shipment["billed_weight_kg"])
    distance = D(shipment["distance_km"]) if "distance_km" in shipment else None

    weight_basis = weight
    floor_trace = None
    if ratecard["chargeable_weight_floor"] is not None:
        floor = D(ratecard["chargeable_weight_floor"]["minimum_kg"])
        weight_basis = max(weight, floor)
        floor_trace = {
            "minimum_kg": str(floor), "actual_kg": str(weight), "chargeable_kg": str(weight_basis),
            "clause": ratecard["chargeable_weight_floor"]["clause"],
        }

    trace = {
        "actual_weight_kg": str(weight),
        "distance_km": str(distance) if distance is not None else None,
        "chargeable_weight_floor_applied": floor_trace,
    }

    has_bands = bool(ratecard["rate_bands"])
    has_flat = bool(ratecard["flat_rates"])

    if has_bands:
        banded_on = ratecard["rate_bands"][0]["banded_on"]
        if any(b["banded_on"] != banded_on for b in ratecard["rate_bands"]):
            raise RuntimeError("rate card integrity error: rate_bands mix more than one banded_on dimension")
        band_value = weight_basis if banded_on == "weight_kg" else distance
        band = _match_band(ratecard["rate_bands"], banded_on, band_value)
        if band is None:
            trace["base_freight"] = None
            return None, trace, (
                f"no rate band covers {banded_on}={band_value} -- this is a gap between "
                f"adjacent bands in the contract, not a computable amount"
            )
        multiplier = weight_basis if band["rate_unit"] == "per_kg" else distance
        amount = D(band["rate"]) * multiplier
        trace["base_freight"] = {
            "mechanism": "rate_band", "band_matched": band["name"],
            "rate": str(band["rate"]), "rate_unit": band["rate_unit"],
            "multiplier": str(multiplier), "amount": str(amount), "clause": band["clause"],
        }
        return amount, trace, None

    if has_flat:
        total = Decimal("0")
        components = []
        for fr in ratecard["flat_rates"]:
            if fr["basis"] == "weight_kg":
                qty = weight_basis
            elif fr["basis"] == "distance_km":
                qty = distance
            else:  # "consignment"
                qty = Decimal("1")
            amt = D(fr["rate"]) * qty
            total += amt
            components.append({
                "name": fr["name"], "rate": str(fr["rate"]), "rate_unit": fr["rate_unit"],
                "basis": fr["basis"], "quantity": str(qty), "amount": str(amt), "clause": fr["clause"],
            })
        trace["base_freight"] = {"mechanism": "flat_rates", "components": components, "amount": str(total)}
        return total, trace, None

    trace["base_freight"] = None
    return None, trace, "rate card defines neither rate_bands nor flat_rates -- no mechanism to compute a base freight"


def _apply_compounding_rules(rules: list[dict], base_freight: Decimal, shipment: dict) -> tuple[Decimal, list[dict]]:
    """Applies premiums+surcharges in dependency order derived from each
    rule's own compounds_on list (e.g. Falcon's fuel surcharge names both
    "base_freight" and the express premium's own name, so it is only
    applied once the premium's amount -- fired or not -- is known)."""
    amounts = {"base_freight": base_freight}
    trace = []
    pending = list(rules)
    progressed = True
    while pending and progressed:
        progressed = False
        still_pending = []
        for rule in pending:
            deps = rule["compounds_on"]
            if not all(d in amounts for d in deps):
                still_pending.append(rule)
                continue
            fires = rule["trigger"] is None or _trigger_matches(rule["trigger"], shipment)
            applied_to = sum((amounts[d] for d in deps), Decimal("0"))
            if fires:
                amt = applied_to * D(rule["rate"]) if rule["type"] == "percentage" else D(rule["amount"])
            else:
                amt = Decimal("0")
            amounts[rule["name"]] = amt
            trace.append({
                "name": rule["name"], "type": rule["type"], "fired": fires,
                "compounds_on": deps, "applied_to": str(applied_to),
                "rate_or_amount": rule["rate"] if rule["type"] == "percentage" else rule["amount"],
                "amount": str(amt), "clause": rule["clause"],
            })
            progressed = True
        pending = still_pending
    if pending:
        raise RuntimeError(
            f"unresolved compounds_on dependency (cycle or missing reference) among: "
            f"{[r['name'] for r in pending]} -- this should have been caught by verify_ratecards"
        )
    total_addon = sum((amounts[r["name"]] for r in rules), Decimal("0"))
    return total_addon, trace


def _apply_accessorials(accessorials: list[dict], freight_charge: Decimal, shipment: dict) -> tuple[Decimal, list[dict]]:
    total = Decimal("0")
    trace = []
    for acc in accessorials:
        fires = acc["trigger"] is None or _trigger_matches(acc["trigger"], shipment)
        if not fires:
            trace.append({"name": acc["name"], "fired": False, "amount": "0", "clause": acc["clause"]})
            continue
        # A flat_fee is a fixed amount by definition. A percentage accessorial
        # has no compounds_on field in this schema (none of the real contracts
        # need one) -- absent contract guidance, it is applied to the freight
        # charge computed so far, the only quantity that makes contextual
        # sense; this branch is untested against real data because no
        # contract in this dataset uses a percentage accessorial.
        amt = D(acc["amount"]) if acc["type"] == "flat_fee" else freight_charge * D(acc["rate"])
        total += amt
        trace.append({"name": acc["name"], "fired": True, "type": acc["type"], "amount": str(amt), "clause": acc["clause"]})
    return total, trace


def _check_service_level_denial(service_level_rules: list[dict], shipment: dict) -> dict | None:
    for rule in service_level_rules:
        if rule["allowed"] is False and rule["service_level"].lower() == shipment["service_level"].lower():
            return rule
    return None


def _check_exclusions(exclusions: list[dict], shipment: dict) -> dict | None:
    """Only exclusions with a non-null trigger are mechanically evaluated.
    An exclusion with trigger=None means the agent judged its condition
    inexpressible as a single attribute predicate (e.g. Sagar's "cannot be
    matched to a booking" rule, which depends on a join outcome Phase 1
    already resolved, not a shipment attribute) -- treating that as
    "applies to every line" would be wrong, so it is preserved as context
    only, not auto-fired. This is the opposite default from premiums and
    surcharges, where trigger=None correctly means "applies unconditionally"
    (e.g. Falcon's fuel surcharge, which the contract states applies to
    every consignment) -- for an additive charge, "no condition" defaulting
    to universal is safe; for a carve-out that removes a line from pricing
    entirely, it is not."""
    for excl in exclusions:
        if excl["trigger"] is not None and _trigger_matches(excl["trigger"], shipment):
            return excl
    return None


def _collect_clauses(*objs) -> list[str]:
    clauses = set()
    for obj in objs:
        for _, node in _iter_clause_bearing_dicts(obj):
            clauses.add(node["clause"])
    return sorted(clauses)


def _outcome(pricing_outcome: str, expected_amount: Decimal | None, reason: str | None,
             clauses: list[str], trace: dict) -> dict:
    return {
        "pricing_outcome": pricing_outcome,
        "expected_amount": float(expected_amount) if expected_amount is not None else None,
        "reason": reason,
        "clauses": clauses,
        "trace": trace,
    }


def price_line(shipment: dict, ratecard: dict) -> dict:
    """Prices one shipment against one rate card. Never guesses: any
    condition the rate card doesn't unambiguously resolve ends in
    'ambiguous' with expected_amount=None, never a fabricated number."""
    denial = _check_service_level_denial(ratecard["service_level_rules"], shipment)
    if denial:
        return _outcome("ambiguous", None,
                         f"service_level {shipment['service_level']!r} is explicitly not offered under this agreement",
                         [denial["clause"]], {"service_level_denied": denial})

    excl = _check_exclusions(ratecard["exclusions"], shipment)
    if excl:
        return _outcome("out_of_scope", None, excl["description"], [excl["clause"]], {"exclusion_matched": excl})

    base_freight, trace, base_error = _compute_base_freight(shipment, ratecard)
    if base_error:
        clauses = _collect_clauses(ratecard["rate_bands"], ratecard["flat_rates"]) or [ratecard["pricing_basis"]["clause"]]
        return _outcome("ambiguous", None, base_error, clauses, trace)

    addon_rules = ratecard["premiums"] + ratecard["surcharges"]
    total_addon, addon_trace = _apply_compounding_rules(addon_rules, base_freight, shipment)
    freight_charge = base_freight + total_addon
    trace["premiums_and_surcharges_applied"] = addon_trace
    trace["freight_charge_before_accessorials"] = str(freight_charge)

    accessorial_total, accessorial_trace = _apply_accessorials(ratecard["accessorial_charges"], freight_charge, shipment)
    trace["accessorials_applied"] = accessorial_trace

    total_exact = freight_charge + accessorial_total
    expected_amount = round_money(total_exact)
    trace["total_before_rounding"] = str(total_exact)
    trace["rounding"] = ROUNDING_POLICY

    fired_rule_dicts = [r for r, t in zip(addon_rules, addon_trace) if t["fired"]] + \
                        [a for a, t in zip(ratecard["accessorial_charges"], accessorial_trace) if t["fired"]]
    clauses = _collect_clauses(trace.get("base_freight"), fired_rule_dicts)
    return _outcome("determined", expected_amount, None, clauses, trace)


def price_lines(ctx) -> dict:
    matched_lines = ctx.vars["match_and_normalise"]["matched_lines"]
    shipments_by_ref = ctx.vars["load_shipments"]["by_ref"]
    ratecards = {c: ctx.vars[f"extract_ratecard_{c}"] for c in ("alpine", "falcon", "sagar")}

    priced_lines = []
    for line in matched_lines:
        billed_amount = line["billed_amount"]

        if not line["matched"]:
            outcome = _outcome("ambiguous", None,
                                "no matched shipment record; cannot determine an expected amount without shipment facts",
                                [], {})
        else:
            shipment = shipments_by_ref[line["consignment_ref"]]
            ratecard = ratecards.get(shipment["carrier"])
            if ratecard is None:
                outcome = _outcome("ambiguous", None, f"no rate card available for carrier {shipment['carrier']!r}", [], {})
            else:
                outcome = price_line(shipment, ratecard)

        delta = None
        if outcome["expected_amount"] is not None:
            delta = float(round_money(D(billed_amount) - D(outcome["expected_amount"])))

        priced_lines.append({
            "invoice": line["invoice"],
            "consignment_ref": line["consignment_ref"],
            "carrier": line["carrier"],
            "shipment_id": line["shipment_id"],
            "billed_amount": billed_amount,
            "delta": delta,
            **outcome,
        })

    counts = Counter(l["pricing_outcome"] for l in priced_lines)
    return {
        "priced_lines": priced_lines,
        "summary": {
            "line_count": len(priced_lines),
            "determined": counts.get("determined", 0),
            "ambiguous": counts.get("ambiguous", 0),
            "out_of_scope": counts.get("out_of_scope", 0),
        },
    }


def pricing_gate(ctx, output: dict) -> None:
    matched_count = len(ctx.vars["match_and_normalise"]["matched_lines"])
    priced = output["priced_lines"]
    errors = []

    if len(priced) != matched_count:
        raise GateFailure(f"line count changed during pricing: matched {matched_count}, priced {len(priced)}")

    for i, l in enumerate(priced):
        label = f"priced_lines[{i}] ({l['invoice']}/{l['consignment_ref']})"
        if l["pricing_outcome"] not in ("determined", "ambiguous", "out_of_scope"):
            errors.append(f"{label}: invalid pricing_outcome {l['pricing_outcome']!r}")
            continue

        if l["pricing_outcome"] == "determined":
            if l["expected_amount"] is None:
                errors.append(f"{label}: determined but expected_amount is null")
            if not l["clauses"]:
                errors.append(f"{label}: determined but no governing clause was recorded")
            if l["delta"] is None:
                errors.append(f"{label}: determined but delta is null")
            elif l["expected_amount"] is not None:
                expected_delta = round(l["billed_amount"] - l["expected_amount"], 2)
                if abs(l["delta"] - expected_delta) > 0.005:
                    errors.append(
                        f"{label}: delta {l['delta']} is not billed ({l['billed_amount']}) "
                        f"minus expected ({l['expected_amount']}) = {expected_delta}"
                    )
        else:
            if l["expected_amount"] is not None:
                errors.append(f"{label}: outcome={l['pricing_outcome']} but expected_amount {l['expected_amount']} was fabricated")
            if l["delta"] is not None:
                errors.append(f"{label}: outcome={l['pricing_outcome']} but delta is set despite no expected_amount")
            if not l["reason"]:
                errors.append(f"{label}: outcome={l['pricing_outcome']} but no reason was recorded")

    if errors:
        raise GateFailure("pricing_gate failed:\n  - " + "\n  - ".join(errors))

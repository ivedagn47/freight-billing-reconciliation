"""verify_ratecards: deterministic structural/provenance checks on an
extracted rate card. This never judges whether a rule is the *correct*
business rule -- only whether the rate card is internally consistent and
honestly traceable to the contract it claims to come from. A genuine gap
or ambiguity in the contract is not a defect to reject; failing to *say
so* is.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pipeline.runner import GateFailure

CLAUSE_NUM_RE = re.compile(r"^(\d+)\.\s", re.MULTILINE)
CLAUSE_CITATION_RE = re.compile(r"^(\S+\.md)\s+(.*)$")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contract_clause_numbers(path: Path) -> set[int]:
    """Every top-level numbered clause actually present in the contract,
    e.g. {1,2,...,7} for alpine-express.md. Used to check that a rate
    card's clause citations point at clauses that really exist."""
    return {int(n) for n in CLAUSE_NUM_RE.findall(path.read_text())}


def _iter_clause_bearing_dicts(node, path="$"):
    """Walks the whole rate card and yields (json_path, dict) for every
    nested object that carries a 'clause' key -- so every citation
    anywhere in the structure gets checked, without hand-enumerating each
    field path."""
    if isinstance(node, dict):
        if "clause" in node and isinstance(node["clause"], str):
            yield path, node
        for k, v in node.items():
            yield from _iter_clause_bearing_dicts(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _iter_clause_bearing_dicts(item, f"{path}[{i}]")


def _check_clause_citations(ratecard: dict, contract_path: Path, valid_numbers: set[int]) -> list[str]:
    errors = []
    expected_filename = contract_path.name
    for json_path, obj in _iter_clause_bearing_dicts(ratecard):
        citation = obj["clause"]
        stripped = citation.strip()
        if not stripped:
            continue  # empty clause strings are caught by schema minLength on required ones
        if stripped == expected_filename:
            continue  # a bare filename is a legitimate whole-document reference,
                       # e.g. citing "no restriction is stated anywhere" -- there
                       # is no single clause number for an absence
        m = CLAUSE_CITATION_RE.match(stripped)
        if not m:
            errors.append(f"{json_path}.clause: {citation!r} is not of the form '<file>.md §N[, §M...]'")
            continue
        cited_file, rest = m.group(1), m.group(2)
        if cited_file != expected_filename:
            errors.append(f"{json_path}.clause: cites {cited_file!r}, expected {expected_filename!r}")
            continue
        cited_numbers = [int(n) for n in re.findall(r"§(\d+)", rest)]
        if not cited_numbers:
            errors.append(f"{json_path}.clause: {citation!r} has no §N clause number")
            continue
        for n in cited_numbers:
            if n not in valid_numbers:
                errors.append(f"{json_path}.clause: §{n} does not exist in {expected_filename} (valid: {sorted(valid_numbers)})")
    return errors


# --- band interval arithmetic -------------------------------------------------

def _band_contains(band: dict, value: float) -> bool:
    if band["min"] is not None:
        if band["min_op"] == ">" and not (value > band["min"]):
            return False
        if band["min_op"] == ">=" and not (value >= band["min"]):
            return False
    if band["max"] is not None:
        if band["max_op"] == "<" and not (value < band["max"]):
            return False
        if band["max_op"] == "<=" and not (value <= band["max"]):
            return False
    return True


def _boundary_probe_points(bands: list[dict]) -> list[float]:
    """Every band edge, plus the midpoint between consecutive edges and a
    point below the lowest / above the highest -- enough probe points to
    catch any gap or overlap between adjacent bands without needing true
    interval-algebra (bands here are always simple half-open/closed
    rays, so finite probing at edges + one epsilon step is exhaustive)."""
    edges = sorted({b[k] for b in bands for k in ("min", "max") if b[k] is not None})
    if not edges:
        return []
    points = set()
    eps = 1e-6
    points.add(edges[0] - 1)
    points.add(edges[-1] + 1)
    for e in edges:
        points.add(e)
        points.add(e - eps)
        points.add(e + eps)
    for a, b in zip(edges, edges[1:]):
        points.add((a + b) / 2)
    return sorted(points)


def _check_bands(ratecard: dict, banded_on: str) -> tuple[list[str], list[float]]:
    """Returns (structural_errors, gap_points). Overlaps are structural
    errors (a contradiction -- two bands can't both legitimately apply).
    Gaps are not errors here; the caller cross-checks them against the
    declared ambiguities."""
    bands = [b for b in ratecard["rate_bands"] if b["banded_on"] == banded_on]
    errors = []
    for b in bands:
        if b["min"] is not None and b["max"] is not None and b["min"] >= b["max"]:
            errors.append(f"rate_bands[{b['name']!r}]: min {b['min']} >= max {b['max']}")
    if len(bands) < 2:
        return errors, []

    probes = _boundary_probe_points(bands)
    gap_points = []
    for p in probes:
        matches = [b["name"] for b in bands if _band_contains(b, p)]
        if len(matches) > 1:
            errors.append(f"rate_bands overlap at {banded_on}={p}: {matches}")
        elif len(matches) == 0:
            gap_points.append(p)
    return errors, gap_points


def verify_ratecard(ratecard: dict, contract_path: Path, expected_carrier: str) -> None:
    """Raises GateFailure on any structural/provenance defect. Never
    raises because a business rule looks surprising -- only because the
    rate card is inconsistent with itself or misattributed."""
    errors = []

    if ratecard["carrier"] != expected_carrier:
        errors.append(f"carrier is {ratecard['carrier']!r}, expected {expected_carrier!r}")

    if ratecard["source_contract"]["file"] != str(contract_path):
        errors.append(f"source_contract.file is {ratecard['source_contract']['file']!r}, expected {str(contract_path)!r}")

    actual_hash = sha256_of(contract_path)
    if ratecard["source_contract"]["sha256"] != actual_hash:
        errors.append(
            f"source_contract.sha256 {ratecard['source_contract']['sha256']!r} does not match "
            f"the contract file's current hash {actual_hash!r} -- rate card was not extracted "
            f"from the current contract text"
        )

    valid_clause_numbers = contract_clause_numbers(contract_path)
    errors.extend(_check_clause_citations(ratecard, contract_path, valid_clause_numbers))

    all_gap_points = []
    for banded_on in ("weight_kg", "distance_km"):
        band_errors, gaps = _check_bands(ratecard, banded_on)
        errors.extend(band_errors)
        all_gap_points.extend((banded_on, p) for p in gaps)

    if all_gap_points and not ratecard["ambiguities"]:
        errors.append(
            f"rate_bands leave {len(all_gap_points)} point(s) uncovered by any band "
            f"(e.g. {all_gap_points[0]}), but ambiguities is empty -- a genuine gap "
            f"must be declared, not silently left out"
        )

    def _check_rate_amount_pairing(group_name: str, rule: dict, percentage_type: str, flat_type: str) -> None:
        """A rule must carry exactly the field its type implies -- a
        percentage-typed rule with amount also set (or vice versa) is
        exactly the kind of internal inconsistency this gate exists to
        catch, regardless of which rule group it appears in."""
        label = f"{group_name}[{rule['name']!r}]"
        if rule["type"] == percentage_type:
            if rule["rate"] is None:
                errors.append(f"{label}: type={percentage_type} but rate is null")
            elif not (0 < rule["rate"] <= 1):
                errors.append(f"{label}: rate {rule['rate']} is not a fraction in (0, 1]")
            if rule["amount"] is not None:
                errors.append(f"{label}: type={percentage_type} but amount is also set")
        elif rule["type"] == flat_type:
            if rule["amount"] is None:
                errors.append(f"{label}: type={flat_type} but amount is null")
            elif rule["amount"] < 0:
                errors.append(f"{label}: negative amount {rule['amount']}")
            if rule["rate"] is not None:
                errors.append(f"{label}: type={flat_type} but rate is also set")

    STRING_ATTRS = {"service_level", "special_handling"}
    NUMERIC_ATTRS = {"billed_weight_kg", "distance_km"}
    NUMERIC_OPS = {"gt", "gte", "lt", "lte"}

    def _check_trigger(label: str, trigger) -> None:
        """A trigger is either null (unconditional) or a well-formed
        predicate whose op is compatible with its attribute's type --
        e.g. 'contains' only makes sense against special_handling (a
        list), numeric comparisons only against a numeric attribute."""
        if trigger is None:
            return
        attribute, op, value = trigger["attribute"], trigger["op"], trigger["value"]
        if attribute in NUMERIC_ATTRS:
            if op not in NUMERIC_OPS | {"eq"}:
                errors.append(f"{label}: trigger.op {op!r} is not valid for numeric attribute {attribute!r}")
            elif not isinstance(value, (int, float)):
                errors.append(f"{label}: trigger.value {value!r} is not numeric for attribute {attribute!r}")
        elif attribute in STRING_ATTRS:
            if op in NUMERIC_OPS:
                errors.append(f"{label}: trigger.op {op!r} is not valid for attribute {attribute!r}")
            elif attribute == "special_handling" and op != "contains":
                errors.append(f"{label}: special_handling is a list -- trigger.op should be 'contains', got {op!r}")
            elif not isinstance(value, str):
                errors.append(f"{label}: trigger.value {value!r} is not a string for attribute {attribute!r}")

    for group_name in ("premiums", "surcharges"):
        for rule in ratecard[group_name]:
            _check_rate_amount_pairing(group_name, rule, "percentage", "flat_fee")
            _check_trigger(f"{group_name}[{rule['name']!r}]", rule["trigger"])
            names_referenced = set(rule["compounds_on"])
            known_names = {"base_freight"} | {r["name"] for r in ratecard["premiums"]} | {r["name"] for r in ratecard["surcharges"]}
            unknown = names_referenced - known_names - {rule["name"]}
            if unknown:
                errors.append(f"{group_name}[{rule['name']!r}]: compounds_on references unknown rule name(s) {unknown}")

    for rule in ratecard["exclusions"]:
        _check_trigger(f"exclusions[{rule['description']!r}]", rule["trigger"])

    for rule in ratecard["accessorial_charges"]:
        _check_trigger(f"accessorial_charges[{rule['name']!r}]", rule["trigger"])
        _check_rate_amount_pairing("accessorial_charges", rule, "percentage", "flat_fee")

    for rule in ratecard["discounts"]:
        _check_rate_amount_pairing("discounts", rule, "percentage_of_invoice_total", "flat")
        threshold = rule["threshold"]
        if threshold is not None and not isinstance(threshold["value"], (int, float)):
            errors.append(f"discounts[{rule['name']!r}]: threshold.value {threshold['value']!r} is not numeric")

    if ratecard["accessorial_policy"]["type"] == "whitelist" and not ratecard["accessorial_policy"]["allowed_names"]:
        errors.append("accessorial_policy.type is 'whitelist' but allowed_names is empty")

    if ratecard["chargeable_weight_floor"] is not None:
        if ratecard["chargeable_weight_floor"]["minimum_kg"] <= 0:
            errors.append("chargeable_weight_floor.minimum_kg must be positive")

    if errors:
        raise GateFailure("verify_ratecards failed:\n  - " + "\n  - ".join(errors))

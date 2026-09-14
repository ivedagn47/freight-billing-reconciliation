"""extract_ratecard_<carrier>: one real agent turn per carrier contract,
turning prose into a structured rate card. Reused across runs via
content-hash pinning: ratecards/<carrier>.json is a committed artifact,
and a run only calls the agent again if the contract's sha256 has
changed since that artifact was produced.

carrier and source_contract (including the sha256) are never asked of the
model -- see ratecard_extraction.schema.json. They are computed by this
module and injected after the agent turn, so the hash that proves
provenance can never be something the model merely claimed.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.claude_worker import run_agent_turn
from pipeline.nodes.ratecard_verify import sha256_of

CONTRACTS_DIR = Path("data/contracts")
RATECARDS_DIR = Path("ratecards")

CONTRACTS = {
    "alpine": CONTRACTS_DIR / "alpine-express.md",
    "falcon": CONTRACTS_DIR / "falcon-freight.md",
    "sagar": CONTRACTS_DIR / "sagar-roadlines.md",
}

EXTRACTION_SCHEMA = json.loads(Path("schemas/ratecard_extraction.schema.json").read_text())


PROMPT_TEMPLATE = """\
You are extracting a carrier's freight rate contract into a structured, \
machine-readable rate card. This rate card will feed a deterministic pricing \
engine directly -- it must be a faithful, literal transcription of what the \
contract says, not your best guess at what a "reasonable" freight contract \
would say.

## The contract (verbatim, in full)

Source file: {contract_filename}

```
{contract_text}
```

## Ground rules for this extraction

1. Every numbered clause in this contract (each top-level "N." list item) is a \
citable unit. Every rule you extract must carry a "clause" field citing it, in \
the exact form "{contract_filename} §N" (or "{contract_filename} §N, §M" \
if the rule draws on more than one clause). Do not cite a clause number that \
doesn't appear in the text above.

2. Represent weight/distance bands literally, including their boundary \
operators. If the contract says "under 50 kg" and "over 50 kg" (or similar \
strict-inequality language on both sides), encode that as two bands whose \
boundary operators are strict ("<" and ">") -- do NOT widen either operator to \
"<=" or ">=" to make the bands meet. If this leaves a value (e.g. exactly \
50 kg) covered by neither band, that is a real gap in the contract, not a \
mistake in your extraction. Do not invent a rule to fill it. Instead, add an \
entry to "ambiguities" describing exactly what value(s) are uncovered and by \
which clause. Apply this same literal-boundary discipline to every band in the \
contract, not just an example that happens to look like this one -- check \
every band boundary against its neighbors for gaps as a discrete exercise, \
including boundaries that look closed on both sides (e.g. "up to and including \
X" next to "X+1 to Y") where a fractional value between them could still fall \
in neither band.

3. Do not resolve genuine ambiguity by picking the interpretation that seems \
more sensible. If the contract's wording leaves something underspecified -- \
how a condition is measured, what "calendar month" means for an invoice that \
doesn't align with one, whether two clauses interact -- put it in \
"ambiguities" with a clear description and the relevant clause, and represent \
the rest of the rate card without depending on your resolution of it.

4. Only extract what this contract actually states. Leave a field null / an \
array empty rather than inferring a rule the contract doesn't state. In \
particular:
   - "chargeable_weight_floor": null unless the contract explicitly states a \
minimum chargeable weight distinct from actual/billed weight.
   - "accessorial_policy.type": "whitelist" only if the contract explicitly \
restricts accessorial charges to a stated list (cite that clause and list the \
allowed charge names in "allowed_names"); otherwise "no_restriction_stated" \
with empty allowed_names -- do not infer a restriction that isn't written, and \
do not infer permissiveness either. Just say what the contract says.
   - "exclusions": cases the contract explicitly carves out of its own rate \
card (e.g. "quoted case by case", "outside this rate card").

5. "rate_bands" is for a rate that varies by band of weight_kg or distance_km \
(one row per band). "flat_rates" is for a single unbanded rate applied \
uniformly (e.g. "₹X per kg" with no bands at all). A carrier may use one, the \
other, both, or neither -- use whichever the contract actually describes.

6. "premiums" and "surcharges" are percentage or flat-fee rules that apply on \
top of the base freight (the amount from rate_bands/flat_rates). Use the \
reserved name "base_freight" in "compounds_on" to mean that base amount. If \
the contract says a surcharge applies to "the freight charge (base freight \
plus express premium, where applicable)", that means it compounds on both \
"base_freight" AND the express premium's own "name" -- list both in \
compounds_on. If a rule applies only to the bare base freight, compounds_on \
should be ["base_freight"]. Read compounding order directly from the \
contract's own wording; do not assume rules compound just because a real \
contract elsewhere in the world might work that way.

6b. Every premium, surcharge, accessorial charge, and exclusion also needs a \
"trigger" -- a machine-evaluable predicate, separate from the human-readable \
"condition" text, so a deterministic program can decide whether the rule fires \
without parsing English. "trigger" is either null (the rule applies \
unconditionally to every consignment -- e.g. Falcon's fuel surcharge) or an \
object with exactly three fields:
   - "attribute": one of "service_level", "special_handling", \
"billed_weight_kg", "distance_km" -- whichever shipment fact the contract's \
condition actually depends on.
   - "op": one of "eq" (equals), "contains" (list membership -- use this for \
special_handling, which is a list like ["fragile"] or ["cold_chain"]), "gt", \
"gte", "lt", "lte" (numeric comparison, for weight/distance thresholds).
   - "value": the value to compare against. For special_handling, use the \
lowercase underscored form a shipment record would actually use (e.g. \
"fragile", "cold_chain", "residential"), not a human phrase like "cold chain".
Examples: an express-only premium -> {{"attribute": "service_level", "op": \
"eq", "value": "express"}}. A cold-chain premium -> {{"attribute": \
"special_handling", "op": "contains", "value": "cold_chain"}}. A >2000kg \
exclusion -> {{"attribute": "billed_weight_kg", "op": "gt", "value": 2000}}. \
If a rule's condition depends on something no single attribute+op pair can \
express, set trigger to null and explain the gap in "ambiguities" instead of \
forcing an inaccurate trigger.

7. "accessorial_charges" is for named extra charges tied to a shipment \
attribute (e.g. fragile handling, residential delivery) -- flat fee or \
percentage, with the condition that triggers it in plain English (e.g. \
"special_handling includes 'fragile'") AND the matching structured "trigger".

8. "discounts" apply to the invoice as a whole (not a single line), e.g. a \
volume-based discount on the total invoice. Each discount also needs a \
"threshold": null if it has no numeric trigger condition, otherwise an object \
{{"metric": "consignment_count" (the only supported metric today), "period": \
the time window the contract names for that count, stated as the contract \
words it (e.g. "calendar_month") -- do NOT resolve which date field (ship \
date, booking date, invoice date) defines that period; that ambiguity belongs \
in "ambiguities", not in this field, "op": one of "gt"/"gte"/"lt"/"lte", \
"value": the numeric threshold itself}}. Example: "more than 12 consignments \
in a calendar month" -> {{"metric": "consignment_count", "period": \
"calendar_month", "op": "gt", "value": 12}}.

9. currency: use the ISO code implied by the "₹" / "Rs" symbols used \
throughout (INR).

10. "notes": anything you think is relevant context that doesn't fit a \
structured field above -- but this is not a place to smuggle in a rule that \
belongs in one of the structured fields instead.

## Output

Reply with ONLY a raw JSON object (no markdown, no code fences, no commentary) \
matching the required schema exactly. Every object in every array must include \
every required field, using null/[] for anything not applicable rather than \
omitting the key.
"""


def _build_prompt(contract_filename: str, contract_text: str) -> str:
    return PROMPT_TEMPLATE.format(contract_filename=contract_filename, contract_text=contract_text)


def make_extract_node_fn(carrier: str):
    contract_path = CONTRACTS[carrier]

    def fn(ctx) -> dict:
        source_hash = sha256_of(contract_path)
        cached_path = RATECARDS_DIR / f"{carrier}.json"

        if cached_path.exists():
            cached = json.loads(cached_path.read_text())
            if cached.get("source_contract", {}).get("sha256") == source_hash:
                (ctx.node_dir / "cache_hit.json").write_text(json.dumps({
                    "reused_from": str(cached_path),
                    "sha256": source_hash,
                    "reason": "contract content unchanged since this rate card was last extracted",
                }, indent=2))
                return cached

        contract_text = contract_path.read_text()
        prompt = _build_prompt(contract_path.name, contract_text)
        extracted = run_agent_turn(
            prompt=prompt,
            output_schema=EXTRACTION_SCHEMA,
            node_dir=ctx.node_dir,
            model="sonnet",
        )

        ratecard = dict(extracted)
        ratecard["carrier"] = carrier
        ratecard["source_contract"] = {"file": str(contract_path), "sha256": source_hash}

        RATECARDS_DIR.mkdir(exist_ok=True)
        cached_path.write_text(json.dumps(ratecard, indent=2))
        return ratecard

    return fn

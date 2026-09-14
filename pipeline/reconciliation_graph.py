"""The real reconciliation graph.

Phase 1: discovery, parsing, matching, duplicate detection -- all
deterministic script nodes, no agent turns (there is no judgment call to
make until pricing and adjudication).

Phase 2: one real agent turn per carrier contract, turning prose into a
structured rate card, each independently gated for structural/provenance
consistency (not business correctness -- see ratecard_verify.py).

Phase 3: a deterministic pricing engine (no LLM) that interprets the rate
cards against shipment facts to compute expected_amount/delta per line,
never guessing when a rule doesn't unambiguously determine an amount.

Phase 4: invoice-level findings (self-consistency, rate-card-driven
invoice rules, duplicate billing, credit-note relationships) and
dispute-unit attribution, so every disputable rupee is counted exactly
once later. Still no LLM, and still no accept/dispute/escalate decision.

Phase 5: the second agent-judgment stage. One real agent turn per finding
requiring adjudication, deciding accept/dispute/escalate and drafting
memo prose -- structurally unable to touch any monetary value (see
adjudication.py). Memos are then assembled by deterministic Python from
the finding's own amounts + the agent's prose, never the other way round.

Phase 6: final report assembly (deterministic -- reads only Phase 1/3/4
amounts and Phase 5's disposition/justification/clauses, never a number
from an agent) and validation, which independently re-derives every
invariant from Phase 1-5 outputs rather than trusting assemble_report's
own bookkeeping.

Run with: python3 -m pipeline.reconciliation_graph
"""
import json
from pathlib import Path

from pipeline.nodes.adjudication import (
    adjudicate_findings, adjudication_gate, memos_gate, write_memos,
)
from pipeline.nodes.discover import discover_invoices
from pipeline.nodes.findings import detect_findings, findings_gate
from pipeline.nodes.match import line_conservation_gate, load_shipments, match_and_normalise
from pipeline.nodes.parse import parse_invoices
from pipeline.nodes.pricing import price_lines, pricing_gate
from pipeline.nodes.ratecard_verify import verify_ratecard
from pipeline.nodes.ratecards import CONTRACTS, make_extract_node_fn
from pipeline.nodes.report import assemble_report, validate_report
from pipeline.runner import NodeSpec, run_graph

SCHEMA_DIR = Path("schemas")


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


NODES = [
    NodeSpec(
        name="discover_invoices",
        kind="script",
        fn=discover_invoices,
        output_schema=_schema("phase1_manifest.schema.json"),
        description="Classify every file in data/invoices/ by carrier, doc type, and billing period.",
    ),
    NodeSpec(
        name="parse_invoices",
        kind="script",
        fn=parse_invoices,
        output_schema=_schema("phase1_parsed_invoices.schema.json"),
        description="Deterministically parse every in-scope invoice into canonical lines.",
    ),
    NodeSpec(
        name="load_shipments",
        kind="script",
        fn=load_shipments,
        output_schema=_schema("phase1_shipments_index.schema.json"),
        description="Load data/shipments.json into a consignment_ref-keyed index.",
    ),
    NodeSpec(
        name="match_and_normalise",
        kind="script",
        fn=match_and_normalise,
        output_schema=_schema("phase1_matched_lines.schema.json"),
        gate=line_conservation_gate,
        description="Match canonical lines to shipments; detect cross-invoice duplicates.",
    ),
]

RATECARD_SCHEMA = _schema("ratecard.schema.json")


def _ratecard_gate(carrier: str, contract_path: Path):
    def gate(ctx, output: dict) -> None:
        verify_ratecard(output, contract_path, expected_carrier=carrier)
    return gate


for _carrier, _contract_path in CONTRACTS.items():
    NODES.append(NodeSpec(
        name=f"extract_ratecard_{_carrier}",
        kind="script",  # wraps one real agent turn; see ratecards.make_extract_node_fn
        fn=make_extract_node_fn(_carrier),
        output_schema=RATECARD_SCHEMA,
        gate=_ratecard_gate(_carrier, _contract_path),
        description=f"Extract {_carrier}'s rate contract into a structured rate card (one real agent turn, content-hash cached).",
    ))

NODES.append(NodeSpec(
    name="price_lines",
    kind="script",
    fn=price_lines,
    output_schema=_schema("priced_lines.schema.json"),
    gate=pricing_gate,
    description="Deterministically compute expected_amount/delta per line from shipment facts + rate cards. No LLM.",
))

NODES.append(NodeSpec(
    name="detect_findings",
    kind="script",
    fn=detect_findings,
    output_schema=_schema("findings.schema.json"),
    gate=findings_gate,
    description="Invoice-level findings and disjoint dispute-unit attribution. No LLM, no disposition decided.",
))

NODES.append(NodeSpec(
    name="adjudicate_findings",
    kind="script",  # wraps one real agent turn per finding; see adjudication.adjudicate_findings
    fn=adjudicate_findings,
    output_schema=_schema("adjudication_results.schema.json"),
    gate=adjudication_gate,
    description="One real agent turn per finding requiring adjudication: accept/dispute/escalate + memo prose. No monetary fields possible.",
))

NODES.append(NodeSpec(
    name="write_memos",
    kind="script",
    fn=write_memos,
    output_schema=_schema("memos_written.schema.json"),
    gate=memos_gate,
    description="Deterministically assemble memos/ from each non-accept adjudication's prose + the finding's own amounts. No LLM.",
))

REPORT_SCHEMA = json.loads(Path("report.schema.json").read_text())

NODES.append(NodeSpec(
    name="assemble_report",
    kind="script",
    fn=assemble_report,
    output_schema=REPORT_SCHEMA,
    gate=validate_report,
    description="Deterministically assemble reconciliation-report.json from Phase 1/3/4 amounts + Phase 5 dispositions. No LLM, no invented numbers.",
))


if __name__ == "__main__":
    ctx = run_graph(NODES, graph_name="reconciliation")
    result = ctx.vars["match_and_normalise"]
    print("\n=== summary ===")
    print(json.dumps(result["summary"], indent=2))
    if result["duplicate_groups"]:
        print("\n=== duplicate groups ===")
        print(json.dumps(result["duplicate_groups"], indent=2))
    unmatched = [l for l in result["matched_lines"] if not l["matched"]]
    if unmatched:
        print("\n=== unmatched lines ===")
        for l in unmatched:
            print(f"  {l['invoice']} / {l['consignment_ref']}")

    for carrier in CONTRACTS:
        rc = ctx.vars[f"extract_ratecard_{carrier}"]
        print(f"\n=== ratecard: {carrier} ===")
        print(f"  agreement_ref: {rc['agreement_ref']}")
        print(f"  rate_bands: {len(rc['rate_bands'])}  flat_rates: {len(rc['flat_rates'])}  "
              f"premiums: {len(rc['premiums'])}  surcharges: {len(rc['surcharges'])}  "
              f"accessorials: {len(rc['accessorial_charges'])}  discounts: {len(rc['discounts'])}")
        if rc["ambiguities"]:
            print(f"  ambiguities ({len(rc['ambiguities'])}):")
            for a in rc["ambiguities"]:
                print(f"    - [{a['clause']}] {a['description']}")

    priced = ctx.vars["price_lines"]
    print("\n=== pricing summary ===")
    print(json.dumps(priced["summary"], indent=2))
    non_determined = [l for l in priced["priced_lines"] if l["pricing_outcome"] != "determined"]
    if non_determined:
        print(f"\n=== {len(non_determined)} non-determined line(s) ===")
        for l in non_determined:
            print(f"  [{l['pricing_outcome']}] {l['invoice']}/{l['consignment_ref']}: {l['reason']}")

    findings_out = ctx.vars["detect_findings"]
    print("\n=== findings summary ===")
    print(json.dumps(findings_out["summary"], indent=2))
    print("\n=== all findings ===")
    for f in findings_out["findings"]:
        du = f"[{f['dispute_unit_id']} = {f['dispute_amount']}]" if f["dispute_unit_id"] else "[no dispute unit]"
        print(f"  {f['finding_id']:45} {f['finding_type']:25} {du}")
        print(f"      -> {f['adjudication_reason']}")

    adj = ctx.vars["adjudicate_findings"]
    print("\n=== adjudication summary ===")
    print(json.dumps(adj["summary"], indent=2))
    for a in adj["adjudications"]:
        print(f"\n  {a['finding_id']} -> {a['disposition'].upper()}")
        print(f"    clauses: {a['governing_clauses']}")
        print(f"    justification: {a['justification']}")

    memos = ctx.vars["write_memos"]
    print("\n=== memos written ===")
    print(json.dumps(memos["summary"], indent=2))
    for p in memos["memos_written"]:
        print(f"  {p}")

    report = ctx.vars["assemble_report"]
    print("\n=== final report summary ===")
    print(json.dumps(report["summary"], indent=2))

    # The deliverable file is written here, in __main__, only after a full
    # successful run (validate_report's gate has already passed by this
    # point) -- never inside the node function itself, so unit-testing
    # assemble_report's logic can never clobber the real deliverable.
    report_path = Path("reconciliation-report.json")
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\n{report_path} written with {len(report['lines'])} line(s), "
          f"{len(report['invoice_findings'])} invoice finding(s), {len(report['invoice_totals'])} invoice total(s).")

"""adjudicate_findings: the second agent-judgment stage. One real, fresh
Claude Code turn per finding that requires adjudication, deciding
accept/dispute/escalate, a justification, governing clauses, and memo
prose -- never a monetary figure. Every number in the final memo (Phase
Phase 6's report too) still comes only from Phases 1-4; see write_memos
in this module for the deterministic assembly step that enforces that.

Structural guarantees, not just prompt instructions:
  - the agent has zero tool access (--tools ""), same as every other
    agent node in this pipeline
  - its output schema (adjudication_output.schema.json) has no field a
    monetary value could occupy, and additionalProperties: false makes
    adding one a schema failure, not silently accepted
  - governing_clauses are checked, with retries, against the actual
    contract text for the correct carrier before the result is accepted
    at all (see _make_validator)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from pipeline.claude_worker import run_agent_turn
from pipeline.nodes.ratecard_verify import CLAUSE_CITATION_RE, contract_clause_numbers
from pipeline.nodes.ratecards import CONTRACTS
from pipeline.runner import GateFailure

SKILL_PATH = Path(".claude/skills/adjudication/SKILL.md")
MEMOS_DIR = Path("memos")

ADJUDICATION_OUTPUT_FIELDS = {"finding_id", "disposition", "justification", "governing_clauses", "memo_narrative"}


def _sanitize(finding_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "__", finding_id)


def _finding_carrier(finding: dict, invoices_by_id: dict, priced_by_key: dict) -> str | None:
    for inv in finding["affected_invoices"]:
        if inv in invoices_by_id:
            return invoices_by_id[inv]["carrier"]
    for inv in finding["affected_invoices"]:
        for ref in finding["affected_consignments"]:
            p = priced_by_key.get((inv, ref))
            if p:
                return p["carrier"]
    return None


PROMPT_TEMPLATE = """\
{skill_text}

---

## The finding to adjudicate

```json
{finding_json}
```

## Shipment facts for the affected consignment(s)

```json
{shipment_facts_json}
```

## The governing rate card ({carrier})

```json
{rate_card_json}
```

## The governing contract, verbatim ({contract_file})

```
{contract_text}
```

---

Reply with ONLY a raw JSON object (no markdown, no code fences, no \
commentary) with exactly these fields: "finding_id" (must be exactly \
{finding_id_literal}), "disposition" ("accept"/"dispute"/"escalate"), \
"justification", "governing_clauses" (array of strings, each \
"{contract_file} §N"), and "memo_narrative" (an object with \
"what_happened", "why", "recommended_action"). Do not include any other \
field, and never include a monetary amount anywhere in your answer.
"""


def _build_prompt(skill_text: str, finding: dict, shipment_facts: dict, rate_card: dict,
                   carrier: str, contract_file: str, contract_text: str) -> str:
    return PROMPT_TEMPLATE.format(
        skill_text=skill_text,
        finding_json=json.dumps(finding, indent=2),
        shipment_facts_json=json.dumps(shipment_facts, indent=2),
        rate_card_json=json.dumps(rate_card, indent=2),
        carrier=carrier,
        contract_file=contract_file,
        contract_text=contract_text,
        finding_id_literal=json.dumps(finding["finding_id"]),
    )


def _make_validator(expected_finding_id: str, contract_path: Path):
    valid_numbers = contract_clause_numbers(contract_path)
    expected_filename = contract_path.name

    def validate(parsed: dict) -> None:
        if parsed["finding_id"] != expected_finding_id:
            raise ValueError(f"finding_id {parsed['finding_id']!r} does not match the finding you were asked to adjudicate ({expected_finding_id!r})")
        for citation in parsed["governing_clauses"]:
            stripped = citation.strip()
            if stripped == expected_filename:
                continue  # a bare whole-document reference is acceptable, same as in rate cards
            m = CLAUSE_CITATION_RE.match(stripped)
            if not m:
                raise ValueError(f"governing_clauses entry {citation!r} is not of the form '<file>.md §N'")
            cited_file, rest = m.group(1), m.group(2)
            if cited_file != expected_filename:
                raise ValueError(f"governing_clauses entry cites {cited_file!r}, expected {expected_filename!r}")
            numbers = [int(n) for n in re.findall(r"§(\d+)", rest)]
            if not numbers:
                raise ValueError(f"governing_clauses entry {citation!r} has no §N clause number")
            bad = [n for n in numbers if n not in valid_numbers]
            if bad:
                raise ValueError(f"governing_clauses entry cites §{bad}, which does not exist in {expected_filename} (valid: {sorted(valid_numbers)})")

    return validate


def adjudicate_findings(ctx) -> dict:
    findings = ctx.vars["detect_findings"]["findings"]
    to_adjudicate = [f for f in findings if f["requires_adjudication"]]

    parsed = ctx.vars["parse_invoices"]
    invoices_by_id = {inv["invoice"]: inv for inv in parsed["invoices"]}
    priced_by_key = {(p["invoice"], p["consignment_ref"]): p for p in ctx.vars["price_lines"]["priced_lines"]}
    shipments_by_ref = ctx.vars["load_shipments"]["by_ref"]
    ratecards = {c: ctx.vars[f"extract_ratecard_{c}"] for c in ("alpine", "falcon", "sagar")}
    skill_text = SKILL_PATH.read_text()

    output_schema = json.loads(Path("schemas/adjudication_output.schema.json").read_text())

    adjudications = []
    for finding in to_adjudicate:
        carrier = _finding_carrier(finding, invoices_by_id, priced_by_key)
        ratecard = ratecards[carrier]
        contract_path = CONTRACTS[carrier]

        shipment_facts = {
            ref: shipments_by_ref[ref]
            for ref in finding["affected_consignments"]
            if ref in shipments_by_ref
        }

        prompt = _build_prompt(skill_text, finding, shipment_facts, ratecard, carrier,
                                contract_path.name, contract_path.read_text())
        node_subdir = ctx.node_dir / _sanitize(finding["finding_id"])

        result = run_agent_turn(
            prompt=prompt,
            output_schema=output_schema,
            node_dir=node_subdir,
            model="sonnet",
            extra_validate=_make_validator(finding["finding_id"], contract_path),
        )
        adjudications.append(result)

    counts = Counter(a["disposition"] for a in adjudications)
    return {
        "adjudications": adjudications,
        "summary": {
            "adjudicated_count": len(adjudications),
            "by_disposition": {
                "accept": counts.get("accept", 0),
                "dispute": counts.get("dispute", 0),
                "escalate": counts.get("escalate", 0),
            },
        },
    }


def adjudication_gate(ctx, output: dict) -> None:
    """Fails closed on: a finding requiring adjudication that got no
    result (or more than one), an invalid disposition, a monetary field
    anywhere in a result, or a governing clause that doesn't actually
    exist in the relevant contract -- the last one redundantly, since
    _make_validator already enforced it with retries at generation time,
    but this phase asked for the check to exist as a gate in its own
    right, independent of whatever happened during generation."""
    errors = []
    findings = ctx.vars["detect_findings"]["findings"]
    findings_by_id = {f["finding_id"]: f for f in findings}
    required_ids = {f["finding_id"] for f in findings if f["requires_adjudication"]}

    parsed = ctx.vars["parse_invoices"]
    invoices_by_id = {inv["invoice"]: inv for inv in parsed["invoices"]}
    priced_by_key = {(p["invoice"], p["consignment_ref"]): p for p in ctx.vars["price_lines"]["priced_lines"]}

    adjudications = output["adjudications"]
    result_ids = [a["finding_id"] for a in adjudications]

    missing = required_ids - set(result_ids)
    if missing:
        errors.append(f"finding(s) requiring adjudication got no result: {missing}")
    extra = set(result_ids) - required_ids
    if extra:
        errors.append(f"adjudication result(s) for finding(s) that don't require adjudication (or don't exist): {extra}")
    if len(result_ids) != len(set(result_ids)):
        dupes = [fid for fid in set(result_ids) if result_ids.count(fid) > 1]
        errors.append(f"duplicate adjudication result(s) for: {dupes}")

    for a in adjudications:
        label = a.get("finding_id", "<unknown>")
        extra_keys = set(a.keys()) - ADJUDICATION_OUTPUT_FIELDS
        if extra_keys:
            errors.append(f"{label}: adjudication result has unexpected field(s) {extra_keys}")
        if a.get("disposition") not in ("accept", "dispute", "escalate"):
            errors.append(f"{label}: invalid disposition {a.get('disposition')!r}")

        finding = findings_by_id.get(label)
        if finding is None:
            continue  # already reported above as "extra"
        carrier = _finding_carrier(finding, invoices_by_id, priced_by_key)
        if carrier is not None:
            contract_path = CONTRACTS[carrier]
            valid_numbers = contract_clause_numbers(contract_path)
            for citation in a.get("governing_clauses", []):
                stripped = citation.strip()
                if stripped == contract_path.name:
                    continue
                m = CLAUSE_CITATION_RE.match(stripped)
                if not m or m.group(1) != contract_path.name:
                    errors.append(f"{label}: governing_clauses entry {citation!r} does not cite {contract_path.name}")
                    continue
                numbers = [int(n) for n in re.findall(r"§(\d+)", m.group(2))]
                bad = [n for n in numbers if n not in valid_numbers]
                if bad:
                    errors.append(f"{label}: governing_clauses cites §{bad}, not present in {contract_path.name}")

    if errors:
        raise GateFailure("adjudication_gate failed:\n  - " + "\n  - ".join(errors))


# --- deterministic memo assembly (no LLM) -------------------------------------

def _format_amount(finding: dict) -> str:
    if finding["dispute_amount"] is not None:
        return f"Rs {finding['dispute_amount']:,.2f}"
    if finding["delta"] is not None:
        return f"Rs {abs(finding['delta']):,.2f}"
    return "not yet determinable from the deterministic evidence (see finding evidence)"


def _assemble_memo(finding: dict, adjudication: dict) -> str:
    narrative = adjudication["memo_narrative"]
    lines = [
        f"# {finding['finding_id']}",
        "",
        f"**Disposition:** {adjudication['disposition']}",
        f"**Finding type:** {finding['finding_type']}",
        f"**Invoice(s):** {', '.join(finding['affected_invoices']) or '(none)'}",
        f"**Consignment(s):** {', '.join(finding['affected_consignments']) or '(none)'}",
        f"**Shipment(s):** {', '.join(finding['related_shipments']) or '(none)'}",
        f"**Amount at issue:** {_format_amount(finding)}",
        f"**Governing clause(s):** {', '.join(adjudication['governing_clauses']) or '(none cited)'}",
        "",
        "## What happened",
        narrative["what_happened"],
        "",
        "## Why",
        narrative["why"],
        "",
        "## Recommended action",
        narrative["recommended_action"],
        "",
        "---",
        f"_finding_id: {finding['finding_id']} · dispute_unit_id: {finding['dispute_unit_id']}_",
    ]
    return "\n".join(lines) + "\n"


def write_memos(ctx) -> dict:
    """Deterministic: combines each non-accept adjudication's prose with
    the ORIGINAL finding's amounts/identifiers (never the adjudication
    result's own fields, which carry no amounts to begin with) into the
    final memo file. This is the one place financial values reach a memo,
    and they never pass through the agent to get there."""
    findings_by_id = {f["finding_id"]: f for f in ctx.vars["detect_findings"]["findings"]}
    adjudications = ctx.vars["adjudicate_findings"]["adjudications"]

    MEMOS_DIR.mkdir(exist_ok=True)
    memos_written = []
    for a in adjudications:
        if a["disposition"] == "accept":
            continue
        finding = findings_by_id[a["finding_id"]]
        memo_text = _assemble_memo(finding, a)
        memo_path = MEMOS_DIR / f"{_sanitize(finding['finding_id'])}.md"
        memo_path.write_text(memo_text)
        memos_written.append(str(memo_path))

    return {
        "memos_written": sorted(memos_written),
        "summary": {"memo_count": len(memos_written)},
    }


def memos_gate(ctx, output: dict) -> None:
    adjudications = ctx.vars["adjudicate_findings"]["adjudications"]
    expected_count = sum(1 for a in adjudications if a["disposition"] != "accept")
    if output["summary"]["memo_count"] != expected_count:
        raise GateFailure(
            f"memo_count {output['summary']['memo_count']} != number of non-accept "
            f"adjudications ({expected_count})"
        )
    if len(output["memos_written"]) != expected_count:
        raise GateFailure(f"memos_written has {len(output['memos_written'])} entries, expected {expected_count}")

    findings_by_id = {f["finding_id"]: f for f in ctx.vars["detect_findings"]["findings"]}
    for a in adjudications:
        if a["disposition"] == "accept":
            continue
        finding = findings_by_id[a["finding_id"]]
        path = Path(MEMOS_DIR / f"{_sanitize(a['finding_id'])}.md")
        if not path.exists():
            raise GateFailure(f"expected memo file {path} was not written")
        text = path.read_text()
        expected_amount_str = _format_amount(finding)
        if expected_amount_str not in text:
            raise GateFailure(f"{path}: does not contain the deterministic amount string {expected_amount_str!r}")

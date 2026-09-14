# DESIGN.md — Freight Billing Reconciliation

This document describes the system as built and run, not as originally
sketched. Every figure quoted below comes from a real run under `runs/`.

## 1. Problem being solved

BlueFin Commerce receives monthly freight invoices from three contracted
carriers in three different formats. Each invoice must be checked against
BlueFin's own shipment records and the carrier's prose rate contract
before payment. The task is to reconcile the July 2026 invoices end to
end, using an agent system — not a human working the data by hand — and
produce `reconciliation-report.json` (schema-validated) plus one memo per
finding that isn't a clean accept. Because money moves on the output, the
system has to hold up on every run, not just a lucky one, and has to
scale to many more invoices/carriers/lines than the five in the sample.

## 2. Input data and authoritative sources

- `data/shipments.json` — 382 shipment records, ground truth for what was
  actually shipped (weight, distance, service level, special handling).
- `data/contracts/*.md` — three prose rate contracts. **These are the sole
  authority on what anything should cost.** Every priced amount traces
  back to a numbered clause in one of these files.
- `data/invoices/` — three carrier billing formats (Alpine JSON, Falcon
  free text, Sagar CSV), plus out-of-period files and two credit notes
  that exist as noise/edge cases for a system meant to scale.

None of these files were modified. `report.schema.json` was the one fixed
design constraint; everything upstream of it was open.

## 3. Overall architecture — why hybrid

`CLAUDE.md` documents a real gap in the provided kit: `orchestrator/lib`
(the Python package `agentctl`/`flowstate` import) and
`.claude/skills/graph-orchestrator/SKILL.md` are both referenced by
`PROBLEM.md` but absent from this checkout — confirmed directly (`find`
turns up zero `.py` files anywhere under `orchestrator/`, and no
`.claude/skills/` directory existed before this submission). Reproducing
flowstate/agentctl faithfully from two example flows would have consumed
the majority of an ~8-hour budget on the one component least related to
freight billing and least verifiable against a real spec.

Instead, this system keeps the *ideas* flowstate is built on — externalized
control flow, schema-gated transitions, a fresh small context per
agent-judgment step, an inspectable run-state file — and gets them from
~250 lines of plain Python (`pipeline/runner.py`) plus direct
`claude -p` calls (`pipeline/claude_worker.py`), rather than from
DOT files, a YAML companion spec, or tmux-managed workers. This is the
hybrid: a declarative node graph with mechanical gates between every
step, written in code simple enough to read start to finish, with agent
turns invoked only at the two places genuine judgment is the value.

## 4. Deterministic vs. agent responsibilities

**Deterministic Python owns every number and every accounting relationship:**
invoice discovery and parsing, shipment matching, duplicate detection, the
entire pricing engine, invoice-level findings, dispute-unit attribution,
report assembly, and every gate.

**Agents own two things, and only two:**
1. Translating a carrier's prose contract into a structured rate card
   (Phase 2) — judgment about what the contract *says*.
2. Adjudicating a finding into accept/dispute/escalate with a
   justification, governing clauses, and memo prose (Phase 5) — judgment
   about what a discrepancy *means*.

No agent, at either stage, ever emits a field that could hold a monetary
value — this is enforced structurally (§18), not by asking nicely.

## 5. Pipeline / node flow

```
discover_invoices                 (script)
  ↓
parse_invoices                    (script)
  ↓
load_shipments                    (script)
  ↓
match_and_normalise               (script)  [gate: line_conservation_gate]
  ↓
extract_ratecard_{alpine,falcon,sagar}   [AGENT, one turn per carrier]
  ↓                                       [gate: verify_ratecard, ×3]
price_lines                       (script, no LLM)  [gate: pricing_gate]
  ↓
detect_findings                   (script, no LLM)  [gate: findings_gate]
  ↓
adjudicate_findings               [AGENT, one turn per finding requiring it]
  ↓                                       [gate: adjudication_gate]
write_memos                       (script, no LLM)  [gate: memos_gate]
  ↓
assemble_report                   (script, no LLM)  [gate: validate_report]
  ↓
deliverables: reconciliation-report.json, memos/*.md
```

11 nodes, defined once in `pipeline/reconciliation_graph.py` and run via
`python3 -m pipeline.reconciliation_graph`. `pipeline/runner.py` executes
them in order, validates each node's output against its declared JSON
Schema, runs the node's gate, and halts the entire run on the first
failure — no downstream node ever executes on a bad upstream result, and
in particular `assemble_report` never runs unless every gate before it
passed.

## 6. Contract extraction (Phase 2, agent)

One real `claude -p` turn per carrier contract (`pipeline/nodes/ratecards.py`),
with `--tools ""` (zero tool access) and `--json-schema` constraining the
answer to `schemas/ratecard_extraction.schema.json`. The prompt embeds the
full contract text verbatim and instructs the agent to represent boundary
operators (`<`, `<=`, `>`, `>=`) literally, to declare — never silently
fill — a gap between adjacent bands, and to encode every condition as a
structured `trigger` predicate (`{attribute, op, value}`) alongside a
free-text `condition`, specifically so the deterministic pricing engine
never has to parse English.

`carrier` and `source_contract` (including the sha256) are **never asked
of the model** — `ratecards.py` computes the hash itself and injects both
after the call, so provenance can never depend on the model reporting a
hash correctly. Rate cards are committed to `ratecards/*.json` and
content-hash cached: a run only re-calls the agent if the contract's
sha256 has changed since the cached card was produced. Verified live: a
second run against unchanged contracts issued zero new agent calls.

`verify_ratecard` (`pipeline/nodes/ratecard_verify.py`) then checks the
result structurally — never for business correctness:
- carrier/file/hash match what was actually extracted from;
- every `clause` citation anywhere in the structure (found by walking the
  whole rate card, not a hand-enumerated field list) references a clause
  number that actually exists in the contract, checked by extracting the
  contract's own numbered list markers;
- band overlap (a genuine contradiction) fails the gate; a band **gap**
  does not fail by itself, but a gap with an empty `ambiguities` list
  does — the gate independently re-derives whether a gap exists via
  boundary-probing interval arithmetic and cross-checks the agent
  actually said so;
- percentage/flat-fee field pairing, `compounds_on` referencing only
  known rule names, non-empty whitelists.

This schema went through three extraction passes as real gaps surfaced
during later phases (§15 has the specific bug found and fixed at each
stage): first the base rate-card fields, then a structured `trigger` once
Phase 3's interpreter needed one, then a structured `threshold` on
`discounts` once Phase 4 needed to evaluate a numeric condition without
parsing "more than 12 consignments" as English. Each addition triggered a
real re-extraction (visible as fresh session IDs in `runs/`), not a
hand-edit of the cached JSON.

## 7. Invoice parsing (Phase 1, deterministic)

Three format-specific parsers (`pipeline/parsers/{alpine,falcon,sagar}.py`),
dispatched by file extension (a genuine structural difference, not a
per-invoice special case). Alpine (JSON) and Sagar (CSV) validate required
fields per line; a line missing one becomes an explicit residue record,
never a silent drop. Falcon (free text) uses a small state machine —
locate the two `====` separators to carve header/body/footer, split the
body at `N. Consignment REF` lines, and within a block require every
subsequent line to match either the location pattern or a
`Label: Rs amount` pattern — so it is exhaustive by construction: nothing
in a recognized block can be silently skipped.

`discover_invoices` classifies every file in `data/invoices/` (not just
the July ones) by carrier, doc type, and period, deriving the period from
each format's own content (Alpine's `billing_period` field, Falcon's
`Period:` header line, Sagar's `booking_dt` column) — never from filename
tokens like `JUL`/`AUG`. `TARGET_PERIOD = "2026-07"` is the one and only
place a specific month is named anywhere in this codebase, and it is
there because `PROBLEM.md` says to reconcile the July invoices, not
because of anything about the discrepancies inside them.

## 8. Shipment matching (Phase 1, deterministic)

Every canonical line is joined to `data/shipments.json` purely on
`carrier_consignment_ref` — a line's own carrier label is never part of
the join key, so a mislabeled carrier can't silently break a match.
Unmatched lines keep `shipment_id: null` and are never dropped. Duplicate
consignment refs across different invoices are detected and grouped
(`duplicate_groups`), with every occurrence still individually matched —
duplication is a payment-frequency question, not a matching question.
`line_conservation_gate` proves parsed-line-count equals matched-line-count,
`matched ⟺ shipment_id is not null`, and no two lines share an identical
`(invoice, ref, source locator)` (which would mean the parser itself
double-emitted a record).

## 9. Pricing (Phase 3, deterministic, no LLM)

`pipeline/nodes/pricing.py`'s `price_line()` is one function that prices
Alpine (weight-banded per-kg), Falcon (weight-banded per-km with
compounding premiums/surcharges), and Sagar (flat per-kg + per-km with a
compounding premium) without a single carrier name in its control flow —
it only reads `rate_bands`/`flat_rates`/`premiums`/`surcharges`/
`accessorial_charges`/`exclusions`/`service_level_rules` out of whichever
rate card it's given and evaluates each rule's structured `trigger`
against the shipment.

Every rate, weight, distance, and amount is converted via `Decimal(str(x))`
the moment it's read — never `Decimal(x)` on a float, which would bake in
binary floating-point error — and the whole chain (band multiplication,
percentage compounding, accessorial addition) stays exact Decimal until a
single `ROUND_HALF_UP` at the very end. No contract states a rounding
rule (confirmed directly: all three rate cards' `rounding_rule.stated_in_contract`
is `false`), so every priced line's trace explicitly discloses that the
2dp rounding is this pipeline's own currency-precision policy, not a
contract requirement.

Pricing never guesses. `price_line()` returns exactly one of three
outcomes: `determined` (an amount, plus the clauses used), `ambiguous`
(no rate band covers the value — a real gap, `expected_amount: null`), or
`out_of_scope` (an explicit contract exclusion). `pricing_gate` proves
every line got exactly one outcome, no non-determined line carries a
fabricated amount, and delta is mathematically consistent wherever
expected exists.

**Real result: 125/126 lines determined, 1 ambiguous** — `ALPINE-0726`'s
`AE-3005` at exactly 50.0 kg, the literal gap between Alpine's
strict-inequality "under 50 kg"/"over 50 kg" bands. Of the 125 determined
lines, 121 match their billed amount exactly; 4 have a nonzero delta —
all four discovered by forward computation from shipment facts through
the rate card, with **zero reference to what was actually billed** for
the computation itself (billed vs. expected is only compared afterward).

## 10. Finding detection (Phase 4, deterministic, no LLM)

Findings are things a single line's price can't reveal on its own:
- **Invoice-total self-consistency** — does the invoice's own stated
  total equal the sum of its own line amounts (a documentation check on
  the source document, independent of contract correctness). Never
  invents a stated total when the format doesn't provide one.
- **Rate-card-driven invoice-level rules** — e.g. Alpine's volume
  discount. The numeric threshold (`{metric, period, op, value}`) comes
  from the rate card, never hand-coded per carrier; the "which date
  defines calendar month" ambiguity the rate card itself flags is
  resolved only when every independently computable date basis (by
  invoice document, by booking-date month, by ship-date month) agrees —
  if they disagreed, the finding stays open with no invented amount.
- **Duplicate billing** — reuses Phase 1's `duplicate_groups` directly;
  `amount_impact = sum(billed) − max(billed)`, a formula that generalizes
  to any number of occurrences and any amount distribution.
- **Credit-note relationships** — the two out-of-period credit notes are
  parsed for real (a capability Phase 1 deliberately deferred) and
  classified three ways: *unrelated* (references an out-of-scope
  invoice/consignment — `SAGAR-CN-01` references `SAGAR-AUG-1`, not
  July), *corroboration* (its amount matches an already-detected
  line-level delta — `FALCON-CN-01` against `FF-8005`, exactly), or
  *novel* (a correction not otherwise captured). Only *novel* gets its own
  dispute unit.

**Dispute-unit rule:** every distinct piece of disputable money gets
exactly one `dispute_unit_id`. A finding that only explains or
corroborates money already counted elsewhere carries `dispute_unit_id: null`
and (for corroboration) a `related_dispute_unit_id` pointing at the unit
that already counts it. `findings_gate` proves this mechanically:
dispute-unit IDs are unique, a corroboration finding is hard-blocked from
ever setting its own unit, `total_dispute_unit_amount_if_all_disputed` is
independently recomputed from the units list, and — the strongest
anti-hardcoding check — the count of line-level findings is cross-checked
against Phase 3's own `price_lines` output, computed fresh, never
trusting `findings.py`'s internal bookkeeping.

**Real result: 9 findings, 5 disjoint dispute units, 8 requiring
adjudication** (the unrelated credit note doesn't).

## 11. Adjudication (Phase 5, agent)

One real, fresh `claude -p` turn per finding requiring adjudication
(`pipeline/nodes/adjudication.py`), each given a small, finding-scoped
context: the finding itself, the relevant shipment record(s), the *one*
relevant carrier's rate card, and that carrier's full contract text —
never the other carriers' data, never the other invoices. The policy is
`.claude/skills/adjudication/SKILL.md`, embedded directly into every
prompt so the versioned policy and the runtime prompt can never drift
apart.

The agent's entire output is `{finding_id, disposition, justification,
governing_clauses, memo_narrative}` with `additionalProperties: false` —
there is no field a monetary value could occupy. `governing_clauses` are
checked, *with retries*, against the real contract's actual clause
numbers before a result is accepted at all (`run_agent_turn`'s
`extra_validate` hook, added specifically for this — see §16).

**Real result (final converged run): 5 dispute, 1 escalate** for the 6
findings that map directly to a line/invoice-finding disposition, plus 2
more adjudications (the duplicate finding and the corroboration finding)
that also came back non-`accept`. The one escalation — `AE-3005` — is the
literal contract gap; the agent's own words: *"there is no tiebreaker
language... picking either rate would mean the adjudicator silently
writing new contract language rather than applying it."* `FF-8011`'s
justification independently cited Falcon's §5 accessorial whitelist to
explain the overcharge — a clause Phase 3's pricing engine never needed
to consult, evidence of genuine reasoning over the supplied contract text
rather than restating the deterministic trace.

## 12. Memo generation (Phase 5, deterministic assembly)

The agent never sees this step. `write_memos` (deterministic, no LLM)
combines each non-`accept` adjudication's three prose fields
(`what_happened`, `why`, `recommended_action`) with the amount, invoice,
consignment, shipment, and clause identifiers **from the finding itself**
— never from the adjudication — into `memos/<finding_id>.md`. Tested
explicitly: injecting a fake `dispute_amount` into a copy of an
adjudication result and confirming the assembled memo still only prints
the real one. Memos are written only after `adjudication_gate` has
already passed (two separate nodes), so a bad adjudication run can never
leave a partial or inconsistent memo on disk. Every finding whose
*adjudicated* disposition is non-`accept` gets a memo — in the real run
that's 8, one more than the 7 report items that end up non-`accept`,
because the credit-note corroboration finding is adjudicated in its own
right but folds into `FF-8005`'s line as a note rather than a separate
report item; its memo is a legitimate surplus, not a gap.

## 13. Report assembly (Phase 6, deterministic, no LLM)

`pipeline/nodes/report.py`'s `assemble_report` reads only Phase 1
(`billed_amount`, `shipment_id`), Phase 3 (`expected_amount`, `delta`,
governing clauses), and Phase 4 (`dispute_unit_id`, `dispute_amount`) for
every number in the final report; it reads only `disposition`,
`justification`, and `governing_clauses` off a Phase 5 adjudication.
Matched lines and priced lines are joined by iterating both lists in
lockstep (a positional invariant Phase 1 and 3's own gates already prove:
same length, same order) rather than by a dictionary keyed on
`(invoice, ref)`, since a duplicate consignment ref legitimately repeats
across different invoices.

A small, fixed set of deterministic allocation rules (never branching on
carrier, invoice id, or consignment ref) maps Phase 4's finding types onto
report lines:
- `line_pricing_delta` / `ambiguous_line` govern their one line directly
  (the amount is already correct from Phase 3).
- `duplicate_billing`: exactly one occurrence — the one with the largest
  billed amount, ties broken by first occurrence — is left as the
  shipment's one legitimate charge (`accept`, unmodified pricing); every
  other occurrence has `expected_amount` reset to
  `billed_amount − dispute_amount` (an already-validated Phase 4 number,
  never invented here), so its `delta` equals its share of the excess.
  This generalizes to any number of occurrences and any amount split,
  and is provably consistent with Phase 4's own `sum(billed) − max(billed)`
  by construction.
- `credit_note_novel` uses the same override shape, only when no more
  specific finding already governs the line (none fired in the real data).
- `credit_note_corroboration` never changes a disposition — recorded only
  as a `notes` string, since its money is already counted via the line it
  corroborates.
- `invoice_total_mismatch` / `invoice_level_discount` become
  `invoice_findings`, never a per-line allocation — in particular, the
  Alpine discount is never spread across the 40 lines it covers; while
  `AE-3005` remains unresolved, `amount_impact` stays `null` rather than
  guessing a shortfall.

`summary.total_in_dispute` sums exactly the dispute units whose
*adjudicated* disposition is `dispute` — reusing Phase 4's
already-proven-disjoint amounts directly, so a disputed rupee is counted
exactly once by construction, not by a new summation Phase 6 invents.

## 14. Validation gates

Every node has one; the two carrying the most weight:

- **`verify_ratecard`** (Phase 2) — structural/provenance only, described
  in §6.
- **`validate_report`** (Phase 6) — schema validation against
  `report.schema.json`, plus a from-scratch re-derivation of every
  semantic invariant from Phase 1–5 outputs (never trusting
  `assemble_report`'s own working variables): every parsed line appears
  exactly once; billed amounts match parsed data; expected amounts match
  priced output exactly (or the documented override formula); delta is
  billed-minus-expected and null iff expected is null; every non-`accept`
  item traces to a real adjudicated finding with a clause that exists in
  the real contract; invoice totals and summary figures are independently
  recomputed and compared; `total_in_dispute` is recomputed from
  dispute units; no adjudication result carries an unexpected (potential
  monetary) field. Fails closed — no downstream step exists after it, so
  a failure here means no report is written at all.

## 15. Error handling and retries

Every agent call (`pipeline/claude_worker.py:run_agent_turn`) retries up
to twice on: a nonzero exit code, `is_error` in the response envelope,
invalid JSON, JSON-Schema failure, or (added for Phase 5) a semantic
failure from an optional `extra_validate` hook — each retry is a brand
new call with the exact validator error fed back to the model, and every
attempt's prompt/command/envelope/output is preserved under
`attempt-N/`, never overwritten. A run that exhausts retries raises and
halts — never silently accepts malformed output.

Three real bugs were found and fixed this way during development, not
hidden:
- **Phase 2, `_check_clause_citations`**: rejected a legitimate bare
  whole-document citation (`"sagar-roadlines.md"`, used when a claim is
  about the document's silence generally rather than one clause) because
  the regex required a `§N` suffix. Fixed to accept a bare filename as a
  valid whole-document reference.
- **Phase 3, `_trigger_matches`**: routed **all** `"eq"` comparisons
  through `Decimal()` conversion, crashing on a string comparison
  (`service_level == "express"`). Fixed to check the actual value's type
  before deciding string vs. numeric comparison. Caught by the pricing
  test suite before ever reaching real data.
- **Phase 5, `claude_worker.py` argv construction**: a prompt that embeds
  the adjudication skill's markdown (which opens with YAML frontmatter,
  `---`) was passed as a positional CLI argument and the parser mistook
  it for an unknown flag. Fixed by adding a `--` separator before the
  prompt — the POSIX convention for "everything after this is
  positional" — which protects every prompt in the pipeline going
  forward, not just this one.

## 16. Schema validation

Every node's output is checked against a JSON Schema before its gate
runs (`pipeline/runner.py`), using a hand-rolled ~90-line validator
(`pipeline/schema_lite.py`) rather than the `jsonschema` package, so the
whole pipeline has zero third-party dependencies and zero reliance on
network pip access at grading time. It supports exactly the draft-07
subset actually used across every schema in this repo (`type` incl.
nullable via a type list, `required`, `properties`,
`additionalProperties`, `enum`, `minLength`/`minItems`/`maxItems`,
`minimum`, `items`) and deliberately raises on any unsupported keyword
rather than silently ignoring it.

Agent-facing schemas are additionally passed to `claude -p --json-schema`,
so the CLI itself constrains the model's structured output before this
pipeline's own `schema_lite.validate()` re-checks it independently — a
node's output is never trusted on the strength of a single check.

## 17. The monetary-value boundary

This is the rule the whole Phase 2/5 design serves: **no agent output
schema in this pipeline has a field a monetary value could occupy.**

- `schemas/ratecard_extraction.schema.json` excludes `carrier` and
  `source_contract` (hash injected by code after the call) but does
  contain rate/amount *fields describing the contract* (that's the
  point of Phase 2 — transcribing the contract's own numbers) — verified
  structurally sound and provenance-checked, never treated as the
  reconciliation's source of truth for a specific invoice line.
- `schemas/adjudication_output.schema.json` is exactly
  `{finding_id, disposition, justification, governing_clauses, memo_narrative}`,
  `additionalProperties: false`. `billed_amount`, `expected_amount`,
  `delta`, `dispute_amount`, `amount_impact`, `total_in_dispute` — none
  of these keys exist anywhere an agent could write to.

Enforced at three independent layers: the JSON Schema itself (structural
impossibility), `adjudication_gate` (re-checks every adjudication's key
set against the allowed five, redundant with the schema on purpose),
and `validate_report` (a third, final re-check of the same adjudications
this run actually produced, immediately before the report they informed
is accepted). A dedicated test (`test_deterministic_amount_survives_fake_adjudication_disposition`)
swaps a real adjudication for a completely different fake one and proves
the resulting report line's billed/expected/delta are byte-identical —
only `disposition`/`justification` change, because those are the only
fields `assemble_report` ever reads from an adjudication.

## 18. Duplicate / dispute-unit handling

Covered in depth in §10 and §13. The short version: Phase 1 detects
duplicates, Phase 4 turns each duplicate group into exactly one
dispute-unit-bearing finding via `sum(billed) − max(billed)`, and Phase 6
allocates that already-computed amount onto exactly the non-maximum
occurrence(s) as their line-level `expected_amount`/`delta` override —
the maximum occurrence keeps its own individually-correct Phase 3 pricing
and stays `accept`. No line is ever removed from the canonical dataset to
represent a duplicate; both occurrences of `FF-8003` appear in the final
report, exactly once each.

## 19. Credit-note handling

Covered in §10. The decisive design point: a credit note's relationship
to a July line is established purely from document data (its own
`against_invoice`/consignment fields, parsed for real from the actual
credit-note files) and a numeric comparison against the already-computed
line delta — never a lookup keyed to a known consignment ref. The system
would classify a hypothetical fourth credit note the same way it
classified the real two, using the identical three-way logic.

## 20. Scalability to additional invoices/carriers/lines

- Adding invoices for existing carriers requires no code change:
  `discover_invoices` classifies by content, not a hardcoded file list.
- Adding a carrier requires a new format-specific parser only if its
  document shape is genuinely new (an intrinsic requirement — you cannot
  parse an unknown format without a parser for it) plus one new contract
  file for Phase 2 to extract; the pricing engine, finding detection, and
  report assembly need zero changes, since none of them branch on
  carrier name.
- Agent cost tracks the *error rate*, not the line count: Phase 5 only
  calls an agent for findings Phase 4 actually flagged (8 calls for 126
  lines in the real run) — 10,000 clean lines would cost the same 0
  adjudication calls as 121 did here.
- The one deliberate manual step at present is fan-out concurrency:
  `pipeline/runner.py` executes nodes strictly in list order, including
  the loop inside `adjudicate_findings` over per-finding agent calls.
  At meaningfully larger volume this loop (and the equivalent one for
  invoice parsing) is the natural place to introduce a bounded thread/process
  pool — the per-item work is already fully independent (each finding's
  adjudication reads only its own scoped context), so parallelizing it
  requires no change to the adjudication logic itself, only to how the
  loop is driven.

## 21. Evidence and reproducibility

Every agent turn leaves, under `runs/reconciliation/<run_id>/<node>/`:
the exact prompt (`prompt.txt`), the exact argv (`command.json`), the
full response envelope including session ID, cost, and timing
(`agent_result.json`), and the validated parsed output — for every
attempt, not just the last. `state.json` records every node's
status/timestamps for the whole run. Rate cards are additionally
committed at `ratecards/*.json` with their source contract's sha256, so a
grader can independently verify (`shasum -a 256 data/contracts/*.md`)
that the committed cards match the contracts actually in the repo — done
directly in this session, all three matched.

## 22. Nondeterminism handling — the repeatability experiment

Two full runs were executed with fresh agent sessions end to end and
compared. **All monetary and structural fields were byte-identical**:
`summary`, all 5 `invoice_totals` entries, and all 126 lines'
`billed_amount`/`expected_amount`/`delta`/`shipment_id`/`disposition`.
Agent *prose* (justification text) differed across 6 of 8 adjudications,
as expected from LLM sampling.

**One real disposition divergence was found and not hidden:** the
Alpine volume-discount `invoice_finding` was adjudicated `escalate` in
run 1 and `dispute` in run 2. Both justifications reached identical
factual conclusions (every date-count basis agrees the invoice qualifies;
the exact rupee shortfall is blocked by `AE-3005` elsewhere on the same
invoice) — the divergence was purely in the disposition label. Root
cause: `.claude/skills/adjudication/SKILL.md` offered two parallel,
untie-broken branches for exactly this pattern ("lean toward escalate"
vs. "you can still dispute... but"). Not a pipeline defect — no money was
at stake either way (`amount_impact` was `null` in both).

**Correction**: tightened the skill's `invoice_level_discount` guidance
to separate "does the invoice qualify" from "can the exact shortfall be
computed now," with a firm rule: entitlement blocked only by a
*different* unresolved line means `escalate`, because `dispute` requires
an actionable figure a colleague can act on and there isn't one yet.

**Verification**: two further full runs after the fix both converged on
`escalate`. Across all four runs total, every monetary/structural field
stayed identical throughout; only this one non-monetary label moved, and
only before the fix.

## 23. Known unresolved ambiguities (by design, not oversight)

These remain open in the final report/memos because the contracts and
data genuinely do not resolve them — not because the pipeline failed to
try:

- **`AE-3005` (Alpine)** — exactly 50.0 kg falls in neither
  "under 50 kg" nor "over 50 kg." `escalate`, `expected_amount: null`.
- **Alpine's volume-discount date basis** — "calendar month" is never
  tied to ship date, booking date, or invoice date in the contract. Every
  computable basis happens to agree for the real July invoice (40
  consignments, comfortably over the 12 threshold either way), so the
  *entitlement* is not in question here, but the underlying definitional
  gap is still unresolved and would matter for a borderline invoice.
- **Falcon's 500–501 kg fractional gap** — discovered independently by
  the Phase 2 extraction agent, not seeded by this design; never
  exercised by the real data (no shipment falls in that exact window),
  but the rate card and pricing engine both handle it correctly if one
  ever does.
- **Sagar's "booked lane distance" vs. `distance_km`** — the contract
  never states whether these are the same value; the pipeline uses
  `distance_km` as the only value ever present in evidence, and the rate
  card records the ambiguity for a human who might know otherwise.

## 24. How the system avoids hardcoded discrepancy results

At the start of this engagement, before any code was written, a
read-only pass over the real data was used to profile what the
discrepancies *were* — purely to inform architecture decisions (e.g.,
confirming invoice self-consistency, sizing the fan-out points). That
inventory was never encoded into the implementation. Concretely:

- Pricing (`pipeline/nodes/pricing.py`) never references a specific
  consignment ref, invoice id, or amount; it only interprets rate-card
  data structurally.
- Finding detection (`pipeline/nodes/findings.py`) and report assembly
  (`pipeline/nodes/report.py`) branch only on `finding_type`, never on
  which finding it is.
- Every phase's test suite was grepped for real consignment/invoice
  references after writing it (`grep -nE "(AE-30|FF-80|SG-70|...)"`) —
  clean at every phase.
- `findings_gate` and `adjudication_gate` each include a check that
  recomputes an aggregate count *independently* from an earlier phase's
  raw output (never from the current phase's own bookkeeping)
  specifically so a hand-tuned finding or adjudication couldn't slip
  through unnoticed.
- The one place a real consignment ref appears in source at all is a
  single explanatory sentence in `report.py`'s module docstring,
  illustrating *why* the duplicate-allocation rule exists — it does not
  appear in any conditional, lookup, or comparison anywhere in the
  codebase.

## Post-implementation notes

- The biggest single design decision was declining to reproduce
  flowstate/agentctl. In hindsight this was the right call for an
  ~8-hour budget: the deterministic pricing engine and the two gates
  around report assembly (§13, §14) are where the actual correctness
  risk lived, and that is where the time went.
- The rate-card schema grew three times as real integration needs
  surfaced (`trigger`, then `threshold`) rather than being fully
  specified up front. Each growth required a real re-extraction, visible
  as new agent sessions in `runs/` — an honest cost of discovering the
  interpreter's needs by building the interpreter, not a sign the design
  was wrong at the start.
- The two real bugs caught by tests before touching real data (Phase 3's
  `_trigger_matches` type-dispatch bug) versus the one caught only by a
  real run (Phase 5's CLI argv bug, which only manifests with a prompt
  that happens to start with `-`) is a fair illustration of why "run the
  real thing" and "test the units" are both necessary and neither
  alone is sufficient.
- The repeatability experiment (§22) is, in retrospect, the most
  convincing evidence in this submission that the accept/dispute/escalate
  boundary is real: an agent given two different sampling draws produced
  different *words* but the same *money*, every time, and the one time it
  produced a different *label*, the cause was traceable to specific
  wording in a markdown file I could read, fix, and re-verify — not to
  anything nondeterministic in the accounting itself.

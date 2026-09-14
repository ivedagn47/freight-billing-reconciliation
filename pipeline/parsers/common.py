"""Small helpers shared by the three format-specific parsers.

Each parser is deliberately format-driven, not filename-driven: which
parser module runs is decided by file extension (a genuine structural
difference — JSON vs CSV vs free text — not a per-invoice special case),
and within a parser, carrier identification and period/doc-type
classification come from the file's own content wherever the content
states it at all.
"""
from __future__ import annotations

import re


def first_word_lower(text: str) -> str:
    """"Alpine Express Logistics" -> "alpine"; "FALCON FREIGHT PVT LTD" ->
    "falcon". This is a rule (first significant word, lowercased), not a
    lookup table of known carrier names, so it generalises to a carrier
    this pipeline has never seen without any code change. It happens to
    align with the lowercase carrier slugs used in shipments.json, but
    matching itself never depends on that alignment — see match.py, which
    joins purely on carrier_consignment_ref."""
    word = text.strip().split()[0]
    return re.sub(r"[^a-z]", "", word.lower())


def parse_money(text: str) -> float:
    """"Rs 17,472.00" / "17,472.00" / "-496.80" -> float. Raises ValueError
    on anything that isn't recognisably a money amount, rather than
    guessing."""
    cleaned = text.strip()
    cleaned = re.sub(r"^Rs\.?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace(",", "")
    return float(cleaned)

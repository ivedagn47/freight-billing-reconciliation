"""Exact decimal money arithmetic. No stage of the pricing engine performs
currency math in binary float -- every rate, weight, distance, and amount
is converted to Decimal via its string representation (never via float
directly, which would bake in binary floating-point error) before any
arithmetic happens, and the entire pricing chain stays in Decimal until
the single, final quantize to the nearest paisa.

No contract states a rounding convention (confirmed directly from Phase
2's extraction -- every rate card's rounding_rule.stated_in_contract is
false). ROUND_HALF_UP to 2 decimal places is this pipeline's own declared
policy for expressing a final amount in INR's smallest unit, applied
uniformly and disclosed in every pricing trace -- it is not presented as
something the contracts require.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")
ROUNDING_POLICY = {
    "mode": "ROUND_HALF_UP to the nearest 0.01 INR",
    "note": "Not specified by any contract (all three rate cards mark rounding_rule.stated_in_contract=false). Applied as this pipeline's own currency-precision policy, since INR has no unit smaller than the paisa.",
}


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def round_money(x: Decimal) -> Decimal:
    return x.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

"""Exact quantity parsing and conversion.

Quantities keep the amount as the supplier expressed it and the unit as its
own column.  A separate, explicitly annotated base amount is computed for
cross-supplier comparison.  Conversions are exact rationals rounded once, at a
scale declared by the supplier mapping; nothing passes through float.
"""

from decimal import Decimal, InvalidOperation
from fractions import Fraction
import re

from .canon import q, qs, frac_text

# Length base = mm, mass base = g, count base = item, volume base = ml.
_LENGTH = {
    "mm": (Fraction(1), Fraction(1)), "millimeter": (Fraction(1), Fraction(1)),
    "millimetre": (Fraction(1), Fraction(1)),
    "cm": (Fraction(10), Fraction(1)), "centimeter": (Fraction(10), Fraction(1)),
    "centimetre": (Fraction(10), Fraction(1)),
    "m": (Fraction(1000), Fraction(1)), "meter": (Fraction(1000), Fraction(1)),
    "metre": (Fraction(1000), Fraction(1)),
    "in": (Fraction(254, 10), Fraction(1)), "inch": (Fraction(254, 10), Fraction(1)),
    "ft": (Fraction(3048, 10), Fraction(1)), "foot": (Fraction(3048, 10), Fraction(1)),
    "feet": (Fraction(3048, 10), Fraction(1)),
}
_MASS = {
    "g": (Fraction(1), Fraction(1)), "gram": (Fraction(1), Fraction(1)),
    "kg": (Fraction(1000), Fraction(1)), "kilogram": (Fraction(1000), Fraction(1)),
    "mg": (Fraction(1, 1000), Fraction(1)),
    "lb": (Fraction(45359237, 100000), Fraction(1)),
    "lbs": (Fraction(45359237, 100000), Fraction(1)),
    "pound": (Fraction(45359237, 100000), Fraction(1)),
    "oz": (Fraction(28349523125, 1000000000), Fraction(1)),
    "ounce": (Fraction(28349523125, 1000000000), Fraction(1)),
}
_COUNT = {
    "item": (Fraction(1), Fraction(1)), "each": (Fraction(1), Fraction(1)),
    "ea": (Fraction(1), Fraction(1)), "pcs": (Fraction(1), Fraction(1)),
    "pc": (Fraction(1), Fraction(1)), "piece": (Fraction(1), Fraction(1)),
    "carton": (Fraction(1), Fraction(1)), "ctn": (Fraction(1), Fraction(1)),
    "box": (Fraction(1), Fraction(1)),
}
_VOLUME = {
    "ml": (Fraction(1), Fraction(1)), "milliliter": (Fraction(1), Fraction(1)),
    "millilitre": (Fraction(1), Fraction(1)),
    "l": (Fraction(1000), Fraction(1)), "liter": (Fraction(1000), Fraction(1)),
    "litre": (Fraction(1000), Fraction(1)),
}

DIMENSIONS = {
    "length": (_LENGTH, "mm"),
    "mass": (_MASS, "g"),
    "count": (_COUNT, "item"),
    "volume": (_VOLUME, "ml"),
}


class UnknownUnit(ValueError):
    pass


def dimension_of(unit):
    if unit is None:
        return None
    key = unit.strip().lower()
    for dim, (table, _base) in DIMENSIONS.items():
        if key in table:
            return dim
    return None


def to_fraction(amount):
    d = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    num, den = d.as_integer_ratio()
    return Fraction(num, den)


def _round_fraction(frac, scale):
    # Integer arithmetic only: exact half-even rounding, no float/Decimal
    # division context surprises.
    scaled = frac * (10 ** scale)
    n, d = scaled.numerator, scaled.denominator
    sign = -1 if n < 0 else 1
    n = abs(n)
    whole, rem = divmod(n, d)
    twice = rem * 2
    if twice > d or (twice == d and whole % 2 == 1):
        whole += 1
    text = str(whole).rjust(scale + 1, "0")
    if scale:
        text = text[:-scale] + "." + text[-scale:]
    return ("-" if sign < 0 else "") + text


def to_base(amount, unit, scale=9, decimal_style="dot"):
    """Return (exact base amount text, base unit, conversion note or None)."""
    dim = dimension_of(unit)
    if dim is None:
        raise UnknownUnit(f"unknown unit {unit!r}")
    table, base = DIMENSIONS[dim]
    key = unit.strip().lower()
    num, den = table[key]
    amount = amount if isinstance(amount, Decimal) else q(amount, decimal_style)
    amount_frac = to_fraction(amount)
    frac = amount_frac * num / den
    dec = _round_fraction(frac, scale)
    exact_matrix = [amount_frac.numerator, amount_frac.denominator,
                    num.numerator, num.denominator, den.numerator, den.denominator]
    return dec, base, {
        "source_amount": qs(amount),
        "source_unit": key,
        "factor": frac_text(num / den),
        "method": "exact_rational",
        "rounding": "half_even",
        "scale": scale,
        "base_amount": dec,
        "base_unit": base,
        "exact_parts": exact_matrix,
    }


def canonical_zero(unit, scale=9):
    if unit is None or dimension_of(unit) is None:
        return None, None, None
    return to_base(Decimal(0), unit, scale)


_QUANTITY = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*([A-Za-z]*)\s*$")


def parse_quantity_text(text, default_unit=None, decimal_style="auto"):
    """Split '12.5 kg' / '12,5 KG' / '7' into (amount text, unit)."""
    text = (text or "").strip()
    if not text:
        return None, default_unit
    m = _QUANTITY.match(text)
    if not m:
        raise ValueError(f"unparseable quantity {text!r}")
    number, unit = m.group(1), m.group(2)
    amount = q(number, decimal_style)
    return qs(amount), (unit.lower() if unit else default_unit)


def fraction_digits(text):
    text = (text or "").strip()
    m = re.match(r"^\s*[+-]?\d+(?:[.,](\d+))?\s*[A-Za-z]*\s*$", text)
    return len(m.group(1)) if m and m.group(1) else 0

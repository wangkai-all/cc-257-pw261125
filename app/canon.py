"""Canonicalisation helpers shared by parsers and storage.

The canonical text forms are deliberately boring and deterministic so that
round-trip checks can compare the in-memory representation of a parsed
document with the same representation rebuilt from storage.
"""

import hashlib
import json
import re
from decimal import Decimal
from decimal import ROUND_HALF_EVEN


def q(value, decimal_style="auto"):
    """Exact Decimal from supplier text.

    ``decimal_style`` is "dot", "comma" or "auto".  This matters for values
    such as ``1.234``: Northwind means one-point-two-three-four, Helios means
    one thousand two hundred thirty-four.  The supplier mapping declares its
    convention, so we never guess when a mapping is available.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    s = str(value).strip()
    if s.lower() in ("", "nan", "none", "null", "-", "–", "—"):
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.endswith("-"):  # trailing credit sign used by a few ERPs
        negative, s = True, s[:-1]
    s = re.sub(r"[€$£¥₹]", "", s)
    s = re.sub(r"\b(USD|EUR|GBP|JPY|CNY|RMB)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\s'   ]", "", s)
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    if not s:
        return None
    if decimal_style == "comma":
        if s.count(",") > 1:
            raise ValueError(f"ambiguous numeric text {value!r}")
        s = s.replace(".", "").replace(",", ".")
    elif decimal_style == "dot":
        s = s.replace(",", "")
    else:
        has_dot, has_comma = "." in s, "," in s
        if has_dot and has_comma:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif has_comma:
            parts = s.split(",")
            if len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) > 1:
                s = s.replace(",", "")
            elif len(parts) == 2 and 1 <= len(parts[1]) <= 2:
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")
    try:
        d = Decimal(s)
    except Exception as exc:
        raise ValueError(f"unparseable number {value!r}") from exc
    return -d if negative else d


def qs(value):
    """Stable text for a Decimal, never using exponent notation."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = q(value)
    return format(value, "f")


def q_round(value, scale):
    if value is None:
        return None
    return value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN)


def j(obj):
    """Canonical JSON used for records, source documents and tripwire compares."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def frac_text(frac):
    return f"{frac.numerator}/{frac.denominator}"

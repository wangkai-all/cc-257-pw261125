"""Mapping registry and parser entry points.

A mapping is data, not code.  Changing a mapping creates a new version; every
interpretation records the mapping version that produced it.  Replaying an old
version is therefore always distinguishable from a fresh interpretation.
"""

import copy

from . import northwind, helios, orion

PARSERS = {
    "northwind_csv": northwind,
    "helios_json": helios,
    "orion_fixed": orion,
}

SUPPLIERS = ("northwind", "helios", "orion")
FORMAT_BY_SUPPLIER = {
    "northwind": "northwind_csv",
    "helios": "helios_json",
    "orion": "orion_fixed",
}


def _mapping(supplier, version, **overrides):
    base = {
        "supplier": supplier,
        "format": FORMAT_BY_SUPPLIER[supplier],
        "version": version,
        "name": f"{supplier} default v{version}",
    }
    base.update(overrides)
    return base


NORTHWIND_V1 = _mapping(
    "northwind", 1,
    decimal_style="dot", base_scale=3, quantity_scale=3,
    unit_aliases={},
    columns={
        "record": "REC", "document": "DOCUMENT", "revision": "REV",
        "order": "ORDER", "issued": "ISSUED", "currency": "CCY",
        "corrects": "CORRECTS", "line": "LINE", "action": "ACTION",
        "sku": "SKU", "description": "DESCRIPTION", "quantity": "QTY",
        "unit": "UOM", "price": "PRICE", "amount": "AMOUNT",
        "target": "TARGET",
    },
)
NORTHWIND_V2 = _mapping(
    "northwind", 2,
    decimal_style="dot", base_scale=9, quantity_scale=6,
    unit_aliases={"kgs": "kg", "mtrs": "m", "pcs.": "pcs", "ltr": "l", "lbs.": "lb"},
    columns=NORTHWIND_V1["columns"],
)

HELIOS_V1 = _mapping(
    "helios", 1,
    decimal_style="comma", base_scale=6, quantity_scale=4,
    description_lang="de", unit_aliases={},
)
HELIOS_V2 = _mapping(
    "helios", 2,
    decimal_style="comma", base_scale=9, quantity_scale=6,
    description_lang="en",
    unit_aliases={"kgs": "kg", "mtr": "m", "ltr": "l", "pcs.": "pcs"},
)

ORION_V1 = _mapping(
    "orion", 1,
    base_scale=3, quantity_scale=3,
    unit_aliases={},
)
ORION_V2 = _mapping(
    "orion", 2,
    base_scale=9, quantity_scale=3,
    unit_aliases={"kgs": "kg", "mtr": "m", "ltr": "l"},
)

PARSER_REVISION = "2026-11-17.1"

DEFAULT_MAPPINGS = [NORTHWIND_V1, HELIOS_V1, ORION_V1]
UPGRADE_MAPPINGS = [NORTHWIND_V2, HELIOS_V2, ORION_V2]


def all_supplier_mappings():
    return [m for m in DEFAULT_MAPPINGS + UPGRADE_MAPPINGS]


def validate_mapping(mapping):
    fmt = mapping.get("format")
    if fmt not in PARSERS:
        raise ValueError(f"unknown format {fmt!r}")
    if not isinstance(mapping.get("version"), int):
        raise ValueError("mapping version must be an integer")
    if mapping.get("supplier") not in SUPPLIERS:
        raise ValueError("mapping supplier is not known")
    if mapping.get("base_scale") is None:
        raise ValueError("mapping must declare base_scale")
    return True


def parse(document, mapping):
    """Parse a decoded SourceDocument using one mapping version.

    Returns a dict with document fields and canonical lines.  Parser errors are
    raised as ParseError so the caller can preserve the source and record a
    failed interpretation instead of losing the payload.
    """
    validate_mapping(mapping)
    module = PARSERS[mapping["format"]]
    return module.parse(document, mapping)


def clone(mapping):
    return copy.deepcopy(mapping)


class ParseError(ValueError):
    def __init__(self, message, *, line=None, detail=None):
        super().__init__(message)
        self.line = line
        self.detail = detail or {}

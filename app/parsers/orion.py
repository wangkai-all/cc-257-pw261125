"""Orion: fixed-width EBCDIC (cp037) records with zoned-decimal quantities.

Every record is 80 bytes.  Column positions are 1-based and inclusive, exactly
as printed in the supplier's layout:

  detail (D/C/E)
    1        record type
    2-16     document number (15)
    17-19    revision (3)
    20-35    order reference (16)
    36-43    issued, YYYYMMDD (8)
    44-46    currency (3)
    47-50    line number (4)
    51       action A/C/D (1)
    52-66    SKU (15)
    67-76    quantity, zoned decimal, 3 implied fraction digits (10)
    77-79    unit (3)
    80       filler

  continuation (X)
    1        record type
    2-16     document number (15)
    17-20    line number this continuation belongs to (4)
    21-60    description (40)
    61-68    unit price, zoned decimal, 2 implied fraction digits (8)
    69-80    filler

  header (H) and trailer (T) carry document identity and control totals.

Zoned decimal carries its sign in the final byte's high nibble (C/F positive,
D negative), so decoding happens on the raw bytes, never on a decoded string.
A record whose length is not 80 is retained as unparsed evidence; it is never
truncated or padded to fit.
"""

from decimal import Decimal

from ..canon import qs
from ..units import to_base, UnknownUnit

RECORD_LENGTH = 80

FIELDS = {
    "record_type": (1, 1),
    "document": (2, 16),
    "revision": (17, 19),
    "order": (20, 35),
    "issued": (36, 43),
    "currency": (44, 46),
    "line": (47, 50),
    "action": (51, 51),
    "sku": (52, 66),
    "quantity": (67, 76),
    "unit": (77, 79),
}
CONTINUATION_FIELDS = {
    "record_type": (1, 1),
    "document": (2, 16),
    "line": (17, 20),
    "description": (21, 60),
    "price": (61, 68),
}
HEADER_FIELDS = {
    "record_type": (1, 1),
    "document": (2, 16),
    "revision": (17, 19),
    "order": (20, 35),
    "issued": (36, 43),
    "currency": (44, 46),
}
TRAILER_FIELDS = {
    "record_type": (1, 1),
    "total_records": (2, 5),
    "checksum": (6, 9),
    "corrects": (10, 24),
}

EBCDIC_DIGIT = {}
for i in range(10):
    EBCDIC_DIGIT[0xF0 + i] = (i, 0)
    EBCDIC_DIGIT[0xC0 + i] = (i, 0)
    EBCDIC_DIGIT[0xD0 + i] = (i, -1)


def decode_zoned(raw, scale):
    """Decode zoned decimal bytes.  Returns (Decimal, metadata) or (None, meta)."""
    if not raw:
        return None, {"error": "empty numeric field"}
    if all(byte in (0x00, 0x40) for byte in raw):
        return None, {"error": "blank numeric field", "bytes": raw.hex()}
    digits = []
    sign = 0
    for idx, byte in enumerate(raw):
        if byte not in EBCDIC_DIGIT:
            return None, {"error": f"invalid zoned byte 0x{byte:02X} at {idx}",
                          "bytes": raw.hex()}
        digit, s = EBCDIC_DIGIT[byte]
        if idx < len(raw) - 1 and s:
            return None, {"error": f"sign nibble before final byte at {idx}",
                          "bytes": raw.hex()}
        digits.append(digit)
        if idx == len(raw) - 1:
            sign = s
    text = "".join(str(d) for d in digits)
    value = int(text or "0")
    exact = Decimal(value).scaleb(-scale)
    if sign < 0:
        exact = -exact
    return exact, {
        "zoned_bytes": raw.hex(),
        "digits": text,
        "scale": scale,
        "negative": sign < 0,
        "precision": len(text),
        "encoding": "EBCDIC zoned decimal",
    }


def _text(raw):
    return raw.decode("cp037").rstrip(" ")


def _field(body, layout, name):
    if name not in layout:
        return b""
    start, end = layout[name]
    return body[start - 1:end]


def parse(doc, mapping):
    from . import ParseError

    document = {
        "supplier": doc.supplier,
        "filename": doc.filename,
        "kind": "order",
        "document_number": None,
        "revision": 1,
        "order_reference": None,
        "issued_at": None,
        "currency": None,
        "corrects": None,
        "totals": {},
        "declared_filename": doc.declared_filename,
        "mapping_version": mapping["version"],
        "format": mapping["format"],
        "encoding": doc.encoding,
        "eol": doc.eol,
        "sha256": doc.sha256,
        "record_length": RECORD_LENGTH,
        "fields": {k: list(v) for k, v in FIELDS.items()},
        "continuation_fields": {k: list(v) for k, v in CONTINUATION_FIELDS.items()},
        "lines": [],
        "warnings": [],
        "unparsed_lines": [],
        "declared_counts": {},
        "header_records": [],
        "trailer_records": [],
    }

    base_scale = mapping["base_scale"]
    aliases = mapping.get("unit_aliases") or {}
    total_declared = checksum_declared = None
    detail_by_line = {}

    for line in doc.lines:
        try:
            body = line.raw.encode("cp037")
        except UnicodeEncodeError:
            body = b""
        if not body.strip():
            line.kind = "blank"
            continue
        if len(body) != RECORD_LENGTH:
            line.kind = "unparsed"
            document["unparsed_lines"].append({
                "source_lines": [line.number], "raw": line.raw,
                "reason": f"record is {len(body)} bytes, expected {RECORD_LENGTH}"})
            document["warnings"].append(
                f"line {line.number}: record is {len(body)} bytes, expected "
                f"{RECORD_LENGTH}; retained unparsed")
            continue

        rec_type = _text(_field(body, FIELDS, "record_type"))
        if rec_type == "H":
            line.kind = "header"
            document["header_records"].append({
                "source_lines": [line.number], "raw": line.raw})
            doc_no = _text(_field(body, HEADER_FIELDS, "document"))
            if doc_no:
                document["document_number"] = doc_no
            rev = _text(_field(body, HEADER_FIELDS, "revision"))
            if rev.isdigit():
                document["revision"] = int(rev)
            order = _text(_field(body, HEADER_FIELDS, "order"))
            if order:
                document["order_reference"] = order
            issued = _text(_field(body, HEADER_FIELDS, "issued"))
            if len(issued) == 8 and issued.isdigit():
                document["issued_at"] = f"{issued[:4]}-{issued[4:6]}-{issued[6:]}"
            currency = _text(_field(body, HEADER_FIELDS, "currency"))
            if currency:
                document["currency"] = currency
            continue
        if rec_type == "T":
            line.kind = "trailer"
            document["trailer_records"].append({
                "source_lines": [line.number], "raw": line.raw})
            total = _text(_field(body, TRAILER_FIELDS, "total_records"))
            checksum = _text(_field(body, TRAILER_FIELDS, "checksum"))
            corrects = _text(_field(body, TRAILER_FIELDS, "corrects"))
            if total.isdigit():
                total_declared = int(total)
            if checksum.isdigit():
                checksum_declared = int(checksum)
            if corrects:
                document["corrects"] = corrects
                document["kind"] = "correction"
            continue
        if rec_type in ("D", "C", "E"):
            line.kind = "data"
            parsed_line = _parse_detail(document, body, line, rec_type,
                                        base_scale, aliases, mapping)
            detail_by_line.setdefault(_norm(parsed_line["line_number"]), parsed_line)
            continue
        if rec_type == "X":
            line.kind = "data"
            _attach_continuation(document, body, line, detail_by_line)
            continue
        line.kind = "unparsed"
        document["unparsed_lines"].append({
            "source_lines": [line.number], "raw": line.raw,
            "reason": f"unknown record type {rec_type!r}"})
        document["warnings"].append(
            f"line {line.number}: unknown record type {rec_type!r}")

    if not document["document_number"]:
        raise ParseError("no header record with a document number")

    if total_declared is not None:
        document["declared_counts"]["detail_records"] = total_declared
        if total_declared != len(document["lines"]):
            document["warnings"].append(
                f"trailer declares {total_declared} detail records but "
                f"{len(document['lines'])} were parsed")
    if checksum_declared is not None:
        computed = sum(int(ln["line_number"]) for ln in document["lines"]
                       if str(ln.get("line_number") or "").isdigit())
        document["declared_counts"]["checksum"] = checksum_declared
        document["computed_checksum"] = computed
        if computed != checksum_declared:
            document["warnings"].append(
                f"trailer checksum {checksum_declared} does not match computed "
                f"{computed}")

    document["source_line_assignment"] = {ln.number: ln.kind for ln in doc.lines}
    return document


def _norm(value):
    text = str(value or "").strip()
    return str(int(text)) if text.isdigit() else text.lower()


def _parse_detail(document, body, line, rec_type, base_scale, aliases, mapping):
    issues = []
    line_no = _text(_field(body, FIELDS, "line")).strip()
    action_raw = _text(_field(body, FIELDS, "action")).strip().upper()
    action = {"A": "add", "C": "change", "D": "delete", "": "add"}.get(
        action_raw, action_raw.lower())
    if action not in ("add", "change", "delete"):
        issues.append(f"unsupported action {action_raw!r}")

    quantity, zoned = decode_zoned(_field(body, FIELDS, "quantity"), scale=3)
    if quantity is None:
        issues.append(f"quantity: {zoned.get('error')}")

    unit_text = _text(_field(body, FIELDS, "unit")).strip() or None
    unit = unit_text.lower() if unit_text else None
    unit_resolution = None
    if unit and unit in aliases:
        unit_resolution = {"supplier_text": unit_text, "resolved": aliases[unit],
                           "mapping_version": mapping["version"]}
        issues.append(f"unit alias {unit!r} resolved to {aliases[unit]!r}")
        unit = aliases[unit]

    base_quantity = base_unit = conversion = None
    if quantity is not None and unit:
        try:
            base_quantity, base_unit, conversion = to_base(
                quantity, unit, scale=base_scale)
            conversion = dict(conversion) if conversion else None
            conversion["supplier_zoned"] = zoned
        except UnknownUnit as exc:
            issues.append(str(exc))

    status = "invalid" if quantity is None or not unit or action not in (
        "add", "change", "delete") else "ok"
    entry = {
        "line_id": f"L{len(document['lines']) + 1:04d}",
        "source_line": line.number,
        "source_lines": [line.number],
        "line_number": line_no,
        "record_type": rec_type,
        "sku": _text(_field(body, FIELDS, "sku")).strip() or None,
        "description": None,
        "quantity": qs(quantity),
        "quantity_text": zoned.get("digits"),
        "unit": unit,
        "unit_text": unit_text,
        "unit_resolution": unit_resolution,
        "unit_price": None,
        "amount": None,
        "base_quantity": base_quantity,
        "base_unit": base_unit,
        "conversion": conversion,
        "status": status,
        "issues": issues,
        "action": action,
        "target_line": line_no if action in ("change", "delete") else None,
        "raw": line.raw,
        "zoned": zoned,
        "continuation_lines": [],
        "supplier_fields": {
            "record_type": rec_type,
            "document": _text(_field(body, FIELDS, "document")),
            "revision": _text(_field(body, FIELDS, "revision")),
            "order": _text(_field(body, FIELDS, "order")),
            "issued": _text(_field(body, FIELDS, "issued")),
            "currency": _text(_field(body, FIELDS, "currency")),
            "line": line_no,
            "action": action_raw,
            "sku": _text(_field(body, FIELDS, "sku")),
        },
    }
    document["lines"].append(entry)
    return entry


def _attach_continuation(document, body, line, detail_by_line):
    line_no = _norm(_text(_field(body, CONTINUATION_FIELDS, "line")))
    target = detail_by_line.get(line_no)
    description = _text(_field(body, CONTINUATION_FIELDS, "description")).strip()
    price, meta = decode_zoned(_field(body, CONTINUATION_FIELDS, "price"), scale=2)
    if target is None:
        target = document["lines"][-1] if document["lines"] else None
    if target is None:
        document["warnings"].append(
            f"line {line.number}: continuation has no preceding detail record")
        document["unparsed_lines"].append({
            "source_lines": [line.number], "raw": line.raw,
            "reason": "continuation without detail record"})
        return
    target["continuation_lines"].append(line.number)
    target["source_lines"].append(line.number)
    if description:
        target["description"] = description
    if price is not None:
        target["unit_price"] = qs(price)
        target["price_zoned"] = meta
        qty = target.get("quantity")
        if qty:
            target["amount"] = qs(price * Decimal(qty))

"""Northwind: comma-separated, dot decimals, quoted fields, free-text remit.

Records are cut from the physical line view first, so every source line is
accounted for even when a quoted description contains newlines.  The CSV
dialect is recorded with the interpretation rather than re-sniffed on replay.
"""

import csv

from ..canon import q, qs, q_round
from ..units import to_base, UnknownUnit


def _dialect(text):
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _cut_records(lines, dialect):
    """Yield (source_line_numbers, record_text) honouring quoted newlines."""
    quote = dialect.quotechar
    doublequote = dialect.doublequote
    escape = getattr(dialect, "escapechar", None)
    buf = []
    numbers = []
    in_quotes = False
    i = 0

    def flush(next_line):
        pass

    for line in lines:
        if not numbers:
            numbers = []
        numbers.append(line.number)
        buf.append(line.raw)
        i = 0
        text = line.raw
        escaped = False
        while i < len(text):
            ch = text[i]
            if escaped:
                escaped = False
            elif escape and ch == escape:
                escaped = True
            elif ch == quote:
                if in_quotes and doublequote and i + 1 < len(text) and text[i + 1] == quote:
                    i += 1
                else:
                    in_quotes = not in_quotes
            i += 1
        if not in_quotes:
            yield numbers, "\n".join(buf)
            buf = []
            numbers = []
    if numbers:
        yield numbers, "\n".join(buf)


def _record(row, header):
    return dict(zip(header, row))


def parse(doc, mapping):
    from . import ParseError

    text = doc.text
    dialect = _dialect(text)
    columns = mapping["columns"]
    decimal_style = mapping.get("decimal_style", "dot")
    base_scale = mapping["base_scale"]

    document = {
        "supplier": doc.supplier,
        "filename": doc.filename,
        "kind": "order",
        "document_number": None,
        "revision": 1,
        "order_reference": None,
        "issued_at": None,
        "currency": None,
        "totals": {},
        "declared_filename": doc.declared_filename,
        "mapping_version": mapping["version"],
        "format": mapping["format"],
        "encoding": doc.encoding,
        "eol": doc.eol,
        "sha256": doc.sha256,
        "dialect": {"delimiter": dialect.delimiter, "quotechar": dialect.quotechar,
                    "doublequote": bool(dialect.doublequote)},
        "lines": [],
        "trailer": {},
        "trailer_fields": {},
        "warnings": [],
        "unparsed_lines": [],
        "declared_counts": {},
        "header_columns": [],
    }

    header = None
    data_records = []
    line_by_number = doc.line_map()

    for numbers, record_text in _cut_records(doc.lines, dialect):
        for n in numbers:
            line_by_number[n].kind = "data"
        if record_text.strip() == "":
            for n in numbers:
                line_by_number[n].kind = "blank"
            continue
        try:
            rows = list(csv.reader([record_text], dialect))
        except csv.Error as exc:
            for n in numbers:
                line_by_number[n].kind = "unparsed"
            document["unparsed_lines"].append(
                {"source_lines": numbers, "raw": record_text, "reason": str(exc)})
            document["warnings"].append(f"source lines {numbers}: {exc}")
            continue
        row = rows[0]
        if header is None:
            header = [c.strip().upper() for c in row]
            for n in numbers:
                line_by_number[n].kind = "header"
            document["header_columns"] = header
            document["header_record"] = {"source_lines": numbers, "raw": record_text,
                                         "fields": row}
            continue
        record = _record(row, header)
        rec_type = (record.get(columns["record"]) or "").strip().upper()
        if rec_type == "H":
            _absorb_header(document, record, columns)
            for n in numbers:
                line_by_number[n].kind = "header"
            document["header_record"] = {"source_lines": numbers, "raw": record_text,
                                         "fields": row}
        elif rec_type == "T":
            _absorb_trailer(document, record, columns)
            for n in numbers:
                line_by_number[n].kind = "trailer"
            document["trailer_record"] = {"source_lines": numbers, "raw": record_text,
                                          "fields": row}
        elif rec_type in ("D", "C"):
            data_records.append((record, numbers, record_text))
        else:
            for n in numbers:
                line_by_number[n].kind = "unparsed"
            document["warnings"].append(
                f"unrecognised record type {rec_type!r} at source line {numbers[0]}")
            document["unparsed_lines"].append({
                "source_lines": numbers, "raw": record_text,
                "reason": f"unknown record type {rec_type!r}"})

    if header is None:
        raise ParseError("CSV document has no header row")
    required = ("record", "line", "sku", "quantity", "unit")
    missing = [k for k in required if columns.get(k) not in header]
    if missing:
        raise ParseError(
            f"CSV header is missing required column(s): {', '.join(missing)}",
            detail={"header": header, "missing": missing})

    for record, numbers, record_text in data_records:
        for n in numbers:
            line_by_number[n].kind = "data"
        _parse_line(document, record, columns, numbers, record_text,
                    decimal_style, base_scale, mapping)

    _verify_counts(document)
    document["source_line_assignment"] = {
        ln.number: ln.kind for ln in doc.lines}
    return document


def _absorb_header(document, record, columns):
    def g(key):
        name = columns.get(key)
        value = record.get(name) if name else None
        return value.strip() if isinstance(value, str) else value

    if g("document"):
        document["document_number"] = g("document")
    if g("revision"):
        try:
            document["revision"] = int(g("revision"))
        except (TypeError, ValueError):
            document["warnings"].append(f"bad revision {g('revision')!r}; using 1")
    if g("corrects"):
        document["corrects"] = g("corrects")
        document["kind"] = "correction"
    if g("order"):
        document["order_reference"] = g("order")
    if g("issued"):
        document["issued_at"] = g("issued")
    if g("currency"):
        document["currency"] = g("currency")


def _absorb_trailer(document, record, columns):
    def g(key):
        name = columns.get(key)
        value = record.get(name) if name else None
        return value.strip() if isinstance(value, str) else value

    if g("corrects"):
        document["corrects"] = g("corrects")
        document["kind"] = "correction"
    if g("document") and not document.get("document_number"):
        document["document_number"] = g("document")
    if g("revision") and not document.get("revision_explicit"):
        try:
            document["revision"] = int(g("revision"))
        except (TypeError, ValueError):
            pass
    if g("order") and not document.get("order_reference"):
        document["order_reference"] = g("order")
    document["trailer_fields"] = {
        name: value for name, value in record.items() if value not in (None, "")}


def _parse_line(document, record, columns, numbers, record_text,
                decimal_style, base_scale, mapping):
    def g(key):
        name = columns.get(key)
        value = record.get(name) if name else None
        return value.strip() if isinstance(value, str) else value

    issues = []
    quantity_text = g("quantity")
    try:
        amount = q(quantity_text, decimal_style)
        if amount is None:
            issues.append("missing quantity")
    except ValueError as exc:
        amount = None
        issues.append(str(exc))

    unit_text = (g("unit") or "").strip() or None
    unit = unit_text.lower() if unit_text else None
    unit_resolution = None
    aliases = mapping.get("unit_aliases") or {}
    if unit and unit in aliases:
        unit_resolution = {"supplier_text": unit_text, "resolved": aliases[unit],
                           "mapping_version": mapping["version"]}
        issues.append(f"unit alias {unit!r} resolved to {aliases[unit]!r}")
        unit = aliases[unit]
    if not unit:
        issues.append("missing unit")

    price = line_amount = None
    try:
        price = q(g("price"), decimal_style)
    except ValueError as exc:
        issues.append(str(exc))
    try:
        line_amount = q(g("amount"), decimal_style)
    except ValueError as exc:
        issues.append(str(exc))

    base_quantity = base_unit = conversion = None
    if amount is not None and unit:
        try:
            base_quantity, base_unit, conversion = to_base(
                amount, unit, scale=base_scale, decimal_style=decimal_style)
        except UnknownUnit as exc:
            issues.append(str(exc))

    if price is not None and amount is not None and line_amount is not None:
        expected = q_round(amount * price, 6)
        if abs(line_amount - expected) > q("0.000001"):
            issues.append(
                f"computed amount {qs(expected)} differs from stated {qs(line_amount)}")

    action = (g("action") or "add").strip().lower()
    if action not in ("add", "change", "delete"):
        issues.append(f"unsupported action {action!r}")

    status = "ok"
    if amount is None or not unit or action not in ("add", "change", "delete"):
        status = "invalid"

    document["lines"].append({
        "line_id": f"L{len(document['lines']) + 1:04d}",
        "source_line": numbers[0],
        "source_lines": numbers,
        "line_number": g("line"),
        "sku": g("sku"),
        "description": g("description"),
        "quantity": qs(amount),
        "quantity_text": quantity_text,
        "unit": unit,
        "unit_text": unit_text,
        "unit_resolution": unit_resolution,
        "unit_price": qs(price),
        "amount": qs(line_amount),
        "status": status,
        "issues": issues,
        "raw": record_text,
        "fields": record,
        "base_quantity": base_quantity,
        "base_unit": base_unit,
        "conversion": conversion,
        "action": action,
        "target_line": (g("target") or None),
        "replaces": (g("corrects") or None),
    })


def _verify_counts(document):
    declared = {}
    for name, value in (document.get("trailer_fields") or {}).items():
        text = (value or "").strip()
        if text.lstrip("+-").isdigit():
            upper = name.strip().upper()
            if upper in ("COUNT", "LINE_COUNT", "LINECOUNT") or "COUNT" in upper:
                declared[upper] = int(text)
    document["declared_counts"] = declared
    found = len(document["lines"])
    for key, value in declared.items():
        if value != found:
            document["warnings"].append(
                f"trailer field {key} declares {value} but {found} data lines parsed")

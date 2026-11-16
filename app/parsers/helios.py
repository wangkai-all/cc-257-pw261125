"""Helios: nested JSON, comma decimal marks, positions as ``pos/sub``.

JSON has no inherent line structure, so the parser records the physical span of
each position object by scanning the decoded text.  Structural lines are still
classified, which lets the evidence view account for every physical line.
"""

import bisect
import json

from ..canon import q, qs, q_round
from ..units import to_base, UnknownUnit


def _line_starts(text):
    starts = [0]
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                i += 1
            starts.append(i + 1)
        elif ch == "\n":
            starts.append(i + 1)
        i += 1
    return starts


def _pos_objects(text):
    """Return [(start_offset, end_offset, object_dict, start_line, end_line)].

    A tiny JSON-aware scan finds object literals that directly contain a
    ``pos`` key.  It is not a general parser; json.loads validates the slices.
    """
    starts = _line_starts(text)
    objects = []
    stack = []          # [(start_offset, marked_as_position)]
    in_string = False
    escaped = False
    string_start = 0
    last_string = None
    awaiting_key = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                last_string = text[string_start + 1:i]
            i += 1
            continue
        if ch == '"':
            in_string = True
            string_start = i
        elif ch == "{":
            stack.append([i, False])
        elif ch == "}":
            if stack:
                start, marked = stack.pop()
                if marked:
                    snippet = text[start:i + 1]
                    try:
                        obj = json.loads(snippet)
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict) and "pos" in obj:
                        objects.append((start, i + 1, obj,
                                        bisect.bisect_right(starts, start),
                                        bisect.bisect_right(starts, i)))
        elif ch == ":":
            if last_string is not None and stack and last_string == "pos":
                stack[-1][1] = True
            last_string = None
        elif ch in ",]}":
            last_string = None
        i += 1
    objects.sort(key=lambda item: item[0])
    return objects


def _ref(pos, sub):
    sub = "" if sub is None else str(sub)
    return f"{str(pos)}/{sub}" if sub != "" else str(pos)


def _target_ref(target):
    if target is None:
        return None
    if isinstance(target, str):
        return target.strip()
    if isinstance(target, dict):
        if "ref" in target:
            return str(target["ref"]).strip()
        if "pos" in target:
            return _ref(target["pos"], target.get("sub", "1"))
    return None


def parse(doc, mapping):
    from . import ParseError

    text = doc.text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON: {exc.msg}",
                         line=exc.lineno, detail={"pos": exc.pos}) from exc
    if not isinstance(payload, dict):
        raise ParseError("JSON root must be an object")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ParseError("JSON has no 'message' object")

    decimal_style = mapping.get("decimal_style", "comma")
    base_scale = mapping["base_scale"]
    aliases = mapping.get("unit_aliases") or {}

    doc_no = message.get("document") or payload.get("document")
    if not doc_no:
        raise ParseError("message has no document number")
    revision = message.get("revision", 1)
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        revision = 1
    kind_raw = (message.get("type") or message.get("kind") or "ORDER").upper()
    corrects = message.get("correctionOf") or message.get("corrects")
    kind = "correction" if (corrects or kind_raw in ("CORRECTION", "CHANGE")) else "order"

    positions = message.get("positions")
    if not isinstance(positions, list):
        raise ParseError("message.positions must be an array")

    spans = _pos_objects(text)
    if len(spans) < len(positions):
        # Fall back to whole-document span rather than losing the line anchor.
        whole = [(0, len(text), {}, 1, len(doc.lines) or 1)]
        spans = spans + whole * (len(positions) - len(spans))

    document = {
        "supplier": doc.supplier,
        "filename": doc.filename,
        "kind": kind,
        "document_number": str(doc_no),
        "revision": revision,
        "order_reference": message.get("orderRef") or message.get("order_reference"),
        "issued_at": message.get("issued"),
        "currency": message.get("currency"),
        "corrects": str(corrects) if corrects else None,
        "totals": message.get("totals") or {},
        "declared_filename": doc.declared_filename,
        "mapping_version": mapping["version"],
        "format": mapping["format"],
        "encoding": doc.encoding,
        "eol": doc.eol,
        "sha256": doc.sha256,
        "schema": payload.get("schema"),
        "transmission": payload.get("transmission"),
        "lines": [],
        "warnings": [],
        "unparsed_lines": [],
        "declared_counts": {},
        "supplier_fields": {k: v for k, v in message.items()
                            if k not in ("positions",)},
        "json_pointers": {},
    }
    for idx, position in enumerate(positions):
        obj = spans[idx][2] if idx < len(spans) and spans[idx][2] else position
        start_line = spans[idx][3] if idx < len(spans) else None
        end_line = spans[idx][4] if idx < len(spans) else None
        source_lines = list(range(start_line, end_line + 1)) if start_line else []
        raw = "\n".join(ln.raw for ln in doc.lines
                        if ln.number in set(source_lines)) if source_lines else ""
        _parse_position(document, position, obj, source_lines, raw, idx,
                        decimal_style, base_scale, aliases, mapping, doc)

    # Line-role accounting for the JSON view.
    data_lines = set()
    for line in document["lines"]:
        data_lines.update(line["source_lines"])
    position_starts = [ln["source_line"] for ln in document["lines"] if ln["source_line"]]
    first_pos = min(position_starts) if position_starts else None
    last_pos = max(ln["source_lines"][-1] for ln in document["lines"] if ln["source_lines"]) \
        if position_starts else None
    for line in doc.lines:
        if line.raw.strip() == "":
            line.kind = "blank"
        elif line.number in data_lines:
            line.kind = "data"
        elif first_pos is not None and line.number < first_pos:
            line.kind = "header"
        elif last_pos is not None and line.number > last_pos:
            line.kind = "trailer"
        else:
            line.kind = "structure"

    totals = message.get("totals") or {}
    declared_positions = None
    for key in ("positions", "positionCount", "count"):
        value = totals.get(key)
        if value is None:
            value = message.get(key)
        if value is not None:
            try:
                declared_positions = int(str(value).replace(",", ""))
                break
            except ValueError:
                pass
    if declared_positions is not None:
        document["declared_counts"] = {"positions": declared_positions}
        if declared_positions != len(document["lines"]):
            document["warnings"].append(
                f"declared {declared_positions} positions but parsed {len(document['lines'])}")

    document["source_line_assignment"] = {ln.number: ln.kind for ln in doc.lines}
    return document


def _parse_position(document, position, obj, source_lines, raw, idx,
                    decimal_style, base_scale, aliases, mapping, doc):
    issues = []
    if not isinstance(position, dict):
        position = {}
    article = position.get("article") or {}
    if not isinstance(article, dict):
        article = {}
    quantity = position.get("quantity") or {}
    price = position.get("price") or {}
    if not isinstance(quantity, dict):
        quantity = {"value": quantity}
    if not isinstance(price, dict):
        price = {"unit": price}

    pos = position.get("pos", str(idx + 1))
    sub = position.get("sub", "1")
    ref = _ref(pos, sub)
    op = (position.get("op") or position.get("action") or "ADD").strip().upper()
    action = {"A": "add", "C": "change", "D": "delete"}.get(op, op.lower())
    if action not in ("add", "change", "delete"):
        issues.append(f"unsupported operation {op!r}")

    qty_text = quantity.get("value")
    try:
        amount = q(qty_text, decimal_style)
        if amount is None:
            issues.append("missing quantity")
    except ValueError as exc:
        amount = None
        issues.append(str(exc))
    unit_text = (quantity.get("unit") or "").strip() or None
    unit = unit_text.lower() if unit_text else None
    unit_resolution = None
    if unit and unit in aliases:
        unit_resolution = {"supplier_text": unit_text, "resolved": aliases[unit],
                           "mapping_version": mapping["version"]}
        issues.append(f"unit alias {unit!r} resolved to {aliases[unit]!r}")
        unit = aliases[unit]
    if not unit:
        issues.append("missing unit")

    unit_price = line_amount = None
    try:
        unit_price = q(price.get("unit"), decimal_style)
    except ValueError as exc:
        issues.append(str(exc))
    try:
        line_amount = q(price.get("line"), decimal_style)
    except ValueError as exc:
        issues.append(str(exc))
    if unit_price is not None and amount is not None and line_amount is not None:
        expected = q_round(amount * unit_price, 6)
        if abs(line_amount - expected) > q("0.000001"):
            issues.append(
                f"computed amount {qs(expected)} differs from stated {qs(line_amount)}")

    base_quantity = base_unit = conversion = None
    if amount is not None and unit:
        try:
            base_quantity, base_unit, conversion = to_base(
                amount, unit, scale=base_scale, decimal_style=decimal_style)
        except UnknownUnit as exc:
            issues.append(str(exc))

    if action in ("change", "delete") and not position.get("target"):
        issues.append(f"{action} without target")

    status = "invalid" if amount is None or not unit or action not in (
        "add", "change", "delete") else "ok"

    document["lines"].append({
        "line_id": f"L{len(document['lines']) + 1:04d}",
        "supplier_line_ref": ref,
        "line_number": str(pos),
        "source_line": source_lines[0] if source_lines else None,
        "source_lines": source_lines,
        "raw": raw,
        "sku": article.get("sku") or position.get("sku"),
        "description": article.get("description") or article.get("desc"),
        "quantity": qs(amount),
        "quantity_text": str(qty_text) if qty_text is not None else None,
        "unit": unit,
        "unit_text": unit_text,
        "unit_resolution": unit_resolution,
        "unit_price": qs(unit_price),
        "amount": qs(line_amount),
        "base_quantity": base_quantity,
        "base_unit": base_unit,
        "conversion": conversion,
        "status": status,
        "issues": issues,
        "action": action,
        "target_line_ref": _target_ref(position.get("target")),
        "supplier_fields": position,
    })

#!/usr/bin/env python3
"""Generate the supplier fixtures used by tests and the demo.

Run from the repository root:  python3 scripts/make_fixtures.py
The files are deterministic; re-running produces byte-identical output.
"""

import json
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NW = os.path.join(ROOT, "fixtures", "northwind")
HE = os.path.join(ROOT, "fixtures", "helios")
OR = os.path.join(ROOT, "fixtures", "orion")


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


# --------------------------------------------------------------------------- #
# Northwind CSV
# --------------------------------------------------------------------------- #

NW_HEADER = ("DOCUMENT,REV,ORDER,ISSUED,CCY,CORRECTS,REC,LINE,ACTION,SKU,"
             "DESCRIPTION,QTY,UOM,PRICE,AMOUNT,TARGET")


def nw_row(rec, line="", action="", sku="", desc="", qty="", uom="",
           price="", amount="", target="", document="NW-1001", rev="1",
           order="PO-77", issued="2026-11-01", ccy="EUR", corrects=""):
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="")
    writer.writerow([document, rev, order, issued, ccy, corrects, rec, line,
                     action, sku, desc, qty, uom, price, amount, target])
    return buf.getvalue()


def northwind_fixtures():
    nw1001 = "\r\n".join([
        NW_HEADER,
        "NW-1001,1,PO-77,2026-11-01,EUR,,H,,,,,,,,,,",
        'NW-1001,1,PO-77,2026-11-01,EUR,,D,1,ADD,NW-WIDGET-01,"Widget, large",2.5,kg,10.00,25.00,',
        'NW-1001,1,PO-77,2026-11-01,EUR,,D,2,ADD,NW-BOLT-M8,Bolt M8 plain,12,pcs,0.50,6.00,',
        'NW-1001,1,PO-77,2026-11-01,EUR,,T,LINES=2,,,,,,,31.00,',
    ]) + "\r\n"
    # Same filename as NW-1001, different content (reused name must not dedupe).
    nw1002 = "\r\n".join([
        NW_HEADER,
        "NW-1002,1,PO-77,2026-11-03,EUR,NW-1001,H,,,,,,,,,,",
        "NW-1002,1,PO-77,2026-11-03,EUR,NW-1001,C,2,CHANGE,NW-BOLT-M8,Bolt M8 plain,14,pcs,0.50,7.00,2",
        "NW-1002,1,PO-77,2026-11-03,EUR,NW-1001,D,3,ADD,NW-GASKET-01,Washer gasket,1.5,kg,4.00,6.00,",
        "NW-1002,1,PO-77,2026-11-03,EUR,NW-1001,T,LINES=3,,,,,,,38.00,",
    ]) + "\r\n"
    # A copied byte-for-byte resend, delivered under a different name.
    nw1001_resend = nw1001
    # Non-ASCII description encoded as cp1252 (no BOM), to exercise fallback.
    nw3001 = "\r\n".join([
        NW_HEADER,
        "NW-3001,1,PO-88,2026-11-05,EUR,,H,,,,,,,,,,",
        "NW-3001,1,PO-88,2026-11-05,EUR,,D,1,ADD,NW-CABLE-02,Kabelbügel Größe 2,3,m,Meterpreis,4.20,12.60,",
        "NW-3001,1,PO-88,2026-11-05,EUR,,T,LINES=1,,,,,,,12.60,",
    ]) + "\r\n"
    # Correction that arrives before its original and states its order.
    nw3002 = "\r\n".join([
        NW_HEADER,
        "NW-3002,1,PO-88,2026-11-06,EUR,NW-3001,H,,,,,,,,,,",
        "NW-3002,1,PO-88,2026-11-06,EUR,NW-3001,C,1,CHANGE,NW-CABLE-02,Kabelbügel Größe 2,4,m,Meterpreis,4.20,16.80,1",
        "NW-3002,1,PO-88,2026-11-06,EUR,NW-3001,T,LINES=1,,,,,,,16.80,",
    ]) + "\r\n"
    # Malformed: trailer claims 3 lines, one data row has a bad quantity and a
    # missing SKU; the file must be retained and diagnosable, not lost.
    malformed = "\r\n".join([
        NW_HEADER,
        "BAD-1,1,PO-90,2026-11-07,EUR,,H,,,,,,,,,,",
        "BAD-1,1,PO-90,2026-11-07,EUR,,D,1,ADD,SKU-X,Thing,twelve,pcs,1.00,12.00,",
        "BAD-1,1,PO-90,2026-11-07,EUR,,Z,2,ADD,SKU-Y,What,1,pcs,1.00,1.00,",
        "BAD-1,1,PO-90,2026-11-07,EUR,,T,LINES=3,,,,,,,13.00,",
    ]) + "\r\n"

    write(os.path.join(NW, "NW-1001.csv"), nw1001.encode("utf-8"))
    write(os.path.join(NW, "NW-1002.csv"), nw1002.encode("utf-8"))
    write(os.path.join(NW, "NW-1001-resend.csv"), nw1001_resend.encode("utf-8"))
    write(os.path.join(NW, "NW-3001.csv"), nw3001.encode("cp1252"))
    write(os.path.join(NW, "NW-3002.csv"), nw3002.encode("cp1252"))
    write(os.path.join(NW, "NW-MALFORMED.csv"), malformed.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Helios nested JSON
# --------------------------------------------------------------------------- #

def helios_doc(document, revision, corrects, issued, positions, order="PO-77"):
    return {
        "schema": "helios.order/2.1",
        "transmission": {
            "id": f"T-{document}",
            "sentAt": f"{issued}T09:15:00+01:00",
            "sender": "HELIOS SUPPLY GMBH",
        },
        "message": {
            "document": document,
            "revision": revision,
            "type": "CORRECTION" if corrects else "ORDER",
            "correctionOf": corrects,
            "orderRef": order,
            "issued": issued,
            "currency": "EUR",
            "positions": positions,
            "totals": {"positions": len(positions)},
        },
    }


def hp(pos, sub, op, sku, desc, value, unit, unit_price, line_price, target=None):
    item = {
        "pos": pos,
        "sub": sub,
        "op": op,
        "article": {"sku": sku, "description": desc},
        "quantity": {"value": value, "unit": unit},
        "price": {"unit": unit_price, "line": line_price},
        "delivery": {"plant": "Werk-2", "date": "2026-11-20"},
    }
    if target:
        item["target"] = {"ref": target}
    return item


def helios_fixtures():
    he500 = helios_doc("HE-500", 1, None, "2026-11-02", [
        hp(1, 1, "A", "NW-WIDGET-01", "Widget groß", "3,500", "kg", "8,00", "28,00"),
        hp(1, 2, "A", "HE-SEAL-07", "Dichtung Ø 40", "500", "g", "0,02", "10,00"),
        hp(2, 1, "A", "HE-TUBE-01", "Schlauch 2 m", "2", "m", "3,25", "6,50"),
    ])
    he501 = helios_doc("HE-501", 1, "HE-500", "2026-11-04", [
        hp(1, 2, "C", "HE-SEAL-07", "Dichtung Ø 40", "750", "g", "0,02", "15,00",
           target="1/2"),
        hp(3, 1, "A", "HE-CLAMP-04", "Klemme Edelstahl", "4", "pcs", "1,10", "4,40"),
    ])
    # Correction delivered before its original; same nested shape.
    he800 = helios_doc("HE-800", 1, None, "2026-11-08", [
        hp(1, 1, "A", "HE-HOSE-09", "Schlauch 5 m", "5", "m", "2,00", "10,00"),
    ], order="PO-91")
    he801 = helios_doc("HE-801", 1, "HE-800", "2026-11-09", [
        hp(1, 1, "C", "HE-HOSE-09", "Schlauch 5 m", "6,5", "m", "2,00", "13,00",
           target="1/1"),
    ], order="PO-91")
    # A correction whose target never arrives and which names no order.
    he900 = helios_doc("HE-900", 1, "HE-000", "2026-11-10", [
        hp(1, 1, "A", "HE-ODD-01", "Sonderteil", "1", "pcs", "99,00", "99,00"),
    ], order=None)
    he900["message"].pop("orderRef")

    write(os.path.join(HE, "HE-500.json"),
          json.dumps(he500, ensure_ascii=False, indent=2).encode("utf-8"))
    write(os.path.join(HE, "HE-501.json"),
          json.dumps(he501, ensure_ascii=False, indent=2).encode("utf-8"))
    # cp1252 with umlauts and no BOM, exercising the decoding fallback.
    write(os.path.join(HE, "HE-800.json"),
          json.dumps(he800, ensure_ascii=False, indent=2).encode("cp1252"))
    write(os.path.join(HE, "HE-801.json"),
          json.dumps(he801, ensure_ascii=False, indent=2).encode("utf-8"))
    write(os.path.join(HE, "HE-900.json"),
          json.dumps(he900, ensure_ascii=False, indent=2).encode("utf-8"))
    # Truncated JSON: must be retained with a line number, not silently dropped.
    broken = json.dumps(he501, ensure_ascii=False, indent=2)
    write(os.path.join(HE, "HE-BROKEN.json"),
          broken[: len(broken) // 2].encode("utf-8"))


# --------------------------------------------------------------------------- #
# Orion fixed-width EBCDIC
# --------------------------------------------------------------------------- #

def eb(text, width):
    raw = text.encode("cp037")
    if len(raw) > width:
        raise ValueError(f"{text!r} exceeds {width} bytes")
    return raw + b"\x40" * (width - len(raw))


def zoned(value, width, scale, negative=False):
    """Encode Decimal as EBCDIC zoned decimal, sign in the last nibble."""
    scaled = int((Decimal(value) * (10 ** scale)).to_integral_value())
    negative = negative or scaled < 0
    digits = str(abs(scaled)).rjust(width, "0")
    if len(digits) > width:
        raise ValueError(f"{value} does not fit in {width} digits at scale {scale}")
    out = bytearray(0xF0 + int(d) for d in digits)
    out[-1] = (0xD0 if negative else 0xC0) + int(digits[-1])
    return bytes(out)


def orion_header(document, revision, order, issued, currency):
    return (b"\xc8" + eb(document, 15) + eb(f"{revision:03d}", 3)
            + eb(order, 16) + eb(issued, 8) + eb(currency, 3)
            + b"\x40" * (80 - 46))


def orion_detail(document, revision, order, issued, currency, line, action, sku,
                 quantity, unit):
    body = (b"\xc4" + eb(document, 15) + eb(f"{revision:03d}", 3)
            + eb(order, 16) + eb(issued, 8) + eb(currency, 3)
            + eb(f"{line:04d}", 4) + eb(action, 1) + eb(sku, 15)
            + zoned(quantity, 10, 3) + eb(unit, 3) + b"\x40")
    assert len(body) == 80, len(body)
    return body


def orion_cont(document, line, description, price):
    body = (b"\xe7" + eb(document, 15) + eb(f"{line:04d}", 4)
            + eb(description, 40) + zoned(price, 8, 2) + b"\x40" * 12)
    assert len(body) == 80, len(body)
    return body


def orion_trailer(document, total, checksum, corrects=""):
    body = (b"\xe3" + eb(f"{total:04d}", 4) + eb(f"{checksum:04d}", 4)
            + eb(corrects, 15))
    return body + b"\x40" * (80 - len(body))


def orion_fixtures():
    eol = b"\x25"  # EBCDIC newline (NL, U+0085 in cp037)
    lines = [
        orion_header("OR-900", 1, "PO-77", "20261102", "EUR"),
        orion_detail("OR-900", 1, "PO-77", "20261102", "EUR", 1, "A",
                     "OR-BOLT-M8", "8", "pcs"),
        orion_cont("OR-900", 1, "Sechskantbolzen M8", "0.45"),
        orion_detail("OR-900", 1, "PO-77", "20261102", "EUR", 2, "A",
                     "OR-PLATE-01", "1.250", "kg"),
        orion_cont("OR-900", 2, "Stahlplatte 3 mm", "12.00"),
        orion_trailer("OR-900", 2, 3),
    ]
    write(os.path.join(OR, "OR-900.ebc"), eol.join(lines) + eol)

    lines = [
        orion_header("OR-901", 1, "PO-77", "20261104", "EUR"),
        orion_detail("OR-901", 1, "PO-77", "20261104", "EUR", 1, "C",
                     "OR-BOLT-M8", "9", "pcs"),
        orion_cont("OR-901", 1, "Sechskantbolzen M8", "0.45"),
        orion_detail("OR-901", 1, "PO-77", "20261104", "EUR", 2, "D",
                     "OR-PLATE-01", "0", "kg"),
        orion_trailer("OR-901", 2, 3, corrects="OR-900"),
    ]
    write(os.path.join(OR, "OR-901.ebc"), eol.join(lines) + eol)

    # Correction before its original.
    lines = [
        orion_header("OR-950", 1, "PO-92", "20261103", "EUR"),
        orion_detail("OR-950", 1, "PO-92", "20261103", "EUR", 1, "A",
                     "OR-RING-02", "2.5", "kg"),
        orion_cont("OR-950", 1, "Dichtring", "3.20"),
        orion_trailer("OR-950", 1, 1),
    ]
    write(os.path.join(OR, "OR-950.ebc"), eol.join(lines) + eol)
    lines = [
        orion_header("OR-951", 1, "PO-92", "20261105", "EUR"),
        orion_detail("OR-951", 1, "PO-92", "20261105", "EUR", 1, "C",
                     "OR-RING-02", "3.750", "kg"),
        orion_cont("OR-951", 1, "Dichtring", "3.20"),
        orion_trailer("OR-951", 1, 1, corrects="OR-950"),
    ]
    write(os.path.join(OR, "OR-951.ebc"), eol.join(lines) + eol)

    # Malformed: a short record between valid ones; trailer count says 2.
    good = orion_detail("OR-999", 1, "PO-93", "20261106", "EUR", 1, "A",
                        "OR-SHORT-01", "1", "pcs")
    lines = [
        orion_header("OR-999", 1, "PO-93", "20261106", "EUR"),
        good,
        b"\xc4" + eb("OR-999", 15) + b"\x40" * 20,  # short
        orion_trailer("OR-999", 2, 2),
    ]
    write(os.path.join(OR, "OR-MALFORMED.ebc"), eol.join(lines) + eol)


def main():
    northwind_fixtures()
    helios_fixtures()
    orion_fixtures()
    print("fixtures written")


if __name__ == "__main__":
    main()

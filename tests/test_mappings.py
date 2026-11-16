"""Mapping upgrades, reprocessing, and the early-vs-late normalisation trade-off."""

import json
import unittest

from tests.base import TempDB
from app import engine
from app.parsers import HELIOS_V2, NORTHWIND_V2, ORION_V2


class MappingUpgrades(TempDB):
    def test_upgrade_creates_new_interpretation_and_keeps_old(self):
        result = self.ingest("northwind", "northwind/NW-1001.csv")
        revision_id = result["revision_id"]
        before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM interpretations WHERE revision_id=?",
            (revision_id,)).fetchone()["n"]
        engine.register_mapping(self.conn, NORTHWIND_V2, activate=True,
                                reprocess=True)
        after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM interpretations WHERE revision_id=?",
            (revision_id,)).fetchone()["n"]
        self.assertEqual(before, 1)
        self.assertGreaterEqual(after, 1)
        current = self.conn.execute(
            "SELECT i.*, m.version FROM interpretations i "
            "JOIN mapping_versions m ON m.id=i.mapping_version_id "
            "WHERE i.revision_id=? AND i.is_current=1", (revision_id,)).fetchone()
        self.assertEqual(current["version"], 2)

    def test_upgrade_without_business_change_is_a_noop(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        order_id = self.order("northwind", "PO-77")["id"]
        before = self.order("northwind", "PO-77")["state_hash"]
        engine.register_mapping(self.conn, NORTHWIND_V2, activate=True,
                                reprocess=True)
        after = self.order("northwind", "PO-77")["state_hash"]
        self.assertEqual(before, after)
        noops = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE order_id=? "
            "AND kind='replay_noop'", (order_id,)).fetchone()["n"]
        self.assertGreaterEqual(noops, 1)

    def test_old_interpretation_is_retained_for_audit(self):
        result = self.ingest("northwind", "northwind/NW-1001.csv")
        v1_interp = result["interpretation_id"]
        engine.register_mapping(self.conn, NORTHWIND_V2, activate=True,
                                reprocess=True)
        row = self.conn.execute("SELECT * FROM interpretations WHERE id=?",
                                (v1_interp,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_current"], 0)

    def test_mapping_versions_are_immutable(self):
        engine.register_mapping(self.conn, NORTHWIND_V2)
        tampered = json.loads(json.dumps(NORTHWIND_V2))
        tampered["base_scale"] = 2
        with self.assertRaises(ValueError):
            engine.register_mapping(self.conn, tampered)

    def test_upgrade_applies_to_new_unconfirmed_receipts(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        engine.register_mapping(self.conn, NORTHWIND_V2, activate=True,
                                reprocess=True)
        result = self.ingest("northwind", "northwind/NW-1003.csv")
        interp = self.conn.execute(
            "SELECT i.*, m.version FROM interpretations i "
            "JOIN mapping_versions m ON m.id=i.mapping_version_id "
            "WHERE i.id=?", (result["interpretation_id"],)).fetchone()
        self.assertEqual(interp["version"], 2)

    def test_reprocess_with_explicit_version_does_not_change_current_pin(self):
        result = self.ingest("northwind", "northwind/NW-1001.csv")
        v1 = self.conn.execute(
            "SELECT id FROM mapping_versions WHERE supplier='northwind' "
            "AND version=1").fetchone()["id"]
        engine.register_mapping(self.conn, NORTHWIND_V2, activate=True,
                                reprocess=True)
        engine.reprocess_receipt(self.conn, result["receipt_id"], v1, "auditor")
        current = self.conn.execute(
            "SELECT i.*, m.version FROM interpretations i "
            "JOIN mapping_versions m ON m.id=i.mapping_version_id "
            "WHERE i.revision_id=? AND i.is_current=1",
            (result["revision_id"],)).fetchone()
        self.assertEqual(current["version"], 2)


class NormalisationTradeoff(TempDB):
    """Comparison of early vs late standardisation, exercised on real records.

    Late conversion (what this system does) keeps the supplied amount, unit and
    raw text, and derives the base amount with an annotated factor.  The tests
    below show why that matters for corrections: a correction to the same line
    can change the *unit* without changing the *base* amount, and the record
    must tell those two events apart.
    """

    def _ingest_text(self, supplier, filename, text):
        return engine.ingest(self.conn, supplier=supplier, filename=filename,
                             data=text.encode("utf-8"))

    def test_unit_change_with_constant_base_is_visible(self):
        self._ingest_text("northwind", "a.csv", _nw(
            document="NX-1", rev="1", corrects="",
            rows=[("1", "ADD", "SKU-1", "2.5", "kg", "10.00", "25.00", "")]))
        self._ingest_text("northwind", "b.csv", _nw(
            document="NX-2", rev="1", corrects="NX-1",
            rows=[("1", "CHANGE", "SKU-1", "2500", "g", "0.01", "25.00", "1")]))
        state = self.state("northwind", "PO-77")
        line = state["lines"][0]
        # The base amount is identical, but the commercial expression changed.
        self.assertEqual(line["quantity"], "2500")
        self.assertEqual(line["unit"], "g")
        self.assertEqual(line["base_quantity"], "2500.000")
        self.assertEqual(line["unit_price"], "0.01")
        self.assertEqual(len(line["provenance"]), 2)
        # Early normalisation would have collapsed these into one invisible edit.
        self.assertEqual(line["provenance"][-1]["action"], "change")

    def test_decimal_style_is_declared_not_sniffed(self):
        # The same printed text "1.234" means 1234 under Helios' comma
        # convention (dot groups thousands) and 1.234 under Northwind's dot
        # convention.  Neither value is guessed from magnitude.
        self._ingest_text("helios", "h.json", _helios("HX-1", "1.234"))
        self._ingest_text("northwind", "n.csv", _nw(
            document="NX-9", rev="1", corrects="",
            rows=[("1", "ADD", "SKU-1", "1.234", "kg", "1.00", "1.23", "")]))
        helios = self.state("helios", "PO-77")["lines"][0]
        northwind = self.state("northwind", "PO-77")["lines"][0]
        self.assertEqual(helios["quantity"], "1234")
        self.assertEqual(northwind["quantity"], "1.234")
        self.assertEqual(helios["base_quantity"], "1234000.000000")
        self.assertEqual(northwind["base_quantity"], "1234.000")

    def test_conversion_note_carries_factor_and_rounding(self):
        self.ingest("helios", "helios/HE-500.json")
        state = self.state("helios", "PO-77")
        line = state["lines"][0]
        note = line["conversion"]
        self.assertEqual(note["source_unit"], "kg")
        self.assertEqual(note["base_unit"], "g")
        self.assertEqual(note["factor"], "1000/1")
        self.assertEqual(note["rounding"], "half_even")
        self.assertEqual(note["method"], "exact_rational")

    def test_repeated_corrections_keep_every_step(self):
        self._ingest_text("northwind", "a.csv", _nw(
            document="NX-1", rev="1", corrects="",
            rows=[("1", "ADD", "SKU-1", "1", "kg", "1.00", "1.00", "")]))
        self._ingest_text("northwind", "b.csv", _nw(
            document="NX-2", rev="1", corrects="NX-1",
            rows=[("1", "CHANGE", "SKU-1", "2", "kg", "1.00", "2.00", "1")]))
        self._ingest_text("northwind", "c.csv", _nw(
            document="NX-3", rev="1", corrects="NX-2",
            rows=[("1", "CHANGE", "SKU-1", "3", "kg", "1.00", "3.00", "1")]))
        line = self.state("northwind", "PO-77")["lines"][0]
        self.assertEqual(line["quantity"], "3")
        self.assertEqual([p["document"] for p in line["provenance"]],
                         ["NX-1", "NX-2", "NX-3"])
        self.assertEqual(line["provenance"][1]["target"], "1")


def _nw(document, rev, corrects, rows):
    header = ("DOCUMENT,REV,ORDER,ISSUED,CCY,CORRECTS,REC,LINE,ACTION,SKU,"
              "DESCRIPTION,QTY,UOM,PRICE,AMOUNT,TARGET\n")
    out = [header, f"{document},{rev},PO-77,2026-11-01,EUR,{corrects},H,,,,,,,,,,\n"]
    total = 0
    for line, action, sku, qty, uom, price, amount, target in rows:
        out.append(f'{document},{rev},PO-77,2026-11-01,EUR,{corrects},D,'
                   f'{line},{action},{sku},Item,{qty},{uom},{price},{amount},{target}\n')
    out.append(f"{document},{rev},PO-77,2026-11-01,EUR,{corrects},T,LINES={len(rows)},,,,,,,\n")
    return "".join(out)


def _helios(document, value):
    payload = {
        "schema": "helios.order/2.1",
        "message": {
            "document": document, "revision": 1, "type": "ORDER",
            "orderRef": "PO-77", "issued": "2026-11-01", "currency": "EUR",
            "positions": [{
                "pos": 1, "sub": 1, "op": "A",
                "article": {"sku": "H-1", "description": "Teil"},
                "quantity": {"value": value, "unit": "kg"},
                "price": {"unit": "1,00", "line": "1234,00"},
            }],
            "totals": {"positions": 1},
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    unittest.main()

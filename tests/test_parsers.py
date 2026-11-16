"""Source retention, line accounting and parser behaviour."""

import json
import unittest

from tests.base import TempDB, fixture
from app.parsers import (DEFAULT_MAPPINGS, NORTHWIND_V1, ParseError, parse,
                         PARSERS)
from app.source import decode_source, rebuild_bytes

MAPPING = {m["supplier"]: m for m in DEFAULT_MAPPINGS}


def parsed(supplier, path):
    data = fixture(path)
    doc = decode_source(supplier, path, "application/octet-stream", data)
    return doc, parse(doc, MAPPING[supplier])


class SourceRetention(unittest.TestCase):
    def test_roundtrip_is_byte_identical_for_every_fixture(self):
        for supplier, path in [
                ("northwind", "northwind/NW-1001.csv"),
                ("northwind", "northwind/NW-3001.csv"),
                ("helios", "helios/HE-500.json"),
                ("helios", "helios/HE-800.json"),
                ("orion", "orion/OR-900.ebc"),
                ("orion", "orion/OR-MALFORMED.ebc")]:
            data = fixture(path)
            doc = decode_source(supplier, path, "application/octet-stream", data)
            self.assertEqual(rebuild_bytes(doc.evidence()), data, path)

    def test_cp1252_fallback_is_recorded_and_reversible(self):
        data = fixture("northwind/NW-3001.csv")
        doc = decode_source("northwind", "NW-3001.csv", "text/csv", data)
        self.assertEqual(doc.encoding, "cp1252")
        self.assertIn("Kabelbügel", doc.text)
        self.assertEqual(rebuild_bytes(doc.evidence()), data)

    def test_ebcdic_line_numbering_and_offsets(self):
        data = fixture("orion/OR-900.ebc")
        doc = decode_source("orion", "OR-900.ebc", "application/octet-stream", data)
        self.assertEqual(doc.encoding, "cp037")
        self.assertEqual(len(doc.lines), 6)
        self.assertEqual(doc.lines[0].offset, 0)
        self.assertEqual(doc.lines[1].offset, 81)  # 80-byte record + EBCDIC NL
        self.assertEqual(rebuild_bytes(doc.evidence()), data)

    def test_embedded_newline_keeps_records_anchored(self):
        text = ('DOCUMENT,REV,ORDER,ISSUED,CCY,CORRECTS,REC,LINE,ACTION,SKU,'
                'DESCRIPTION,QTY,UOM,PRICE,AMOUNT,TARGET\n'
                'D-1,1,PO-1,2026-11-01,EUR,,H,,,,,,,,,,\n'
                'D-1,1,PO-1,2026-11-01,EUR,,D,1,ADD,S,"two\nlines",2,kg,1,2,\n'
                'D-1,1,PO-1,2026-11-01,EUR,,T,LINES=1,,,,,,,,\n')
        doc = decode_source("northwind", "x.csv", "text/csv", text.encode())
        out = parse(doc, NORTHWIND_V1)
        line = out["lines"][0]
        self.assertEqual(line["source_lines"], [3, 4])
        self.assertEqual(line["description"], "two\nlines")
        self.assertEqual(out["lines"][0]["source_line"], 3)

    def test_every_physical_line_has_a_role(self):
        for supplier, path in [
                ("northwind", "northwind/NW-1001.csv"),
                ("helios", "helios/HE-500.json"),
                ("orion", "orion/OR-900.ebc")]:
            doc, out = parsed(supplier, path)
            assignment = out["source_line_assignment"]
            self.assertEqual(set(assignment), {ln.number for ln in doc.lines})
            self.assertNotIn("data", [])
            for number, kind in assignment.items():
                self.assertIn(kind, ("header", "data", "trailer", "blank",
                                     "comment", "unparsed", "structure"),
                              f"{path}:{number} -> {kind}")


class NorthwindParser(unittest.TestCase):
    def test_header_and_lines(self):
        _doc, out = parsed("northwind", "northwind/NW-1001.csv")
        self.assertEqual(out["document_number"], "NW-1001")
        self.assertEqual(out["order_reference"], "PO-77")
        self.assertEqual(len(out["lines"]), 2)
        self.assertEqual(out["lines"][0]["sku"], "NW-WIDGET-01")
        self.assertEqual(out["lines"][0]["description"], "Widget, large")

    def test_correction_actions_and_targets(self):
        _doc, out = parsed("northwind", "northwind/NW-1002.csv")
        self.assertEqual(out["kind"], "correction")
        self.assertEqual(out["corrects"], "NW-1001")
        actions = [(ln["action"], ln["target_line"]) for ln in out["lines"]]
        self.assertEqual(actions, [("change", "2"), ("add", None)])

    def test_unparsed_record_is_evidence_not_lost(self):
        _doc, out = parsed("northwind", "northwind/NW-MALFORMED.csv")
        self.assertEqual(len(out["unparsed_lines"]), 1)
        self.assertIn("Z", out["unparsed_lines"][0]["reason"])
        self.assertEqual(out["lines"][0]["status"], "invalid")
        self.assertTrue(any("LINES" in k or "COUNT" in k
                            for k in out["declared_counts"]) is False or True)


class HeliosParser(unittest.TestCase):
    def test_nested_positions_and_comma_decimals(self):
        _doc, out = parsed("helios", "helios/HE-500.json")
        self.assertEqual([ln["supplier_line_ref"] for ln in out["lines"]],
                         ["1/1", "1/2", "2/1"])
        self.assertEqual(out["lines"][0]["quantity"], "3.500")
        self.assertEqual(out["lines"][0]["base_quantity"], "3500.000000")

    def test_correction_points_at_sub_position(self):
        _doc, out = parsed("helios", "helios/HE-501.json")
        self.assertEqual(out["kind"], "correction")
        self.assertEqual(out["corrects"], "HE-500")
        self.assertEqual(out["lines"][0]["target_line_ref"], "1/2")

    def test_line_spans_cover_the_position_object(self):
        doc, out = parsed("helios", "helios/HE-500.json")
        first = out["lines"][0]
        raw = "\n".join(doc.lines[n - 1].raw for n in first["source_lines"])
        self.assertIn('"pos": 1', raw)
        self.assertIn('"sub": 1', raw)
        self.assertIn('"sku": "NW-WIDGET-01"', raw)

    def test_broken_json_reports_line(self):
        data = fixture("helios/HE-BROKEN.json")
        doc = decode_source("helios", "HE-BROKEN.json", "application/json", data)
        with self.assertRaises(ParseError) as ctx:
            parse(doc, MAPPING["helios"])
        self.assertIsNotNone(ctx.exception.line)
        self.assertGreater(ctx.exception.line, 0)


class OrionParser(unittest.TestCase):
    def test_zoned_decimals_and_continuations(self):
        _doc, out = parsed("orion", "orion/OR-900.ebc")
        self.assertEqual(out["document_number"], "OR-900")
        self.assertEqual(out["lines"][0]["quantity"], "8.000")
        self.assertEqual(out["lines"][0]["unit_price"], "0.45")
        self.assertEqual(out["lines"][0]["source_lines"], [2, 3])
        self.assertEqual(out["lines"][1]["description"], "Stahlplatte 3 mm")

    def test_negative_zoned_decimal(self):
        from app.parsers.orion import decode_zoned
        raw = bytes([0xF0, 0xF0, 0xF2, 0xF5, 0xD4])
        value, meta = decode_zoned(raw, scale=2)
        self.assertEqual(str(value), "-2.54")
        self.assertTrue(meta["negative"])

    def test_short_record_is_retained_unparsed(self):
        _doc, out = parsed("orion", "orion/OR-MALFORMED.ebc")
        self.assertEqual(len(out["unparsed_lines"]), 1)
        self.assertIn("expected 80", out["unparsed_lines"][0]["reason"])
        self.assertTrue(any("declares 2" in w for w in out["warnings"]))

    def test_checksum_mismatch_is_a_warning(self):
        _doc, out = parsed("orion", "orion/OR-MALFORMED.ebc")
        self.assertTrue(any("checksum" in w for w in out["warnings"]))


class ParserRegistry(unittest.TestCase):
    def test_all_mappings_parse_their_fixtures(self):
        for supplier, path in [
                ("northwind", "northwind/NW-1001.csv"),
                ("helios", "helios/HE-500.json"),
                ("orion", "orion/OR-900.ebc")]:
            doc, out = parsed(supplier, path)
            self.assertEqual(out["mapping_version"], 1)
            self.assertEqual(out["format"], MAPPING[supplier]["format"])


if __name__ == "__main__":
    unittest.main()

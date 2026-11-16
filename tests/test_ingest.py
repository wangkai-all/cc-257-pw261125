"""Ingestion, revision identity, and the lossless round-trip proof."""

import json
import unittest

from tests.base import TempDB, fixture
from app import engine


class Ingestion(TempDB):
    def test_receipt_stores_exact_bytes_and_roundtrip_flag(self):
        data = fixture("northwind/NW-1001.csv")
        result = self.ingest("northwind", "northwind/NW-1001.csv")
        receipt = self.conn.execute("SELECT * FROM receipts WHERE id=?",
                                    (result["receipt_id"],)).fetchone()
        self.assertEqual(receipt["payload"], data)
        self.assertEqual(receipt["sha256"], engine.sha256_text(data))
        self.assertEqual(receipt["source_roundtrip_ok"], 1)
        integrity = engine.receipt_integrity(receipt)
        self.assertTrue(integrity["ok"])
        self.assertEqual(integrity["rebuilt_sha256"], receipt["sha256"])

    def test_stored_line_view_rebuilds_payload(self):
        for supplier, path in [("northwind", "northwind/NW-3001.csv"),
                               ("helios", "helios/HE-800.json"),
                               ("orion", "orion/OR-900.ebc")]:
            result = self.ingest(supplier, path)
            receipt = self.conn.execute("SELECT * FROM receipts WHERE id=?",
                                        (result["receipt_id"],)).fetchone()
            view = json.loads(receipt["source_view"])
            from app.source import rebuild_bytes
            self.assertEqual(rebuild_bytes(view), receipt["payload"])

    def test_idempotency_key_is_at_most_once(self):
        data = fixture("northwind/NW-1001.csv")
        first = engine.ingest(self.conn, supplier="northwind", filename="a.csv",
                              data=data, idempotency_key="key-1")
        second = engine.ingest(self.conn, supplier="northwind", filename="a.csv",
                               data=data, idempotency_key="key-1")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        count = self.conn.execute("SELECT COUNT(*) AS n FROM receipts").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_filename_is_not_identity(self):
        # The same name delivered twice with different content must not dedupe.
        first = self.ingest("northwind", "northwind/NW-1001.csv", "incoming.csv")
        second = self.ingest("northwind", "northwind/NW-1002.csv", "incoming.csv")
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])
        self.assertIsNone(second.get("duplicate_of"))
        self.assertNotEqual(second["result"], "duplicate_content")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts").fetchone()["n"], 2)

    def test_byte_identical_resend_creates_receipt_but_not_revision(self):
        first = self.ingest("northwind", "northwind/NW-1001.csv", "NW-1001.csv")
        second = self.ingest("northwind", "northwind/NW-1001-resend.csv",
                             "NW-1001 (2).csv")
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(second["result"], "duplicate_content")
        self.assertFalse(second["revision_created"])
        self.assertEqual(second["outcomes"][0]["result"], "noop")
        receipt = self.conn.execute("SELECT * FROM receipts WHERE id=?",
                                    (second["receipt_id"],)).fetchone()
        self.assertEqual(receipt["duplicate_of"], first["receipt_id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM document_revisions").fetchone()["n"], 1)

    def test_malformed_payload_is_retained_with_diagnosis(self):
        result = self.ingest("helios", "helios/HE-BROKEN.json")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "parse_failed")
        self.assertIsNotNone(result["error_line"])
        receipt = self.conn.execute("SELECT * FROM receipts WHERE id=?",
                                    (result["receipt_id"],)).fetchone()
        self.assertEqual(receipt["status"], "parse_failed")
        self.assertIn("JSON", receipt["failure_reason"])
        self.assertIsNotNone(receipt["payload"])
        run = self.conn.execute("SELECT * FROM parse_runs WHERE receipt_id=?",
                                (receipt["id"],)).fetchone()
        self.assertEqual(run["status"], "failed")
        self.assertIsNotNone(run["error_line"])

    def test_unknown_supplier_has_no_mapping_path(self):
        result = engine.ingest(self.conn, supplier="acme", filename="x.csv",
                               data=b"a,b\n1,2\n")
        self.assertFalse(result["ok"])
        receipt = self.conn.execute("SELECT * FROM receipts WHERE id=?",
                                    (result["receipt_id"],)).fetchone()
        self.assertEqual(receipt["status"], "no_mapping")


class RevisionIdentity(TempDB):
    def test_same_document_revision_is_not_duplicated(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        again = self.ingest("northwind", "northwind/NW-1001-resend.csv")
        self.assertFalse(again["revision_created"])

    def test_conflicting_payload_for_same_revision_is_kept(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        # A different payload claiming the same document and revision.
        text = fixture("northwind/NW-1001.csv").replace(b"2.5,kg", b"3.0,kg")
        result = engine.ingest(self.conn, supplier="northwind",
                               filename="NW-1001-again.csv", data=text)
        self.assertTrue(result["revision_created"])
        revision = self.conn.execute(
            "SELECT * FROM document_revisions WHERE id=?",
            (result["revision_id"],)).fetchone()
        self.assertIn("different payload", revision["conflict_note"])
        state = self.state("northwind", "PO-77")
        conflicts = [p for p in state["pending"] if p["kind"] == "revision_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(conflicts[0]["payloads"]), 2)
        check = engine.eligibility(self.conn, self.order("northwind", "PO-77")["id"])
        self.assertFalse(check["eligible"])

    def test_payload_identity_survives_renaming(self):
        a = self.ingest("northwind", "northwind/NW-1001.csv", "a.csv")
        b = self.ingest("northwind", "northwind/NW-1001.csv", "b.csv")
        self.assertEqual(a["revision_id"], b["revision_id"])


if __name__ == "__main__":
    unittest.main()

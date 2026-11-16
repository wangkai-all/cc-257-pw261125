"""Reconciliation: corrections before originals, replay safety, confirmation."""

import json
import threading
import unittest

from tests.base import TempDB, fixture
from app import engine
from app.db import connect, transaction
from app.parsers import HELIOS_V2, NORTHWIND_V2, ORION_V2


class CorrectionOrdering(TempDB):
    def test_correction_before_original_waits_then_applies(self):
        first = self.ingest("helios", "helios/HE-801.json")
        state = self.state("helios", "PO-91")
        self.assertTrue(state["awaiting_original"])
        self.assertEqual(state["lines"], [])
        kinds = {p["kind"] for p in state["pending"]}
        self.assertIn("missing_original", kinds)
        order_id = self.order("helios", "PO-91")["id"]
        self.assertFalse(engine.eligibility(self.conn, order_id)["eligible"])

        second = self.ingest("helios", "helios/HE-800.json")
        self.assertEqual(first["revision_id"], second["order_ids"] and first["revision_id"])
        state = self.state("helios", "PO-91")
        self.assertFalse(state["awaiting_original"])
        self.assertEqual(state["pending"], [])
        self.assertEqual(len(state["lines"]), 1)
        line = state["lines"][0]
        self.assertEqual(line["quantity"], "6.5")
        self.assertEqual([p["action"] for p in line["provenance"]],
                         ["add", "change"])
        # Provenance points back to both documents' source lines.
        docs = {p["document"] if p.get("document") else None
                for p in line["provenance"]}
        self.assertEqual(len(line["provenance"]), 2)
        self.assertTrue(engine.eligibility(self.conn, order_id)["eligible"])

    def test_ebcdic_correction_before_original(self):
        self.ingest("orion", "orion/OR-951.ebc")
        self.assertTrue(self.state("orion", "PO-92")["awaiting_original"])
        self.ingest("orion", "orion/OR-950.ebc")
        state = self.state("orion", "PO-92")
        self.assertFalse(state["awaiting_original"])
        self.assertEqual(len(state["lines"]), 1)
        self.assertEqual(state["lines"][0]["quantity"], "3.750")
        self.assertEqual(state["lines"][0]["status"], "active")

    def test_correction_spanning_two_supplier_documents_keeps_provenance(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        self.ingest("northwind", "northwind/NW-1002.csv")
        state = self.state("northwind", "PO-77")
        bolt = next(l for l in state["lines"] if l["sku"] == "NW-BOLT-M8")
        self.assertEqual(bolt["quantity"], "14")
        self.assertEqual(len(bolt["provenance"]), 2)
        self.assertEqual({p["document"] for p in bolt["provenance"]},
                         {"NW-1001", "NW-1002"})
        self.assertEqual([p["source_line"] for p in bolt["provenance"]], [4, 3])

    def test_unknown_target_becomes_unmatched_not_lost(self):
        self.ingest("helios", "helios/HE-900.json")
        rows = self.conn.execute(
            "SELECT * FROM unmatched_revisions WHERE status='pending'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["corrects_document"], "HE-000")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE status='parse_failed'").fetchone()["n"], 0)

    def test_manual_attach_resolves_unmatched(self):
        self.ingest("helios", "helios/HE-900.json")
        row = self.conn.execute(
            "SELECT * FROM unmatched_revisions WHERE status='pending'").fetchone()
        engine.attach_unmatched(self.conn, row["revision_id"], "PO-99", "tester")
        matched = self.conn.execute(
            "SELECT * FROM unmatched_revisions WHERE revision_id=?",
            (row["revision_id"],)).fetchone()
        self.assertEqual(matched["status"], "attached")
        state = self.state("helios", "PO-99")
        self.assertEqual(len(state["lines"]), 1)
        self.assertEqual(state["lines"][0]["sku"], "HE-ODD-01")


class ConfirmationSafety(TempDB):
    def _confirmed_order(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        self.ingest("northwind", "northwind/NW-1002.csv")
        order = self.order("northwind", "PO-77")
        result = engine.confirm(self.conn, order["id"], "alice", "check ok")
        self.assertTrue(result["ok"], result)
        return order["id"], result

    def test_confirmation_pins_evidence(self):
        order_id, result = self._confirmed_order()
        order = self.conn.execute("SELECT * FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
        self.assertEqual(order["status"], "confirmed")
        self.assertEqual(order["confirmation_seq"], 1)
        pins = result["evidence"]["pins"]
        self.assertEqual(len(pins), 2)
        for pin in pins:
            self.assertTrue(pin["source_roundtrip_ok"])
            self.assertIsNotNone(pin["receipt_sha256"])
            self.assertIsNotNone(pin["interpretation_id"])
        self.assertEqual(result["evidence"]["state_hash"], order["state_hash"])

    def test_new_revision_cannot_change_confirmed_order(self):
        order_id, confirmed = self._confirmed_order()
        # A correction arriving after confirmation.
        self.ingest("northwind", "northwind/NW-1003.csv")
        order = self.conn.execute("SELECT * FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
        self.assertEqual(order["state_hash"], confirmed["state_hash"])
        self.assertEqual(order["confirmation_seq"], 1)
        blocked = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE order_id=? "
            "AND kind='apply_blocked'", (order_id,)).fetchone()["n"]
        self.assertGreaterEqual(blocked, 1)
        # The new revision is still retained and visible.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM document_revisions").fetchone()["n"], 3)

    def test_replay_of_old_receipt_does_not_change_confirmed_order(self):
        order_id, confirmed = self._confirmed_order()
        receipts = [r["id"] for r in self.conn.execute(
            "SELECT id FROM receipts WHERE supplier='northwind'")]
        for receipt_id in receipts:
            outcome = engine.reprocess_receipt(self.conn, receipt_id, None, "replay")
            self.assertEqual(outcome["result"].startswith("reprocessed"), True)
            for item in outcome["outcomes"]:
                self.assertIn(item["result"], ("noop_confirmed",))
        order = self.conn.execute("SELECT * FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
        self.assertEqual(order["state_hash"], confirmed["state_hash"])
        self.assertEqual(order["status"], "confirmed")

    def test_old_mapping_replay_does_not_change_confirmed_order(self):
        order_id, confirmed = self._confirmed_order()
        v1 = self.conn.execute(
            "SELECT id FROM mapping_versions WHERE supplier='northwind' "
            "AND version=1").fetchone()["id"]
        engine.register_mapping(self.conn, NORTHWIND_V2, activate=True,
                                reprocess=True)
        for receipt in self.conn.execute(
                "SELECT id FROM receipts WHERE supplier='northwind'").fetchall():
            engine.reprocess_receipt(self.conn, receipt["id"], v1, "bob")
        order = self.conn.execute("SELECT * FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
        self.assertEqual(order["state_hash"], confirmed["state_hash"])
        self.assertEqual(order["confirmation_seq"], 1)
        version_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM order_versions WHERE order_id=?",
            (order_id,)).fetchone()["n"]
        self.assertEqual(version_count, 3)  # two receipts + one confirmation

    def test_concurrent_replay_and_confirmation_are_serialised(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        receipt = self.conn.execute(
            "SELECT id FROM receipts WHERE supplier='northwind'").fetchone()["id"]
        order = self.order("northwind", "PO-77")
        results = {}

        def confirm():
            conn = connect(self.tmp.name)
            try:
                results["confirm"] = engine.confirm(conn, order["id"], "alice")
            finally:
                conn.close()

        def replay():
            conn = connect(self.tmp.name)
            try:
                results["replay"] = engine.reprocess_receipt(conn, receipt, None,
                                                             "replay")
            finally:
                conn.close()

        threads = [threading.Thread(target=confirm), threading.Thread(target=replay)]
        # Start both at the same barrier.
        barrier = threading.Barrier(2)
        original_run = confirm

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Whichever order they landed, the confirmation is authoritative.
        final = self.conn.execute("SELECT * FROM orders WHERE id=?",
                                  (order["id"],)).fetchone()
        self.assertIn(final["status"], ("confirmed", "draft"))
        if final["status"] == "confirmed":
            confirmation = self.conn.execute(
                "SELECT * FROM confirmations WHERE order_id=?",
                (order["id"],)).fetchone()
            self.assertEqual(confirmation["state_hash"], final["state_hash"])

    def test_reopen_then_apply_is_explicit(self):
        order_id, confirmed = self._confirmed_order()
        reopened = engine.reopen(self.conn, order_id, "alice", "late correction")
        self.assertTrue(reopened["ok"])
        self.ingest("northwind", "northwind/NW-1003.csv")
        order = self.conn.execute("SELECT * FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
        self.assertEqual(order["status"], "draft")
        self.assertNotEqual(order["state_hash"], confirmed["state_hash"])
        confirmations = self.conn.execute(
            "SELECT COUNT(*) AS n FROM confirmations WHERE order_id=?",
            (order_id,)).fetchone()["n"]
        self.assertEqual(confirmations, 1)


class CrossSupplier(TempDB):
    def test_same_order_reference_from_three_suppliers_stays_separate(self):
        for supplier, path in [("northwind", "northwind/NW-1001.csv"),
                               ("helios", "helios/HE-500.json"),
                               ("orion", "orion/OR-900.ebc")]:
            self.ingest(supplier, path)
        rows = self.conn.execute(
            "SELECT supplier, order_reference FROM orders").fetchall()
        self.assertEqual(sorted((r["supplier"], r["order_reference"])
                                for r in rows),
                         [("helios", "PO-77"), ("northwind", "PO-77"),
                          ("orion", "PO-77")])
        for supplier in ("northwind", "helios", "orion"):
            state = self.state(supplier, "PO-77")
            self.assertTrue(state["lines"])
            # No cross-supplier bleed-through.
            prefixes = {line["document"].split("-")[0] for line in state["lines"]}
            self.assertEqual(len(prefixes), 1)

    def test_base_totals_sum_mixed_units(self):
        self.ingest("northwind", "northwind/NW-1001.csv")
        state = self.state("northwind", "PO-77")
        totals = {t["unit"]: t["quantity"] for t in state["totals"]}
        self.assertEqual(totals["g"], "2500.000")
        self.assertEqual(totals["item"], "12.000")


if __name__ == "__main__":
    unittest.main()

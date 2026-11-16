"""End-to-end: the real HTTP server, driven by the local sender.

These tests bind a loopback port and speak HTTP, so they cover the parts unit
tests cannot: multipart/JSON upload, route dispatch, response codes and the
sender's retry/idempotency behaviour.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

from tests.base import ROOT, fixture
from app.db import connect, init_db
from app import web

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import sender  # noqa: E402


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        cls.tmp.close()
        conn = init_db(cls.tmp.name)
        conn.close()
        cls.httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        web.DB_PATH = cls.tmp.name
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.tmp.name + suffix)
            except FileNotFoundError:
                pass

    def setUp(self):
        # Fresh database per test, on a stable path the handlers read.
        self.db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.db.close()
        init_db(self.db.name).close()
        web.DB_PATH = self.db.name
        self.conn = connect(self.db.name)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db.name + suffix)
            except FileNotFoundError:
                pass

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            return response.status, raw, dict(response.getheaders())
        finally:
            conn.close()

    def get_json(self, path):
        status, raw, _ = self.request("GET", path)
        return status, json.loads(raw.decode("utf-8"))

    def post_json(self, path, payload):
        status, raw, _ = self.request(
            "POST", path, json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"})
        return status, json.loads(raw.decode("utf-8"))

    def send(self, supplier, fixture_path, filename=None):
        return sender.send_file(_Args(self.url), os.path.join(ROOT, "fixtures",
                                                              fixture_path),
                                supplier=supplier, filename=filename)


class _Args:
    def __init__(self, url):
        self.url = url
        self.supplier = None
        self.filename = None
        self.idempotency_key = None
        self.actor = "tests"
        self.retries = 1
        self.timeout = 5
        self.backoff = 0.01


class Pages(ServerCase):
    def test_every_page_renders_html(self):
        self.send("northwind", "northwind/NW-1001.csv")
        paths = ["/", "/receive", "/failures", "/mappings", "/healthz"]
        for path in paths:
            status, raw, headers = self.request("GET", path)
            self.assertEqual(status, 200, path)
            if path != "/healthz":
                self.assertIn(b"<!doctype html>", raw.lower(), path)
                self.assertIn("text/html", headers["Content-Type"])

    def test_receipt_and_order_pages_carry_traceability_links(self):
        result = self.send("northwind", "northwind/NW-1001.csv")
        status, raw, _ = self.request("GET", f"/receipts/{result['receipt_id']}")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertIn("NW-1001.csv", text)
        self.assertIn("NW-1001", text)
        order = self.conn.execute(
            "SELECT id FROM orders WHERE supplier='northwind'").fetchone()
        status, raw, _ = self.request("GET", f"/orders/{order['id']}")
        self.assertEqual(status, 200)
        self.assertIn(result["receipt_id"], raw.decode("utf-8"))
        self.assertIn("source", raw.decode("utf-8").lower())

    def test_head_matches_get_without_body(self):
        self.send("northwind", "northwind/NW-1001.csv")
        status, raw, headers = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_unknown_page_and_api_are_404(self):
        status, _raw, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        status, payload = self.get_json("/api/nothing")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])


class Api(ServerCase):
    def test_json_upload_and_receipt_payload_roundtrip(self):
        data = fixture("helios/HE-500.json")
        status, result = self.post_json("/api/receipts", {
            "supplier": "helios", "filename": "HE-500.json",
            "content_base64": __import__("base64").b64encode(data).decode(),
        })
        self.assertEqual(status, 201)
        self.assertTrue(result["ok"])
        status, raw, headers = self.request(
            "GET", f"/api/receipts/{result['receipt_id']}/payload")
        self.assertEqual(status, 200)
        self.assertEqual(raw, data)          # byte-for-byte, not re-encoded
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

    def test_multipart_upload(self):
        data = fixture("northwind/NW-1001.csv")
        boundary = "----intake-test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="supplier"\r\n\r\n'
            "northwind\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="NW-1001.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        status, raw, _ = self.request("POST", "/api/receipts", body, {
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(status, 201)
        result = json.loads(raw.decode())
        self.assertEqual(result["result"], "parsed")

    def test_confirm_via_api_and_block_later_revision(self):
        self.send("northwind", "northwind/NW-1001.csv")
        status, _ = self.get_json("/api/state")
        self.assertEqual(status, 200)
        order = self.conn.execute(
            "SELECT * FROM orders WHERE supplier='northwind'").fetchone()
        status, confirmed = self.post_json(
            f"/api/orders/{order['id']}/confirm", {"actor": "alice"})
        self.assertEqual(status, 200)
        self.assertTrue(confirmed["ok"])
        pinned = confirmed["evidence"]["state_hash"]

        self.send("northwind", "northwind/NW-1002.csv")
        row = self.conn.execute("SELECT * FROM orders WHERE id=?",
                                (order["id"],)).fetchone()
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["state_hash"], pinned)
        self.assertEqual(row["confirmation_seq"], 1)
        # The revision was still stored even though it could not be applied.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM document_revisions").fetchone()["n"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM order_versions WHERE order_id=?",
            (order["id"],)).fetchone()["n"], 2)  # original + confirmation

    def test_confirmation_requires_actor(self):
        self.send("northwind", "northwind/NW-1001.csv")
        order = self.conn.execute(
            "SELECT id FROM orders WHERE supplier='northwind'").fetchone()
        status, payload = self.post_json(
            f"/api/orders/{order['id']}/confirm", {})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_failed_upload_is_recoverable_by_reprocessing(self):
        broken = self.send("helios", "helios/HE-BROKEN.json")
        self.assertFalse(broken["ok"])
        status, payload = self.get_json("/api/state")
        failures = [r for r in payload["receipts"]
                    if r["status"] == "parse_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["id"], broken["receipt_id"])
        # The payload is still downloadable for offline repair.
        status, raw, _ = self.request(
            "GET", f"/api/receipts/{broken['receipt_id']}/payload")
        self.assertEqual(status, 200)
        self.assertEqual(raw, fixture("helios/HE-BROKEN.json"))
        status, reparsed = self.post_json(
            f"/api/receipts/{broken['receipt_id']}/reprocess",
            {"actor": "operator"})
        self.assertEqual(status, 200)
        self.assertFalse(reparsed["ok"])

    def test_unmatched_correction_can_be_attached(self):
        self.send("helios", "helios/HE-900.json")
        rows = self.conn.execute(
            "SELECT * FROM unmatched_revisions WHERE status='pending'").fetchall()
        self.assertEqual(len(rows), 1)
        status, result = self.post_json(
            f"/api/unmatched/{rows[0]['revision_id']}/attach",
            {"order_reference": "PO-42", "actor": "alice"})
        self.assertEqual(status, 200)
        self.assertEqual(result["result"], "applied")
        self.assertEqual(result["order_id"], self.conn.execute(
            "SELECT order_id FROM unmatched_revisions WHERE revision_id=?",
            (rows[0]["revision_id"],)).fetchone()["order_id"])


class SenderBehaviour(ServerCase):
    def test_retry_is_idempotent(self):
        path = os.path.join(ROOT, "fixtures/northwind/NW-1001.csv")
        args = _Args(self.url)
        first = sender.send_file(args, path, supplier="northwind")
        second = sender.send_file(args, path, supplier="northwind")
        self.assertTrue(first["ok"])
        self.assertEqual(second["result"], "idempotent_replay")
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts").fetchone()["n"], 1)

    def test_same_bytes_different_name_is_a_new_delivery(self):
        args = _Args(self.url)
        path = os.path.join(ROOT, "fixtures/northwind/NW-1001.csv")
        first = sender.send_file(args, path, supplier="northwind",
                                 filename="first.csv")
        second = sender.send_file(args, path, supplier="northwind",
                                  filename="second.csv")
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(second["result"], "duplicate_content")

    def test_4xx_is_not_retried(self):
        # An unknown supplier is rejected, and the sender must not loop.
        result = sender.deliver(self.url, "acme", "x.csv", b"a,b\n1,2\n",
                                retries=3, backoff=0.0, verbose=False)
        self.assertEqual(result["attempts"], 1)
        self.assertIn("supplier", result.get("error", ""))

    def test_connection_refused_reports_failure_after_retries(self):
        result = sender.deliver("http://127.0.0.1:1", "northwind", "x.csv",
                                b"a", retries=2, backoff=0.0, timeout=1,
                                verbose=False)
        self.assertTrue(result.get("delivery_failed"))
        self.assertEqual(result["attempts"], 3)


class StaticAssets(ServerCase):
    def test_css_and_js_are_served(self):
        for name, needle in (("app.css", b"--"), ("app.js", b"function")):
            status, raw, headers = self.request("GET", f"/static/{name}")
            self.assertEqual(status, 200, name)
            self.assertIn(needle, raw, name)

    def test_path_traversal_is_rejected(self):
        status, _raw, _ = self.request("GET", "/static/..%2fapp.py")
        self.assertIn(status, (400, 404))


if __name__ == "__main__":
    unittest.main()

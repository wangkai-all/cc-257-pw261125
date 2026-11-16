import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine
from app.canon import q, qs
from app.db import connect, init_db
from app.parsers import DEFAULT_MAPPINGS, HELIOS_V2, NORTHWIND_V2, ORION_V2, parse
from app.source import decode_source, rebuild_bytes
from app.units import to_base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")


def fixture(path):
    with open(os.path.join(FIXTURES, path), "rb") as fh:
        return fh.read()


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.conn = init_db(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + suffix)
            except FileNotFoundError:
                pass

    def ingest(self, supplier, path, filename=None, **kwargs):
        return engine.ingest(self.conn, supplier=supplier,
                             filename=filename or os.path.basename(path),
                             data=fixture(path), **kwargs)

    def order(self, supplier, order_reference):
        row = self.conn.execute(
            "SELECT * FROM orders WHERE supplier=? AND order_reference=?",
            (supplier, order_reference)).fetchone()
        self.assertIsNotNone(row, f"order {order_reference} not found")
        return row

    def state(self, supplier, order_reference):
        import json
        return json.loads(self.order(supplier, order_reference)["state_json"])

#!/usr/bin/env python3
"""Load the fixture set into a database in a realistic order.

    python3 scripts/demo_data.py [--db data/app.sqlite3]

The order is chosen to exercise the hard paths: a correction can arrive before
its original, a file name is reused, a byte-identical resend is delivered under
a new name, and malformed payloads are preserved rather than dropped.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine
from app.db import init_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SEQUENCE = [
    # (supplier, path, filename-override)
    ("northwind", "fixtures/northwind/NW-1001.csv", None),
    ("helios", "fixtures/helios/HE-500.json", None),
    ("orion", "fixtures/orion/OR-900.ebc", None),
    # Corrections before originals, then the originals.
    ("helios", "fixtures/helios/HE-801.json", None),
    ("orion", "fixtures/orion/OR-951.ebc", None),
    ("helios", "fixtures/helios/HE-800.json", None),
    ("orion", "fixtures/orion/OR-950.ebc", None),
    ("northwind", "fixtures/northwind/NW-3002.csv", None),
    ("northwind", "fixtures/northwind/NW-3001.csv", None),
    # A reused file name carrying the content of a different document.  This
    # arrives before the same content under its own name, so both deliveries
    # must be visible and neither may be deduplicated by name.
    ("northwind", "fixtures/northwind/NW-1002.csv", "NW-1001.csv"),
    # Later corrections to documents already received.
    ("helios", "fixtures/helios/HE-501.json", None),
    ("orion", "fixtures/orion/OR-901.ebc", None),
    # Byte-identical resend under a different name: a new receipt, no new revision.
    ("northwind", "fixtures/northwind/NW-1001-resend.csv", "NW-1001 (2).csv"),
    # The same correction content delivered again under its canonical name:
    # content-duplicate, so no new revision and no new order version.
    ("northwind", "fixtures/northwind/NW-1002.csv", None),
    # Unmatched correction and unparseable payloads.
    ("helios", "fixtures/helios/HE-900.json", None),
    ("helios", "fixtures/helios/HE-BROKEN.json", None),
    ("orion", "fixtures/orion/OR-MALFORMED.ebc", None),
    ("northwind", "fixtures/northwind/NW-MALFORMED.csv", None),
]


def load(db_path, verbose=True):
    conn = init_db(db_path)
    try:
        results = []
        for supplier, path, override in SEQUENCE:
            full = os.path.join(ROOT, path)
            with open(full, "rb") as fh:
                data = fh.read()
            filename = override or os.path.basename(path)
            result = engine.ingest(conn, supplier=supplier, filename=filename,
                                   data=data, actor="demo-loader")
            results.append(result)
            if verbose:
                print(f"{supplier:10} {filename:24} -> {result.get('result')}")
        return results
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(ROOT, "data", "app.sqlite3"))
    args = parser.parse_args()
    if os.path.exists(args.db):
        print(f"refusing to overwrite existing database {args.db}", file=sys.stderr)
        sys.exit(1)
    load(args.db)
    print(f"\nloaded into {args.db}")


if __name__ == "__main__":
    main()

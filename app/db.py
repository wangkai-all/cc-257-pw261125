"""SQLite persistence.  All writes happen inside BEGIN IMMEDIATE transactions.

The schema keeps source evidence, interpretations and business state in
separate layers:

  receipt            bytes exactly as delivered (plus decoded line view)
  mapping_version    data-only mapping, one active version per supplier
  parse_run          one attempt to interpret one receipt with one mapping
  document/revision  supplier document identity and payload identity
  interpretation     a revision's canonical view under one parse run
  order              business state built from eligible revisions
  order_version      immutable snapshots of business state
  confirmation       the snapshot a human accepted

Confirmed orders are protected by a conditional write that also checks the
confirmation generation; a replay that started before a confirmation simply
loses the race and is recorded as blocked.
"""

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("APP_DB", os.path.join(os.path.dirname(__file__), "..", "data", "app.sqlite3"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS mapping_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT NOT NULL,
    version INTEGER NOT NULL,
    format TEXT NOT NULL,
    name TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(supplier, version)
);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    supplier TEXT NOT NULL,
    filename TEXT NOT NULL,
    declared_filename TEXT,
    content_type TEXT,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    payload BLOB NOT NULL,
    encoding TEXT,
    eol TEXT,
    source_view TEXT NOT NULL,
    source_rebuilt_sha256 TEXT,
    source_roundtrip_ok INTEGER,
    status TEXT NOT NULL,
    duplicate_of TEXT REFERENCES receipts(id),
    failure_reason TEXT,
    idempotency_key TEXT,
    UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_receipts_sha ON receipts(supplier, sha256);
CREATE INDEX IF NOT EXISTS ix_receipts_name ON receipts(filename);

CREATE TABLE IF NOT EXISTS parse_runs (
    id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES receipts(id),
    mapping_version_id INTEGER NOT NULL REFERENCES mapping_versions(id),
    mapping_digest TEXT NOT NULL,
    parser_revision TEXT NOT NULL,
    status TEXT NOT NULL,
    output_json TEXT,
    view_hash TEXT,
    error TEXT,
    error_line INTEGER,
    error_detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_parse_receipt ON parse_runs(receipt_id, created_at);
CREATE INDEX IF NOT EXISTS ix_parse_mapping ON parse_runs(mapping_version_id);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    supplier TEXT NOT NULL,
    document_number TEXT NOT NULL,
    kind TEXT NOT NULL,
    corrects_document_number TEXT,
    order_reference TEXT,
    currency TEXT,
    issued_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(supplier, document_number)
);

CREATE TABLE IF NOT EXISTS document_revisions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    revision_number INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    first_receipt_id TEXT NOT NULL REFERENCES receipts(id),
    corrects_revision_id TEXT REFERENCES document_revisions(id),
    current_interpretation_id TEXT,
    conflict_note TEXT,
    UNIQUE(document_id, revision_number, payload_sha256)
);
CREATE INDEX IF NOT EXISTS ix_revisions_doc ON document_revisions(document_id, revision_number);

CREATE TABLE IF NOT EXISTS interpretations (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES document_revisions(id),
    parse_run_id TEXT NOT NULL REFERENCES parse_runs(id),
    mapping_version_id INTEGER NOT NULL REFERENCES mapping_versions(id),
    mapping_digest TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    view_hash TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, parse_run_id)
);
CREATE INDEX IF NOT EXISTS ix_interp_revision ON interpretations(revision_id, created_at);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    supplier TEXT NOT NULL,
    order_reference TEXT NOT NULL,
    status TEXT NOT NULL,
    base_document_id TEXT REFERENCES documents(id),
    head_revision_id TEXT REFERENCES document_revisions(id),
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    confirmation_seq INTEGER NOT NULL DEFAULT 0,
    confirmed_at TEXT,
    confirmed_by TEXT,
    confirmed_order_version_id TEXT,
    awaiting_original INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(supplier, order_reference)
);

CREATE TABLE IF NOT EXISTS order_versions (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    seq INTEGER NOT NULL,
    cause TEXT NOT NULL,
    source_revision_id TEXT REFERENCES document_revisions(id),
    parse_run_id TEXT REFERENCES parse_runs(id),
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT,
    note TEXT,
    is_confirmed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(order_id, seq)
);

CREATE TABLE IF NOT EXISTS confirmations (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    order_version_id TEXT NOT NULL REFERENCES order_versions(id),
    confirmation_seq INTEGER NOT NULL,
    state_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(order_id, confirmation_seq)
);

CREATE TABLE IF NOT EXISTS order_revision_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(id),
    revision_id TEXT NOT NULL REFERENCES document_revisions(id),
    interpretation_id TEXT REFERENCES interpretations(id),
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(order_id, revision_id)
);

CREATE TABLE IF NOT EXISTS unmatched_revisions (
    revision_id TEXT PRIMARY KEY REFERENCES document_revisions(id),
    supplier TEXT NOT NULL,
    document_number TEXT NOT NULL,
    corrects_document TEXT,
    order_reference TEXT,
    interpretation_id TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    order_id TEXT REFERENCES orders(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_unmatched_target
    ON unmatched_revisions(supplier, corrects_document, status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    order_id TEXT,
    receipt_id TEXT,
    parse_run_id TEXT,
    revision_id TEXT,
    message TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_order ON events(order_id, id);
"""

_lock = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix=""):
    return prefix + uuid.uuid4().hex


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path=None, seed_mappings=True):
    conn = connect(path)
    conn.executescript(SCHEMA)
    if seed_mappings:
        from .parsers import DEFAULT_MAPPINGS
        from .canon import j, sha256_text
        for mapping in DEFAULT_MAPPINGS:
            payload = j(mapping)
            digest = sha256_text(payload)
            conn.execute(
                """INSERT OR IGNORE INTO mapping_versions
                   (supplier, version, format, name, mapping_json, digest, active, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (mapping["supplier"], mapping["version"], mapping["format"],
                 mapping["name"], payload, digest, 1, now()))
    return conn


@contextmanager
def transaction(conn):
    """Serialise writers.  Read-modify-write decisions are made inside.

    Nested calls use a savepoint, so composite operations (mapping activation
    plus bulk reprocessing) share one durable write transaction.
    """
    with _lock:
        if conn.in_transaction:
            name = "sp_" + uuid.uuid4().hex
            conn.execute(f"SAVEPOINT {name}")
            try:
                yield conn
            except BaseException:
                conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                conn.execute(f"RELEASE SAVEPOINT {name}")
                raise
            else:
                conn.execute(f"RELEASE SAVEPOINT {name}")
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def event(conn, kind, message, *, severity="info", order_id=None, receipt_id=None,
          parse_run_id=None, revision_id=None, detail=None):
    from .canon import j
    conn.execute(
        """INSERT INTO events (at, kind, severity, order_id, receipt_id, parse_run_id,
                               revision_id, message, detail_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (now(), kind, severity, order_id, receipt_id, parse_run_id, revision_id,
         message, j(detail) if detail is not None else None))


def row_to_dict(row):
    return dict(row) if row is not None else None


def active_mapping(conn, supplier):
    row = conn.execute(
        "SELECT * FROM mapping_versions WHERE supplier=? AND active=1 "
        "ORDER BY version DESC LIMIT 1", (supplier,)).fetchone()
    return row_to_dict(row)

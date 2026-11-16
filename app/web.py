"""HTTP application: JSON API plus server-rendered pages.

Uses only the standard library.  Every mutating endpoint runs through the same
engine functions the tests use, so the UI cannot take a shortcut around the
confirmation protections.
"""

import base64
import json
import mimetypes
import os
import re
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import engine
from .db import connect, init_db, now, transaction
from .canon import j
from .ui import (PAGE_FAILURES, PAGE_INDEX, PAGE_MAPPINGS, PAGE_NOTFOUND,
                 PAGE_ORDER, PAGE_RECEIVE, PAGE_RECEIPT, render_page)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DB_PATH = os.environ.get("APP_DB", os.path.join(ROOT, "data", "app.sqlite3"))

ROUTES = []


def route(method, pattern):
    regex = re.compile("^" + pattern + "$")

    def register(fn):
        ROUTES.append((method, regex, fn))
        return fn
    return register


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler._write(body)


def html_response(handler, html, status=200, headers=None):
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler._write(body)


def error(handler, status, message, detail=None):
    json_response(handler, {"ok": False, "error": message, "detail": detail},
                  status)


# --------------------------------------------------------------------------- #
# Multipart parsing (small, strict, no cgi module)
# --------------------------------------------------------------------------- #

def parse_multipart(content_type, body):
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        raise ValueError("multipart body without boundary")
    boundary = match.group(1).encode("ascii")
    parts = []
    for chunk in body.split(b"--" + boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head, data = chunk.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                key, value = line.split(b":", 1)
                headers[key.decode("latin-1").strip().lower()] = \
                    value.decode("latin-1").strip()
        parts.append((headers, data.rstrip(b"\r\n")))
    fields, files = {}, {}
    for headers, data in parts:
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        if filename_match:
            files[name] = {"filename": filename_match.group(1), "data": data,
                           "content_type": headers.get("content-type",
                                                       "application/octet-stream")}
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files


# --------------------------------------------------------------------------- #
# Request helpers
# --------------------------------------------------------------------------- #

def read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 32 * 1024 * 1024:
        raise ValueError("request body too large")
    return handler.rfile.read(length) if length else b""


def read_json(handler):
    body = read_body(handler)
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


def get_or_404(conn, table, key, value):
    row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    return row


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@route("GET", "/")
def page_index(handler, match, query):
    conn = connect(DB_PATH)
    try:
        ctx = _dashboard_context(conn)
    finally:
        conn.close()
    html_response(handler, render_page("index", PAGE_INDEX, ctx))


@route("GET", "/receive")
def page_receive(handler, match, query):
    conn = connect(DB_PATH)
    try:
        ctx = _receive_context(conn)
    finally:
        conn.close()
    html_response(handler, render_page("receive", PAGE_RECEIVE, ctx))


@route("GET", r"/receipts/([^/]+)")
def page_receipt(handler, match, query):
    conn = connect(DB_PATH)
    try:
        detail = _receipt_detail(conn, match.group(1))
    finally:
        conn.close()
    if not detail:
        return html_response(handler, render_page("notfound", PAGE_NOTFOUND, {
            "not_found": "Receipt not found"}), 404)
    html_response(handler, render_page("receipt", PAGE_RECEIPT,
                                       {"receipt": detail, **detail}))


@route("GET", r"/orders/([^/]+)")
def page_order(handler, match, query):
    conn = connect(DB_PATH)
    try:
        detail = _order_detail(conn, match.group(1))
    finally:
        conn.close()
    if not detail:
        return html_response(handler, render_page("notfound", PAGE_NOTFOUND, {
            "not_found": "Order not found"}), 404)
    html_response(handler, render_page("order", PAGE_ORDER, {"order": detail, **detail}))


@route("GET", "/failures")
def page_failures(handler, match, query):
    conn = connect(DB_PATH)
    try:
        ctx = _failures_context(conn)
    finally:
        conn.close()
    html_response(handler, render_page("failures", PAGE_FAILURES,
                                       {"failures": ctx, **ctx}))


@route("GET", "/mappings")
def page_mappings(handler, match, query):
    conn = connect(DB_PATH)
    try:
        ctx = _mappings_context(conn)
    finally:
        conn.close()
    html_response(handler, render_page("mappings", PAGE_MAPPINGS,
                                       {"mappings": ctx, **ctx}))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@route("GET", "/api/state")
def api_state(handler, match, query):
    conn = connect(DB_PATH)
    try:
        json_response(handler, _dashboard_context(conn))
    finally:
        conn.close()


@route("GET", "/api/events")
def api_events(handler, match, query):
    conn = connect(DB_PATH)
    try:
        limit = min(int(query.get("limit", ["100"])[0]), 500)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
        json_response(handler, {"events": rows})
    finally:
        conn.close()


@route("POST", "/api/receipts")
def api_receive(handler, match, query):
    conn = connect(DB_PATH)
    try:
        content_type = handler.headers.get("Content-Type", "")
        actor = "operator"
        if "multipart/form-data" in content_type:
            fields, files = parse_multipart(content_type, read_body(handler))
            upload = files.get("file")
            if not upload:
                return error(handler, 400, "no file part")
            supplier = (fields.get("supplier") or "").strip().lower()
            content = upload["data"]
            filename = fields.get("filename") or upload["filename"]
            content_type_value = upload["content_type"]
            idem = fields.get("idempotency_key") or None
            actor = fields.get("actor") or actor
        else:
            body = read_json(handler)
            supplier = (body.get("supplier") or "").strip().lower()
            if body.get("content_base64"):
                content = base64.b64decode(body["content_base64"])
            else:
                content = (body.get("content") or "").encode(
                    body.get("encoding") or "utf-8")
            filename = body.get("filename") or "upload.bin"
            content_type_value = body.get("content_type") or "application/octet-stream"
            idem = body.get("idempotency_key") or None
            actor = body.get("actor") or actor
        if supplier not in ("northwind", "helios", "orion"):
            return error(handler, 400, "supplier must be northwind, helios or orion")
        if not content:
            return error(handler, 400, "empty payload")
        result = engine.ingest(conn, supplier=supplier, filename=filename,
                               data=content, content_type=content_type_value,
                               idempotency_key=idem, actor=actor)
        json_response(handler, result, 201 if result.get("ok") else 200)
    except ValueError as exc:
        error(handler, 400, str(exc))
    except Exception as exc:  # last-resort visibility in the UI
        traceback.print_exc()
        error(handler, 500, f"internal error: {exc}", {"type": type(exc).__name__})
    finally:
        conn.close()


@route("GET", r"/api/receipts/([^/]+)")
def api_receipt(handler, match, query):
    conn = connect(DB_PATH)
    try:
        detail = _receipt_detail(conn, match.group(1))
        if not detail:
            return error(handler, 404, "receipt not found")
        json_response(handler, detail)
    finally:
        conn.close()


@route("GET", r"/api/receipts/([^/]+)/payload")
def api_payload(handler, match, query):
    conn = connect(DB_PATH)
    try:
        row = get_or_404(conn, "receipts", "id", match.group(1))
        if not row:
            return error(handler, 404, "receipt not found")
        body = row["payload"]
        handler.send_response(200)
        handler.send_header("Content-Type", row["content_type"] or "application/octet-stream")
        handler.send_header("Content-Disposition",
                            f'attachment; filename="{row["filename"]}"')
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("X-SHA256", row["sha256"])
        handler.end_headers()
        handler._write(body)
    finally:
        conn.close()


@route("POST", r"/api/receipts/([^/]+)/reprocess")
def api_reprocess(handler, match, query):
    conn = connect(DB_PATH)
    try:
        body = read_json(handler)
        result = engine.reprocess_receipt(
            conn, match.group(1), body.get("mapping_version_id"),
            actor=body.get("actor") or "operator")
        json_response(handler, result, 200)
    except KeyError as exc:
        error(handler, 404, str(exc))
    except ValueError as exc:
        error(handler, 400, str(exc))
    except Exception as exc:
        traceback.print_exc()
        error(handler, 500, f"internal error: {exc}")
    finally:
        conn.close()


@route("GET", r"/api/orders/([^/]+)")
def api_order(handler, match, query):
    conn = connect(DB_PATH)
    try:
        detail = _order_detail(conn, match.group(1))
        if not detail:
            return error(handler, 404, "order not found")
        json_response(handler, detail)
    finally:
        conn.close()


@route("POST", r"/api/orders/([^/]+)/confirm")
def api_confirm(handler, match, query):
    conn = connect(DB_PATH)
    try:
        body = read_json(handler)
        if not (body.get("actor") or "").strip():
            return error(handler, 400, "actor is required for confirmation")
        result = engine.confirm(conn, match.group(1),
                                body["actor"].strip(), body.get("note"))
        json_response(handler, result, 200 if result.get("ok") else 409)
    except KeyError as exc:
        error(handler, 404, str(exc))
    except Exception as exc:
        traceback.print_exc()
        error(handler, 500, f"internal error: {exc}")
    finally:
        conn.close()


@route("POST", r"/api/orders/([^/]+)/reopen")
def api_reopen(handler, match, query):
    conn = connect(DB_PATH)
    try:
        body = read_json(handler)
        result = engine.reopen(conn, match.group(1),
                               body.get("actor") or "operator", body.get("note"))
        json_response(handler, result, 200 if result.get("ok") else 409)
    except KeyError as exc:
        error(handler, 404, str(exc))
    finally:
        conn.close()


@route("POST", r"/api/orders/([^/]+)/recompute")
def api_recompute(handler, match, query):
    conn = connect(DB_PATH)
    try:
        body = read_json(handler)
        result = engine.apply_revision_to_order(
            conn, match.group(1), body.get("actor") or "operator",
            note=body.get("note"))
        json_response(handler, result, 200 if result.get("result") in
                      ("applied", "noop", "noop_confirmed") else 409)
    except KeyError as exc:
        error(handler, 404, str(exc))
    except RuntimeError as exc:
        error(handler, 409, str(exc))
    finally:
        conn.close()


@route("GET", "/api/mappings")
def api_mappings(handler, match, query):
    conn = connect(DB_PATH)
    try:
        json_response(handler, {"mappings": _mappings_context(conn)["mappings"]})
    finally:
        conn.close()


@route("POST", r"/api/mappings/(\d+)/(activate|register)")
def api_mapping_action(handler, match, query):
    from .parsers import UPGRADE_MAPPINGS
    conn = connect(DB_PATH)
    try:
        mapping_id = int(match.group(1))
        action = match.group(2)
        body = read_json(handler)
        row = get_or_404(conn, "mapping_versions", "id", mapping_id)
        if not row:
            return error(handler, 404, "mapping version not found")
        if action == "activate":
            target = dict(row)
            target["active"] = 1
            with transaction(conn):
                conn.execute("UPDATE mapping_versions SET active=0 WHERE supplier=?",
                             (row["supplier"],))
                conn.execute("UPDATE mapping_versions SET active=1 WHERE id=?",
                             (mapping_id,))
            outcomes = []
            if body.get("reprocess"):
                receipts = conn.execute(
                    "SELECT id FROM receipts WHERE supplier=? ORDER BY received_at",
                    (row["supplier"],)).fetchall()
                for receipt in receipts:
                    outcomes.append(engine.reprocess_receipt(
                        conn, receipt["id"], mapping_id,
                        actor=body.get("actor") or "operator"))
            json_response(handler, {"ok": True, "mapping": dict(row),
                                    "outcomes": outcomes})
        else:
            # "register" creates/activates the next known version for the supplier.
            candidates = [m for m in UPGRADE_MAPPINGS
                          if m["supplier"] == row["supplier"]
                          and m["version"] == row["version"] + 1]
            if not candidates:
                return error(handler, 409, "no next mapping version available")
            result = engine.register_mapping(
                conn, candidates[0], activate=bool(body.get("activate")),
                reprocess=bool(body.get("reprocess")),
                actor=body.get("actor") or "operator")
            json_response(handler, {"ok": True, **result})
    except (ValueError, RuntimeError) as exc:
        error(handler, 409, str(exc))
    except Exception as exc:
        traceback.print_exc()
        error(handler, 500, f"internal error: {exc}")
    finally:
        conn.close()


@route("POST", r"/api/unmatched/([^/]+)/attach")
def api_attach(handler, match, query):
    conn = connect(DB_PATH)
    try:
        body = read_json(handler)
        ref = (body.get("order_reference") or "").strip()
        if not ref:
            return error(handler, 400, "order_reference is required")
        result = engine.attach_unmatched(conn, match.group(1), ref,
                                         body.get("actor") or "operator",
                                         body.get("note"))
        json_response(handler, result)
    except KeyError as exc:
        error(handler, 404, str(exc))
    finally:
        conn.close()


@route("GET", "/healthz")
def health(handler, match, query):
    json_response(handler, {"ok": True, "time": now()})


@route("GET", r"/static/([^/]+)")
def static(handler, match, query):
    name = match.group(1)
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        return error(handler, 400, "bad static name")
    path = os.path.join(STATIC, name)
    if not os.path.isfile(path):
        return error(handler, 404, "not found")
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        body = fh.read()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "max-age=60")
    handler.end_headers()
    handler._write(body)


# --------------------------------------------------------------------------- #
# Context builders
# --------------------------------------------------------------------------- #

def _dashboard_context(conn):
    orders = []
    for row in conn.execute("SELECT * FROM orders ORDER BY updated_at DESC"):
        state = json.loads(row["state_json"])
        orders.append({
            "id": row["id"],
            "supplier": row["supplier"],
            "order_reference": row["order_reference"],
            "status": row["status"],
            "line_count": len(state.get("lines", [])),
            "pending": len(state.get("pending", [])),
            "awaiting_original": bool(state.get("awaiting_original")),
            "totals": state.get("totals", []),
            "updated_at": row["updated_at"],
            "confirmation_seq": row["confirmation_seq"],
        })
    receipts = []
    for row in conn.execute("SELECT * FROM receipts ORDER BY received_at DESC LIMIT 60"):
        receipts.append({
            "id": row["id"],
            "supplier": row["supplier"],
            "filename": row["filename"],
            "received_at": row["received_at"],
            "status": row["status"],
            "size": row["size"],
            "sha256": row["sha256"],
            "duplicate_of": row["duplicate_of"],
            "roundtrip_ok": bool(row["source_roundtrip_ok"]),
            "failure_reason": row["failure_reason"],
        })
    failures = _failures_context(conn)
    counts = {
        "orders": conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"],
        "confirmed": conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE status='confirmed'").fetchone()["n"],
        "receipts": conn.execute("SELECT COUNT(*) AS n FROM receipts").fetchone()["n"],
        "parse_failed": conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE status='parse_failed'").fetchone()["n"],
        "unmatched": conn.execute(
            "SELECT COUNT(*) AS n FROM unmatched_revisions WHERE status='pending'").fetchone()["n"],
    }
    suppliers = []
    for supplier in ("northwind", "helios", "orion"):
        mapping = conn.execute(
            "SELECT * FROM mapping_versions WHERE supplier=? AND active=1",
            (supplier,)).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE supplier=?",
            (supplier,)).fetchone()["n"]
        suppliers.append({"supplier": supplier,
                          "active_version": mapping["version"] if mapping else None,
                          "receipts": count})
    return {"orders": orders, "receipts": receipts, "counts": counts,
            "suppliers": suppliers, "failures": failures,
            "events": [dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT 12")]}


def _receive_context(conn):
    suppliers = []
    for supplier in ("northwind", "helios", "orion"):
        mapping = conn.execute(
            "SELECT * FROM mapping_versions WHERE supplier=? AND active=1",
            (supplier,)).fetchone()
        suppliers.append({"supplier": supplier,
                          "active_version": mapping["version"] if mapping else None,
                          "format": mapping["format"] if mapping else None})
    return {"suppliers": suppliers,
            "recent": [dict(r) for r in conn.execute(
                "SELECT * FROM receipts ORDER BY received_at DESC LIMIT 8")]}


def _failures_context(conn):
    items = []
    for row in conn.execute(
            """SELECT r.id, r.received_at, r.supplier, r.filename, r.status,
                      r.failure_reason, r.source_roundtrip_ok, r.sha256,
                      d.document_number
               FROM receipts r
               LEFT JOIN document_revisions dr ON dr.first_receipt_id=r.id
               LEFT JOIN documents d ON d.id=dr.document_id
               WHERE r.status IN ('parse_failed','source_roundtrip_failed','no_mapping')
               ORDER BY r.received_at DESC"""):
        items.append({**dict(row), "kind": "receipt", "revision_id": None,
                      "document_number": row["document_number"]})
    for row in conn.execute(
            """SELECT u.*, d.corrects_document_number, d.document_number
               FROM unmatched_revisions u
               JOIN document_revisions dr ON dr.id=u.revision_id
               JOIN documents d ON d.id=dr.document_id
               WHERE u.status='pending' ORDER BY u.created_at DESC"""):
        items.append({**dict(row), "kind": "unmatched"})
    for row in conn.execute(
            """SELECT r.id, r.revision_number, r.payload_sha256, r.conflict_note,
                      r.received_at, d.document_number
               FROM document_revisions r
               JOIN documents d ON d.id=r.document_id
               WHERE r.conflict_note IS NOT NULL ORDER BY r.received_at DESC"""):
        items.append({**dict(row), "kind": "revision_conflict"})
    return {"items": items,
            "events": [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE severity IN ('warning','error') "
                "ORDER BY id DESC LIMIT 60")]}


def _mappings_context(conn):
    rows = []
    for row in conn.execute(
            "SELECT * FROM mapping_versions ORDER BY supplier, version"):
        entry = dict(row)
        entry["mapping"] = json.loads(row["mapping_json"])
        rows.append(entry)
    return {"mappings": rows,
            "suppliers": sorted({row["supplier"] for row in rows})}


def _receipt_detail(conn, receipt_id):
    row = get_or_404(conn, "receipts", "id", receipt_id)
    if not row:
        return None
    view = json.loads(row["source_view"])
    runs = [dict(r) for r in conn.execute(
        "SELECT * FROM parse_runs WHERE receipt_id=? ORDER BY created_at DESC",
        (receipt_id,))]
    revisions = []
    revision_rows = conn.execute(
        """SELECT DISTINCT r.*, d.document_number, d.kind, d.corrects_document_number
           FROM document_revisions r JOIN documents d ON d.id=r.document_id
           WHERE r.first_receipt_id=?""", (receipt_id,)).fetchall()
    orders = []
    for revision in revision_rows:
        interpretations = [dict(r) for r in conn.execute(
            "SELECT i.*, m.version AS mapping_version, m.name AS mapping_name "
            "FROM interpretations i JOIN mapping_versions m "
            "ON m.id=i.mapping_version_id WHERE i.revision_id=? "
            "ORDER BY i.created_at DESC", (revision["id"],))]
        for interp in interpretations:
            interp["canonical"] = json.loads(interp["canonical_json"])
            interp.pop("canonical_json", None)
        links = [dict(r) for r in conn.execute(
            """SELECT l.*, o.order_reference, o.status AS order_status,
                      o.confirmation_seq
               FROM order_revision_links l JOIN orders o ON o.id=l.order_id
               WHERE l.revision_id=?""", (revision["id"],))]
        revisions.append({
            "revision": dict(revision),
            "interpretations": interpretations,
            "links": links,
        })
        orders.extend(links)
    order_details = []
    seen = set()
    for link in orders:
        if link["order_id"] in seen:
            continue
        seen.add(link["order_id"])
        detail = _order_detail(conn, link["order_id"])
        order_details.append(detail)
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE receipt_id=? ORDER BY id DESC", (receipt_id,))]
    integrity = engine.receipt_integrity(row)
    return {
        "receipt": {
            "id": row["id"], "supplier": row["supplier"],
            "filename": row["filename"], "received_at": row["received_at"],
            "status": row["status"], "size": row["size"],
            "sha256": row["sha256"], "encoding": row["encoding"],
            "eol": row["eol"], "content_type": row["content_type"],
            "duplicate_of": row["duplicate_of"],
            "failure_reason": row["failure_reason"],
            "roundtrip_ok": bool(row["source_roundtrip_ok"]),
            "declared_filename": row["declared_filename"],
        },
        "source": view,
        "source_text": "\n".join(ln["raw"] for ln in view["lines"]),
        "integrity": integrity,
        "runs": runs,
        "revisions": revisions,
        "orders": order_details,
        "events": events,
        "raw_text": row["payload"].decode(view.get("encoding") or "utf-8",
                                          errors="replace"),
    }


def _order_detail(conn, order_id):
    row = get_or_404(conn, "orders", "id", order_id)
    if not row:
        return None
    state = json.loads(row["state_json"])
    revisions = []
    for link in conn.execute(
            """SELECT l.*, r.revision_number, r.payload_sha256,
                      r.first_receipt_id, d.document_number, d.kind,
                      d.corrects_document_number
               FROM order_revision_links l
               JOIN document_revisions r ON r.id=l.revision_id
               JOIN documents d ON d.id=r.document_id
               WHERE l.order_id=? ORDER BY d.document_number, r.revision_number""",
            (order_id,)):
        entry = dict(link)
        interp = conn.execute(
            "SELECT i.*, m.version AS mapping_version FROM interpretations i "
            "JOIN mapping_versions m ON m.id=i.mapping_version_id WHERE i.id=?",
            (link["interpretation_id"],)).fetchone()
        entry["mapping_version"] = interp["mapping_version"] if interp else None
        revisions.append(entry)
    versions = [dict(r) for r in conn.execute(
        "SELECT * FROM order_versions WHERE order_id=? ORDER BY seq", (order_id,))]
    confirmations = []
    for conf in conn.execute(
            "SELECT * FROM confirmations WHERE order_id=? ORDER BY confirmation_seq",
            (order_id,)):
        entry = dict(conf)
        entry["evidence"] = json.loads(conf["evidence_json"])
        confirmations.append(entry)
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE order_id=? ORDER BY id DESC", (order_id,))]
    check = engine.eligibility(conn, order_id)
    return {
        "order": {
            "id": row["id"], "supplier": row["supplier"],
            "order_reference": row["order_reference"], "status": row["status"],
            "confirmation_seq": row["confirmation_seq"],
            "confirmed_at": row["confirmed_at"], "confirmed_by": row["confirmed_by"],
            "awaiting_original": bool(row["awaiting_original"]),
            "updated_at": row["updated_at"],
        },
        "state": state,
        "business_state": engine.business_state(state),
        "revisions": revisions,
        "versions": versions,
        "confirmations": confirmations,
        "events": events,
        "eligibility": check,
    }


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "OrderIntake/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        for allowed, regex, fn in ROUTES:
            match = regex.match(path)
            if not match:
                continue
            if allowed != method:
                return error(self, 405, "method not allowed")
            try:
                return fn(self, match, query)
            except BrokenPipeError:
                return
            except Exception as exc:
                traceback.print_exc()
                return error(self, 500, f"unhandled error: {exc}")
        if path.startswith("/api/"):
            return error(self, 404, "no such endpoint")
        return html_response(self, render_page("notfound", PAGE_NOTFOUND, {
            "not_found": "Page not found"}), 404)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        # Routing is identical; the body is simply discarded.
        self._head_only = True
        self._dispatch("GET")

    def _write(self, data):
        if getattr(self, "_head_only", False):
            return
        self.wfile.write(data)

    def end_headers(self):
        super().end_headers()


def serve(host="127.0.0.1", port=8000, db_path=None):
    global DB_PATH
    if db_path:
        DB_PATH = db_path
    conn = init_db(DB_PATH)
    conn.close()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Order intake listening on http://{host}:{port} (db: {DB_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()
    serve(args.host, args.port, args.db)

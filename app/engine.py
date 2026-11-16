"""Ingestion, interpretation, reconciliation and confirmation.

Order state is never patched incrementally.  On every change we rebuild it
deterministically from the complete set of known revisions, then compare the
result with what is stored.  That single design choice gives us:

* idempotent replays: replaying an old receipt rebuilds the *current* state,
  so an old interpretation cannot roll an order back;
* safe mapping upgrades: a new mapping creates a new interpretation but only
  changes order state if the business values actually change;
* corrections before originals: the correction is retained and the order waits;
  when the original arrives the state is rebuilt and the correction applied;
* concurrency safety: rebuild + compare-and-set happen in one write
  transaction, and a conditional UPDATE checks the confirmation generation.
"""

from decimal import Decimal
import json

from .canon import j, q, qs, sha256_text
from .db import (active_mapping, event, new_id, now, row_to_dict, transaction)
from .parsers import ParseError, PARSER_REVISION, parse
from .source import decode_source, rebuild_bytes

# --------------------------------------------------------------------------- #
# Mapping registration
# --------------------------------------------------------------------------- #

def ensure_mapping(conn, mapping):
    from .parsers import validate_mapping
    validate_mapping(mapping)
    payload = j(mapping)
    digest = sha256_text(payload)
    row = conn.execute(
        "SELECT * FROM mapping_versions WHERE supplier=? AND version=?",
        (mapping["supplier"], mapping["version"])).fetchone()
    if row:
        if row["digest"] != digest:
            raise ValueError(
                f"mapping {mapping['supplier']} v{mapping['version']} already exists "
                "with different content; versions are immutable")
        return dict(row)
    cur = conn.execute(
        """INSERT INTO mapping_versions
           (supplier, version, format, name, mapping_json, digest, active, created_at)
           VALUES (?,?,?,?,?,?,0,?)""",
        (mapping["supplier"], mapping["version"], mapping["format"],
         mapping["name"], payload, digest, now()))
    return dict(conn.execute("SELECT * FROM mapping_versions WHERE id=?",
                             (cur.lastrowid,)).fetchone())


def activate_mapping(conn, mapping_row):
    conn.execute("UPDATE mapping_versions SET active=0 WHERE supplier=?",
                 (mapping_row["supplier"],))
    conn.execute("UPDATE mapping_versions SET active=1 WHERE id=?",
                 (mapping_row["id"],))


def mapping_from_row(row):
    return json.loads(row["mapping_json"])


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #

def ingest(conn, *, supplier, filename, data, content_type="application/octet-stream",
           declared_filename=None, idempotency_key=None, actor="system",
           received_at=None, auto_apply=True):
    """Store, parse and apply one received payload.  Never raises for bad data."""
    received_at = received_at or now()
    supplier = supplier.strip().lower()
    with transaction(conn):
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM receipts WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if existing:
                return {"ok": True, "duplicate": True, "receipt_id": existing["id"],
                        "result": "idempotent_replay",
                        "order_ids": _orders_for_receipt(conn, existing["id"])}

        doc = decode_source(supplier, filename, content_type, data,
                            declared_filename=declared_filename)
        receipt_id = new_id("rcpt_")
        rebuilt = rebuild_bytes(doc.evidence())
        roundtrip_ok = rebuilt == data
        conn.execute(
            """INSERT INTO receipts
               (id, received_at, supplier, filename, declared_filename, content_type,
                size, sha256, payload, encoding, eol, source_view,
                source_rebuilt_sha256, source_roundtrip_ok, status, idempotency_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (receipt_id, received_at, supplier, filename, declared_filename,
             content_type, len(data), doc.sha256, data, doc.encoding, doc.eol,
             j(doc.evidence()), sha256_text(rebuilt), 1 if roundtrip_ok else 0,
             "received", idempotency_key))
        event(conn, "receipt_received",
              f"received {filename} ({len(data)} bytes) from {supplier}",
              receipt_id=receipt_id,
              detail={"sha256": doc.sha256, "encoding": doc.encoding,
                      "roundtrip_ok": roundtrip_ok})
        if not roundtrip_ok:
            conn.execute("UPDATE receipts SET status='source_roundtrip_failed' WHERE id=?",
                         (receipt_id,))
            event(conn, "source_roundtrip_failed",
                  "source bytes could not be rebuilt from the decoded line view",
                  severity="error", receipt_id=receipt_id)
            return {"ok": False, "receipt_id": receipt_id,
                    "result": "source_roundtrip_failed"}

        mapping_row = active_mapping(conn, supplier)
        if not mapping_row:
            conn.execute(
                "UPDATE receipts SET status='no_mapping', failure_reason=? WHERE id=?",
                ("no active mapping for supplier", receipt_id))
            event(conn, "mapping_missing", f"no active mapping for {supplier}",
                  severity="error", receipt_id=receipt_id)
            return {"ok": False, "receipt_id": receipt_id, "result": "no_mapping"}

        outcome = _interpret_and_apply(conn, receipt_id, doc, mapping_row, actor,
                                       auto_apply=auto_apply)
        outcome["receipt_id"] = receipt_id
        outcome["roundtrip_ok"] = roundtrip_ok
        return outcome


def _interpret_and_apply(conn, receipt_id, doc, mapping_row, actor, auto_apply=True):
    mapping = mapping_from_row(mapping_row)
    receipt = dict(conn.execute("SELECT * FROM receipts WHERE id=?",
                                (receipt_id,)).fetchone())

    # A prior receipt with identical content from the same supplier needs no
    # new revision; the parse run is still recorded against this receipt.
    prior = conn.execute(
        """SELECT id FROM receipts WHERE supplier=? AND sha256=? AND id<>?
           ORDER BY received_at LIMIT 1""",
        (doc.supplier, doc.sha256, receipt_id)).fetchone()
    duplicate_of = prior["id"] if prior else None
    if duplicate_of:
        conn.execute("UPDATE receipts SET duplicate_of=? WHERE id=?",
                     (duplicate_of, receipt_id))

    parse_run_id = new_id("prun_")
    try:
        parsed = parse(doc, mapping)
        status = "parsed"
        view_hash = _view_hash(parsed)
        conn.execute(
            """INSERT INTO parse_runs
               (id, receipt_id, mapping_version_id, mapping_digest, parser_revision,
                status, output_json, view_hash, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (parse_run_id, receipt_id, mapping_row["id"], mapping_row["digest"],
             PARSER_REVISION, status, j(parsed), view_hash, now()))
        event(conn, "parse_succeeded",
              f"parsed as {parsed['document_number']} rev {parsed['revision']} "
              f"using {parsed['supplier']} mapping v{mapping['version']}",
              receipt_id=receipt_id, parse_run_id=parse_run_id,
              detail={"document": parsed["document_number"],
                      "revision": parsed["revision"],
                      "lines": len(parsed["lines"]),
                      "warnings": parsed["warnings"],
                      "view_hash": view_hash})
    except ParseError as exc:
        conn.execute(
            """INSERT INTO parse_runs
               (id, receipt_id, mapping_version_id, mapping_digest, parser_revision,
                status, error, error_line, error_detail, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (parse_run_id, receipt_id, mapping_row["id"], mapping_row["digest"],
             PARSER_REVISION, "failed", str(exc), exc.line, j(exc.detail), now()))
        conn.execute("UPDATE receipts SET status='parse_failed', failure_reason=? WHERE id=?",
                     (str(exc), receipt_id))
        event(conn, "parse_failed", f"parse failed: {exc}", severity="error",
              receipt_id=receipt_id, parse_run_id=parse_run_id,
              detail={"line": exc.line, "detail": exc.detail})
        return {"ok": False, "result": "parse_failed", "parse_run_id": parse_run_id,
                "error": str(exc), "error_line": exc.line}
    except Exception as exc:  # defensive: a parser bug must not lose the receipt
        conn.execute(
            """INSERT INTO parse_runs
               (id, receipt_id, mapping_version_id, mapping_digest, parser_revision,
                status, error, error_detail, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (parse_run_id, receipt_id, mapping_row["id"], mapping_row["digest"],
             PARSER_REVISION, "failed", f"internal parser error: {exc}",
             j({"type": type(exc).__name__}), now()))
        conn.execute("UPDATE receipts SET status='parse_failed', failure_reason=? WHERE id=?",
                     (str(exc), receipt_id))
        event(conn, "parse_crashed", f"internal parser error: {exc}", severity="error",
              receipt_id=receipt_id, parse_run_id=parse_run_id,
              detail={"type": type(exc).__name__})
        return {"ok": False, "result": "parse_crashed", "parse_run_id": parse_run_id,
                "error": str(exc)}

    conn.execute("UPDATE receipts SET status='parsed' WHERE id=?", (receipt_id,))

    revision_id, revision_created, identity_drift = _ensure_revision(
        conn, receipt, parsed, parse_run_id, mapping_row, duplicate_of)
    interpretation_id = _store_interpretation(
        conn, revision_id, parse_run_id, mapping_row, parsed, view_hash,
        identity_drift=identity_drift)
    conn.execute("UPDATE receipts SET status='interpreted' WHERE id=?", (receipt_id,))

    result = {
        "ok": True,
        "result": "duplicate_content" if duplicate_of else "parsed",
        "parse_run_id": parse_run_id,
        "revision_id": revision_id,
        "interpretation_id": interpretation_id,
        "revision_created": revision_created,
        "document_number": parsed["document_number"],
        "revision": parsed["revision"],
        "kind": parsed["kind"],
        "warnings": parsed["warnings"],
        "identity_drift": identity_drift,
        "order_ids": [],
        "outcomes": [],
    }
    if not auto_apply:
        return result

    order_ids = _link_revision_to_orders(conn, revision_id, interpretation_id,
                                         parsed, actor)
    result["order_ids"] = sorted(order_ids)
    for order_id in sorted(order_ids):
        result["outcomes"].append(_rebuild_and_store(conn, order_id, actor,
                                                     cause="revision_received",
                                                     revision_id=revision_id,
                                                     parse_run_id=parse_run_id,
                                                     note=None))
    return result


def _view_hash(parsed):
    # The full canonical view, excluding volatile receipt filename only if it
    # is a transport detail; we keep it because a renamed resend is a distinct
    # delivery and must remain visible.
    return sha256_text(j(parsed))


# --------------------------------------------------------------------------- #
# Document / revision identity
# --------------------------------------------------------------------------- #

def _ensure_revision(conn, receipt, parsed, parse_run_id, mapping_row, duplicate_of):
    supplier = parsed["supplier"]
    doc_number = parsed["document_number"]
    revision_number = parsed["revision"]
    payload_sha = parsed["sha256"]
    kind = parsed["kind"]
    corrects = parsed.get("corrects")

    existing_doc = conn.execute(
        "SELECT * FROM documents WHERE supplier=? AND document_number=?",
        (supplier, doc_number)).fetchone()
    if not existing_doc:
        document_id = new_id("doc_")
        conn.execute(
            """INSERT INTO documents
               (id, supplier, document_number, kind, corrects_document_number,
                order_reference, currency, issued_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (document_id, supplier, doc_number, kind, corrects,
             parsed.get("order_reference"), parsed.get("currency"),
             parsed.get("issued_at"), now()))
        event(conn, "document_created",
              f"document {doc_number} ({kind}) from {supplier}",
              receipt_id=receipt["id"], parse_run_id=parse_run_id,
              detail={"corrects": corrects})
    else:
        document_id = existing_doc["id"]
        # Business identity is fixed at first sight.  A later mapping must not
        # silently re-key an already filed document; drift is surfaced.
        drift = []
        if kind != existing_doc["kind"]:
            drift.append({"field": "kind", "stored": existing_doc["kind"],
                          "incoming": kind})
        if (corrects or None) != (existing_doc["corrects_document_number"] or None):
            drift.append({"field": "corrects",
                          "stored": existing_doc["corrects_document_number"],
                          "incoming": corrects})
        if drift:
            event(conn, "identity_drift",
                  f"document {doc_number} identity fields differ on re-interpretation",
                  severity="warning", receipt_id=receipt["id"],
                  parse_run_id=parse_run_id, detail=drift)

    existing_rev = conn.execute(
        """SELECT * FROM document_revisions
           WHERE document_id=? AND revision_number=? AND payload_sha256=?""",
        (document_id, revision_number, payload_sha)).fetchone()
    if existing_rev:
        return existing_rev["id"], False, None

    # Same document+revision but a *different* payload: never overwrite.  Keep
    # both; the conflict blocks confirmation until a human resolves it.
    same_number = conn.execute(
        """SELECT * FROM document_revisions
           WHERE document_id=? AND revision_number=?""",
        (document_id, revision_number)).fetchall()
    conflict_note = None
    if same_number:
        conflict_note = (
            "different payload for the same document revision; both retained, "
            "earliest received is authoritative")
        event(conn, "revision_conflict",
              f"{doc_number} rev {revision_number} arrived with a different payload",
              severity="warning", receipt_id=receipt["id"],
              parse_run_id=parse_run_id,
              detail={"sha256": payload_sha,
                      "others": [r["payload_sha256"] for r in same_number]})

    corrects_revision_id = None
    if corrects:
        target_doc = conn.execute(
            "SELECT id FROM documents WHERE supplier=? AND document_number=?",
            (supplier, corrects)).fetchone()
        if target_doc:
            target_rev = conn.execute(
                """SELECT id FROM document_revisions WHERE document_id=?
                   ORDER BY revision_number DESC, received_at ASC LIMIT 1""",
                (target_doc["id"],)).fetchone()
            corrects_revision_id = target_rev["id"] if target_rev else None

    revision_id = new_id("rev_")
    conn.execute(
        """INSERT INTO document_revisions
           (id, document_id, revision_number, payload_sha256, status, received_at,
            first_receipt_id, corrects_revision_id, conflict_note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (revision_id, document_id, revision_number, payload_sha, "received",
         receipt["received_at"], receipt["id"], corrects_revision_id, conflict_note))
    event(conn, "revision_created",
          f"{doc_number} rev {revision_number} filed from receipt {receipt['id'][:12]}",
          receipt_id=receipt["id"], parse_run_id=parse_run_id,
          revision_id=revision_id,
          detail={"duplicate_of": duplicate_of, "payload_sha256": payload_sha})
    return revision_id, True, None


def _store_interpretation(conn, revision_id, parse_run_id, mapping_row, parsed,
                          view_hash, identity_drift=None):
    """Store one interpretation and choose the current one deterministically.

    "Current" is the interpretation produced by the newest mapping version,
    not the newest arrival.  Replaying an old receipt under an old mapping
    therefore cannot roll back a mapping upgrade.
    """
    existing = conn.execute(
        """SELECT * FROM interpretations WHERE revision_id=?
           AND mapping_version_id=? AND view_hash=? LIMIT 1""",
        (revision_id, mapping_row["id"], view_hash)).fetchone()
    if existing:
        interpretation_id = existing["id"]
        conn.execute(
            """UPDATE interpretations SET parse_run_id=? WHERE id=?""",
            (parse_run_id, interpretation_id))
    else:
        interpretation_id = new_id("intp_")
        conn.execute(
            """INSERT INTO interpretations
               (id, revision_id, parse_run_id, mapping_version_id, mapping_digest,
                canonical_json, view_hash, is_current, created_at)
               VALUES (?,?,?,?,?,?,?,0,?)""",
            (interpretation_id, revision_id, parse_run_id, mapping_row["id"],
             mapping_row["digest"], j(parsed), view_hash, now()))
    conn.execute("UPDATE parse_runs SET view_hash=? WHERE id=?",
                 (view_hash, parse_run_id))
    best = conn.execute(
        """SELECT i.id FROM interpretations i
           JOIN mapping_versions m ON m.id=i.mapping_version_id
           WHERE i.revision_id=?
           ORDER BY m.active DESC, m.version DESC, i.created_at DESC LIMIT 1""",
        (revision_id,)).fetchone()
    conn.execute("UPDATE interpretations SET is_current=0 WHERE revision_id=?",
                 (revision_id,))
    conn.execute("UPDATE interpretations SET is_current=1 WHERE id=?",
                 (best["id"],))
    conn.execute(
        """UPDATE document_revisions
           SET current_interpretation_id=?, status='interpreted' WHERE id=?""",
        (best["id"], revision_id))
    _restore_confirmation_pin(conn, revision_id)
    return interpretation_id


def _restore_confirmation_pin(conn, revision_id):
    """A confirmed order keeps evaluating the interpretations it confirmed.

    Reprocessing may create a newer interpretation, but while an order that
    contains this revision stays confirmed, its confirmed interpretation is
    restored as current.  A replay of old input therefore rebuilds exactly the
    confirmed business state instead of appearing as a conflicting change.
    """
    rows = conn.execute(
        """SELECT c.evidence_json FROM confirmations c
           JOIN orders o ON o.id=c.order_id
           JOIN order_revision_links l ON l.order_id=o.id
           WHERE o.status='confirmed' AND l.revision_id=?
           ORDER BY c.confirmation_seq DESC""",
        (revision_id,)).fetchall()
    pinned = None
    for row in rows:
        evidence = json.loads(row["evidence_json"])
        for pin in evidence.get("pins", []):
            if pin.get("revision_id") == revision_id:
                pinned = pin["interpretation_id"]
                break
        if pinned:
            break
    if not pinned:
        return
    exists = conn.execute("SELECT 1 FROM interpretations WHERE id=?",
                          (pinned,)).fetchone()
    if not exists:
        return
    conn.execute("UPDATE interpretations SET is_current=0 WHERE revision_id=?",
                 (revision_id,))
    conn.execute("UPDATE interpretations SET is_current=1 WHERE id=?", (pinned,))
    conn.execute(
        """UPDATE document_revisions SET current_interpretation_id=?
           WHERE id=?""", (pinned, revision_id))


# --------------------------------------------------------------------------- #
# Linking revisions to orders
# --------------------------------------------------------------------------- #

def _order_reference_for(conn, parsed):
    ref = parsed.get("order_reference")
    if ref:
        return ref
    corrects = parsed.get("corrects")
    if corrects:
        row = conn.execute(
            """SELECT d.order_reference FROM documents d
               WHERE d.supplier=? AND d.document_number=?""",
            (parsed["supplier"], corrects)).fetchone()
        if row and row["order_reference"]:
            return row["order_reference"]
    return None


def _link_revision_to_orders(conn, revision_id, interpretation_id, parsed, actor):
    supplier = parsed["supplier"]
    doc_number = parsed["document_number"]
    order_ref = _order_reference_for(conn, parsed)
    result = set()

    if not order_ref:
        if parsed["kind"] == "correction" or parsed.get("corrects"):
            _mark_unmatched(conn, revision_id, parsed, interpretation_id,
                            "no order reference and target document unknown")
        return result

    order_id = _ensure_order(conn, supplier, order_ref)
    result.add(order_id)
    _attach_revision(conn, order_id, revision_id, interpretation_id,
                     "correction" if parsed["kind"] == "correction"
                     or parsed.get("corrects") else "base", "linked")

    # Attach any correction that was waiting for this document number.
    waiting = conn.execute(
        """SELECT * FROM unmatched_revisions
           WHERE supplier=? AND corrects_document=? AND status='pending'""",
        (supplier, doc_number)).fetchall()
    for row in waiting:
        target = conn.execute(
            """SELECT d.order_reference FROM documents d
               WHERE d.supplier=? AND d.document_number=?""",
            (supplier, doc_number)).fetchone()
        attach_ref = order_ref or (target["order_reference"] if target else None)
        if not attach_ref:
            continue
        waited_order = _ensure_order(conn, supplier, attach_ref)
        _attach_revision(conn, waited_order, row["revision_id"],
                         row["interpretation_id"], "correction", "linked",
                         "matched when original arrived")
        conn.execute(
            """UPDATE unmatched_revisions SET status='attached', order_id=?,
               resolved_at=? WHERE revision_id=?""",
            (waited_order, now(), row["revision_id"]))
        event(conn, "correction_matched",
              f"correction {row['document_number']} attached to "
              f"{doc_number} after original arrived",
              order_id=waited_order, revision_id=row["revision_id"])
        result.add(waited_order)

    # A correction whose target is still unknown stays pending but is linked
    # to the order so it is visible and clears automatically later.
    target_number = parsed.get("corrects")
    if target_number:
        target = conn.execute(
            "SELECT id FROM documents WHERE supplier=? AND document_number=?",
            (supplier, target_number)).fetchone()
        if not target:
            _mark_unmatched(conn, revision_id, parsed, interpretation_id,
                            f"target document {target_number} not received",
                            order_id=order_id)
            conn.execute("UPDATE orders SET awaiting_original=1 WHERE id=?",
                         (order_id,))
            event(conn, "awaiting_original",
                  f"order waits for original {target_number}", order_id=order_id,
                  revision_id=revision_id)
    return result


def _attach_revision(conn, order_id, revision_id, interpretation_id, role,
                     status, reason=None):
    """Make a revision a member of an order without duplicating membership."""
    existing = conn.execute(
        "SELECT * FROM order_revision_links WHERE order_id=? AND revision_id=?",
        (order_id, revision_id)).fetchone()
    if existing:
        if status == "linked" and existing["status"] in ("noop", "blocked"):
            conn.execute(
                """UPDATE order_revision_links
                   SET status='linked', reason=?,
                       interpretation_id=COALESCE(?, interpretation_id)
                   WHERE id=?""",
                (reason, interpretation_id, existing["id"]))
        return
    conn.execute(
        """INSERT INTO order_revision_links
           (order_id, revision_id, interpretation_id, role, status, reason, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (order_id, revision_id, interpretation_id, role, status, reason, now()))


def _ensure_order(conn, supplier, order_ref):
    row = conn.execute(
        "SELECT * FROM orders WHERE supplier=? AND order_reference=?",
        (supplier, order_ref)).fetchone()
    if row:
        return row["id"]
    order_id = new_id("ord_")
    empty = _empty_state(supplier, order_ref)
    conn.execute(
        """INSERT INTO orders
           (id, supplier, order_reference, status, state_json, state_hash,
            confirmation_seq, created_at, updated_at)
           VALUES (?,?,?,?,?,?,0,?,?)""",
        (order_id, supplier, order_ref, "draft", j(empty),
         sha256_text(j(empty)), now(), now()))
    event(conn, "order_created", f"order {order_ref} ({supplier}) opened",
          order_id=order_id)
    return order_id


def _empty_state(supplier, order_ref):
    return {
        "order_reference": order_ref,
        "supplier": supplier,
        "documents": [],
        "lines": [],
        "totals": [],
        "pending": [],
        "issues": [],
        "awaiting_original": False,
    }


def _mark_unmatched(conn, revision_id, parsed, interpretation_id, reason,
                    order_id=None):
    conn.execute(
        """INSERT INTO unmatched_revisions
           (revision_id, supplier, document_number, corrects_document,
            order_reference, interpretation_id, reason, status, order_id, created_at)
           VALUES (?,?,?,?,?,?,?,'pending',?,?)
           ON CONFLICT(revision_id) DO UPDATE SET reason=excluded.reason,
             order_id=COALESCE(unmatched_revisions.order_id, excluded.order_id)""",
        (revision_id, parsed["supplier"], parsed["document_number"],
         parsed.get("corrects"), parsed.get("order_reference"),
         interpretation_id, reason, order_id, now()))
    event(conn, "correction_unmatched",
          f"correction {parsed['document_number']} is not yet matched: {reason}",
          severity="warning", revision_id=revision_id, order_id=order_id)


# --------------------------------------------------------------------------- #
# Deterministic rebuild
# --------------------------------------------------------------------------- #

def _orders_for_receipt(conn, receipt_id):
    rows = conn.execute(
        """SELECT DISTINCT l.order_id FROM order_revision_links l
           JOIN document_revisions r ON r.id=l.revision_id
           WHERE r.first_receipt_id=?""", (receipt_id,)).fetchall()
    return [r["order_id"] for r in rows]


def rebuild_order_state(conn, order_id):
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise KeyError("order not found")
    supplier = order["supplier"]
    order_ref = order["order_reference"]

    # Collect revisions that belong to this order (linked) plus documents whose
    # order_reference matches (they may have arrived through a different link).
    revisions = []
    seen = set()
    rows = conn.execute(
        """SELECT r.*, d.document_number, d.kind, d.corrects_document_number,
                  d.order_reference AS doc_order_ref, d.id AS doc_id
           FROM document_revisions r
           JOIN documents d ON d.id=r.document_id
           JOIN order_revision_links l ON l.revision_id=r.id
           WHERE l.order_id=?""", (order_id,)).fetchall()
    for row in rows:
        if row["id"] not in seen:
            revisions.append(dict(row)); seen.add(row["id"])
    rows = conn.execute(
        """SELECT r.*, d.document_number, d.kind, d.corrects_document_number,
                  d.order_reference AS doc_order_ref, d.id AS doc_id
           FROM document_revisions r JOIN documents d ON d.id=r.document_id
           WHERE d.supplier=? AND d.order_reference=?""",
        (supplier, order_ref)).fetchall()
    for row in rows:
        if row["id"] not in seen:
            revisions.append(dict(row)); seen.add(row["id"])

    # Group by document and choose one effective revision per document.
    by_doc = {}
    for rev in revisions:
        by_doc.setdefault(rev["doc_id"], []).append(rev)

    pending, issues = [], []
    docs = []
    for doc_id, group in by_doc.items():
        # Highest revision wins; ties go to the earliest received payload.
        by_rev = {}
        for rev in group:
            by_rev.setdefault(rev["revision_number"], []).append(rev)
        highest = max(by_rev)
        candidates = sorted(by_rev[highest],
                            key=lambda r: (r["received_at"], r["payload_sha256"]))
        effective = candidates[0]
        if len(candidates) > 1:
            pending.append({
                "kind": "revision_conflict",
                "document": effective["document_number"],
                "revision": highest,
                "payloads": [{"revision_id": c["id"], "sha256": c["payload_sha256"],
                              "receipt_id": c["first_receipt_id"]} for c in candidates],
                "detail": "multiple payloads claim this document revision; "
                          "the earliest received is authoritative",
            })
        interp = conn.execute(
            "SELECT * FROM interpretations WHERE revision_id=? AND is_current=1 "
            "ORDER BY created_at DESC LIMIT 1", (effective["id"],)).fetchone()
        if not interp:
            pending.append({"kind": "missing_interpretation",
                            "document": effective["document_number"],
                            "revision": effective["revision_number"]})
            continue
        parsed = json.loads(interp["canonical_json"])
        for bad_line in parsed.get("lines", []):
            if bad_line.get("status") == "invalid":
                pending.append({
                    "kind": "invalid_line",
                    "document": effective["document_number"],
                    "ref": bad_line.get("supplier_line_ref")
                    or bad_line.get("line_number"),
                    "detail": "; ".join(bad_line.get("issues") or []) or
                              "line could not be interpreted",
                })
        for unparsed in parsed.get("unparsed_lines", []):
            pending.append({
                "kind": "unparsed_source_line",
                "document": effective["document_number"],
                "ref": ",".join(str(n) for n in unparsed.get("source_lines", [])),
                "detail": unparsed.get("reason"),
            })
        receipt = conn.execute("SELECT * FROM receipts WHERE id=?",
                               (effective["first_receipt_id"],)).fetchone()
        roundtrip_ok = bool(receipt["source_roundtrip_ok"]) if receipt else False
        if not roundtrip_ok:
            pending.append({"kind": "source_roundtrip_failed",
                            "document": effective["document_number"],
                            "receipt_id": receipt["id"] if receipt else None})
        docs.append({
            "document_id": doc_id,
            "document_number": effective["document_number"],
            "kind": effective["kind"],
            "corrects": effective["corrects_document_number"],
            "revision_id": effective["id"],
            "revision_number": effective["revision_number"],
            "payload_sha256": effective["payload_sha256"],
            "interpretation_id": interp["id"],
            "parse_run_id": interp["parse_run_id"],
            "mapping_version_id": interp["mapping_version_id"],
            "view_hash": interp["view_hash"],
            "parsed": parsed,
            "received_at": effective["received_at"],
            "receipt_id": effective["first_receipt_id"],
        })

    ordered, order_issues = _topological_documents(docs)
    issues.extend(order_issues)

    # Every correction chain resolves to one root namespace: the original
    # document.  A correction to a correction therefore still edits the same
    # lines, and a rebuild after a late original puts them there consistently.
    by_number = {d["document_number"]: d for d in docs}
    roots = {}
    for doc in docs:
        roots[doc["document_id"]] = _root_document_id(doc, by_number, roots)

    # Namespaces: root_doc_id -> {ref: line}
    namespaces = {doc["document_id"]: {} for doc in docs}
    missing_targets = set()
    for doc in ordered:
        parsed = doc["parsed"]
        if doc["corrects"] and doc["corrects"] not in by_number:
            missing_targets.add(doc["corrects"])
        dest = namespaces[roots[doc["document_id"]]]
        for line in parsed["lines"]:
            _apply_line(doc, line, dest, pending, issues)

    # Missing originals are reported once each.
    for number in sorted(missing_targets):
        pending.append({"kind": "missing_original", "document": number,
                        "detail": "correction filed before its original"})
    for row in conn.execute(
            """SELECT * FROM unmatched_revisions
               WHERE order_id=? AND status='pending'""", (order_id,)).fetchall():
        if not any(p.get("document") == row["corrects_document"] for p in pending):
            pending.append({"kind": "unmatched_correction",
                            "document": row["document_number"],
                            "target": row["corrects_document"],
                            "revision_id": row["revision_id"],
                            "detail": row["reason"]})

    lines = []
    for doc in ordered:
        for ref, entry in namespaces[doc["document_id"]].items():
            lines.append(entry)
    lines.sort(key=lambda e: (e["_doc_order"], _ref_sort_key(e["ref"])))
    for entry in lines:
        entry.pop("_doc_order", None)

    totals = _totals(lines)
    awaiting = bool(missing_targets or any(
        p["kind"] in ("missing_original", "unmatched_correction") for p in pending))

    pending.sort(key=lambda p: (p.get("kind", ""), str(p.get("document") or ""),
                                str(p.get("ref") or "")))
    issues.sort(key=lambda p: (p.get("kind", ""), str(p.get("document") or ""),
                               str(p.get("ref") or "")))
    state = {
        "order_reference": order_ref,
        "supplier": supplier,
        "documents": [{
            "document_number": d["document_number"],
            "kind": d["kind"],
            "corrects": d["corrects"],
            "revision": d["revision_number"],
            "revision_id": d["revision_id"],
            "payload_sha256": d["payload_sha256"],
            "effective": True,
        } for d in ordered],
        "lines": lines,
        "totals": totals,
        "pending": pending,
        "issues": issues,
        "awaiting_original": awaiting,
    }
    return state, docs


def _root_document_id(doc, by_number, memo):
    """Follow ``corrects`` links to the document that owns the line namespace."""
    path = []
    current = doc
    seen = set()
    while current.get("corrects"):
        if current["document_id"] in seen:
            break
        seen.add(current["document_id"])
        target = by_number.get(current["corrects"])
        if not target:
            break
        current = target
        path.append(current)
    return current["document_id"]


def _topological_documents(docs):
    by_number = {d["document_number"]: d for d in docs}
    issues = []
    ordered = []
    visiting = set()
    done = set()

    def visit(doc, stack):
        key = doc["document_number"]
        if key in done:
            return
        if key in visiting:
            issues.append({"kind": "correction_cycle",
                           "documents": stack + [key],
                           "detail": "correction chain loops; cycle ignored"})
            return
        visiting.add(key)
        target = doc.get("corrects")
        if target and target in by_number:
            visit(by_number[target], stack + [key])
        visiting.discard(key)
        done.add(key)
        ordered.append(doc)

    for doc in sorted(docs, key=lambda d: (1 if d["corrects"] else 0,
                                           d["document_number"])):
        visit(doc, [])
    # Assign a stable document order for deterministic output.
    for idx, doc in enumerate(ordered):
        doc["_order"] = idx
    return ordered, issues


def _norm_ref(ref):
    if ref is None:
        return None
    text = str(ref).strip()
    if not text:
        return None
    parts = []
    for piece in text.split("/"):
        piece = piece.strip()
        if piece.isdigit():
            parts.append(str(int(piece)))
        else:
            parts.append(piece.lower())
    return "/".join(parts)


def _ref_sort_key(ref):
    text = _norm_ref(ref) or ""
    return tuple(int(p) if p.isdigit() else 0 for p in text.split("/"))


def _empty_line(doc, line, ref):
    conversion = line.get("conversion") or {}
    return {
        "key": f"{doc['document_number']}:{ref}",
        "document": doc["document_number"],
        "ref": ref,
        "sku": line.get("sku"),
        "description": line.get("description"),
        "quantity": line.get("quantity"),
        "unit": line.get("unit"),
        "base_quantity": line.get("base_quantity"),
        "base_unit": line.get("base_unit"),
        "unit_price": line.get("unit_price"),
        "amount": line.get("amount"),
        "status": "active",
        "provenance": [],
        "conversion": conversion or None,
        "_doc_order": doc.get("_order", 0),
    }


def _apply_line(doc, line, dest, pending, issues):
    action = line.get("action") or "add"
    ref = _norm_ref(line.get("supplier_line_ref") or line.get("line_number"))
    target_ref = _norm_ref(line.get("target_line_ref") or line.get("target_line"))
    if not ref:
        ref = f"line{len(own) + 1}"
    prov = {
        "document": doc["document_number"],
        "revision_id": doc["revision_id"],
        "interpretation_id": doc["interpretation_id"],
        "parse_run_id": doc["parse_run_id"],
        "source_line": line.get("source_line"),
        "source_lines": line.get("source_lines"),
        "action": action,
        "target": target_ref,
        "receipt_id": doc.get("receipt_id"),
        "supplier_line_ref": line.get("supplier_line_ref") or line.get("line_number"),
        "raw": line.get("raw"),
    }
    if action == "add":
        if ref in dest:
            original_ref = ref
            ref = f"{doc['document_number']}:{ref}"
            pending.append({
                "kind": "duplicate_line_reference",
                "document": doc["document_number"],
                "ref": original_ref,
                "detail": "an added line reuses a reference already present; "
                          "both are retained under qualified references",
            })
            issues.append({"kind": "duplicate_line_reference",
                           "document": doc["document_number"],
                           "ref": original_ref})
        entry = _empty_line(doc, line, ref)
        entry["provenance"].append(prov)
        dest[ref] = entry
        return
    if action in ("change", "delete"):
        target_ref = target_ref or ref
        target_entry = dest.get(target_ref)
        if target_entry is None:
            pending.append({
                "kind": "unmatched_line_reference",
                "document": doc["document_number"],
                "ref": target_ref,
                "detail": f"{action} refers to a line that does not exist",
            })
            issues.append({"kind": "unmatched_line_reference",
                           "document": doc["document_number"], "ref": target_ref,
                           "action": action})
            return
        if action == "delete":
            target_entry["status"] = "deleted"
            target_entry["provenance"].append(prov)
            return
        target_entry.update({
            "sku": line.get("sku") or target_entry.get("sku"),
            "description": line.get("description") or target_entry.get("description"),
            "quantity": line.get("quantity"),
            "unit": line.get("unit"),
            "base_quantity": line.get("base_quantity"),
            "base_unit": line.get("base_unit"),
            "unit_price": line.get("unit_price"),
            "amount": line.get("amount"),
            "status": "active",
        })
        target_entry["provenance"].append(prov)
        return
    issues.append({"kind": "unsupported_action",
                   "document": doc["document_number"], "action": action})


_NUMERIC_LINE_FIELDS = ("quantity", "base_quantity", "unit_price", "amount")


def _numeric_key(value):
    """Canonical numeric text for hashing: 2500.000 and 2500.000000000 match."""
    if value in (None, ""):
        return value
    try:
        d = q(value)
    except (ValueError, ArithmeticError):
        return value
    if d is None:
        return value
    text = format(d.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def business_state(state):
    """The projection whose hash protects business meaning.

    Provenance (receipts, parse runs, interpretations) is retained in the full
    stored state for audit, but it is deliberately excluded here.  Replaying an
    old receipt under a newer mapping therefore produces the same business
    hash and cannot fabricate a change.
    """
    return {
        "order_reference": state.get("order_reference"),
        "supplier": state.get("supplier"),
        "documents": [
            {k: d.get(k) for k in ("document_number", "kind", "corrects",
                                   "revision", "payload_sha256")}
            for d in state.get("documents", [])
        ],
        "lines": [
            {**{k: v for k, v in line.items()
                if k not in ("provenance", "conversion")},
             **{f: _numeric_key(line.get(f)) for f in _NUMERIC_LINE_FIELDS}}
            for line in state.get("lines", [])
        ],
        "totals": [
            {**t, "quantity": _numeric_key(t.get("quantity"))}
            for t in state.get("totals", [])
        ],
        "pending": state.get("pending", []),
        "issues": state.get("issues", []),
        "awaiting_original": bool(state.get("awaiting_original")),
    }


def _state_hash(state):
    return sha256_text(j(business_state(state)))


def _totals(lines):
    groups = {}
    for entry in lines:
        if entry.get("status") != "active":
            continue
        if entry.get("quantity") is None or not entry.get("unit"):
            continue
        unit = entry.get("base_unit")
        if not unit:
            continue
        amount = q(entry.get("base_quantity")) if entry.get("base_quantity") is not None else None
        if amount is None:
            continue
        bucket = groups.setdefault(unit, {"unit": unit, "quantity": Decimal(0),
                                          "lines": 0})
        bucket["quantity"] += amount
        bucket["lines"] += 1
    out = []
    for unit in sorted(groups):
        bucket = groups[unit]
        out.append({"unit": unit, "quantity": qs(bucket["quantity"]),
                    "lines": bucket["lines"],
                    "method": "sum of per-line exact base conversions; "
                              "each conversion rounded at its supplier mapping scale"})
    return out


# --------------------------------------------------------------------------- #
# Apply / compare-and-set
# --------------------------------------------------------------------------- #

def _rebuild_and_store(conn, order_id, actor, *, cause, revision_id=None,
                       parse_run_id=None, note=None, force_reopen=False):
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        return {"order_id": order_id, "result": "missing"}
    state, docs = rebuild_order_state(conn, order_id)
    state["awaiting_original"] = bool(state["awaiting_original"])
    state_hash = _state_hash(state)

    if order["status"] == "confirmed" and not force_reopen:
        confirmed = conn.execute(
            "SELECT * FROM confirmations WHERE order_id=? ORDER BY confirmation_seq DESC "
            "LIMIT 1", (order_id,)).fetchone()
        confirmed_hash = confirmed["state_hash"] if confirmed else order["state_hash"]
        if state_hash != confirmed_hash:
            _link_revision(conn, order_id, revision_id, parse_run_id, cause,
                           "blocked", "order is confirmed; replay or new revision "
                           "cannot alter it without an explicit reopen")
            event(conn, "apply_blocked",
                  "order is confirmed; incoming revision was retained but not applied",
                  severity="warning", order_id=order_id, revision_id=revision_id,
                  parse_run_id=parse_run_id,
                  detail={"candidate_hash": state_hash,
                          "confirmed_hash": confirmed_hash})
            return {"order_id": order_id, "result": "blocked_confirmed",
                    "reason": "confirmed order is immutable",
                    "candidate_hash": state_hash}
        conn.execute("UPDATE orders SET awaiting_original=? WHERE id=?",
                     (1 if state["awaiting_original"] else 0, order_id))
        _link_revision(conn, order_id, revision_id, parse_run_id, cause,
                       "noop", "candidate state equals confirmed state")
        return {"order_id": order_id, "result": "noop_confirmed",
                "state_hash": state_hash}

    baseline_hash = order["state_hash"]
    baseline_seq = order["confirmation_seq"]
    if state_hash == baseline_hash:
        conn.execute("UPDATE orders SET awaiting_original=?, updated_at=? WHERE id=?",
                     (1 if state["awaiting_original"] else 0, now(), order_id))
        _link_revision(conn, order_id, revision_id, parse_run_id, cause,
                       "noop", "rebuilt state is unchanged")
        event(conn, "replay_noop",
              "rebuild produced identical business state; no new order version",
              order_id=order_id, revision_id=revision_id, parse_run_id=parse_run_id)
        return {"order_id": order_id, "result": "noop", "state_hash": state_hash}

    next_seq = (conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 AS n FROM order_versions WHERE order_id=?",
        (order_id,)).fetchone()["n"])
    version_id = new_id("ov_")
    conn.execute(
        """INSERT INTO order_versions
           (id, order_id, seq, cause, source_revision_id, parse_run_id, state_json,
            state_hash, created_at, actor, note, is_confirmed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
        (version_id, order_id, next_seq, cause,
         revision_id or order["head_revision_id"], parse_run_id,
         j(state), state_hash, now(), actor, note))
    cur = conn.execute(
        """UPDATE orders SET state_json=?, state_hash=?, head_revision_id=?,
             status='draft', awaiting_original=?, updated_at=?
           WHERE id=? AND confirmation_seq=? AND state_hash=?
             AND status<>'confirmed'""",
        (j(state), state_hash, revision_id or order["head_revision_id"],
         1 if state["awaiting_original"] else 0, now(), order_id,
         baseline_seq, baseline_hash))
    if cur.rowcount != 1:
        # Another writer changed the order between rebuild and CAS.  Abort this
        # attempt; the caller can retry against the new head.
        raise RuntimeError("concurrent modification detected; retry")
    _link_revision(conn, order_id, revision_id, parse_run_id, cause, "applied", None)
    event(conn, "order_version",
          f"order {order['order_reference']} rebuilt from {len(state['documents'])} "
          f"document revision(s)", order_id=order_id, revision_id=revision_id,
          parse_run_id=parse_run_id,
          detail={"seq": next_seq, "state_hash": state_hash,
                  "pending": len(state["pending"]), "lines": len(state["lines"])})
    return {"order_id": order_id, "result": "applied", "version_id": version_id,
            "seq": next_seq, "state_hash": state_hash}


def _link_revision(conn, order_id, revision_id, parse_run_id, cause, status, reason):
    """Record the latest apply outcome on the revision's membership row."""
    if not revision_id:
        return
    cur = conn.execute(
        """UPDATE order_revision_links SET status=?, reason=?
           WHERE order_id=? AND revision_id=?""",
        (status, reason, order_id, revision_id))
    if cur.rowcount == 0:
        conn.execute(
            """INSERT INTO order_revision_links
               (order_id, revision_id, interpretation_id, role, status, reason,
                created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (order_id, revision_id, None, cause, status, reason, now()))


# --------------------------------------------------------------------------- #
# Reprocessing and mapping upgrades
# --------------------------------------------------------------------------- #

def reprocess_receipt(conn, receipt_id, mapping_version_id=None, actor="system",
                      auto_apply=True):
    """Re-interpret a stored receipt.  Safe to run after confirmation."""
    with transaction(conn):
        receipt = conn.execute("SELECT * FROM receipts WHERE id=?",
                               (receipt_id,)).fetchone()
        if not receipt:
            raise KeyError("receipt not found")
        if mapping_version_id:
            mapping_row = dict(conn.execute(
                "SELECT * FROM mapping_versions WHERE id=?",
                (mapping_version_id,)).fetchone() or {})
            if not mapping_row:
                raise ValueError("mapping version not found")
        else:
            mapping_row = active_mapping(conn, receipt["supplier"])
        if not mapping_row:
            raise ValueError("no mapping available")

        if receipt["source_roundtrip_ok"] != 1:
            return {"ok": False, "receipt_id": receipt_id,
                    "result": "source_unavailable"}

        # Rebuild the source view from storage, not from the original object,
        # and prove it still reproduces the delivered bytes before parsing.
        integrity = receipt_integrity(receipt)
        if not integrity["ok"]:
            conn.execute("UPDATE receipts SET status='source_roundtrip_failed', "
                         "source_roundtrip_ok=0 WHERE id=?", (receipt_id,))
            event(conn, "source_roundtrip_failed",
                  "stored source view no longer reproduces the delivered bytes",
                  severity="error", receipt_id=receipt_id,
                  detail={"stored": receipt["sha256"],
                          "rebuilt": integrity.get("rebuilt_sha256")})
            return {"ok": False, "receipt_id": receipt_id,
                    "result": "source_rebuild_mismatch", "detail": integrity}
        doc = _doc_from_receipt(receipt)
        prior_revisions = conn.execute(
            """SELECT DISTINCT r.id FROM document_revisions r
               WHERE r.first_receipt_id=?""", (receipt_id,)).fetchall()

        outcome = _interpret_and_apply(conn, receipt_id, doc, mapping_row, actor,
                                       auto_apply=auto_apply)
        outcome["receipt_id"] = receipt_id
        outcome["prior_revision_ids"] = [r["id"] for r in prior_revisions]
        outcome["result"] = ("reprocessed_" + outcome.get("result", "unknown"))
        outcome["source_integrity"] = integrity
        return outcome


def receipt_integrity(receipt):
    """Rebuild bytes from the stored source view and compare to the payload."""
    view = json.loads(receipt["source_view"])
    try:
        rebuilt = rebuild_bytes(view)
    except (KeyError, LookupError, UnicodeEncodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    rebuilt_sha = sha256_text(rebuilt)
    return {
        "ok": rebuilt == receipt["payload"] and rebuilt_sha == receipt["sha256"],
        "stored_sha256": receipt["sha256"],
        "rebuilt_sha256": rebuilt_sha,
        "stored_bytes": len(receipt["payload"]),
        "rebuilt_bytes": len(rebuilt),
    }


def _doc_from_receipt(receipt):
    from .source import SourceDocument, SourceLine
    view = json.loads(receipt["source_view"])
    doc = SourceDocument(
        supplier=view["supplier"], filename=view["filename"],
        content_type=view["content_type"], data=receipt["payload"],
        encoding=view["encoding"], eol=view["eol"],
        declared_filename=view.get("declared_filename"),
        extra=view.get("extra") or {})
    doc.lines = [SourceLine(**ln) for ln in view["lines"]]
    return doc


def upgrades_available(conn, supplier=None):
    rows = conn.execute(
        "SELECT * FROM mapping_versions WHERE active=0 ORDER BY supplier, version")
    out = []
    for row in rows:
        if supplier and row["supplier"] != supplier:
            continue
        out.append(dict(row))
    return out


def register_mapping(conn, mapping, *, activate=False, reprocess=False, actor="system"):
    with transaction(conn):
        row = ensure_mapping(conn, mapping)
        already_active = bool(row.get("active"))
        if activate:
            activate_mapping(conn, row)
            event(conn, "mapping_activated",
                  f"{row['supplier']} mapping v{row['version']} is now active",
                  detail={"digest": row["digest"]})
        if reprocess:
            suppliers = [row["supplier"]]
            receipts = conn.execute(
                "SELECT id FROM receipts WHERE supplier=? AND status<>'received' "
                "ORDER BY received_at", (suppliers[0],)).fetchall()
            outcomes = []
            for r in receipts:
                outcomes.append(reprocess_receipt(conn, r["id"], row["id"], actor))
            return {"mapping": dict(row), "outcomes": outcomes,
                    "already_active": already_active}
        return {"mapping": dict(row), "already_active": already_active}


# --------------------------------------------------------------------------- #
# Confirmation and reopen
# --------------------------------------------------------------------------- #

def eligibility(conn, order_id):
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        return {"eligible": False, "reasons": ["order not found"]}
    state = json.loads(order["state_json"])
    reasons = []
    if order["status"] == "confirmed":
        reasons.append("already confirmed")
    if state.get("awaiting_original"):
        reasons.append("waiting for an original document referenced by a correction")
    if not state.get("documents"):
        reasons.append("no documents")
    for p in state.get("pending", []):
        reasons.append(f"{p['kind']}: {p.get('document') or p.get('detail') or ''}")
    for issue in state.get("issues", []):
        reasons.append(f"issue {issue['kind']}: {issue.get('document') or ''}")
    for line in state.get("lines", []):
        if line.get("status") == "active" and (
                line.get("quantity") in (None, "") or not line.get("unit")):
            reasons.append(f"incomplete line {line['key']}")
    docs = conn.execute(
        """SELECT DISTINCT r.id FROM document_revisions r
           JOIN order_revision_links l ON l.revision_id=r.id
           WHERE l.order_id=? AND l.status NOT IN ('noop','blocked')""",
        (order_id,)).fetchall()
    evidence = []
    for d in docs:
        revision = conn.execute("SELECT * FROM document_revisions WHERE id=?",
                                (d["id"],)).fetchone()
        receipt = conn.execute("SELECT * FROM receipts WHERE id=?",
                               (revision["first_receipt_id"],)).fetchone()
        if not receipt["source_roundtrip_ok"]:
            reasons.append(f"source round-trip failed for receipt {receipt['id']}")
        if receipt["status"] not in ("interpreted", "parsed"):
            reasons.append(f"receipt {receipt['id']} is {receipt['status']}")
    return {"eligible": not reasons, "reasons": reasons}


def confirm(conn, order_id, actor, note=None):
    with transaction(conn):
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise KeyError("order not found")
        check = eligibility(conn, order_id)
        if not check["eligible"]:
            event(conn, "confirmation_refused",
                  "confirmation blocked by unresolved items", severity="warning",
                  order_id=order_id, detail={"reasons": check["reasons"]})
            return {"ok": False, "result": "ineligible", "reasons": check["reasons"]}

        state, docs = rebuild_order_state(conn, order_id)
        state_hash = _state_hash(state)
        baseline_hash = order["state_hash"]
        baseline_seq = order["confirmation_seq"]

        version_id = new_id("ov_")
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM order_versions WHERE order_id=?",
            (order_id,)).fetchone()["n"]
        conn.execute(
            """INSERT INTO order_versions
               (id, order_id, seq, cause, source_revision_id, parse_run_id,
                state_json, state_hash, created_at, actor, note, is_confirmed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
            (version_id, order_id, next_seq, "confirmation",
             order["head_revision_id"], None, j(state), state_hash, now(),
             actor, note))
        evidence = _confirmation_evidence(conn, order_id, docs, state_hash)
        next_confirmation = baseline_seq + 1
        confirmation_id = new_id("conf_")
        cur = conn.execute(
            """UPDATE orders SET status='confirmed', state_json=?, state_hash=?,
                 confirmation_seq=?, confirmed_at=?, confirmed_by=?,
                 confirmed_order_version_id=?, updated_at=?
               WHERE id=? AND confirmation_seq=? AND state_hash=?
                 AND status<>'confirmed'""",
            (j(state), state_hash, next_confirmation, now(), actor, version_id,
             now(), order_id, baseline_seq, baseline_hash))
        if cur.rowcount != 1:
            raise RuntimeError("order changed while confirming; retry")
        conn.execute(
            """INSERT INTO confirmations
               (id, order_id, order_version_id, confirmation_seq, state_hash,
                actor, note, evidence_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (confirmation_id, order_id, version_id, next_confirmation, state_hash,
             actor, note, j(evidence), now()))
        conn.execute(
            """UPDATE confirmations SET evidence_json=? WHERE id=?""",
            (j({**evidence, "confirmation_id": confirmation_id}), confirmation_id))
        event(conn, "order_confirmed",
              f"order {order['order_reference']} confirmed by {actor}",
              order_id=order_id, detail={"state_hash": state_hash,
                                         "confirmation_seq": next_confirmation})
        return {"ok": True, "result": "confirmed", "confirmation_id": confirmation_id,
                "state_hash": state_hash, "confirmation_seq": next_confirmation,
                "order_version_id": version_id, "evidence": evidence}


def _confirmation_evidence(conn, order_id, docs, state_hash):
    pins = []
    for doc in docs:
        revision = conn.execute(
            """SELECT r.*, d.document_number FROM document_revisions r
               JOIN documents d ON d.id=r.document_id WHERE r.id=?""",
            (doc["revision_id"],)).fetchone()
        interp = conn.execute("SELECT * FROM interpretations WHERE id=?",
                              (doc["interpretation_id"],)).fetchone()
        mapping = conn.execute("SELECT * FROM mapping_versions WHERE id=?",
                               (interp["mapping_version_id"],)).fetchone()
        receipt = conn.execute("SELECT * FROM receipts WHERE id=?",
                               (revision["first_receipt_id"],)).fetchone()
        pins.append({
            "document_number": revision["document_number"],
            "revision": revision["revision_number"],
            "revision_id": revision["id"],
            "payload_sha256": revision["payload_sha256"],
            "interpretation_id": interp["id"],
            "view_hash": interp["view_hash"],
            "parse_run_id": interp["parse_run_id"],
            "mapping_version": mapping["version"],
            "mapping_digest": mapping["digest"],
            "receipt_id": receipt["id"],
            "receipt_sha256": receipt["sha256"],
            "source_roundtrip_ok": bool(receipt["source_roundtrip_ok"]),
        })
    return {"state_hash": state_hash, "pins": pins, "confirmed_at": now()}


def reopen(conn, order_id, actor, note=None):
    with transaction(conn):
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise KeyError("order not found")
        if order["status"] != "confirmed":
            return {"ok": False, "result": "not_confirmed"}
        conn.execute(
            "UPDATE orders SET status='reopened', updated_at=? WHERE id=? AND status='confirmed'",
            (now(), order_id))
        conn.execute("UPDATE order_versions SET is_confirmed=0 WHERE order_id=?",
                     (order_id,))
        event(conn, "order_reopened",
              f"order {order['order_reference']} reopened by {actor}",
              order_id=order_id,
              detail={"note": note, "confirmation_seq": order["confirmation_seq"]})
        return {"ok": True, "result": "reopened", "order_id": order_id}


def apply_revision_to_order(conn, order_id, actor, revision_id=None, parse_run_id=None,
                            note=None):
    """Explicit re-apply for the UI's 'recompute' action."""
    with transaction(conn):
        return _rebuild_and_store(conn, order_id, actor, cause="manual_recompute",
                                  revision_id=revision_id, parse_run_id=parse_run_id,
                                  note=note)


def attach_unmatched(conn, revision_id, order_reference, actor, note=None):
    with transaction(conn):
        row = conn.execute("SELECT * FROM unmatched_revisions WHERE revision_id=?",
                           (revision_id,)).fetchone()
        if not row:
            raise KeyError("unmatched revision not found")
        order = conn.execute(
            "SELECT * FROM orders WHERE supplier=? AND order_reference=?",
            (row["supplier"], order_reference)).fetchone()
        order_id = order["id"] if order else _ensure_order(
            conn, row["supplier"], order_reference)
        _attach_revision(conn, order_id, revision_id, row["interpretation_id"],
                         "correction", "linked", "attached manually")
        conn.execute(
            """UPDATE unmatched_revisions SET status='attached', order_id=?,
               resolved_at=?, reason=? WHERE revision_id=?""",
            (order_id, now(), (row["reason"] or "") + " | attached manually by " + actor,
             revision_id))
        _link_revision(conn, order_id, revision_id, None, "manual_attach", "applied", None)
        event(conn, "correction_attached",
              f"correction {row['document_number']} attached to {order_reference}",
              order_id=order_id, revision_id=revision_id,
              detail={"actor": actor, "note": note})
        return _rebuild_and_store(conn, order_id, actor, cause="correction_attached",
                                  revision_id=revision_id, note=note)

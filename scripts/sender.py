#!/usr/bin/env python3
"""Local sender: deliver order files to a running intake server.

    python3 scripts/sender.py fixtures/northwind/NW-1001.csv
    python3 scripts/sender.py --manifest scripts/demo_manifest.json
    python3 scripts/sender.py --supplier orion fixtures/orion/OR-900.ebc

The sender is deliberately dumb about content: it uploads the exact bytes,
never rewrites or re-encodes them, and lets the server decide what they mean.
What it *does* own is delivery semantics:

* An idempotency key is derived from the payload digest plus the intended
  file name, so a retry after a timeout cannot create a second revision, while
  deliberately sending the same bytes under another name remains a new
  delivery (the server still recognises the duplicate content).
* Transport errors and 5xx responses are retried with backoff.  4xx responses
  are not: they describe the payload, and resending cannot fix them.
* Every attempt is reported, so a failed delivery is visible rather than
  silently dropped.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUPPLIERS = ("northwind", "helios", "orion")


def guess_supplier(path):
    norm = path.replace("\\", "/").lower()
    for supplier in SUPPLIERS:
        if f"/{supplier}/" in f"/{norm}":
            return supplier
    for supplier, suffix in (("northwind", ".csv"), ("helios", ".json"),
                             ("orion", ".ebc")):
        if norm.endswith(suffix):
            return supplier
    raise ValueError(f"cannot infer supplier for {path}; pass --supplier")


def default_idempotency_key(supplier, filename, data):
    digest = hashlib.sha256(data).hexdigest()
    return f"{supplier}:{filename}:{digest[:32]}"


def deliver(url, supplier, filename, data, idempotency_key=None, actor="sender",
            content_type="application/octet-stream", retries=3, timeout=20,
            backoff=0.5, verbose=True):
    """POST one payload.  Returns the server's JSON response.

    ``attempts`` is attached to the returned dict so callers can see retries.
    """
    endpoint = url.rstrip("/") + "/api/receipts"
    key = idempotency_key or default_idempotency_key(supplier, filename, data)
    body = json.dumps({
        "supplier": supplier,
        "filename": filename,
        "content_type": content_type,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "idempotency_key": key,
        "actor": actor,
    }).encode("utf-8")

    attempts = 0
    last_error = None
    while attempts <= retries:
        attempts += 1
        request = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": key,
                     "User-Agent": "order-intake-sender/1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                payload["attempts"] = attempts
                payload["idempotency_key"] = key
                return payload
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = {"error": raw}
            # 4xx is a verdict on the payload; retrying cannot change it.
            if 400 <= exc.code < 500:
                detail.update({"ok": False, "http_status": exc.code,
                               "attempts": attempts, "idempotency_key": key})
                return detail
            last_error = f"HTTP {exc.code}: {detail.get('error', raw)}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempts <= retries:
            delay = backoff * (2 ** (attempts - 1))
            if verbose:
                print(f"  ! {last_error}; retrying in {delay:.2f}s "
                      f"({attempts}/{retries})", file=sys.stderr)
            time.sleep(delay)

    return {"ok": False, "error": last_error, "attempts": attempts,
            "idempotency_key": key, "delivery_failed": True}


def send_file(args, path, supplier=None, filename=None, idempotency_key=None):
    try:
        supplier = (supplier or args.supplier or guess_supplier(path)).lower()
    except ValueError as exc:
        return {"ok": False, "delivery_failed": True, "error": str(exc),
                "attempts": 0,
                "_sent": {"supplier": supplier or "?", "filename": filename or
                          os.path.basename(path), "bytes": 0, "sha256": None,
                          "path": path}}
    filename = filename or args.filename or os.path.basename(path)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        # A local problem, not a server verdict: report it like a delivery
        # failure so a batch run keeps going and returns non-zero.
        return {"ok": False, "delivery_failed": True, "error": str(exc),
                "attempts": 0,
                "_sent": {"supplier": supplier, "filename": filename,
                          "bytes": 0, "sha256": None, "path": path}}
    result = deliver(
        args.url, supplier, filename, data,
        idempotency_key=idempotency_key or args.idempotency_key,
        actor=args.actor, retries=args.retries, timeout=args.timeout,
        backoff=args.backoff)
    result["_sent"] = {"supplier": supplier, "filename": filename,
                       "bytes": len(data),
                       "sha256": hashlib.sha256(data).hexdigest()}
    return result


def describe(result):
    sent = result["_sent"]
    head = f"{sent['supplier']:10} {sent['filename']:24}"
    if result.get("delivery_failed"):
        return f"{head} FAILED  {result.get('error')}"
    status = result.get("result", "?")
    extra = ""
    if status == "parse_failed":
        extra = f"  line {result.get('error_line')}: {result.get('error') or result.get('failure_reason', '')}"
    elif status == "no_mapping":
        extra = "  no active mapping for this supplier"
    elif status == "duplicate_content":
        extra = "  duplicate content retained, no new revision"
    elif status == "idempotent_replay":
        extra = "  idempotency key already applied"
    return f"{head} {status}{extra}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to deliver")
    parser.add_argument("--url", default=os.environ.get(
        "INTAKE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--supplier", choices=SUPPLIERS,
                        help="override supplier detection")
    parser.add_argument("--filename", help="name recorded by the server")
    parser.add_argument("--idempotency-key",
                        help="explicit key; default is derived from bytes+name")
    parser.add_argument("--manifest",
                        help="JSON list of {path, supplier?, filename?, "
                             "idempotency_key?} deliveries, in order")
    parser.add_argument("--actor", default="sender")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--backoff", type=float, default=0.5)
    parser.add_argument("--json", action="store_true",
                        help="emit newline-delimited JSON results")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--health", action="store_true",
                        help="only check that the server answers")
    args = parser.parse_args(argv)

    if args.health:
        try:
            with urllib.request.urlopen(
                    args.url.rstrip("/") + "/healthz", timeout=args.timeout) as r:
                print(r.read().decode("utf-8"))
            return 0
        except Exception as exc:
            print(f"unhealthy: {exc}", file=sys.stderr)
            return 2

    deliveries = []
    if args.manifest:
        with open(args.manifest, "r", encoding="utf-8") as fh:
            entries = json.load(fh)
        for entry in entries:
            path = entry["path"]
            if not os.path.isabs(path):
                path = os.path.join(ROOT, path)
            deliveries.append({
                "path": path,
                "supplier": entry.get("supplier"),
                "filename": entry.get("filename"),
                "idempotency_key": entry.get("idempotency_key"),
            })
    # A single explicit key cannot describe several different payloads.
    explicit_key = args.idempotency_key if len(args.files) <= 1 else None
    if args.idempotency_key and explicit_key is None:
        parser.error("--idempotency-key is only valid with a single file")
    for path in args.files:
        deliveries.append({"path": path, "supplier": args.supplier,
                           "filename": args.filename,
                           "idempotency_key": explicit_key})
    if not deliveries:
        parser.error("no files or --manifest given")

    failures = 0
    for delivery in deliveries:
        result = send_file(args, delivery["path"],
                           supplier=delivery["supplier"],
                           filename=delivery["filename"],
                           idempotency_key=delivery["idempotency_key"])
        if args.json:
            print(json.dumps(result, ensure_ascii=False, default=str))
        else:
            print(describe(result))
        if result.get("delivery_failed"):
            failures += 1
            if args.fail_fast:
                break
        elif result.get("_sent") and not result.get("ok") and \
                result.get("result") not in ("parse_failed", "no_mapping",
                                             "source_roundtrip_failed"):
            # A rejected payload is a caller bug, not a transport problem.
            failures += 1
            if args.fail_fast:
                break
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

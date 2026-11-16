"""Server-rendered pages.

Plain HTML with one small stylesheet and a little JavaScript for actions.  The
pages are intentionally dependency-free so the whole application runs from a
clean Python install.
"""

import html
import json

from .canon import qs


def esc(value):
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def badge(text, kind="neutral"):
    return f'<span class="badge badge-{esc(kind)}">{esc(text)}</span>'


def status_badge(status):
    mapping = {
        "confirmed": "good", "draft": "info", "reopened": "warn",
        "parsed": "good", "interpreted": "good", "received": "info",
        "parse_failed": "bad", "source_roundtrip_failed": "bad",
        "no_mapping": "bad", "pending": "warn", "attached": "good",
        "linked": "info", "applied": "good", "noop": "neutral",
        "noop_confirmed": "good", "blocked": "bad", "invalid": "bad",
        "ok": "good", "deleted": "neutral", "active": "good",
    }
    return badge(status, mapping.get(status, "neutral"))


def nav(active=""):
    items = [("/", "Overview"), ("/receive", "Receive"),
             ("/failures", "Exceptions"), ("/mappings", "Mappings")]
    links = "".join(
        f'<a class="nav-link{" active" if path == active else ""}" href="{path}">'
        f"{esc(label)}</a>" for path, label in items)
    return f"""<header class="topbar">
  <a class="brand" href="/"><span class="brand-mark">◫</span> Order Intake</a>
  <nav>{links}</nav>
  <div class="topbar-note">source → record → confirmation</div>
</header>"""


def layout(title, body, active="", extra_head="", extra_scripts=""):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Order Intake</title>
<link rel="stylesheet" href="/static/app.css">
{extra_head}
</head>
<body>
{nav(active)}
<main class="page">
{body}
</main>
<footer class="foot">Local demonstration · byte-exact source retention · every mutation audited</footer>
<script src="/static/app.js"></script>
{extra_scripts}
</body>
</html>"""


def json_block(data, max_height="22rem"):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return (f'<pre class="code" style="max-height:{esc(max_height)}">'
            f"{esc(text)}</pre>")


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

def render_index(ctx):
    counts = ctx["counts"]
    cards = "".join(
        f'<div class="stat"><div class="stat-value">{esc(value)}</div>'
        f'<div class="stat-label">{esc(label)}</div></div>'
        for value, label in [
            (counts["orders"], "orders"), (counts["confirmed"], "confirmed"),
            (counts["receipts"], "receipts"), (counts["parse_failed"], "failed parses"),
            (counts["unmatched"], "unmatched corrections"),
        ])
    supplier_rows = "".join(
        f"<tr><td>{esc(s['supplier'])}</td><td>{esc(s['active_version'])}</td>"
        f"<td>{esc(s['receipts'])}</td></tr>" for s in ctx["suppliers"])
    order_rows = "".join(
        f"""<tr>
  <td><a href="/orders/{esc(o['id'])}"><code>{esc(o['order_reference'])}</code></a>
      <span class="muted">{esc(o['supplier'])}</span></td>
  <td>{status_badge(o['status'])}</td>
  <td>{esc(o['line_count'])}</td>
  <td>{esc(o['pending']) if o['pending'] else '—'}</td>
  <td>{_totals(o['totals'])}</td>
  <td class="muted">{esc(o['updated_at'][:19].replace('T', ' '))}</td>
</tr>""" for o in ctx["orders"])
    receipt_rows = "".join(
        f"""<tr>
  <td><a href="/receipts/{esc(r['id'])}">{esc(r['filename'])}</a>
      <div class="muted mono">{esc(r['id'][:14])}</div></td>
  <td>{esc(r['supplier'])}</td>
  <td>{status_badge(r['status'])}</td>
  <td>{'yes' if r['roundtrip_ok'] else '<span class="bad">no</span>'}</td>
  <td class="muted">{esc(r['received_at'][:19].replace('T', ' '))}</td>
</tr>""" for r in ctx["receipts"][:20])
    event_rows = "".join(
        f"""<li class="event event-{esc(e['severity'])}">
  <span class="event-kind">{esc(e['kind'])}</span>
  <span>{esc(e['message'])}</span>
  <span class="muted">{esc(e['at'][:19].replace('T', ' '))}</span>
</li>""" for e in ctx["events"])
    body = f"""
<section class="hero">
  <div>
    <h1>Three suppliers, one auditable order</h1>
    <p class="lede">CSV, nested JSON and EBCDIC fixed-width arrive with different
    encodings, decimal conventions and line semantics. Each receipt keeps its
    original bytes, its canonical record, and the human confirmation that
    follows — linked end to end.</p>
  </div>
  <div class="stats">{cards}</div>
</section>

<section class="panel">
  <div class="panel-head"><h2>Orders</h2>
    <a class="btn btn-quiet" href="/receive">Receive a file</a></div>
  <table class="table">
    <thead><tr><th>Order</th><th>Status</th><th>Lines</th><th>Pending</th>
      <th>Base totals</th><th>Updated</th></tr></thead>
    <tbody>{order_rows or '<tr><td colspan="6" class="muted">No orders yet. Receive a fixture to begin.</td></tr>'}</tbody>
  </table>
</section>

<div class="grid-2">
  <section class="panel">
    <div class="panel-head"><h2>Recent receipts</h2></div>
    <table class="table compact">
      <thead><tr><th>File</th><th>Supplier</th><th>Status</th><th>Round-trip</th>
        <th>Received</th></tr></thead>
      <tbody>{receipt_rows or '<tr><td colspan="5" class="muted">None.</td></tr>'}</tbody>
    </table>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>Active mappings</h2></div>
    <table class="table compact">
      <thead><tr><th>Supplier</th><th>Version</th><th>Receipts</th></tr></thead>
      <tbody>{supplier_rows}</tbody>
    </table>
    <div class="panel-head" style="margin-top:1.2rem"><h2>Latest events</h2></div>
    <ul class="events">{event_rows}</ul>
  </section>
</div>
"""
    return layout("Overview", body, active="/")


def _totals(totals):
    if not totals:
        return '<span class="muted">—</span>'
    return " · ".join(f"{esc(t['quantity'])} {esc(t['unit'])}" for t in totals)


# --------------------------------------------------------------------------- #
# Receive
# --------------------------------------------------------------------------- #

def render_receive(ctx):
    options = "".join(
        f'<option value="{esc(s["supplier"])}">{esc(s["supplier"])} '
        f'(mapping v{esc(s["active_version"])}, {esc(s["format"])})</option>'
        for s in ctx["suppliers"])
    recent = "".join(
        f"""<tr><td><a href="/receipts/{esc(r['id'])}">{esc(r['filename'])}</a></td>
        <td>{esc(r['supplier'])}</td><td>{status_badge(r['status'])}</td>
        <td class="muted">{esc(r['received_at'][:19].replace('T',' '))}</td></tr>"""
        for r in ctx["recent"])
    body = f"""
<h1>Receive</h1>
<p class="lede">Upload exactly the bytes the supplier delivered. The file name is
not used for identity; content hashes and the document number inside the payload are.</p>

<div class="grid-2">
  <section class="panel">
    <h2>Upload a file</h2>
    <form id="receive-form" class="stack">
      <label>Supplier
        <select name="supplier" required>{options}</select>
      </label>
      <label class="dropzone" id="dropzone">
        <input type="file" name="file" id="file-input" required>
        <span class="dropzone-text">Drop a file here or click to choose</span>
        <span class="dropzone-file muted" id="file-name"></span>
      </label>
      <label>Idempotency key (optional)
        <input type="text" name="idempotency_key"
               placeholder="a retry with the same key returns the original result">
      </label>
      <button class="btn" type="submit">Receive and interpret</button>
    </form>
    <div id="receive-result" class="result hidden"></div>
  </section>
  <section class="panel">
    <h2>Why the original matters</h2>
    <ul class="notes">
      <li><strong>Encoding is evidence.</strong> cp1252 fallbacks and EBCDIC
        records are decoded deterministically; the codec is stored with the receipt.</li>
      <li><strong>Line numbers anchor back.</strong> Every canonical line records
        the physical source line(s) it came from, including records split across
        lines and continuation records.</li>
      <li><strong>Names are not identity.</strong> A reused file name or a
        byte-identical resend creates a new receipt; identity comes from the
        document number and payload hash.</li>
    </ul>
    <h2 style="margin-top:1.2rem">Recent receipts</h2>
    <table class="table compact">
      <thead><tr><th>File</th><th>Supplier</th><th>Status</th><th>Received</th></tr></thead>
      <tbody>{recent or '<tr><td colspan="4" class="muted">None.</td></tr>'}</tbody>
    </table>
  </section>
</div>
"""
    return layout("Receive", body, active="/receive")


# --------------------------------------------------------------------------- #
# Receipt detail
# --------------------------------------------------------------------------- #

def render_receipt(ctx):
    receipt = ctx["receipt"]
    source = ctx["source"]
    integrity = ctx["integrity"]
    lines = source.get("lines", [])
    raw_rows = "".join(
        f'<tr><td class="lineno">{ln["number"]}</td>'
        f'<td class="role">{esc(ln["kind"])}</td>'
        f'<td class="rawcell">{esc(ln["raw"])}</td></tr>' for ln in lines)
    run_rows = "".join(
        f"""<tr><td><code>{esc(r['id'][:14])}</code></td>
        <td>{status_badge(r['status'])}</td>
        <td>{esc(r.get('mapping_version_id'))}</td>
        <td class="mono muted">{esc((r.get('view_hash') or '')[:16])}</td>
        <td class="muted">{esc((r.get('error') or '')[:140])}</td></tr>""" for r in ctx["runs"])

    revision_blocks = []
    for entry in ctx["revisions"]:
        revision = entry["revision"]
        interps = entry["interpretations"]
        current = next((i for i in interps if i["is_current"]), None)
        tabs = "".join(
            f'<button class="tab{" active" if i["is_current"] else ""}" '
            f'data-tab="interp-{esc(i["id"])}">v{esc(i["mapping_version"])}'
            f'{" · current" if i["is_current"] else ""}</button>' for i in interps)
        panels = "".join(_interpretation_panel(i) for i in interps)
        links = "".join(
            f'<a class="chip" href="/orders/{esc(l["order_id"])}">'
            f'{esc(l["order_reference"])} {status_badge(l["order_status"])}</a>'
            for l in entry["links"])
        revision_blocks.append(f"""
<div class="panel">
  <div class="panel-head">
    <h2>{esc(revision['document_number'])} · revision {esc(revision['revision_number'])}</h2>
    <div>{status_badge(revision['kind'])}{' ' + badge('corrects ' + revision['corrects_document_number'], 'warn') if revision.get('corrects_document_number') else ''}</div>
  </div>
  {links or '<p class="muted">Not linked to an order (unmatched correction or failed parse).</p>'}
  <div class="tabs">{tabs}</div>
  {panels}
</div>""".replace("__CURRENT__", esc(current["id"]) if current else ""))

    body = f"""
<p class="breadcrumb"><a href="/">Overview</a> / receipt</p>
<h1>{esc(receipt['filename'])}</h1>
<div class="meta-row">
  {status_badge(receipt['status'])}
  {badge(receipt['supplier'], 'info')}
  {badge('encoding ' + str(receipt['encoding']), 'neutral')}
  {badge('round-trip ' + ('ok' if receipt['roundtrip_ok'] else 'failed'), 'good' if receipt['roundtrip_ok'] else 'bad')}
  {badge(f"{receipt['size']} bytes", 'neutral')}
  {'<a class="chip" href="/receipts/' + esc(receipt['duplicate_of']) + '">content duplicate</a>' if receipt['duplicate_of'] else ''}
</div>
<p class="mono muted">sha256 {esc(receipt['sha256'])}</p>
{'<div class="alert alert-bad"><strong>Failure.</strong> ' + esc(receipt['failure_reason']) + '</div>' if receipt['failure_reason'] else ''}

<div class="grid-2 wide-left">
  <section class="panel">
    <div class="panel-head"><h2>Original source</h2>
      <div class="row">
        <a class="btn btn-quiet" href="/api/receipts/{esc(receipt['id'])}/payload">Download exact bytes</a>
        <button class="btn btn-quiet" data-copy="#source-text">Copy text</button>
      </div></div>
    <p class="muted">Physical lines as decoded. The byte-exact payload remains authoritative.</p>
    <div class="rawtable-wrap">
      <table class="rawtable">
        <thead><tr><th>#</th><th>role</th><th>line</th></tr></thead>
        <tbody>{raw_rows}</tbody>
      </table>
    </div>
    <div id="source-text" class="hidden">{esc(ctx['raw_text'])}</div>
  </section>

  <section class="panel">
    <h2>Source integrity</h2>
    <dl class="kv">
      <dt>Stored sha256</dt><dd class="mono">{esc(integrity.get('stored_sha256'))}</dd>
      <dt>Rebuilt sha256</dt><dd class="mono">{esc(integrity.get('rebuilt_sha256'))}</dd>
      <dt>Bytes</dt><dd>{esc(integrity.get('rebuilt_bytes'))} / {esc(integrity.get('stored_bytes'))}</dd>
      <dt>Result</dt><dd>{badge('byte-identical' if integrity.get('ok') else 'mismatch', 'good' if integrity.get('ok') else 'bad')}</dd>
      <dt>EOL observed</dt><dd>{esc(repr(source.get('eol')))}</dd>
    </dl>
    <p class="muted">Rebuilt from the stored line view, not from the original upload
    object. This is the round-trip proof that decoding lost nothing.</p>
  </section>
</div>

<section class="panel">
  <div class="panel-head"><h2>Interpretations</h2>
    <div class="row">
      <select id="reprocess-mapping">{"".join(f'<option value="{esc(i["id"])}">mapping v{esc(i["mapping_version"])} ({esc(i["mapping_name"])})</option>' for i in _mapping_options(ctx))}</select>
      <button class="btn btn-quiet" data-reprocess="{esc(receipt['id'])}">Re-interpret</button>
    </div>
  </div>
  <p class="muted">Every interpretation is kept. The current one is chosen by the
  newest <em>active</em> mapping version, and a confirmed order pins the
  interpretation it was confirmed against.</p>
  <table class="table compact">
    <thead><tr><th>Parse run</th><th>Status</th><th>Mapping</th><th>View hash</th><th>Error</th></tr></thead>
    <tbody>{run_rows}</tbody>
  </table>
</section>

{''.join(revision_blocks) or '<section class="panel"><p class="muted">No revision was created from this receipt.</p></section>'}

<section class="panel">
  <div class="panel-head"><h2>Audit trail</h2></div>
  <ul class="events">{''.join(_event_item(e) for e in ctx['events'])}</ul>
</section>
"""
    return layout(f"Receipt {receipt['filename']}", body, active="/")


def _mapping_options(ctx):
    seen = {}
    for entry in ctx["revisions"]:
        for interp in entry["interpretations"]:
            seen[interp["mapping_version_id"]] = interp
    return [{"id": k, "mapping_version": v["mapping_version"],
             "mapping_name": v["mapping_name"]} for k, v in sorted(seen.items())]


def _event_item(e):
    return (f'<li class="event event-{esc(e["severity"])}">'
            f'<span class="event-kind">{esc(e["kind"])}</span>'
            f'<span>{esc(e["message"])}</span>'
            f'<span class="muted">{esc(e["at"][:19].replace("T", " "))}</span></li>')


def _interpretation_panel(interp):
    canonical = interp.get("canonical") or {}
    doc = {k: canonical.get(k) for k in
           ("document_number", "revision", "kind", "order_reference", "currency",
            "issued_at", "sha256", "mapping_version", "encoding")}
    rows = []
    for line in canonical.get("lines", []):
        conv = line.get("conversion") or {}
        factor = conv.get("factor") or ""
        source_lines = line.get("source_lines") or [line.get("source_line")]
        rows.append(f"""<tr>
  <td class="lineno">{esc(",".join(str(n) for n in source_lines if n is not None))}</td>
  <td>{esc(line.get('supplier_line_ref') or line.get('line_number'))}</td>
  <td>{esc(line.get('action'))}</td>
  <td>{esc(line.get('sku'))}</td>
  <td class="num">{esc(line.get('quantity'))} <span class="muted">{esc(line.get('unit'))}</span></td>
  <td class="num">{esc(line.get('base_quantity'))} <span class="muted">{esc(line.get('base_unit'))}</span></td>
  <td class="muted mono">{esc(factor)}</td>
  <td class="num">{esc(line.get('unit_price'))}</td>
  <td class="num">{esc(line.get('amount'))}</td>
  <td>{status_badge(line.get('status'))}</td>
</tr>""")
    warnings = canonical.get("warnings") or []
    warning_html = "".join(f'<li>{esc(w)}</li>' for w in warnings)
    return f"""<div class="tabpanel" id="interp-{esc(interp['id'])}">
  <div class="meta-row">
    {badge('mapping v' + str(interp['mapping_version']), 'info')}
    {badge('view ' + (interp.get('view_hash') or '')[:12], 'neutral')}
    {badge('parse run ' + (interp.get('parse_run_id') or '')[:12], 'neutral')}
  </div>
  <dl class="kv">{''.join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k, v in doc.items())}</dl>
  {'<div class="alert alert-warn"><strong>Parser notes</strong><ul>' + warning_html + '</ul></div>' if warnings else ''}
  <table class="table compact">
    <thead><tr><th>src</th><th>ref</th><th>action</th><th>sku</th><th>as supplied</th>
      <th>base</th><th>factor</th><th>unit price</th><th>amount</th><th>status</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="10" class="muted">No lines.</td></tr>'}</tbody>
  </table>
  <details><summary>Canonical record (JSON)</summary>{json_block(canonical)}</details>
</div>"""


# --------------------------------------------------------------------------- #
# Order detail
# --------------------------------------------------------------------------- #

def render_order(ctx):
    order = ctx["order"]
    state = ctx["state"]
    check = ctx["eligibility"]
    line_rows = "".join(_order_line_row(line, ctx) for line in state.get("lines", []))
    pending_items = "".join(
        f'<li><strong>{esc(p.get("kind"))}</strong> '
        f'{esc(p.get("document") or "")} {esc(p.get("ref") or "")}'
        f'<div class="muted">{esc(p.get("detail") or "")}</div></li>'
        for p in state.get("pending", []))
    issue_items = "".join(
        f'<li><strong>{esc(i.get("kind"))}</strong> {esc(i.get("document") or "")} '
        f'{esc(i.get("ref") or "")}</li>' for i in state.get("issues", []))
    docs = "".join(
        f"""<tr><td><code>{esc(d['document_number'])}</code></td>
        <td>{esc(d['kind'])}</td><td>{esc(d['revision'])}</td>
        <td class="mono muted">{esc(d['payload_sha256'][:16])}</td>
        <td><a href="/receipts/{esc(_receipt_for_revision(ctx, d['revision_id']))}">source</a></td></tr>"""
        for d in state.get("documents", []))
    version_rows = "".join(
        f"""<tr><td>{esc(v['seq'])}</td><td>{esc(v['cause'])}</td>
        <td>{'confirmed' if v['is_confirmed'] else ''}</td>
        <td class="mono muted">{esc(v['state_hash'][:16])}</td>
        <td>{esc((v.get('note') or ''))}</td>
        <td class="muted">{esc(v['created_at'][:19].replace('T',' '))}</td></tr>"""
        for v in ctx["versions"])
    confirmations = "".join(
        f"""<div class="confirm-card">
        <div class="panel-head"><h3>Confirmation #{esc(c['confirmation_seq'])}</h3>
          {badge('pinned', 'good')}</div>
        <dl class="kv">
          <dt>By</dt><dd>{esc(c['actor'])}</dd>
          <dt>At</dt><dd>{esc(c['created_at'][:19].replace('T',' '))}</dd>
          <dt>Note</dt><dd>{esc(c['note'] or '—')}</dd>
          <dt>State hash</dt><dd class="mono">{esc(c['state_hash'][:24])}</dd>
        </dl>
        <h4>Pinned evidence</h4>
        <table class="table compact">
          <thead><tr><th>Document</th><th>Rev</th><th>Mapping</th><th>Receipt sha256</th>
            <th>View hash</th><th>Round-trip</th></tr></thead>
          <tbody>{''.join(f"<tr><td>{esc(p['document_number'])}</td><td>{esc(p['revision'])}</td><td>v{esc(p['mapping_version'])}</td><td class='mono muted'>{esc(p['receipt_sha256'][:16])}</td><td class='mono muted'>{esc(p['view_hash'][:16])}</td><td>{'ok' if p['source_roundtrip_ok'] else 'failed'}</td></tr>" for p in c['evidence'].get('pins', []))}</tbody>
        </table>
        <details><summary>Evidence JSON</summary>{json_block(c['evidence'], '16rem')}</details>
        </div>"""for c in ctx["confirmations"])
    reasons = "".join(f"<li>{esc(r)}</li>" for r in check.get("reasons", []))
    actions = _order_actions(ctx)
    unmatched = ctx.get("unmatched") or []
    unmatched_html = ""
    if unmatched:
        unmatched_html = f"""
<section class="panel">
  <div class="panel-head"><h2>Unmatched revisions</h2></div>
  <table class="table compact">
    <thead><tr><th>Document</th><th>Corrects</th><th>Reason</th><th>Attach</th></tr></thead>
    <tbody>{''.join(f"<tr><td>{esc(u['document_number'])}</td><td>{esc(u['corrects_document'])}</td><td class='muted'>{esc(u['reason'])}</td><td>—</td></tr>" for u in unmatched)}</tbody>
  </table>
</section>"""
    totals = _totals(state.get("totals"))
    body = f"""
<p class="breadcrumb"><a href="/">Overview</a> / order</p>
<div class="panel-head">
  <div>
    <h1>{esc(order['order_reference'])}</h1>
    <div class="meta-row">
      {status_badge(order['status'])}
      {badge(order['supplier'], 'info')}
      {badge('confirmation #' + str(order['confirmation_seq']), 'neutral')}
      {'<span class="badge badge-warn">awaiting original</span>' if order['awaiting_original'] else ''}
    </div>
  </div>
  <div class="row">{actions}</div>
</div>
<p class="muted">Business state is rebuilt deterministically from all known revisions
and compared by hash. Base totals: {totals}.</p>

{'<div class="alert alert-warn"><strong>Not ready to confirm.</strong><ul>' + reasons + '</ul></div>' if not check['eligible'] else '<div class="alert alert-good">All evidence resolved; confirmation is available.</div>'}

<section class="panel">
  <div class="panel-head"><h2>Effective lines</h2>
    <span class="muted">one row per supplier line, with every revision that touched it</span></div>
  <table class="table">
    <thead><tr><th>Line</th><th>SKU</th><th>As supplied</th><th>Base</th>
      <th>Unit price</th><th>Status</th><th>Revisions</th></tr></thead>
    <tbody>{line_rows or '<tr><td colspan="7" class="muted">No lines.</td></tr>'}</tbody>
  </table>
</section>

<div class="grid-2">
  <section class="panel">
    <h2>Documents</h2>
    <table class="table compact">
      <thead><tr><th>Document</th><th>Kind</th><th>Rev</th><th>Payload</th><th></th></tr></thead>
      <tbody>{docs}</tbody>
    </table>
    <h2 style="margin-top:1.2rem">Pending</h2>
    <ul class="plain">{pending_items or '<li class="muted">Nothing pending.</li>'}</ul>
    <h2 style="margin-top:1.2rem">Issues</h2>
    <ul class="plain">{issue_items or '<li class="muted">No issues.</li>'}</ul>
  </section>
  <section class="panel">
    <h2>State versions</h2>
    <table class="table compact">
      <thead><tr><th>Seq</th><th>Cause</th><th></th><th>Hash</th><th>Note</th><th>At</th></tr></thead>
      <tbody>{version_rows}</tbody>
    </table>
    <div id="order-actions-result" class="result hidden"></div>
  </section>
</div>

<section class="panel">
  <div class="panel-head"><h2>Confirmations</h2></div>
  {confirmations or '<p class="muted">Not confirmed yet.</p>'}
</section>
{unmatched_html}

<section class="panel">
  <div class="panel-head"><h2>Audit trail</h2></div>
  <ul class="events">{''.join(_event_item(e) for e in ctx['events'])}</ul>
</section>
"""
    return layout(f"Order {order['order_reference']}", body, active="/")


def _receipt_for_revision(ctx, revision_id):
    for r in ctx["revisions"]:
        if r["revision_id"] == revision_id or r.get("id") == revision_id:
            return r.get("first_receipt_id") or ""
    return ""


def _order_line_row(line, ctx):
    prov = "".join(
        f'<div class="prov"><span>{esc(p.get("action"))}</span> '
        f'<a href="/receipts/{esc(p.get("receipt_id"))}">'
        f'{esc(p.get("document") or "")} line {esc(p.get("source_line"))}</a>'
        f'<span class="muted"> {esc(p.get("revision_id", "")[:10])}</span></div>'
        for p in line.get("provenance", []))
    base = (f'{esc(line.get("base_quantity"))} {esc(line.get("base_unit"))}'
            if line.get("base_quantity") else '<span class="muted">—</span>')
    return f"""<tr>
  <td><code>{esc(line.get('key'))}</code></td>
  <td>{esc(line.get('sku'))}<div class="muted small">{esc(line.get('description'))}</div></td>
  <td class="num">{esc(line.get('quantity'))} {esc(line.get('unit'))}</td>
  <td class="num">{base}</td>
  <td class="num">{esc(line.get('unit_price') or '—')}</td>
  <td>{status_badge(line.get('status'))}</td>
  <td class="provcell">{prov or '<span class="muted">—</span>'}</td>
</tr>"""


def _order_actions(ctx):
    order = ctx["order"]
    buttons = []
    if order["status"] != "confirmed" and ctx["eligibility"].get("eligible"):
        buttons.append(
            '<button class="btn" data-confirm="' + esc(order["id"]) +
            '">Confirm order</button>')
    if order["status"] == "confirmed":
        buttons.append(
            '<button class="btn btn-warn" data-reopen="' + esc(order["id"]) +
            '">Reopen</button>')
    buttons.append(
        '<button class="btn btn-quiet" data-recompute="' + esc(order["id"]) +
        '">Rebuild state</button>')
    return "".join(buttons)


# --------------------------------------------------------------------------- #
# Failures & mappings
# --------------------------------------------------------------------------- #

def render_failures(ctx):
    receipt_rows = "".join(
        f"""<tr><td><a href="/receipts/{esc(i['id'])}">{esc(i['filename'])}</a></td>
        <td>{esc(i['supplier'])}</td><td>{status_badge(i['status'])}</td>
        <td>{'<span class="bad">failed</span>' if not i['source_roundtrip_ok'] else 'ok'}</td>
        <td class="muted">{esc(i['failure_reason'] or '')}</td>
        <td class="muted">{esc(i['received_at'][:19].replace('T',' '))}</td></tr>"""
        for i in ctx["items"] if i["kind"] == "receipt")
    unmatched_rows = "".join(
        f"""<tr><td><a href="/receipts/{esc(i['revision_id'])}">open</a></td>
        <td><code>{esc(i['document_number'])}</code></td>
        <td>{esc(i['corrects_document'])}</td>
        <td>{esc(i['supplier'])}</td>
        <td class="muted">{esc(i['reason'])}</td>
        <td><form class="inline" data-attach="{esc(i['revision_id'])}">
          <input type="text" name="order_reference" placeholder="order reference" required>
          <button class="btn btn-small" type="submit">Attach</button></form></td></tr>"""
        for i in ctx["items"] if i["kind"] == "unmatched")
    conflict_rows = "".join(
        f"""<tr><td>{esc(i['document_number'])}</td><td>{esc(i['revision_number'])}</td>
        <td class="mono muted">{esc(i['payload_sha256'][:20])}</td>
        <td class="muted">{esc(i['conflict_note'])}</td></tr>"""
        for i in ctx["items"] if i["kind"] == "revision_conflict")
    event_rows = "".join(_event_item(e) for e in ctx["events"])
    body = f"""
<h1>Exceptions</h1>
<p class="lede">Nothing is discarded. Failed payloads keep their original bytes,
unmatched corrections wait for a target, and conflicting payloads are held side
by side until a human decides.</p>

<section class="panel">
  <h2>Failed receipts</h2>
  <table class="table">
    <thead><tr><th>File</th><th>Supplier</th><th>Status</th><th>Source</th>
      <th>Reason</th><th>Received</th></tr></thead>
    <tbody>{receipt_rows or '<tr><td colspan="6" class="muted">None.</td></tr>'}</tbody>
  </table>
</section>

<section class="panel">
  <h2>Unmatched corrections</h2>
  <table class="table">
    <thead><tr><th></th><th>Document</th><th>Corrects</th><th>Supplier</th>
      <th>Reason</th><th>Attach manually</th></tr></thead>
    <tbody>{unmatched_rows or '<tr><td colspan="6" class="muted">None.</td></tr>'}</tbody>
  </table>
</section>

<section class="panel">
  <h2>Conflicting payloads</h2>
  <table class="table">
    <thead><tr><th>Document</th><th>Revision</th><th>Payload</th><th>Note</th></tr></thead>
    <tbody>{conflict_rows or '<tr><td colspan="4" class="muted">None.</td></tr>'}</tbody>
  </table>
</section>

<section class="panel">
  <h2>Warnings and errors</h2>
  <ul class="events">{event_rows or '<li class="muted">None.</li>'}</ul>
</section>
"""
    return layout("Exceptions", body, active="/failures")


def render_mappings(ctx):
    rows = []
    for mapping in ctx["mappings"]:
        rows.append(f"""<tr>
  <td>{esc(mapping['supplier'])}</td>
  <td>{esc(mapping['version'])}</td>
  <td>{esc(mapping['name'])}</td>
  <td>{esc(mapping['format'])}</td>
  <td>{'<span class="badge badge-good">active</span>' if mapping['active'] else badge('available', 'neutral')}</td>
  <td class="mono muted">{esc(mapping['digest'][:16])}</td>
  <td class="row">
    {'' if mapping['active'] else f'<button class="btn btn-small" data-activate-mapping="{esc(mapping["id"])}">Activate</button>'}
    <button class="btn btn-small btn-quiet" data-activate-mapping="{esc(mapping["id"])}" data-reprocess="1">Activate + reprocess</button>
    <button class="btn btn-small btn-quiet" data-next-mapping="{esc(mapping["id"])}">Register next</button>
  </td>
</tr>""")
    body = f"""
<h1>Mappings</h1>
<p class="lede">Mappings are versioned data. Activating a newer mapping
re-interprets stored receipts without touching their bytes, and the order
reconciler decides whether anything business-relevant changed.</p>
<div class="alert alert-info">
  <strong>Safe by construction.</strong> A confirmed order pins the mapping
  version and interpretation it was confirmed with. Adding a new mapping, or
  replaying an old receipt under an old mapping, cannot change a confirmed
  order: the rebuild produces the same business hash and the attempt is logged.
</div>
<section class="panel">
  <table class="table">
    <thead><tr><th>Supplier</th><th>Version</th><th>Name</th><th>Format</th>
      <th>Status</th><th>Digest</th><th>Actions</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <div id="mapping-result" class="result hidden"></div>
</section>
<section class="panel">
  <h2>Retained differences in the canonical model</h2>
  <ul class="notes">
    <li><strong>Amount and unit stay as supplied</strong> (plus the raw supplier
      text), so a later mapping change can reinterpret without pretending the
      supplier said something else.</li>
    <li><strong>Base amount is derived, versioned and annotated</strong> with the
      exact rational factor, the rounding mode and the scale used.</li>
    <li><strong>Supplier line references are preserved</strong> — CSV line
      numbers, JSON <code>pos/sub</code> and EBCDIC record line numbers keep
      their own semantics; a normalized key is used only for matching.</li>
    <li><strong>Unparsed lines and unknown fields remain evidence</strong>, never
      dropped to make the record tidier.</li>
  </ul>
</section>
"""
    return layout("Mappings", body, active="/mappings")


def render_page(name, renderer, ctx):
    return renderer(ctx)


def render_notfound(ctx):
    message = ctx.get("not_found") or "Not found"
    body = f"""
<div class="panel">
  <h1>{esc(message)}</h1>
  <p class="muted">The page or resource does not exist. <a href="/">Return to the overview</a>.</p>
</div>"""
    return layout("Not found", body)


PAGE_INDEX = render_index
PAGE_RECEIVE = render_receive
PAGE_RECEIPT = render_receipt
PAGE_ORDER = render_order
PAGE_FAILURES = render_failures
PAGE_NOTFOUND = render_notfound
PAGE_MAPPINGS = render_mappings

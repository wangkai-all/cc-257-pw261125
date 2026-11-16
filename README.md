# Order intake: three suppliers, one traceable model

A self-contained order-intake service for three suppliers that disagree about
almost everything:

| | Northwind | Helios | Orion |
|---|---|---|---|
| Payload | CSV (RFC-4180, quoted newlines) | nested JSON | fixed-length 80-byte records |
| Encoding | UTF-8 (BOM optional) or cp1252 | UTF-8 or cp1252 | EBCDIC cp037 |
| Decimals | dot, thousands `,` | comma, thousands `.` | zoned decimal, sign nibble |
| Line identity | physical CSV record | `pos` / `sub` object path | record number + continuation |
| Corrections | `CORRECTS` header column | `corrects` at message level | trailer field |

The service stores the exact bytes it received, derives a canonical record with
a line-by-line trail back to the source, reconciles revisions (including
corrections that arrive **before** their original), and gates business
confirmation behind a compare-and-set that a replay cannot override.

Only the Python standard library is used — no web framework, no database, no
third-party packages.

```bash
scripts/run_demo.sh              # server on :8000 + the whole demo corpus
python3 -m unittest discover -s tests -t .
```

---

## 1. The receiving chain

Every receipt is one page that walks the chain end to end:

**original bytes → decoded physical lines → canonical record → business state**

* The raw payload is stored as a BLOB together with its SHA-256 and a decoded
  *line view* (per-line raw text, byte offset, exact EOL bytes, role, and the
  codec that produced it).
* `receipt_integrity()` rebuilds the payload from that view and compares it
  byte-for-byte with what was stored. The receipt page displays both hashes
  and the byte counts; a mismatch marks the receipt `source_roundtrip_failed`
  and it is never applied to an order.
* Each canonical line records `source_line` / `source_lines`, the supplier's own
  line reference (`supplier_line_ref`), the raw record text, the document, the
  revision and interpretation that produced it, the parse run, and the receipt.

So after splitting one source record into several positions (Helios), joining
an EBCDIC detail record with its continuation, or converting `2.5 kg` into
`2500 g`, the order page still points at the exact physical source line and
that line's original text.

## 2. What the canonical model keeps, and why

The design decision is: **keep every supplier-visible difference; derive
normalised values beside them, never in place.**

Retained as supplied:

* the exact printed number and its decimal convention (`quantity_text`,
  parsed with the mapping's declared `dot`/`comma` style — never sniffed),
* the supplied unit and (for CSV/JSON) the description in the source language,
* the supplier's line reference in its native namespace,
* document number, revision, correction target, and all trailer/header claims
  (declared line counts, checksums) even when they disagree with the body.

Derived, and annotated:

* `base_quantity` / `base_unit` via exact `Fraction` arithmetic. The
  conversion note (`conversion`) carries the source amount and unit, the
  exact rational factor (e.g. `1000/1`, `127/5`), the rounding mode
  (`half_even`), the scale, and the six integers that formed the exact
  rational. No float is used anywhere.
* totals, keyed by base unit, with the rounding method named in the output.

This is the *deferred conversion* strategy. The alternative — emitting one
pre-normalised number per line at ingestion — was rejected because it destroys
the evidence needed exactly when a correction arrives:

| | early standardisation | deferred conversion (used here) |
|---|---|---|
| correction to same base amount, different unit (`2.5 kg` → `2500 g`) | two identical rows; edit invisible | base equal, commercial expression differs; provenance shows the change |
| mapping bug or supplier convention change | original text already gone | re-interpret stored bytes under a new mapping version |
| rounding dispute | derived value is all that is left | source amount + factor + scale are all on the record |
| confirmation evidence | can only cite the derived value | cites source hash, view hash, mapping digest and parser revision |
| correction can arrive before original | cannot be interpreted yet | retained as a revision, applied when the original lands |

`tests/test_mappings.py::NormalisationTradeoff` exercises both the constant-base
unit change and the declared-decimal case (`1.234` is `1234` for Helios and
`1.234` for Northwind).

## 3. Revisions, not filenames

There is no name-based deduplication anywhere.

* **Filename reuse** — `NW-1002.csv` delivered under the name `NW-1001.csv`
  creates its own receipt and its own document revision.
* **Byte-identical resend** — a second receipt is recorded, marked
  `duplicate_content`, and linked to the first receipt; it creates no new
  revision and no new order version.
* **Same document + revision, different bytes** — both payloads are kept, the
  revision is flagged with a `revision_conflict`, the conflicting payloads are
  listed on the order page, and the order becomes ineligible for confirmation
  until a human resolves it.
* **Idempotency keys** (used by the sender) make transport retries at-most-once.

Corrections are ordered by the `corrects` graph, not by arrival time. A
correction whose target has not arrived is held with `awaiting_original`, and
the order is ineligible until the original lands; the moment it does, the
correction applies in the same rebuild. A correction whose target is unknown
entirely is stored in `unmatched_revisions`, visible on the failures page, and
can be attached manually to an order reference. A correction *of a correction*
resolves to the chain's root document, so chained edits accumulate on the
original line and each step remains in provenance.

## 4. Confirmation cannot be replayed over

The order's business state is recomputed from all known revisions on every
change (ingest, reprocess, mapping activation, manual attach). It is identified
by a business-state hash that excludes provenance and normalises numeric
precision, so re-deriving the same business facts is provably a no-op.

The hard guarantee is enforced in three layers:

1. **Pins.** Confirmation records, per revision, the interpretation it was
   confirmed against: interpretation id, view hash, parse run, mapping version
   and digest, receipt id and SHA-256, and the source round-trip result.
   `_restore_confirmation_pin()` re-selects those interpretations after any
   reprocessing while the order stays confirmed, so a mapping upgrade cannot
   silently swap the evidence under a confirmation.
2. **Business hash comparison.** A rebuild that produces a different hash for
   a confirmed order is recorded as `apply_blocked` and *not* written to the
   order. Replays that produce the same hash record `noop_confirmed`.
3. **Compare-and-set.** Every write is
   `UPDATE orders ... WHERE confirmation_seq=? AND state_hash=? AND status<>'confirmed'`
   inside `BEGIN IMMEDIATE` (with an in-process re-entrant lock for the
   thread-per-request server). Confirmation, reprocessing, mapping activation
   and manual attach therefore serialise; a writer holding stale facts fails
   the CAS instead of overwriting a confirmation.

Reopening is an explicit, audited action: it creates a new confirmation
sequence, after which later revisions can be applied normally. Re-confirming
adds another confirmation record — earlier ones are never deleted.

The demo script's live check (`POST /api/mappings/{v1}/activate` with
`reprocess` against a confirmed order) returns `noop_confirmed` and the
order's hash and confirmation sequence are unchanged.

## 5. Mapping versions are data

Mappings are rows, not code paths. Each has a digest; re-registering a modified
mapping with the same supplier/version is rejected. Interpretations reference
the mapping version and digest that produced them, and are never deleted.

Upgrading (for example, raising base precision, adding unit aliases, or moving
from scale 3 to 9) creates new interpretations and a new order version if the
business state actually changed. Precision changes are folded into the
business hash, so raising precision alone is a no-op; changing an amount is
not. The order page shows every version and what caused it.

## 6. Failures are recoverable

* Parse failures keep the payload, the failure reason, the failing line and
  the parser's detail; they appear on `/failures` and their original bytes
  remain downloadable from `/api/receipts/{id}/payload`.
* Re-interpretation re-runs the stored bytes (after re-verifying their
  integrity), so a parser/mapping fix recovers a failed receipt without asking
  the supplier to resend.
* Unmatched corrections are listed separately and can be attached to an order.
* UNKNOWN units, short/overlong EBCDIC records, checksum mismatches and
  unrecognised record types are surfaced as warnings or invalid lines, never
  silently dropped: each physical line still has a role in the source view.

## 7. Local sender

`scripts/sender.py` delivers files to a running server; `scripts/run_demo.sh`
uses it for the whole demo corpus.

```bash
python3 scripts/sender.py fixtures/northwind/NW-1001.csv
python3 scripts/sender.py --manifest scripts/demo_manifest.json
python3 scripts/sender.py --url http://host:8000 --supplier orion file.ebc
```

It sends the exact bytes, derives an idempotency key from payload digest plus
intended filename, retries transport errors and 5xx with exponential backoff,
does **not** retry 4xx (which describe the payload), and reports every attempt.

## 8. Layout

```
app/
  source.py        decoding, line view, byte-exact rebuild
  canon.py         exact decimal parsing with declared conventions
  units.py         exact rational conversion + annotated rounding
  parsers/         versioned, data-driven mappings for three formats
  db.py            schema, transactions (BEGIN IMMEDIATE + savepoints)
  engine.py        ingest, revision identity, reconciliation, confirmation
  web.py           dependency-free HTTP API and pages
  ui.py, static/   server-rendered receiving/order/failure/mapping pages
scripts/
  make_fixtures.py deterministic corpus (incl. cp1252 and EBCDIC records)
  sender.py        local sender with retry/idempotency
  demo_data.py     load the corpus directly into SQLite
  run_demo.sh      server + sender demo
tests/             75 tests: arithmetic, parsers, ingest, reconciliation,
                   mapping upgrades, live HTTP, and sender behaviour
```

## 9. Demo walkthrough

`scripts/run_demo.sh` loads, in order:

1. one order from each supplier, in each format;
2. corrections **before** their originals (Helios `HE-801` before `HE-800`,
   Orion `OR-951` before `OR-950`, Northwind `NW-3002` before `NW-3001`);
3. a document delivered under a **reused filename** (`NW-1002` content sent as
   `NW-1001.csv`);
4. a byte-identical resend under a new name (`NW-1001 (2).csv`);
5. an unmatched correction with no known target (`HE-900`);
6. malformed payloads for all three formats.

Open `/` for order states, `/failures` for the unparseable receipt and the
unmatched correction, `/mappings` for versioned mappings, and any order for
per-line provenance back to the receipt's source lines. Confirm an order, then
press *Activate + reprocess* on its supplier's mapping: the confirmation stays
pinned and the replay is logged as `noop_confirmed`.

"""Source-document capture and line anchoring.

Every received payload is kept as bytes, with a decoded line view derived from
those bytes.  A parser must account for every physical line: lines it does not
recognise are retained as unparsed evidence and never silently dropped.  The
original bytes can be rebuilt from the stored line view, which is the basis of
the byte-exact round-trip check.
"""

from dataclasses import dataclass, field, asdict

from .canon import sha256_text

ENCODINGS = {
    # supplier -> codecs tried in order; a BOM always wins.
    "northwind": ["utf-8-sig", "cp1252"],
    "helios": ["utf-8-sig", "cp1252"],
    "orion": ["cp037"],
}


@dataclass
class SourceLine:
    number: int              # 1-based physical line number, kept as evidence
    raw: str                 # exact decoded line body, including padding
    eol: str = "\n"          # observed terminator
    offset: int = 0          # byte offset of the line start in the source bytes
    length: int = 0          # byte length including terminator
    kind: str = "data"       # header | data | trailer | blank | comment | unparsed


@dataclass
class SourceDocument:
    supplier: str
    filename: str
    content_type: str
    data: bytes
    encoding: str = ""
    eol: str = "\n"
    lines: list = field(default_factory=list)
    declared_filename: str = None    # filename stated inside the payload, if any
    extra: dict = field(default_factory=dict)

    @property
    def sha256(self):
        return sha256_text(self.data)

    @property
    def text(self):
        return self.data.decode(self.encoding) if self.encoding else ""

    def line_map(self):
        return {ln.number: ln for ln in self.lines}

    def evidence(self):
        """The stored source view; round-trip checks compare this."""
        return {
            "supplier": self.supplier,
            "filename": self.filename,
            "declared_filename": self.declared_filename,
            "content_type": self.content_type,
            "encoding": self.encoding,
            "eol": self.eol,
            "sha256": self.sha256,
            "bytes": len(self.data),
            "lines": [asdict(ln) for ln in self.lines],
            "extra": self.extra,
        }


def decode_source(supplier, filename, content_type, data, declared_filename=None):
    encodings = list(ENCODINGS.get(supplier, ["utf-8-sig", "utf-8"]))
    if data.startswith(b"\xef\xbb\xbf"):
        encodings = ["utf-8-sig"] + [e for e in encodings if e not in ("utf-8-sig", "utf-8")]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"] + encodings

    encoding = None
    had_bom = data.startswith(b"\xef\xbb\xbf")
    for enc in encodings:
        try:
            data.decode(enc)
            encoding = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if encoding == "utf-8-sig" and not had_bom:
        # Decoding worked, but re-encoding with utf-8-sig would inject a BOM
        # that was never in the source.  Record the codec that reproduces the
        # delivered bytes exactly.
        encoding = "utf-8"
    if encoding == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16-le"

    doc = SourceDocument(supplier=supplier, filename=filename,
                         content_type=content_type, data=data,
                         declared_filename=declared_filename)
    if encoding is None:
        encoding = "utf-8+surrogateescape"
        doc.extra["decode_error"] = "no declared encoding decoded the file"
    doc.encoding = encoding

    if encoding == "cp037":
        # EBCDIC newline is not 0x0A: NL is 0x15 in EBCDIC (U+0085 after
        # decoding).  cp037 is single-byte, so character offsets are byte
        # offsets and decoding first is unambiguous.  CR/LF are also accepted
        # because some gateways transcode before writing the spool file.
        text = data.decode(encoding)
        spans = []
        start = 0
        i = 0
        n = len(text)
        while i < n:
            if text.startswith("\r\n", i):
                spans.append((start, i, "\r\n"))
                i += 2
                start = i
            elif text[i] in "\r\n\x85":
                spans.append((start, i, text[i]))
                i += 1
                start = i
            else:
                i += 1
        if start < n:
            spans.append((start, n, ""))
        for number, (s, e, eol) in enumerate(spans, start=1):
            doc.lines.append(SourceLine(
                number=number, raw=text[s:e], eol=eol,
                offset=len(text[:s].encode(encoding)),
                length=len(text[s:e].encode(encoding)) + len(eol.encode(encoding))))
        if not spans:
            doc.eol = ""
        doc.extra["ebcdic_line_numbers"] = "physical record sequence"
        return doc

    # Find terminators on bytes, then decode each fragment with the same
    # codec.  Multi-byte characters can never be split and offsets stay exact.
    spans = []
    start = 0
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x0D:
            if i + 1 < n and data[i + 1] == 0x0A:
                spans.append((start, i + 2, "\r\n"))
                i += 2
            else:
                spans.append((start, i + 1, "\r"))
                i += 1
            start = i
        elif b == 0x0A:
            spans.append((start, i + 1, "\n"))
            i += 1
            start = i
        else:
            i += 1
    if start < n:
        spans.append((start, n, ""))

    for number, (s, e, eol) in enumerate(spans, start=1):
        try:
            body = data[s:e - len(eol)].decode(encoding) if eol else data[s:e].decode(encoding)
        except (UnicodeDecodeError, LookupError):
            body = data[s:e - len(eol) if eol else e].decode(encoding, errors="surrogateescape")
        doc.lines.append(SourceLine(number=number, raw=body, eol=eol,
                                    offset=s, length=e - s))
        if eol:
            doc.eol = eol
    if not spans:
        doc.eol = ""
    return doc


def rebuild_bytes(doc_view):
    """Re-assemble source bytes from a stored or in-memory evidence view."""
    payload = "".join(ln["raw"] + ln["eol"] for ln in doc_view["lines"])
    return payload.encode(doc_view["encoding"])


def stable_source_view(doc):
    """The source view minus volatile diagnostics, used for equality checks."""
    ev = doc.evidence()
    ev.pop("extra", None)
    return ev

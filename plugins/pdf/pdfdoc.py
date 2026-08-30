# Copyright (C) 2026 xsm909
#
# This file is part of xcommander-plugins.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""The file layer: bytes in, objects and pages out.

A PDF is a heap of numbered objects with a table saying where each one starts,
and everything above this module — text, structure, markdown — is written as if
that heap were a dictionary. So this is the only place that knows about
`startxref`, cross-reference streams, object streams, stream filters and
encryption, and it is deliberately the dullest file in the plugin.

**Read with a real lexer, not with regular expressions.** The prototypes that
proved this viewer worth building scanned for `N G obj` with a pattern, and that
is wrong twice over: a literal string may contain the word `obj`, and a PDF from
1.5 onwards keeps most of its dictionaries inside compressed object streams
where no pattern can see them. The lexer costs perhaps two hundred lines and
buys correctness on files nobody has looked at yet.

**Two ways to find an object, and both are kept.** The cross-reference chain is
authoritative and is what a correct file wants used. A full scan of the bytes
for `N G obj` is the fallback, and it is not a luxury: a file whose xref offsets
are a few bytes out — every file some tools have ever written — reads perfectly
through the scan and not at all through the table.

Nothing here decodes a picture. `DCTDecode`, `JPXDecode`, `CCITTFaxDecode` and
`JBIG2Decode` are left as the raw bytes they arrived as, because this viewer
shows a document as text and never as a page.
"""

from __future__ import annotations

import re
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pdfcrypt


class PdfError(Exception):
    """The file cannot be read, with a sentence saying why."""


class Name(str):
    """A PDF name — `/Type`, `/Page` — kept apart from a string.

    Both are text once decoded, and telling them apart matters: `/S /Table` is a
    name and `(Table)` is a word in the document.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "/" + str(self)


class Ref:
    """`12 0 R` — a pointer to another object."""

    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int = 0):
        self.num = num
        self.gen = gen

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ref) and other.num == self.num and other.gen == self.gen

    def __hash__(self) -> int:
        return hash((self.num, self.gen))

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "%d %d R" % (self.num, self.gen)


class Stream:
    """A dictionary with bytes attached, decoded on first use."""

    __slots__ = ("dict", "raw", "_doc", "_data", "_num", "_gen")

    def __init__(self, d: Dict[str, Any], raw: bytes, doc: Optional["Document"] = None,
                 num: int = 0, gen: int = 0):
        self.dict = d
        self.raw = raw
        self._doc = doc
        self._data: Optional[bytes] = None
        self._num = num
        self._gen = gen

    def get(self, key: str, default: Any = None) -> Any:
        value = self.dict.get(key, default)
        return self._doc.resolve(value) if self._doc is not None else value

    @property
    def data(self) -> bytes:
        """The bytes with every filter this module understands undone."""
        if self._data is None:
            self._data = decode_stream(self, self._doc)
        return self._data

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<stream %d bytes %r>" % (len(self.raw), sorted(self.dict))


# -- the lexer -------------------------------------------------------------

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"
_REGULAR = bytes(c for c in range(256) if c not in WHITESPACE and c not in DELIMITERS)
_IS_REGULAR = bytearray(256)
for _c in _REGULAR:
    _IS_REGULAR[_c] = 1

#: What a name escapes with `#`.
_HEX = b"0123456789abcdefABCDEF"

_LITERAL_ESCAPES = {
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
    ord("b"): b"\b",
    ord("f"): b"\x0c",
    ord("("): b"(",
    ord(")"): b")",
    ord("\\"): b"\\",
}


class Keyword(str):
    """A bare word in the file — `obj`, `stream`, `R`, `true`."""

    __slots__ = ()


class Lexer:
    """Turns bytes into PDF objects, one at a time.

    Shared by the file body and by content streams: the syntax is the same, and
    a content stream is simply objects with operators between them.
    """

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    # -- the small pieces --------------------------------------------------

    def skip_space(self) -> None:
        data, n = self.data, len(self.data)
        i = self.pos
        while i < n:
            c = data[i]
            if c in WHITESPACE:
                i += 1
            elif c == 0x25:  # '%' — a comment runs to the end of the line
                while i < n and data[i] not in b"\r\n":
                    i += 1
            else:
                break
        self.pos = i

    def at_end(self) -> bool:
        self.skip_space()
        return self.pos >= len(self.data)

    def _read_name(self) -> Name:
        data, n = self.data, len(self.data)
        i = self.pos + 1  # past the '/'
        out = bytearray()
        while i < n and _IS_REGULAR[data[i]]:
            c = data[i]
            if c == 0x23 and i + 2 < n and data[i + 1] in _HEX and data[i + 2] in _HEX:
                out.append(int(data[i + 1:i + 3], 16))
                i += 3
            else:
                out.append(c)
                i += 1
        self.pos = i
        return Name(out.decode("utf-8", "replace"))

    def _read_literal_string(self) -> bytes:
        data, n = self.data, len(self.data)
        i = self.pos + 1
        depth = 1
        out = bytearray()
        while i < n:
            c = data[i]
            if c == 0x5C:  # backslash
                i += 1
                if i >= n:
                    break
                c = data[i]
                if c in _LITERAL_ESCAPES:
                    out += _LITERAL_ESCAPES[c]
                    i += 1
                elif 0x30 <= c <= 0x37:  # up to three octal digits
                    digits = 0
                    value = 0
                    while digits < 3 and i < n and 0x30 <= data[i] <= 0x37:
                        value = value * 8 + (data[i] - 0x30)
                        i += 1
                        digits += 1
                    out.append(value & 0xFF)
                elif c in b"\r\n":
                    # A line continuation: the break is not in the string.
                    if c == 0x0D and i + 1 < n and data[i + 1] == 0x0A:
                        i += 1
                    i += 1
                else:
                    out.append(c)
                    i += 1
            elif c == 0x28:
                depth += 1
                out.append(c)
                i += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    i += 1
                    break
                out.append(c)
                i += 1
            else:
                out.append(c)
                i += 1
        self.pos = i
        return bytes(out)

    def _read_hex_string(self) -> bytes:
        data, n = self.data, len(self.data)
        i = self.pos + 1
        digits = bytearray()
        while i < n and data[i] != 0x3E:  # '>'
            if data[i] in _HEX:
                digits.append(data[i])
            i += 1
        self.pos = min(i + 1, n)
        if len(digits) % 2:
            digits.append(0x30)  # an odd count is padded with a zero
        try:
            return bytes.fromhex(digits.decode("ascii"))
        except ValueError:
            return b""

    # -- one object --------------------------------------------------------

    def read(self) -> Any:
        """The next object, or [Keyword] for a bare word, or `None` at the end.

        `None` is also a legitimate PDF `null`; callers that care use
        [at_end] first.
        """
        self.skip_space()
        return self.read_here()

    def read_here(self) -> Any:
        """[read], for a caller that has already skipped the whitespace.

        Worth the second name: a content stream is millions of tokens and its
        interpreter has to look for the end of the data anyway, so skipping the
        space twice per token was a tenth of the time it took to read a page.
        """
        data, n = self.data, len(self.data)
        if self.pos >= n:
            return None
        c = data[self.pos]

        if c == 0x2F:  # '/'
            return self._read_name()
        if c == 0x28:  # '('
            return self._read_literal_string()
        if c == 0x3C:  # '<'
            if self.pos + 1 < n and data[self.pos + 1] == 0x3C:
                return self._read_dict()
            return self._read_hex_string()
        if c == 0x5B:  # '['
            self.pos += 1
            out: List[Any] = []
            while True:
                self.skip_space()
                if self.pos >= n:
                    break
                if data[self.pos] == 0x5D:  # ']'
                    self.pos += 1
                    break
                before = self.pos
                item = self.read_here()
                if self.pos == before:  # a delimiter nothing can consume
                    self.pos += 1
                    continue
                out.append(item)
            return self._fold_refs(out)
        if c in b"]>}":  # a stray closer: step over it
            self.pos += 1
            return self.read()
        if c == 0x7B:  # '{' — a PostScript function body, of no interest here
            self.pos += 1
            depth = 1
            while self.pos < n and depth:
                if data[self.pos] == 0x7B:
                    depth += 1
                elif data[self.pos] == 0x7D:
                    depth -= 1
                self.pos += 1
            return None

        # A regular token: a number or a bare word. Found with one regular
        # expression rather than a loop over the bytes — a content stream is
        # millions of these, and the loop was most of the cost of reading a
        # page.
        start = self.pos
        i = _TOKEN.match(data, start).end()
        if i == start:  # not regular and not a delimiter we know: skip it
            self.pos += 1
            return self.read()
        self.pos = i
        token = data[start:i]
        cached = _TOKENS.get(token)
        if cached is not None:
            return cached
        value = _number_or_keyword(token)
        if len(token) <= 8 and len(_TOKENS) < 8192:
            _TOKENS[token] = value
        return value

    def read_value(self) -> Any:
        """One object, with `n g R` recognised by looking ahead.

        A reference cannot be told from the number that starts it until two more
        tokens have been read, so the position is put back when they turn out to
        be something else. That look-ahead is why a dictionary value is read
        through this and not through [read].
        """
        obj = self.read()
        if isinstance(obj, int) and not isinstance(obj, bool):
            mark = self.pos
            second = self.read()
            if isinstance(second, int) and not isinstance(second, bool):
                third = self.read()
                if isinstance(third, Keyword) and third == "R":
                    return Ref(obj, second)
            self.pos = mark
        return obj

    def _read_dict(self) -> Any:
        data, n = self.data, len(self.data)
        self.pos += 2  # past '<<'
        out: Dict[str, Any] = {}
        while True:
            self.skip_space()
            if self.pos >= n:
                break
            if data[self.pos] == 0x3E and self.pos + 1 < n and data[self.pos + 1] == 0x3E:
                self.pos += 2
                break
            before = self.pos
            key = self.read()
            if self.pos == before:
                self.pos += 1
                continue
            if not isinstance(key, Name):
                # Anything else where a key belongs is a broken dictionary;
                # step over it rather than give up on the object.
                continue
            self.skip_space()
            if data[self.pos:self.pos + 2] == b">>":
                out[str(key)] = None
                continue
            before = self.pos
            value = self.read_value()
            if self.pos == before:
                self.pos += 1
                continue
            out[str(key)] = value
        return out

    @staticmethod
    def _fold_refs(items: List[Any]) -> List[Any]:
        """Collapses every `n g R` triple in a list into one [Ref]."""
        out: List[Any] = []
        i = 0
        while i < len(items):
            if (
                i + 2 < len(items)
                and isinstance(items[i], int)
                and isinstance(items[i + 1], int)
                and isinstance(items[i + 2], Keyword)
                and items[i + 2] == "R"
            ):
                out.append(Ref(items[i], items[i + 1]))
                i += 3
            else:
                out.append(items[i])
                i += 1
        return out


#: One regular token — everything that is neither whitespace nor a delimiter.
_TOKEN = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")

#: What short tokens mean, remembered. A content stream says `0`, `1`, `Tj`
#: and `re` hundreds of thousands of times, and parsing each one afresh was a
#: quarter of the time it took to read a page.
_TOKENS: Dict[bytes, Any] = {}

_DIGITS = b"0123456789+-."


def _number_or_keyword(token: bytes) -> Any:
    if token[0] in _DIGITS:
        # `float` would accept `nan` and `inf`; nothing that starts with a
        # digit or a sign can be either, so this order is the check.
        try:
            return int(token)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            return 0.0
    if token == b"true":
        return True
    if token == b"false":
        return False
    if token == b"null":
        return None
    return Keyword(token.decode("latin-1"))


# -- filters ---------------------------------------------------------------

#: Filters that make bytes out of bytes. A picture codec is not among them —
#: see the module docstring.
_IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}


def flate(data: bytes) -> bytes:
    """Inflate, forgiving the two damages that actually occur.

    A truncated stream is common enough that giving up on one would lose whole
    documents; `decompressobj` hands back what it managed. A missing or wrong
    two-byte header is the other, and raw deflate is tried for it.
    """
    try:
        return zlib.decompressobj().decompress(data)
    except zlib.error:
        pass
    for skip in (0, 1, 2):
        try:
            return zlib.decompressobj(-15).decompress(data[skip:])
        except zlib.error:
            continue
    return b""


def _apply_predictor(data: bytes, params: Dict[str, Any]) -> bytes:
    """Undoes the PNG or TIFF predictor a compressed stream was filtered with."""
    predictor = int(params.get("Predictor", 1) or 1)
    if predictor <= 1:
        return data
    colors = int(params.get("Colors", 1) or 1)
    bpc = int(params.get("BitsPerComponent", 8) or 8)
    columns = int(params.get("Columns", 1) or 1)
    bpp = max(1, (colors * bpc + 7) // 8)
    row_length = (columns * colors * bpc + 7) // 8

    if predictor == 2:
        if bpc != 8:
            return data
        out = bytearray(data)
        for start in range(0, len(out) - row_length + 1, row_length):
            for i in range(bpp, row_length):
                out[start + i] = (out[start + i] + out[start + i - bpp]) & 0xFF
        return bytes(out)

    # PNG predictors: one tag byte in front of every row.
    out = bytearray()
    previous = bytearray(row_length)
    step = row_length + 1
    for start in range(0, len(data) - 1, step):
        tag = data[start]
        row = bytearray(data[start + 1:start + 1 + row_length])
        if len(row) < row_length:
            row += bytes(row_length - len(row))
        if tag == 1:
            for i in range(bpp, row_length):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif tag == 2:
            for i in range(row_length):
                row[i] = (row[i] + previous[i]) & 0xFF
        elif tag == 3:
            for i in range(row_length):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif tag == 4:
            for i in range(row_length):
                left = row[i - bpp] if i >= bpp else 0
                up = previous[i]
                upleft = previous[i - bpp] if i >= bpp else 0
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                if pa <= pb and pa <= pc:
                    value = left
                elif pb <= pc:
                    value = up
                else:
                    value = upleft
                row[i] = (row[i] + value) & 0xFF
        out += row
        previous = row
    return bytes(out)


def _ascii_hex(data: bytes) -> bytes:
    cut = data.find(b">")
    if cut != -1:
        data = data[:cut]
    digits = bytes(c for c in data if c in _HEX)
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return b""


def _ascii85(data: bytes) -> bytes:
    if data.startswith(b"<~"):
        data = data[2:]
    cut = data.find(b"~>")
    if cut != -1:
        data = data[:cut]
    out = bytearray()
    group: List[int] = []
    for byte in data:
        if byte in WHITESPACE:
            continue
        if byte == 0x7A and not group:  # 'z' — four zero bytes
            out += b"\0\0\0\0"
            continue
        if not (0x21 <= byte <= 0x75):
            continue
        group.append(byte - 0x21)
        if len(group) == 5:
            value = 0
            for digit in group:
                value = value * 85 + digit
            out += value.to_bytes(4, "big")
            group = []
    if group:
        count = len(group)
        group += [84] * (5 - count)
        value = 0
        for digit in group:
            value = value * 85 + digit
        out += value.to_bytes(4, "big")[: count - 1]
    return bytes(out)


def _run_length(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        length = data[i]
        if length == 128:
            break
        if length < 128:
            out += data[i + 1: i + 2 + length]
            i += 2 + length
        else:
            if i + 1 < len(data):
                out += bytes([data[i + 1]]) * (257 - length)
            i += 2
    return bytes(out)


def _lzw(data: bytes, early: int = 1) -> bytes:
    """LZW as PDF uses it, with the off-by-one every writer expects."""
    out = bytearray()
    table: List[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    width = 9
    previous: Optional[bytes] = None
    value = 0
    bits = 0
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= width:
            code = (value >> (bits - width)) & ((1 << width) - 1)
            bits -= width
            if code == 256:
                table = [bytes([i]) for i in range(256)] + [b"", b""]
                width = 9
                previous = None
                continue
            if code == 257:
                return bytes(out)
            if previous is None:
                entry = table[code] if code < len(table) else b""
            elif code < len(table):
                entry = table[code]
                table.append(previous + entry[:1])
            else:
                entry = previous + previous[:1]
                table.append(entry)
            out += entry
            previous = entry
            if len(table) + early >= (1 << width) and width < 12:
                width += 1
    return bytes(out)


def decode_stream(stream: Stream, doc: Optional["Document"]) -> bytes:
    """Every filter undone, in order, as far as this module goes."""
    resolve = doc.resolve if doc is not None else (lambda x: x)
    raw = stream.raw
    if doc is not None and doc.crypt is not None:
        raw = doc.crypt.decrypt_stream(raw, stream._num, stream._gen, stream.dict, resolve)

    filters = resolve(stream.dict.get("Filter") or stream.dict.get("F"))
    if filters is None:
        return raw
    if not isinstance(filters, list):
        filters = [filters]
    params = resolve(stream.dict.get("DecodeParms") or stream.dict.get("DP"))
    if not isinstance(params, list):
        params = [params] * len(filters)
    while len(params) < len(filters):
        params.append(None)

    data = raw
    for name, parm in zip(filters, params):
        name = str(resolve(name) or "")
        parm = resolve(parm) or {}
        if not isinstance(parm, dict):
            parm = {}
        parm = {k: resolve(v) for k, v in parm.items()}
        if name in _IMAGE_FILTERS:
            # Left as it arrived, and said so by stopping here: this viewer
            # does not decode pictures.
            break
        if name in ("FlateDecode", "Fl"):
            data = _apply_predictor(flate(data), parm)
        elif name in ("LZWDecode", "LZW"):
            data = _apply_predictor(_lzw(data, int(parm.get("EarlyChange", 1) or 0)), parm)
        elif name in ("ASCIIHexDecode", "AHx"):
            data = _ascii_hex(data)
        elif name in ("ASCII85Decode", "A85"):
            data = _ascii85(data)
        elif name in ("RunLengthDecode", "RL"):
            data = _run_length(data)
        elif name == "Crypt":
            continue
        else:
            break
    return data


# -- the document ----------------------------------------------------------

_OBJ_HEADER = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")

#: What may follow `obj`. The scan is a pattern over the whole file, so it also
#: finds the words `5 0 obj` written inside a string; requiring something that
#: could actually start an object after them costs nothing and throws those out.
_OBJECT_STARTS = b"<[/(0123456789+-.tfn"
_STARTXREF = re.compile(rb"startxref\s+(\d+)")


class Document:
    """Every object in one file, found by number.

    Built lazily: nothing is parsed until it is asked for, which is what keeps a
    two-hundred-page file affordable when the reader only ever looks at the
    first screen.
    """

    def __init__(self, data: bytes, password: bytes = b""):
        if not data:
            raise PdfError("The file is empty.")
        head = data[:2048]
        if b"%PDF-" not in head:
            # Some files carry rubbish in front of the header; find it anywhere
            # before giving up, because the offsets are relative to the file.
            if b"%PDF-" not in data[:4096]:
                raise PdfError("This is not a PDF: it has no %PDF- header.")
        self.data = data
        m = re.search(rb"%PDF-(\d\.\d)", data[:4096])
        self.version = m.group(1).decode("ascii") if m else "?"

        self.trailer: Dict[str, Any] = {}
        self._offsets: Dict[int, int] = {}          # object number -> byte offset
        self._compressed: Dict[int, Tuple[int, int]] = {}  # -> (container, index)
        self._cache: Dict[int, Any] = {}
        self._objstm_cache: Dict[int, Dict[int, Any]] = {}
        self._scanned: Optional[Dict[int, int]] = None
        self.crypt: Optional[pdfcrypt.Decryptor] = None
        self._loading: set = set()

        self._read_xref()
        if not self._offsets and not self._compressed:
            self._scan()
        if not self.trailer:
            self._trailer_by_scan()
        self._start_decryption(password)

    # -- finding objects ---------------------------------------------------

    def _read_xref(self) -> None:
        tail = self.data[-2048:]
        matches = list(_STARTXREF.finditer(tail))
        if not matches:
            return
        start = int(matches[-1].group(1))
        seen = set()
        while start and start not in seen and 0 <= start < len(self.data):
            seen.add(start)
            try:
                trailer = self._read_one_xref(start)
            except Exception:  # noqa: BLE001 - a broken table falls back to the scan
                return
            if trailer is None:
                return
            for key, value in trailer.items():
                self.trailer.setdefault(key, value)
            # A hybrid file keeps the new-style table in /XRefStm; read it, but
            # let the plain table it accompanies win, as the specification says.
            hybrid = trailer.get("XRefStm")
            if isinstance(hybrid, int) and hybrid not in seen:
                seen.add(hybrid)
                try:
                    self._read_one_xref(hybrid)
                except Exception:  # noqa: BLE001
                    pass
            previous = trailer.get("Prev")
            start = int(previous) if isinstance(previous, (int, float)) else 0

    def _read_one_xref(self, offset: int) -> Optional[Dict[str, Any]]:
        lexer = Lexer(self.data, offset)
        lexer.skip_space()
        if self.data[lexer.pos:lexer.pos + 4] == b"xref":
            lexer.pos += 4
            return self._read_xref_table(lexer)
        # Otherwise it is a cross-reference stream: `N G obj << ... >> stream`.
        num, gen, obj = self._parse_indirect(offset)
        if not isinstance(obj, Stream):
            return None
        self._read_xref_stream(obj)
        return dict(obj.dict)

    def _read_xref_table(self, lexer: Lexer) -> Dict[str, Any]:
        """The old-style table: subsections of twenty-byte entries.

        Read through the lexer, three tokens at a time, rather than by slicing
        twenty bytes: the width is what the specification asks for and not what
        every writer produces, and a file one byte per row out of step would
        otherwise turn into nonsense rather than into a document.
        """
        data = self.data
        while True:
            lexer.skip_space()
            if data[lexer.pos:lexer.pos + 7] == b"trailer":
                lexer.pos += 7
                trailer = lexer.read()
                return trailer if isinstance(trailer, dict) else {}
            first = lexer.read()
            count = lexer.read()
            if not isinstance(first, int) or not isinstance(count, int) or count < 0:
                return {}
            for i in range(count):
                offset = lexer.read()
                lexer.read()  # the generation, of no use to a reader
                kind = lexer.read()
                if not isinstance(offset, int) or not isinstance(kind, Keyword):
                    return {}
                number = first + i
                if kind == "n" and number not in self._offsets and number not in self._compressed:
                    self._offsets[number] = offset

    def _read_xref_stream(self, stream: Stream) -> None:
        widths = self.resolve(stream.dict.get("W")) or []
        widths = [int(self.resolve(w) or 0) for w in widths]
        if len(widths) < 3:
            return
        size = int(self.resolve(stream.dict.get("Size")) or 0)
        index = self.resolve(stream.dict.get("Index")) or [0, size]
        index = [int(self.resolve(x) or 0) for x in index]
        data = stream.data
        width = sum(widths)
        if width <= 0:
            return
        position = 0
        for pair in range(0, len(index) - 1, 2):
            first, count = index[pair], index[pair + 1]
            for i in range(count):
                if position + width > len(data):
                    return
                fields = []
                for w in widths:
                    fields.append(int.from_bytes(data[position:position + w], "big") if w else None)
                    position += w
                kind = fields[0] if widths[0] else 1
                number = first + i
                if number in self._offsets or number in self._compressed:
                    continue
                if kind == 1:
                    self._offsets[number] = fields[1] or 0
                elif kind == 2:
                    self._compressed[number] = (fields[1] or 0, fields[2] or 0)

    def _scan(self) -> None:
        """Every `N G obj` in the file, later ones winning.

        An incremental update appends, so the last copy of an object is the
        current one — which is exactly the rule that makes this a safe fallback
        rather than a guess.
        """
        found: Dict[int, int] = {}
        for m in _OBJ_HEADER.finditer(self.data):
            after = self.data[m.end():m.end() + 24].lstrip(WHITESPACE)
            if not after or after[0] not in _OBJECT_STARTS:
                continue
            found[int(m.group(1))] = m.start()
        self._scanned = found

    def _scan_map(self) -> Dict[int, int]:
        if self._scanned is None:
            self._scan()
        return self._scanned or {}

    def _trailer_by_scan(self) -> None:
        for m in re.finditer(rb"trailer", self.data):
            lexer = Lexer(self.data, m.end())
            trailer = lexer.read()
            if isinstance(trailer, dict):
                for key, value in trailer.items():
                    self.trailer.setdefault(key, value)
        if "Root" not in self.trailer:
            # A file with only cross-reference streams keeps /Root in one of
            # them; and a damaged file keeps it in the catalogue itself.
            for number in sorted(self._scan_map()):
                obj = self.get(number)
                d = obj.dict if isinstance(obj, Stream) else obj
                if isinstance(d, dict):
                    if d.get("Type") == "XRef" and "Root" in d:
                        for key, value in d.items():
                            self.trailer.setdefault(key, value)
                    elif d.get("Type") == "Catalog":
                        self.trailer.setdefault("Root", Ref(number, 0))

    def _start_decryption(self, password: bytes) -> None:
        encrypt = self.trailer.get("Encrypt")
        if encrypt is None:
            return
        encrypt_ref = encrypt if isinstance(encrypt, Ref) else None
        d = self.resolve(encrypt)
        if not isinstance(d, dict):
            raise PdfError("The file says it is encrypted but does not say how.")
        ids = self.trailer.get("ID")
        first_id = b""
        if isinstance(ids, list) and ids:
            candidate = self.resolve(ids[0])
            if isinstance(candidate, bytes):
                first_id = candidate
        resolved = {k: self.resolve(v) for k, v in d.items()}
        self.crypt = pdfcrypt.Decryptor(resolved, first_id, password)
        if encrypt_ref is not None:
            self.crypt.skip.add(encrypt_ref.num)

    # -- reading one object ------------------------------------------------

    def _parse_indirect(self, offset: int) -> Tuple[int, int, Any]:
        lexer = Lexer(self.data, offset)
        num = lexer.read()
        gen = lexer.read()
        keyword = lexer.read()
        if not isinstance(num, int) or not isinstance(gen, int) or keyword != "obj":
            raise PdfError("No object at byte %d." % offset)
        value = lexer.read_value()
        lexer.skip_space()
        if self.data[lexer.pos:lexer.pos + 6] == b"stream" and isinstance(value, dict):
            start = lexer.pos + 6
            if self.data[start:start + 2] == b"\r\n":
                start += 2
            elif self.data[start:start + 1] in (b"\n", b"\r"):
                start += 1
            length = self.resolve(value.get("Length"))
            end = -1
            if isinstance(length, int) and length >= 0 and start + length <= len(self.data):
                end = start + length
                # Trust /Length only if `endstream` really follows it.
                after = self.data[end:end + 20]
                if b"endstream" not in after and after.strip()[:9] != b"endstream":
                    end = -1
            if end == -1:
                end = self.data.find(b"endstream", start)
                if end == -1:
                    end = len(self.data)
                while end > start and self.data[end - 1] in b"\r\n":
                    end -= 1
            return num, gen, Stream(value, self.data[start:end], self, num, gen)
        return num, gen, value

    def get(self, number: int, gen: int = 0) -> Any:
        """Object [number], or `None` if the file has no such thing."""
        if number in self._cache:
            return self._cache[number]
        if number in self._loading:
            return None  # a reference cycle in a damaged file
        self._loading.add(number)
        try:
            value = self._load(number, gen)
        except Exception:  # noqa: BLE001 - one bad object is not a bad file
            value = None
        finally:
            self._loading.discard(number)
        self._cache[number] = value
        return value

    def _load(self, number: int, gen: int) -> Any:
        offset = self._offsets.get(number)
        if offset is not None:
            try:
                num, _, value = self._parse_indirect(offset)
                if num == number:
                    return self._decrypt_strings(value, number, gen)
            except PdfError:
                pass
        if number in self._compressed:
            container, index = self._compressed[number]
            value = self._from_object_stream(container, number, index)
            if value is not None:
                return value  # never encrypted: the container already was
        offset = self._scan_map().get(number)
        if offset is not None:
            try:
                num, found_gen, value = self._parse_indirect(offset)
                if num == number:
                    return self._decrypt_strings(value, number, found_gen)
            except PdfError:
                pass
        # Last resort: it may be in an object stream nobody pointed us at.
        return self._search_object_streams(number)

    def _decrypt_strings(self, value: Any, num: int, gen: int) -> Any:
        if self.crypt is None or num in self.crypt.skip:
            return value
        return self.crypt.decrypt_strings(value, num, gen)

    def _object_stream(self, container: int) -> Dict[int, Any]:
        if container in self._objstm_cache:
            return self._objstm_cache[container]
        self._objstm_cache[container] = {}  # against a cycle
        stream = self.get(container)
        if not isinstance(stream, Stream):
            return {}
        data = stream.data
        count = int(self.resolve(stream.dict.get("N")) or 0)
        first = int(self.resolve(stream.dict.get("First")) or 0)
        header = Lexer(data[:first])
        pairs: List[Tuple[int, int]] = []
        for _ in range(count):
            number = header.read()
            offset = header.read()
            if not isinstance(number, int) or not isinstance(offset, int):
                break
            pairs.append((number, offset))
        out: Dict[int, Any] = {}
        for number, offset in pairs:
            lexer = Lexer(data, first + offset)
            out[number] = lexer.read()
        self._objstm_cache[container] = out
        return out

    def _from_object_stream(self, container: int, number: int, index: int) -> Any:
        return self._object_stream(container).get(number)

    def _search_object_streams(self, number: int) -> Any:
        for candidate in list(self._scan_map()):
            if candidate in self._objstm_cache:
                found = self._objstm_cache[candidate].get(number)
                if found is not None:
                    return found
        for candidate in sorted(self._scan_map()):
            obj = self.get(candidate)
            if isinstance(obj, Stream) and obj.dict.get("Type") == "ObjStm":
                found = self._object_stream(candidate).get(number)
                if found is not None:
                    return found
        return None

    def resolve(self, value: Any, depth: int = 0) -> Any:
        """Follows references until something else comes out."""
        while isinstance(value, Ref) and depth < 32:
            value = self.get(value.num, value.gen)
            depth += 1
        return value

    def dict_of(self, value: Any) -> Dict[str, Any]:
        """The dictionary of whatever this is — an object, a stream, or nothing."""
        value = self.resolve(value)
        if isinstance(value, Stream):
            return value.dict
        return value if isinstance(value, dict) else {}

    # -- pages -------------------------------------------------------------

    @property
    def catalog(self) -> Dict[str, Any]:
        return self.dict_of(self.trailer.get("Root"))

    def pages(self) -> List[Dict[str, Any]]:
        """Every page, in reading order, with what it inherits filled in.

        Inheritance is done here rather than by every caller: `/Resources`
        commonly sits on the page tree's root and on no page at all.
        """
        root = self.catalog.get("Pages")
        found: List[Dict[str, Any]] = []
        seen: set = set()

        def walk(node: Any, inherited: Dict[str, Any], depth: int) -> None:
            if len(found) > 20000 or depth > 64:
                return
            key = node.num if isinstance(node, Ref) else id(node)
            if key in seen:
                return
            seen.add(key)
            d = self.dict_of(node)
            if not d:
                return
            passed = dict(inherited)
            for name in ("Resources", "MediaBox", "CropBox", "Rotate"):
                if name in d:
                    passed[name] = d[name]
            kids = self.resolve(d.get("Kids"))
            if d.get("Type") == "Page" or (kids is None and "Contents" in d):
                page = dict(passed)
                page.update(d)
                page["__ref__"] = node if isinstance(node, Ref) else None
                found.append(page)
                return
            if isinstance(kids, list):
                for kid in kids:
                    walk(kid, passed, depth + 1)

        walk(root, {}, 0)
        if not found:
            # A file whose page tree is broken still has its pages somewhere.
            for number in sorted(self._scan_map() or self._offsets):
                d = self.dict_of(Ref(number, 0))
                if d.get("Type") == "Page":
                    page = dict(d)
                    page["__ref__"] = Ref(number, 0)
                    found.append(page)
        return found

    def content_of(self, page: Dict[str, Any]) -> bytes:
        """One page's content streams, joined the way the viewer must see them.

        Joined with a newline: the specification says the division between
        streams is not a token boundary, and a file that ends one stream in the
        middle of `BT` is legal — but a file that ends one *after* a number and
        starts the next with an operator is far commoner, and a separator keeps
        both readable.
        """
        contents = self.resolve(page.get("Contents"))
        streams: List[Any] = contents if isinstance(contents, list) else [contents]
        out: List[bytes] = []
        for item in streams:
            item = self.resolve(item)
            if isinstance(item, Stream):
                out.append(item.data)
        return b"\n".join(out)


def load(data: bytes, password: bytes = b"") -> Document:
    return Document(data, password)


def text_of(value: Any) -> str:
    """A PDF text string as characters — UTF-16 when it says so, else PDFDoc.

    `PDFDocEncoding` is Latin-1 in the range anything here will meet, and
    pretending otherwise would cost a 256-entry table to move eight characters.
    """
    if isinstance(value, Name):
        return str(value)
    if not isinstance(value, bytes):
        return "" if value is None else str(value)
    if value[:2] in (b"\xfe\xff", b"\xff\xfe"):
        return value.decode("utf-16", "replace").lstrip("﻿")
    if value[:3] == b"\xef\xbb\xbf":
        return value[3:].decode("utf-8", "replace")
    return value.decode("latin-1", "replace")


def rectangle(doc: Document, value: Any) -> Optional[Tuple[float, float, float, float]]:
    """A `/MediaBox`-shaped array as `(x0, y0, x1, y1)`, corners sorted."""
    value = doc.resolve(value)
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        numbers = [float(doc.resolve(v)) for v in value[:4]]
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = numbers
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def numbers(doc: Document, value: Any) -> List[float]:
    value = doc.resolve(value)
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        item = doc.resolve(item)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            out.append(float(item))
    return out


def iter_names(value: Any) -> Iterable[str]:
    if isinstance(value, Name):
        yield str(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, Name):
                yield str(item)


__all__ = [
    "Document",
    "Lexer",
    "Name",
    "PdfError",
    "Ref",
    "Stream",
    "flate",
    "load",
    "numbers",
    "rectangle",
    "text_of",
]

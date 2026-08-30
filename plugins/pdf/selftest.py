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

"""Checks the reader, on files made up here and on real ones.

    python3 selftest.py                 # the checks that need no file
    python3 selftest.py <folder>        # and every .pdf under there as well

Every answer is worked out somewhere other than in the reader. A cipher is
checked against the vectors in the standard that defines it; a made-up PDF is
built here byte by byte, so what should come out of it is known before it goes
in; and the two documents that decided this plugin was worth building are
checked against what was measured from them by hand.

The corpus run asks a weaker question — that nothing raises, that nothing takes
longer than the host allows, and that a file with text in it produces some.
That is the question a corpus can answer, and it is the one that catches the
file nobody imagined.
"""

from __future__ import annotations

import os
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdfcrypt  # noqa: E402
import pdfdoc  # noqa: E402
import pdffont  # noqa: E402
import pdfmd  # noqa: E402
import pdfpage  # noqa: E402

FAILURES = []


def check(name, got, want, tolerance=None):
    ok = (
        abs(got - want) <= tolerance
        if tolerance is not None and isinstance(got, (int, float))
        else got == want
    )
    if ok:
        print("  ok   %s" % name)
    else:
        FAILURES.append("%s: got %r, wanted %r" % (name, got, want))
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))


def check_in(name, needle, haystack):
    if needle in haystack:
        print("  ok   %s" % name)
    else:
        FAILURES.append("%s: %r not in %r" % (name, needle, haystack[:400]))
        print("  FAIL %s: %r is missing" % (name, needle))


def check_not_in(name, needle, haystack):
    if needle not in haystack:
        print("  ok   %s" % name)
    else:
        FAILURES.append("%s: %r should not be there" % (name, needle))
        print("  FAIL %s: %r should not be there" % (name, needle))


# -- building a PDF by hand ------------------------------------------------


def pdf(objects, root=1, extra_trailer=b"", header=b"%PDF-1.7\n"):
    """A whole file from `{number: body}`, with a correct cross-reference table.

    Built rather than borrowed: a test that reads a file somebody else wrote
    can only say that today's copy still works, and this says what the reader
    does with a construct nobody has a file of yet.
    """
    out = bytearray(header)
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number
        out += objects[number]
        out += b"\nendobj\n"
    start = len(out)
    top = max(objects) + 1
    out += b"xref\n0 %d\n" % top
    out += b"0000000000 65535 f \n"
    for number in range(1, top):
        out += b"%010d 00000 n \n" % offsets.get(number, 0)
    out += b"trailer\n<< /Size %d /Root %d 0 R %s>>\n" % (top, root, extra_trailer)
    out += b"startxref\n%d\n%%%%EOF\n" % start
    return bytes(out)


def stream(dictionary: bytes, data: bytes, compress: bool = True) -> bytes:
    if compress:
        data = zlib.compress(data)
        dictionary = dictionary[:-2] + b" /Filter /FlateDecode >>"
    return b"%s\nstream\n%s\nendstream" % (
        dictionary[:-2] + b" /Length %d >>" % len(data),
        data,
    )


def one_page(content: bytes, resources: bytes = b"<< /Font << /F1 5 0 R >> >>",
             extra_page: bytes = b"", extra: dict = None) -> bytes:
    """A one-page file with a Helvetica at `/F1` and [content] drawn on it."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
           b"/Resources " + resources + b" /Contents 4 0 R " + extra_page + b">>",
        4: stream(b"<< >>", content),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
           b"/Encoding /WinAnsiEncoding >>",
    }
    objects.update(extra or {})
    return pdf(objects)


# -- the lexer -------------------------------------------------------------


def lexer_checks():
    print("the lexer")
    read = lambda body: pdfdoc.Lexer(body).read_value()  # noqa: E731

    check("a literal string", read(b"(hello)"), b"hello")
    check("nested parentheses", read(rb"(a (b) c)"), b"a (b) c")
    check("an escaped parenthesis", read(rb"(a \) b)"), b"a ) b")
    check("an octal escape", read(rb"(\101\102)"), b"AB")
    check("a line continuation", read(b"(one\\\ntwo)"), b"onetwo")
    check("a hex string", read(b"<48656C6C6F>"), b"Hello")
    check("an odd hex string is padded", read(b"<48656C6C6F7>"), b"Hello\x70")
    check("a name", read(b"/Type"), "Type")
    check("an escaped name", read(b"/A#20B"), "A B")
    check("an integer", read(b"42"), 42)
    check("a real", read(b"-3.5"), -3.5)
    check("a real with no whole part", read(b"-.5"), -0.5)
    check("true", read(b"true"), True)
    check("null", read(b"null"), None)
    check("a reference", read(b"12 0 R"), pdfdoc.Ref(12, 0))
    check("two numbers are not a reference", read(b"[1 2]"), [1, 2])

    d = read(b"<< /Size 497 /Root 1 0 R /Info 2 0 R /Prev 116 >>")
    check("a dictionary of four", sorted(d), ["Info", "Prev", "Root", "Size"])
    check("a number before a reference", d["Size"], 497)
    check("the reference after it", d["Root"], pdfdoc.Ref(1, 0))
    check("and the one after that", d["Prev"], 116)

    nested = read(b"<< /A << /B [1 0 R (x)] >> >>")
    check("a nested dictionary", nested["A"]["B"], [pdfdoc.Ref(1, 0), b"x"])

    # A comment is whitespace, and `obj` inside a string is not an object.
    check("a comment", read(b"% not this\n/Yes"), "Yes")
    body = pdf({1: b"<< /Type /Catalog /Note (5 0 obj is not an object) >>"})
    doc = pdfdoc.load(body)
    check("a string is not scanned for objects", doc.get(5), None)

    # A PDF text string says its own encoding.
    check("utf-16 text", pdfdoc.text_of(b"\xfe\xff\x04\x16"), "Ж")
    check("latin text", pdfdoc.text_of(b"Hi"), "Hi")


# -- filters ---------------------------------------------------------------


def filter_checks():
    print("stream filters")
    check("ASCIIHex", pdfdoc._ascii_hex(b"48 65 6C 6C 6F>"), b"Hello")
    check("ASCII85", pdfdoc._ascii85(b"87cURD]i,\"Ebo80~>"), b"Hello World!")
    check("ASCII85 z", pdfdoc._ascii85(b"z~>"), b"\0\0\0\0")
    check("RunLength", pdfdoc._run_length(bytes([2, 65, 66, 67, 254, 68, 128])), b"ABCDDD")
    check("Flate", pdfdoc.flate(zlib.compress(b"hello")), b"hello")
    check("raw deflate", pdfdoc.flate(zlib.compress(b"hello")[2:-4]), b"hello")

    # LZW, on the example in the specification itself.
    check("LZW", pdfdoc._lzw(bytes([0x80, 0x0B, 0x60, 0x50, 0x22, 0x0C, 0x0C,
                                    0x85, 0x01])), b"-----A---B")

    # A PNG "up" predictor: each row is the difference from the one above.
    raw = bytes([2, 1, 2, 3]) + bytes([2, 1, 1, 1])
    check("PNG predictor", pdfdoc._apply_predictor(
        raw, {"Predictor": 12, "Colors": 1, "BitsPerComponent": 8, "Columns": 3}),
        bytes([1, 2, 3, 2, 3, 4]))

    # And the whole road, through a real object.
    body = pdf({1: b"<< /Type /Catalog >>",
                2: stream(b"<< >>", b"the body", compress=True)})
    doc = pdfdoc.load(body)
    check("a compressed stream", doc.get(2).data, b"the body")

    # What a page inherits from the tree above it, which is usually where
    # /Resources really lives.
    body = pdf({
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 595 842] "
           b"/Resources << /Font << /F1 5 0 R >> >> >>",
        3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
        4: stream(b"<< >>", b"BT /F1 12 Tf 72 700 Td (inherited) Tj ET"),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
           b"/Encoding /WinAnsiEncoding >>",
    })
    doc = pdfdoc.load(body)
    check("resources are inherited",
          pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(), "inherited")

    # An object stream, which is where PDF 1.5 keeps most dictionaries.
    inner = b"<< /Type /Catalog /Pages 20 0 R >>"
    packed = b"10 0 " + inner
    first = len(b"10 0 ")
    body = pdf({
        1: b"<< /Type /Catalog >>",
        2: stream(b"<< /Type /ObjStm /N 1 /First %d >>" % first, packed),
    })
    doc = pdfdoc.load(body)
    check("an object inside an object stream",
          doc.dict_of(pdfdoc.Ref(10, 0)).get("Pages"), pdfdoc.Ref(20, 0))


# -- encryption ------------------------------------------------------------


def crypt_checks():
    print("ciphers")
    # FIPS-197, the appendix of the standard that defines AES.
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    for size, want in ((16, "69c4e0d86a7b0430d8cdb78070b4c55a"),
                       (24, "dda97ca4864cdfe06eaf70a0ec0d7191"),
                       (32, "8ea2b7ca516745bfeafc49904b496089")):
        keys = pdfcrypt._expand_key(bytes(range(size)))
        cipher = pdfcrypt._encrypt_block(plain, keys)
        check("AES-%d encrypts" % (size * 8), cipher.hex(), want)
        check("AES-%d decrypts" % (size * 8),
              pdfcrypt._decrypt_block(cipher, keys), plain)
    check("RC4", pdfcrypt.rc4(b"Key", b"Plaintext").hex(), "bbf316e8d940af0ad3")

    # A whole RC4-encrypted file, locked with the empty user password: built
    # here the way a writer builds one, so the key derivation is checked
    # against the specification and not against itself.
    check_encrypted_file()


def check_encrypted_file():
    first_id = b"0123456789abcdef"
    permissions = 0xFFFFFFFC
    padded = pdfcrypt.PAD  # the empty password, padded
    import hashlib
    import struct

    # /O for the empty owner password, revision 2.
    owner_key = hashlib.md5(padded).digest()[:5]
    owner = pdfcrypt.rc4(owner_key, padded)
    digest = hashlib.md5()
    digest.update(padded)
    digest.update(owner)
    digest.update(struct.pack("<I", permissions))
    digest.update(first_id)
    key = digest.digest()[:5]
    user = pdfcrypt.rc4(key, padded)

    text = b"BT /F1 12 Tf 72 700 Td (Locked but open) Tj ET"
    encrypted = pdfcrypt.rc4(hashlib.md5(key + b"\x04\x00\x00\x00\x00").digest()[:10], text)
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
           b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: stream(b"<< >>", encrypted, compress=False),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
           b"/Encoding /WinAnsiEncoding >>",
        6: b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 /O <%s> /U <%s> >>"
           % (owner.hex().encode(), user.hex().encode()),
    }
    body = pdf(objects, extra_trailer=b"/Encrypt 6 0 R /ID [<%s> <%s>] "
               % (first_id.hex().encode(), first_id.hex().encode()))
    doc = pdfdoc.load(body)
    page = pdfpage.read_page(doc, doc.pages()[0], 1, {})
    check("an RC4 file opens with the empty password", page.text(), "Locked but open")


# -- fonts -----------------------------------------------------------------


def font_checks():
    print("fonts and encodings")
    check("a named glyph", pdffont.glyph_to_char("eacute"), "é")
    check("uniXXXX", pdffont.glyph_to_char("uni0416"), "Ж")
    check("an afii name", pdffont.glyph_to_char("afii10017"), "А")
    check("a suffixed name", pdffont.glyph_to_char("a.sc"), "a")
    check("a caron", pdffont.glyph_to_char("Scaron"), "Š")
    check("nothing known", pdffont.glyph_to_char("wibble"), "")

    table = pdffont.parse_tounicode(
        b"begincmap\n"
        b"1 beginbfchar <0021> <0061> endbfchar\n"
        b"1 beginbfrange <0030> <0032> <0410> endbfrange\n"
        b"1 beginbfrange <0040> <0041> [<0058> <0059>] endbfrange\n"
        b"endcmap"
    )
    check("bfchar", table[0x21], "a")
    check("bfrange counts up", (table[0x30], table[0x31], table[0x32]), ("А", "Б", "В"))
    check("bfrange with an array", (table[0x40], table[0x41]), ("X", "Y"))

    # MacRomanEncoding really decoded: without it a curly quote is `Ò`.
    doc = pdfdoc.load(one_page(
        b"BT /F1 12 Tf 72 700 Td (\322Jhon Wick\323) Tj ET",
        resources=b"<< /Font << /F1 5 0 R >> >>",
        extra={5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Times "
                  b"/Encoding /MacRomanEncoding >>"}))
    check("MacRoman quotes", pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(),
          "“Jhon Wick”")

    # /Differences beats the base encoding.
    doc = pdfdoc.load(one_page(
        b"BT /F1 12 Tf 72 700 Td (AB) Tj ET",
        extra={5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Times /Encoding "
                  b"<< /BaseEncoding /WinAnsiEncoding /Differences "
                  b"[65 /uni0416 /afii10018] >> >>"}))
    check("/Differences", pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(), "ЖБ")


# -- running a page --------------------------------------------------------


def page_checks():
    print("running a page")
    doc = pdfdoc.load(one_page(
        b"BT /F1 12 Tf 72 742 Td (first line) Tj 0 -14 Td (second line) Tj ET"))
    page = pdfpage.read_page(doc, doc.pages()[0], 1, {})
    check("two runs", len(page.runs), 2)
    check("y runs down the page", page.runs[0].y < page.runs[1].y, True)
    check("the first is at the top", round(page.runs[0].y), 100)
    check("and the second is fourteen points under it",
          round(page.runs[1].y - page.runs[0].y), 14)
    check("x is where it was put", round(page.runs[0].x0), 72)
    check("the size is the size", round(page.runs[0].size), 12)
    check("and the run knows how wide it is", page.runs[0].x1 > page.runs[0].x0, True)

    # A kern below a tenth of an em is the space a file never spells out.
    doc = pdfdoc.load(one_page(
        b"BT /F1 12 Tf 72 700 Td [(one)-300(two)] TJ ET"))
    check("a wide kern is a space",
          pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(), "one two")

    # /ActualText replaces the glyphs; it does not accompany them.
    doc = pdfdoc.load(one_page(
        b"BT /F1 12 Tf 72 700 Td (P) Tj "
        b"/Span << /ActualText (lay) >> BDC (x) Tj EMC (Station) Tj ET"))
    check("/ActualText replaces", pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(),
          "PlayStation")

    # A rectangle becomes a shape, in reading coordinates.
    doc = pdfdoc.load(one_page(b"1 0 0 RG 100 600 200 40 re S"))
    page = pdfpage.read_page(doc, doc.pages()[0], 1, {})
    check("one shape", len(page.shapes), 1)
    check("its top is measured from the top of the page",
          round(page.shapes[0].y0), 202)
    check("its width survives", round(page.shapes[0].width), 200)

    # Invisible text — the layer under a scan — is stepped over, not read.
    doc = pdfdoc.load(one_page(b"BT 3 Tr /F1 12 Tf 72 700 Td (ghost) Tj ET"))
    check("invisible text is not read",
          pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(), "")

    # A page turned on its side reads the right way up.
    doc = pdfdoc.load(one_page(
        b"BT /F1 12 Tf 100 100 Td (sideways) Tj ET",
        extra_page=b"/Rotate 90 "))
    page = pdfpage.read_page(doc, doc.pages()[0], 1, {})
    check("a rotated page is as wide as it is tall", round(page.width), 842)
    check("and its text is still there", page.text(), "sideways")

    # An inline image is binary in the middle of the syntax, and stepping over
    # it wrongly would take the rest of the page with it.
    doc = pdfdoc.load(one_page(
        b"BI /W 2 /H 2 /BPC 8 /CS /G ID \x00\xff(\\)\x01 EI\n"
        b"BT /F1 12 Tf 72 700 Td (after the picture) Tj ET"))
    check("an inline image is stepped over",
          pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(), "after the picture")

    # A form XObject is run where it is placed.
    doc = pdfdoc.load(one_page(
        b"q 1 0 0 1 0 0 cm /Fm Do Q",
        resources=b"<< /Font << /F1 5 0 R >> /XObject << /Fm 6 0 R >> >>",
        extra={6: stream(b"<< /Type /XObject /Subtype /Form /BBox [0 0 595 842] "
                         b"/Resources << /Font << /F1 5 0 R >> >> >>",
                         b"BT /F1 12 Tf 72 700 Td (inside a form) Tj ET")}))
    check("a form is run", pdfpage.read_page(doc, doc.pages()[0], 1, {}).text(),
          "inside a form")


# -- the markdown ----------------------------------------------------------


def tagged_table_pdf():
    """A file with a real `/StructTreeRoot`: a heading and a two-column table."""
    content = (
        b"BT /F1 20 Tf 72 760 Td /P << /MCID 0 >> BDC (Game Features) Tj EMC ET\n"
        b"BT /F1 12 Tf 72 700 Td /P << /MCID 1 >> BDC (Head A) Tj EMC ET\n"
        b"BT /F1 12 Tf 300 700 Td /P << /MCID 2 >> BDC (Head B) Tj EMC ET\n"
        b"BT /F1 12 Tf 72 680 Td /P << /MCID 3 >> BDC (one) Tj EMC ET\n"
        b"BT /F1 12 Tf 300 680 Td /P << /MCID 4 >> BDC (two) Tj EMC ET\n"
    )
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 10 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
           b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R "
           b"/StructParents 0 >>",
        4: stream(b"<< >>", content),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
           b"/Encoding /WinAnsiEncoding >>",
        10: b"<< /Type /StructTreeRoot /K [11 0 R] >>",
        11: b"<< /S /Document /P 10 0 R /K [12 0 R 13 0 R] >>",
        12: b"<< /S /H1 /P 11 0 R /Pg 3 0 R /K [0] >>",
        13: b"<< /S /Table /P 11 0 R /Pg 3 0 R /K [14 0 R 17 0 R] >>",
        14: b"<< /S /TR /P 13 0 R /Pg 3 0 R /K [15 0 R 16 0 R] >>",
        15: b"<< /S /TH /P 14 0 R /Pg 3 0 R /K [1] >>",
        16: b"<< /S /TH /P 14 0 R /Pg 3 0 R /K [2] >>",
        17: b"<< /S /TR /P 13 0 R /Pg 3 0 R /K [18 0 R 19 0 R] >>",
        18: b"<< /S /TD /P 17 0 R /Pg 3 0 R /K [3] >>",
        19: b"<< /S /TD /P 17 0 R /Pg 3 0 R /K [4] >>",
    }
    return pdf(objects)


def markdown_checks():
    print("the document as markdown")
    doc = pdfdoc.load(tagged_table_pdf())
    found = pdfmd.convert(doc)
    check("the tags are trusted first", found.route, "tags")
    check_in("the heading", "# Game Features", found.markdown)
    check_in("the head of the table", "| Head A | Head B |", found.markdown)
    check_in("the divider", "|---|---|", found.markdown)
    check_in("the row", "| one | two |", found.markdown)

    # A table drawn as rectangles, with no tags at all.
    boxes = []
    for row, (left, right) in enumerate([("Surname", "Ponomarev"),
                                         ("Name", "Sergei"),
                                         ("Born", "1980")]):
        y = 700 - row * 40
        boxes.append(b"72 %d 200 40 re S 272 %d 200 40 re S" % (y, y))
        boxes.append(b"BT /F1 12 Tf 80 %d Td (%s) Tj ET" % (y + 14, left.encode()))
        boxes.append(b"BT /F1 12 Tf 280 %d Td (%s) Tj ET" % (y + 14, right.encode()))
    doc = pdfdoc.load(one_page(b"\n".join(boxes)))
    found = pdfmd.convert(doc)
    check("with no tags, the drawn grid", found.route, "grid")
    check_in("a row of the grid", "| Surname | Ponomarev |", found.markdown)
    check_in("and the last one", "| Born | 1980 |", found.markdown)
    check("no head was invented", found.markdown.split("\n")[0], "|  |  |")

    # A cell of two lines stays one cell.
    doc = pdfdoc.load(one_page(
        b"72 700 200 40 re S 272 700 200 40 re S\n"
        b"72 660 200 40 re S 272 660 200 40 re S\n"
        b"BT /F1 10 Tf 80 726 Td (Presime) Tj ET\n"
        b"BT /F1 10 Tf 80 710 Td (Surname) Tj ET\n"
        b"BT /F1 10 Tf 280 718 Td (Ponomarev) Tj ET\n"
        b"BT /F1 10 Tf 80 676 Td (Ime) Tj ET\n"
        b"BT /F1 10 Tf 280 676 Td (Sergei) Tj ET\n"))
    found = pdfmd.convert(doc)
    check_in("a cell of two lines", "| Presime<br>Surname | Ponomarev |", found.markdown)

    # Nothing to read is said, not shown as an empty page.
    doc = pdfdoc.load(one_page(b"0 0 1 rg 100 100 300 300 re f"))
    found = pdfmd.convert(doc)
    check("a page with no text says so", found.empty, True)
    check_in("and says what it is", "scan", found.notes[0])

    # A running head repeats and a page number is furniture; neither is the
    # document. Nine pages, so the rule has something to count.
    pages = []
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                  b"/Encoding /WinAnsiEncoding >>"}
    kids = []
    for i in range(9):
        number = 10 + i * 2
        kids.append(b"%d 0 R" % number)
        objects[number] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                           b"/Resources << /Font << /F1 5 0 R >> >> "
                           b"/Contents %d 0 R >>" % (number + 1))
        objects[number + 1] = stream(b"<< >>",
            b"BT /F1 9 Tf 72 800 Td (A Running Head) Tj ET\n"
            b"BT /F1 12 Tf 72 700 Td (Body of page %d.) Tj ET\n"
            b"BT /F1 9 Tf 300 40 Td (%d) Tj ET\n" % (i + 1, i + 1))
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count 9 >>" % b" ".join(kids)
    doc = pdfdoc.load(pdf(objects))
    found = pdfmd.convert(doc)
    check_in("the body survives", "Body of page 1.", found.markdown)
    check_not_in("the running head is gone", "A Running Head", found.markdown)
    check_not_in("and so is the page number", "\n7\n", found.markdown)

    # A word broken across a line is put back together; a real hyphen is not.
    check("hyphenation is undone", pdfmd._tidy("Ве-\nликобритания"), "Великобритания")
    check("a hyphen before a capital stays", pdfmd._tidy("Ново-\nНиколаевск"),
          "Ново-Николаевск")
    check("a line break is a space", pdfmd._tidy("one\ntwo"), "one two")
    check("thin spaces are spaces", pdfmd._tidy("a b"), "a b")

    # A page set in two columns is read down them, not across them.
    sidebar = ["Name", "Sergei", "Address", "Bar", "Phone", "+382",
               "Email", "s@example.com", "Born", "1980", "Licence", "B"]
    body = ["A game developer with a passion for", "creating things that work,",
            "with a proven record across", "several platforms and teams.",
            "Twenty years of it.", "And a knack for problems.",
            "Worked on shooters and on tools,", "and on the pipeline between",
            "the two of them.", "Reads music, badly.",
            "Speaks three languages, one of", "them well."]
    drawn = []
    for i, word in enumerate(sidebar):
        drawn.append(b"BT /F1 11 Tf 60 %d Td (%s) Tj ET" % (760 - i * 30, word.encode()))
    for i, line in enumerate(body):
        drawn.append(b"BT /F1 11 Tf 300 %d Td (%s) Tj ET" % (770 - i * 16, line.encode()))
    doc = pdfdoc.load(one_page(b"\n".join(drawn)))
    found = pdfmd.convert(doc)
    where = found.markdown.find
    check("the sidebar comes first", where("Sergei") < where("A game developer"), True)
    check("and the body is not cut into it",
          where("Address") < where("A game developer"), True)
    check_in("the body reads as a sentence",
             "A game developer with a passion for creating things that work,",
             found.markdown)

    # The same two columns, with every line paired: that is a table, and it
    # must not be read as columns.
    drawn = []
    for i, (left, right) in enumerate(zip(sidebar, body)):
        y = 760 - i * 30
        drawn.append(b"BT /F1 11 Tf 60 %d Td (%s) Tj ET" % (y, left.encode()))
        drawn.append(b"BT /F1 11 Tf 300 %d Td (%s) Tj ET" % (y, right.encode()))
    doc = pdfdoc.load(one_page(b"\n".join(drawn)))
    found = pdfmd.convert(doc)
    check_in("a borderless table stays a table",
             "| Name | A game developer with a passion for |", found.markdown)

    # And the rules that decide whether an untagged table has a head at all.
    body = [["Name", "Size"], ["one", "1"], ["two", "2"]]
    check("nothing marks it out: no head",
          pdfmd._is_head(body, [False] * 3, [False] * 3), False)
    check("the first row alone is bold: a head",
          pdfmd._is_head(body, [True, False, False], [False] * 3), True)
    check("the first row alone is tinted: a head",
          pdfmd._is_head(body, [False] * 3, [True, False, False]), True)
    check("an empty cell in the first row: never a head",
          pdfmd._is_head([["Name", ""], ["one", "1"]], [True, False], [True, False]),
          False)


# -- the two documents this plugin was built on ----------------------------

MEASURED = [
    (
        "a docx-born file, through its tags",
        os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/"
                           "Documents/Game Project 010724-2.pdf"),
        [("route", "tags"),
         ("has", "| Платформы | PlayStation, Xbox, Steam |"),
         ("has", "| Cinematic | 1 |"),
         ("hasnt", "PlaayStaation"),
         ("tables", 6)],
    ),
    (
        "a form with no tags, through the grid it draws",
        os.path.expanduser("~/Downloads/Захтев_EGN00008-1210039-1212646_захтев.pdf"),
        [("route", "grid"),
         ("has", "| Презиме<br>Surname | Ponomarev |"),
         ("has", "| Датум пријаве<br>Date of registration | 16.08.2026. |"),
         ("rows", 14),
         ("has", "ПРИЈАВА БОРАВИШТА СТРАНЦА")],
    ),
]


def measured_checks():
    print("the two documents this was measured on")
    for name, path, wants in MEASURED:
        if not os.path.exists(path):
            print("  --   %s (not on this machine)" % name)
            continue
        found = pdfmd.convert(pdfdoc.load(open(path, "rb").read()))
        for kind, want in wants:
            if kind == "route":
                check("%s: the route" % name, found.route, want)
            elif kind == "has":
                check_in("%s: %s" % (name, want[:40]), want, found.markdown)
            elif kind == "hasnt":
                check_not_in("%s: no %s" % (name, want), want, found.markdown)
            elif kind == "tables":
                check("%s: tables" % name, found.tables, want)
            elif kind == "rows":
                rows = sum(
                    1 for line in found.markdown.split("\n")
                    if line.startswith("|") and not line.startswith("|---")
                    and line.strip("| ")
                )
                check("%s: rows" % name, rows, want)


# -- a folder of real files ------------------------------------------------


def corpus(folder):
    paths = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if name.lower().endswith(".pdf"):
                paths.append(os.path.join(root, name))
    paths.sort()
    print("\n%d file(s) under %s" % (len(paths), folder))
    slowest = []
    routes = {}
    for path in paths:
        started = time.time()
        try:
            found = pdfmd.convert(pdfdoc.load(open(path, "rb").read()))
            route = "empty" if found.empty else found.route
        except pdfcrypt.Locked:
            route = "locked"
        except Exception as failure:  # noqa: BLE001
            FAILURES.append("%s: %s: %s" % (path, type(failure).__name__, failure))
            print("  FAIL %s: %s: %s" % (os.path.basename(path),
                                         type(failure).__name__, failure))
            continue
        spent = time.time() - started
        routes[route] = routes.get(route, 0) + 1
        slowest.append((spent, path))
        if spent > 55:
            FAILURES.append("%s took %.1fs, and a call has sixty" % (path, spent))
            print("  FAIL %s took %.1fs" % (os.path.basename(path), spent))
    for route, count in sorted(routes.items()):
        print("  %-8s %d" % (route, count))
    for spent, path in sorted(slowest, reverse=True)[:5]:
        print("  %5.2fs %s" % (spent, os.path.basename(path)))


def main():
    lexer_checks()
    filter_checks()
    crypt_checks()
    font_checks()
    page_checks()
    markdown_checks()
    measured_checks()
    if len(sys.argv) > 1:
        corpus(sys.argv[1])
    print()
    if FAILURES:
        print("%d failure(s):" % len(FAILURES))
        for failure in FAILURES:
            print("  " + failure)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

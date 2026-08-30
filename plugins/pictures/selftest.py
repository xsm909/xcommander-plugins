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

"""Checks the reader, on made-up files and on real ones.

    python3 selftest.py                 # the checks that need no file
    python3 selftest.py <folder>        # and every .xcf under there as well

**The checks that matter are the ones with an oracle that is not this code.**
The run-length check works its answer out from the bytes by hand; the
compositing check is arithmetic anybody can do on paper. A check whose answer
comes from the thing under test passes a broken reader — that has happened twice
on this project already.
"""

from __future__ import annotations

import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import base64  # noqa: E402
import psd  # noqa: E402
import tga  # noqa: E402
import meta  # noqa: E402
import tiff  # noqa: E402
import xcf  # noqa: E402

FAILURES = []

#: Where the matched TIFFs live, for the checks that need a real file.
FIXTURES = "/Users/Shared/temp/pictures"


def check(name: str, got, want):
    if got != want:
        FAILURES.append("%s: got %r, wanted %r" % (name, got, want))
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
    else:
        print("  ok   %s" % name)


def check_rle():
    """Every one of the four opcodes, against an answer worked out by hand."""
    # 2 (a run of three 0xAA), 127 + 0x0102 (a run of 258 0xBB),
    # 128 + 0x0002 (two literal bytes), 254 (two more literal bytes).
    body = (
        bytes([2, 0xAA])
        + bytes([127, 0x01, 0x02, 0xBB])
        + bytes([128, 0x00, 0x02, 0x01, 0x02])
        + bytes([254, 0x03, 0x04])
    )
    out, at = xcf._unpack_rle(body, 0, 3 + 258 + 2 + 2)
    check("rle length", len(out), 265)
    check("rle short run", bytes(out[:3]), b"\xaa\xaa\xaa")
    check("rle long run", bytes(out[3:261]), b"\xbb" * 258)
    check("rle long literal", bytes(out[261:263]), b"\x01\x02")
    check("rle short literal", bytes(out[263:265]), b"\x03\x04")
    check("rle consumed it all", at, len(body))


def check_compositing():
    """A half-transparent white over solid black is mid grey, and nothing else.

    Worked out on paper: 255·0.5 over 0 is 127 or 128 depending on which way the
    division rounds, and the answer must be one of those and must not be 255
    (which is what laying it on without regard for alpha gives) or 0.
    """
    canvas = bytearray([0, 0, 0, 255] * 4)
    layer = {
        "name": "half", "width": 2, "height": 2, "x": 0, "y": 0, "mode": 0,
        "pixels": bytearray([255, 255, 255, 128] * 4),
    }
    xcf._over(canvas, 2, 2, layer)
    check("half over black is grey", 126 <= canvas[0] <= 129, True)
    check("and stays opaque", canvas[3], 255)

    # And a layer that is opaque everywhere replaces what is under it exactly.
    canvas = bytearray([0, 0, 0, 255] * 4)
    layer["pixels"] = bytearray([10, 20, 30, 255] * 4)
    xcf._over(canvas, 2, 2, layer)
    check("opaque replaces", bytes(canvas[:4]), bytes([10, 20, 30, 255]))


def check_blending():
    """A blend mode, against arithmetic anybody can do.

    Multiply of a half-grey on a half-grey is a quarter-grey — 64 — and the
    same pair screened is 192. A reader that ignored the mode would answer 128
    for both, which is a picture that looks perfectly plausible.
    """
    def laid(mode, under, over, alpha=255):
        canvas = bytearray([under, under, under, 255] * 4)
        xcf._over(canvas, 2, 2, {
            "name": "on", "width": 2, "height": 2, "x": 0, "y": 0, "mode": mode,
            "pixels": bytearray([over, over, over, alpha] * 4),
        })
        return canvas[0]

    check("multiply", laid(30, 128, 128), 64)
    check("screen", laid(31, 128, 128), 192)
    check("difference", laid(32, 200, 50), 150)
    check("darken only takes the lower", laid(35, 200, 50), 50)
    check("lighten only takes the higher", laid(36, 200, 50), 200)
    # And the old numbering means the same thing as the new one.
    check("the legacy number agrees", laid(3, 128, 128), laid(30, 128, 128))
    # Half-transparent, the blend is mixed back with what was under it: the
    # multiply answer 64 halfway to 128 is 96 in real numbers, and 95 once the
    # division has thrown away its remainder — which is a rounding error and
    # not a mistake, so the check allows either.
    check("and alpha still applies", 95 <= laid(30, 128, 128, alpha=128) <= 96,
          True)


def check_offsets():
    """A layer laid partly off the edge keeps the part that is on it."""
    canvas = bytearray(4 * 4 * 4)          # 4x4, transparent
    layer = {
        "name": "corner", "width": 2, "height": 2, "x": 3, "y": 3, "mode": 0,
        "pixels": bytearray([9, 9, 9, 255] * 4),
    }
    xcf._over(canvas, 4, 4, layer)
    check("the one pixel that fits", canvas[(3 * 4 + 3) * 4], 9)
    check("and nothing wrapped round", canvas[0], 0)


def check_planes():
    """A tile is byte planes, not pixels — the mistake this reader exists to
    not make. Built here as three separate planes and read back as pixels."""
    pixels = 4
    planes = [bytes([1, 2, 3, 4]), bytes([10, 20, 30, 40]), bytes([100] * 4)]
    body = b"".join(bytes([128, 0x00, pixels]) + plane for plane in planes)
    got = xcf._tile(body, 0, pixels, 3, 1)
    check("plane count", len(got), 3)
    check("first plane", bytes(got[0]), planes[0])
    check("third plane", bytes(got[2]), planes[2])
    rgba = xcf._as_rgba([bytearray(p) for p in got], pixels, 0, None)
    check("first pixel", bytes(rgba[:4]), bytes([1, 10, 100, 255]))
    check("second pixel", bytes(rgba[4:8]), bytes([2, 20, 100, 255]))


def check_zlib_tiles():
    """The other compression GIMP writes, whose length is not written down."""
    pixels = 4
    body = zlib.compress(bytes([1, 2, 3, 4]) + bytes([5, 6, 7, 8]))
    got = xcf._tile(body + b"junk after the stream", 0, pixels, 2, 2)
    check("zlib first plane", bytes(got[0]), bytes([1, 2, 3, 4]))
    check("zlib second plane", bytes(got[1]), bytes([5, 6, 7, 8]))


def check_packbits():
    """Photoshop's encoding, which is *not* GIMP's — same idea, other numbers.

    Worked out by hand: 1 means two literal bytes, 254 means three copies of
    the byte after it, and 128 means nothing at all.
    """
    body = bytes([1, 0x0A, 0x0B]) + bytes([254, 0x0C]) + bytes([128]) + bytes([0, 0x0D])
    got = psd._unpack_bits(body, 6)
    check("packbits", bytes(got), bytes([0x0A, 0x0B, 0x0C, 0x0C, 0x0C, 0x0D]))


def check_targa_corners():
    """Blue first, and the bottom row first unless the flag says otherwise.

    A two-by-two picture built here by hand: red, green on the top row and
    blue, white on the bottom. Written the way an ordinary Targa is — bottom
    row first — it must come back with red in the top left.
    """
    def targa(descriptor, rows):
        head = struct.pack(
            "<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 2, 2, 24, descriptor
        )
        body = bytearray()
        for row in rows:
            for red, green, blue in row:
                body += bytes([blue, green, red])
        return head + bytes(body)

    top = [(255, 0, 0), (0, 255, 0)]
    bottom = [(0, 0, 255), (255, 255, 255)]

    picture = tga.read(targa(0, [bottom, top]))
    check("bottom-up: red is top left", bytes(picture["pixels"][:3]), b"\xff\x00\x00")
    check("bottom-up: blue is bottom left",
          bytes(picture["pixels"][8:11]), b"\x00\x00\xff")

    picture = tga.read(targa(0x20, [top, bottom]))
    check("top-down: red is top left", bytes(picture["pixels"][:3]), b"\xff\x00\x00")

    # And the same picture run-length encoded must be the same picture.
    head = struct.pack("<BBBHHBHHHHBB", 0, 0, 10, 0, 0, 0, 0, 0, 2, 2, 24, 0x20)
    packed = (
        # Blue first — writing 0,0,255 here would be *red*, which is exactly the
        # mistake this check exists to catch, and it caught it once already.
        bytes([0x80 | 1]) + bytes([255, 0, 0])      # a run of two blue
        + bytes([1]) + bytes([0, 255, 0]) + bytes([255, 255, 255])
    )
    picture = tga.read(head + packed)
    check("a run of two", bytes(picture["pixels"][:3]), b"\x00\x00\xff")
    check("and the literals after it", bytes(picture["pixels"][8:11]), b"\x00\xff\x00")


def check_lzw():
    """TIFF's LZW, against a stream worked out by hand.

    The dictionary is deterministic, so the codes for a short input can be
    written down on paper: clear (256), then A (65), then B (66), then the
    entry the decoder must have just built for "AB" (258), then end (257).
    Nine bits each, packed most significant bit first.
    """
    codes = [256, 65, 66, 258, 257]
    bits = "".join(format(code, "09b") for code in codes)
    bits += "0" * (-len(bits) % 8)
    body = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    check("lzw by hand", tiff._unlzw(body), b"ABAB")


def check_tiff_kinds(folder):
    """Every arrangement of the same picture must come out the same picture.

    The oracle is not this code: the files are written by the system's own
    encoder, one uncompressed and stripped, one tiled with a 512-pixel tile,
    one LZW. If any two of them differ by a byte, one of the three paths
    through this reader is wrong.
    """
    made = {}
    for name in ("stripped", "tiled32", "opt-lzw"):
        path = os.path.join(folder, name + ".tiff")
        if not os.path.exists(path):
            continue
        made[name] = tiff.read(open(path, "rb").read())
    if len(made) < 2:
        print("  (no matched TIFFs under %s)" % folder)
        return
    first = next(iter(made))
    for name, picture in made.items():
        check("%s is the same picture as %s" % (name, first),
              (picture["width"], picture["height"], bytes(picture["pixels"])),
              (made[first]["width"], made[first]["height"],
               bytes(made[first]["pixels"])))


#: Which reader answers for which extension, as `main.py` has it.
READERS = {"xcf": xcf.read, "psd": psd.read, "psb": psd.read, "tga": tga.read,
           "tif": tiff.read, "tiff": tiff.read}


def check_handover():
    """A file this reader refuses is handed to the machine's own decoder.

    What each engine reads differs by machine and differs from what this reads
    — measured on both — so refusing outright throws away a picture somebody
    could have seen. GIMP's format is the exception: nothing anywhere reads it,
    so there is nobody to hand it to.
    """
    try:
        import main                                     # noqa: PLC0415
    except ImportError:
        print("  (the app's python-sdk is not on the path; skipped)")
        return

    # A TIFF that says it is JPEG-compressed inside, which this does not read.
    path = os.path.join(FIXTURES, "stripped.tiff")
    if not os.path.exists(path):
        print("  (no stripped.tiff under %s; skipped)" % FIXTURES)
        return
    body = bytearray(open(path, "rb").read())
    order = "<" if body[:2] == b"II" else ">"
    count, = struct.unpack(order + "H", body[8:10])
    for i in range(count):
        at = 10 + i * 12
        tag, = struct.unpack(order + "H", body[at:at + 2])
        if tag == 259:                                   # compression
            struct.pack_into(order + "H", body, at + 8, 7)
    answer = main.answer("tiff", bytes(body))
    check("handed over rather than refused", answer.get("kind"), "image")
    check("and it is the file itself", answer.get("data"),
          base64.b64encode(bytes(body)).decode("ascii"))
    check("and it says why", "own decoder" in (answer.get("detail") or ""), True)

    # Nothing reads XCF, so there is nobody to hand it to.
    answer = main.answer("xcf", b"gimp xcf v019" + b"\0" * 40)
    check("a GIMP file is refused in words", answer.get("kind"), "error")


def check_files(folder: str):
    found = 0
    for root, _, names in os.walk(folder):
        for name in names:
            reader = READERS.get(name.rsplit(".", 1)[-1].lower())
            if reader is None or name.startswith("."):
                continue
            found += 1
            path = os.path.join(root, name)
            started = time.time()
            try:
                picture = reader(open(path, "rb").read())
            except Exception as failure:  # noqa: BLE001
                FAILURES.append("%s: %s" % (name, failure))
                print("  FAIL %s: %s" % (name, failure))
                continue
            wanted = picture["width"] * picture["height"] * 4
            if len(picture["pixels"]) != wanted:
                FAILURES.append("%s: %d bytes, wanted %d" % (name, len(picture["pixels"]), wanted))
                print("  FAIL %s: wrong number of bytes" % name)
                continue
            print(
                "  ok   %-40s %dx%d %.2fs %s"
                % (
                    name[:40],
                    picture["width"],
                    picture["height"],
                    time.time() - started,
                    " ".join(picture["notes"]),
                )
            )
    if found == 0:
        print("  (nothing this reads under %s)" % folder)


# -- what a file says about itself ----------------------------------------


def tiff_directory(entries, endian=">", header=True, base_extra=b""):
    """A TIFF header and one directory, built by hand.

    Built rather than borrowed, for the reason the whole of this file exists:
    a fixture written by somebody else's encoder only ever proves that today's
    copy of it still reads. ``entries`` is ``[(tag, type, count, payload)]``
    where a payload of four bytes or fewer sits in the entry itself.
    """
    import struct as _s

    pack = _s.pack
    count = len(entries)
    # header 8 + count 2 + entries*12 + next 4
    values_at = 8 + 2 + count * 12 + 4 + len(base_extra)
    body = pack(endian + "H", count)
    tail = b""
    for tag, kind, length, payload in entries:
        if len(payload) <= 4:
            field = payload + b"\0" * (4 - len(payload))
        else:
            field = pack(endian + "I", values_at + len(tail))
            tail += payload
        body += pack(endian + "HHI", tag, kind, length) + field
    body += pack(endian + "I", 0)
    head = (b"II" if endian == "<" else b"MM") + pack(endian + "HI", 42, 8)
    return head + body + base_extra + tail


def rational(numerator, denominator, endian=">"):
    import struct as _s
    return _s.pack(endian + "II", numerator, denominator)


def check_exif_reader():
    import struct as _s
    # A directory with one of every shape that matters: text, a short, a
    # rational too big to sit in its own entry, and a pointer to a sub-IFD.
    data = tiff_directory([
        (0x010F, 2, 5, b"SONY\0"),
        (0x0112, 3, 1, _s.pack(">H", 6) + b"\0\0"),
        (0x011A, 5, 1, rational(72, 1)),
    ])
    found = meta.read_tiff_tags(data)
    check("the make", found.main.get(0x010F), "SONY")
    check("a short in its own entry", found.main.get(0x0112), 6)
    check("a rational out of line", found.main.get(0x011A), (72, 1))
    check("an orientation in words",
          meta.named(meta.ORIENTATION, found.main.get(0x0112)), "turned right")

    # The other byte order has to give the same answers.
    little = tiff_directory([(0x010F, 2, 5, b"SONY\0")], endian="<")
    check("little-endian", (meta.read_tiff_tags(little) or meta.Exif()).main.get(0x010F),
          "SONY")

    # Nonsense is refused rather than guessed at.
    check("not a TIFF at all", meta.read_tiff_tags(b"not a tiff header"), None)


def jpeg_with(exif_payload=b"", extra_segments=b""):
    """A JPEG that is only its markers — no scan, because none is read."""
    import struct as _s
    out = b"\xff\xd8"
    if exif_payload:
        body = b"Exif\0\0" + exif_payload
        out += b"\xff\xe1" + _s.pack(">H", len(body) + 2) + body
    out += extra_segments
    # A baseline frame: 8 bits, 40 high, 60 wide, three components.
    frame = _s.pack(">BHHB", 8, 40, 60, 3) + b"\0" * 9
    out += b"\xff\xc0" + _s.pack(">H", len(frame) + 2) + frame
    out += b"\xff\xd9"
    return out


def check_jpeg_facts():
    import struct as _s
    exif = tiff_directory([
        (0x010F, 2, 6, b"Canon\0"),
        (0x0110, 2, 5, b"R6\0\0\0"),
    ])
    found = meta.read(jpeg_with(exif))
    check("a JPEG is recognised", found.kind, "JPEG")
    check("its size comes from the frame", (found.width, found.height), (60, 40))
    check("its colour", found.depth, "24 bits, colour")
    check("and its make", found.exif.main.get(0x010F) if found.exif else None, "Canon")

    # A comment segment, and a file with no EXIF at all.
    comment = b"a note somebody left"
    plain = meta.read(jpeg_with(b"", b"\xff\xfe" + _s.pack(">H", len(comment) + 2) + comment))
    check("no EXIF is not an error", plain.exif, None)
    check("but a comment is still read", plain.text, [("Comment", "a note somebody left")])


def check_gps():
    """A coordinate as a number somebody can paste into a map.

    Greenwich, near enough: 51° 28' 40.1" N and 0° 0' 5.3" W — and the answer
    is arithmetic anybody can do on paper rather than what this code happens
    to produce.
    """
    check("north is positive",
          meta.degrees([(51, 1), (28, 1), (401, 10)], "N"), "51.477806")
    check("and west is negative",
          meta.degrees([(0, 1), (0, 1), (53, 10)], "W"), "-0.001472")
    check("nothing to read is nothing said", meta.degrees(None, "N"), "")


def check_readings():
    check("a fast shutter", meta.shutter((1, 250)), "1/250 s")
    check("a slow one", meta.shutter((25, 1)), "25 s")
    check("no shutter", meta.shutter(None), "")
    check("an aperture", meta.aperture((28, 10)), "f/2.8")
    check("a focal length", meta.millimetres((425, 100)), "4.2 mm")
    check("a date somebody wrote", meta.when("2026:08:30 17:04:11", "+02:00"),
          "2026-08-30 17:04:11 +02:00")
    check("a size", meta.size(1536), "1.5 KB")


def check_png_facts():
    import struct as _s, zlib as _z

    def chunk(kind, body):
        return _s.pack(">I", len(body)) + kind + body + _s.pack(
            ">I", _z.crc32(kind + body) & 0xFFFFFFFF)

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", _s.pack(">IIBBBBB", 32, 16, 8, 6, 0, 0, 0))
            + chunk(b"tEXt", b"Author\0Somebody")
            + chunk(b"eXIf", tiff_directory([(0x010F, 2, 6, b"Nikon\0")]))
            + chunk(b"IDAT", b"\0")
            + chunk(b"IEND", b""))
    found = meta.read(data)
    check("a PNG is recognised", found.kind, "PNG")
    check("its size", (found.width, found.height), (32, 16))
    check("its colour", found.depth, "32 bits, colour with alpha")
    check("its text", found.text, [("Author", "Somebody")])
    check("and EXIF inside a PNG",
          found.exif.main.get(0x010F) if found.exif else None, "Nikon")


def main() -> int:
    print("run-length encoding");   check_rle()
    print("packbits");              check_packbits()
    print("targa corners");         check_targa_corners()
    print("tiff lzw");              check_lzw()
    print("handing over");          check_handover()
    print("tiles are byte planes"); check_planes()
    print("zlib tiles");            check_zlib_tiles()
    print("compositing");           check_compositing()
    print("blending");              check_blending()
    print("offsets");               check_offsets()
    print("the EXIF directory");    check_exif_reader()
    print("a JPEG's own words");    check_jpeg_facts()
    print("a PNG's own words");     check_png_facts()
    print("coordinates");           check_gps()
    print("readings in words");     check_readings()
    for folder in sys.argv[1:]:
        print("the same TIFF three ways, under %s" % folder)
        check_tiff_kinds(folder)
        print("files under %s" % folder)
        check_files(folder)
    if FAILURES:
        print("\n%d failure(s)" % len(FAILURES))
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

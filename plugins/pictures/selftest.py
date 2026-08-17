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

import psd  # noqa: E402
import tga  # noqa: E402
import xcf  # noqa: E402

FAILURES = []


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


#: Which reader answers for which extension, as `main.py` has it.
READERS = {"xcf": xcf.read, "psd": psd.read, "psb": psd.read, "tga": tga.read}


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


def main() -> int:
    print("run-length encoding");   check_rle()
    print("packbits");              check_packbits()
    print("targa corners");         check_targa_corners()
    print("tiles are byte planes"); check_planes()
    print("zlib tiles");            check_zlib_tiles()
    print("compositing");           check_compositing()
    print("offsets");               check_offsets()
    for folder in sys.argv[1:]:
        print("files under %s" % folder)
        check_files(folder)
    if FAILURES:
        print("\n%d failure(s)" % len(FAILURES))
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

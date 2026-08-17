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

"""Targa — eighteen bytes of header and then the pixels, mostly.

Small, old and still everywhere in game and 3D work: the 3D viewer meets Targa
textures inside FBX files regularly. Three things about it are worth knowing
before reading the code:

- **The colours are stored blue first.**
- **A run in its encoding never crosses a row** in a well-formed file, but
  encoders exist that let it, so this decodes the whole picture as one stream
  rather than row by row.
- **Which corner the first pixel is in is a flag**, and it is the one thing a
  reader gets wrong silently: the picture comes out upside down and looks like a
  picture somebody scanned upside down.

There is no magic number at the front of a Targa. What identifies one is the
extension, plus the fields making sense — which is why this refuses politely
rather than drawing noise.
"""

from __future__ import annotations

import struct

NO_IMAGE, INDEXED, TRUE_COLOUR, GREY = 0, 1, 2, 3
RLE_INDEXED, RLE_TRUE_COLOUR, RLE_GREY = 9, 10, 11

MAX_PIXELS = 40_000_000


class TgaError(Exception):
    """The file is not one, or is one this cannot read."""


def read(data: bytes) -> dict:
    if len(data) < 18:
        raise TgaError("This file is too short to be a Targa.")
    (id_length, map_kind, kind, map_first, map_length, map_depth,
     _x, _y, width, height, depth, descriptor) = struct.unpack(
        "<BBBHHBHHHHBB", data[:18]
    )
    if kind not in (INDEXED, TRUE_COLOUR, GREY, RLE_INDEXED, RLE_TRUE_COLOUR, RLE_GREY):
        raise TgaError("A Targa of a kind this reader does not know: %d." % kind)
    if width == 0 or height == 0:
        raise TgaError("This Targa says it has no pixels.")
    if width * height > MAX_PIXELS:
        raise TgaError(
            "This picture is %d by %d — too big to open as a preview."
            % (width, height)
        )
    if depth not in (8, 15, 16, 24, 32):
        raise TgaError("A depth this reader does not know: %d." % depth)

    at = 18 + id_length
    palette = b""
    if map_kind == 1:
        entry = (map_depth + 7) // 8
        palette = data[at:at + map_length * entry]
        at += map_length * entry

    per = (depth + 7) // 8
    want = width * height * per
    body = (
        _unpack_rle(data, at, want, per)
        if kind in (RLE_INDEXED, RLE_TRUE_COLOUR, RLE_GREY)
        else data[at:at + want]
    )
    if len(body) < want:
        raise TgaError("This Targa stops before the end of its picture.")

    pixels = _as_rgba(
        body, width * height, kind, depth, per, palette, map_depth, map_first
    )
    # Bit five of the descriptor is set when the first pixel is the top left.
    if not descriptor & 0x20:
        pixels = _flip(pixels, width, height)
    return {"width": width, "height": height, "pixels": pixels, "notes": []}


def _unpack_rle(data, at, want, per):
    """Targa's own run-length encoding, which counts *pixels*, not bytes."""
    out = bytearray()
    while len(out) < want and at < len(data):
        code = data[at]
        at += 1
        count = (code & 0x7F) + 1
        if code & 0x80:
            out += data[at:at + per] * count
            at += per
        else:
            out += data[at:at + per * count]
            at += per * count
    return bytes(out[:want])


def _as_rgba(body, pixels, kind, depth, per, palette, map_depth, map_first):
    out = bytearray(pixels * 4)
    if kind in (GREY, RLE_GREY):
        out[0::4] = body[0::per]
        out[1::4] = body[0::per]
        out[2::4] = body[0::per]
        out[3::4] = b"\xff" * pixels
        return out
    if kind in (INDEXED, RLE_INDEXED):
        entry = (map_depth + 7) // 8
        red = bytearray(pixels)
        green = bytearray(pixels)
        blue = bytearray(pixels)
        alpha = bytearray(b"\xff" * pixels)
        for i in range(pixels):
            index = body[i * per] - map_first
            base = index * entry
            if 0 <= base < len(palette) - entry + 1:
                if entry >= 3:
                    blue[i], green[i], red[i] = palette[base], palette[base + 1], palette[base + 2]
                    if entry == 4:
                        alpha[i] = palette[base + 3]
                else:
                    value = palette[base] | (palette[base + 1] << 8)
                    red[i], green[i], blue[i] = _from_15(value)
        out[0::4], out[1::4], out[2::4], out[3::4] = red, green, blue, alpha
        return out

    if depth in (15, 16):
        red = bytearray(pixels)
        green = bytearray(pixels)
        blue = bytearray(pixels)
        alpha = bytearray(b"\xff" * pixels)
        for i in range(pixels):
            value = body[i * 2] | (body[i * 2 + 1] << 8)
            red[i], green[i], blue[i] = _from_15(value)
            if depth == 16 and not value & 0x8000:
                # The top bit is an alpha of one bit, and files that do not
                # mean it leave it clear — so it is only believed when some
                # pixel in the picture actually sets it.
                alpha[i] = 0
        if alpha.count(0) == pixels:
            alpha = bytearray(b"\xff" * pixels)
        out[0::4], out[1::4], out[2::4], out[3::4] = red, green, blue, alpha
        return out

    # Blue first, which is the whole of the format's colour order.
    out[0::4] = body[2::per]
    out[1::4] = body[1::per]
    out[2::4] = body[0::per]
    out[3::4] = body[3::per] if per == 4 else b"\xff" * pixels
    return out


def _from_15(value):
    red = (value >> 10) & 0x1F
    green = (value >> 5) & 0x1F
    blue = value & 0x1F
    # Five bits spread over eight, so that white is white rather than nearly.
    return (red << 3) | (red >> 2), (green << 3) | (green >> 2), (blue << 3) | (blue >> 2)


def _flip(pixels, width, height):
    stride = width * 4
    out = bytearray(len(pixels))
    for row in range(height):
        out[row * stride:(row + 1) * stride] = (
            pixels[(height - 1 - row) * stride:(height - row) * stride]
        )
    return out

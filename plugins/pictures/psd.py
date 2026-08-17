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

"""Photoshop's format — the picture it was saved as, not the document.

**And that is the whole design decision.** A `.psd` holds two things: the layers
somebody was working on, and a **flattened composite** of the lot written at the
end of the file so that everything else in the world can show it. A viewer wants
the second one. It costs one pass, it is what Photoshop itself puts on screen,
and it needs none of the layer arcana — masks, clipping, adjustment layers,
smart objects, effects — which is where a reader that walked the document would
spend all of its life and still be wrong.

The cost of that choice, said out loud: a file saved with **"maximise
compatibility" turned off** has no composite in it, and the honest answer there
is to say so rather than to draw an empty rectangle.

Also read: `.psb`, which is the same format with 64-bit lengths.
"""

from __future__ import annotations

import struct
import zlib

#: Colour modes, from the specification. The ones with a name are read.
BITMAP, GREY, INDEXED, RGB, CMYK, MULTICHANNEL, DUOTONE, LAB = 0, 1, 2, 3, 4, 7, 8, 9

MAX_PIXELS = 40_000_000


class PsdError(Exception):
    """The file is not one, or is one this cannot read."""


def is_psd(head: bytes) -> bool:
    return head[:4] == b"8BPS"


def read(data: bytes) -> dict:
    if not is_psd(data):
        raise PsdError("This is not a Photoshop file.")
    version, = struct.unpack(">H", data[4:6])
    if version not in (1, 2):
        raise PsdError("A version this reader does not know: %d." % version)
    channels, height, width, depth, mode = struct.unpack(">HIIHH", data[12:26])
    if width * height > MAX_PIXELS:
        raise PsdError(
            "This picture is %d by %d — too big to open as a preview."
            % (width, height)
        )
    if depth not in (1, 8, 16, 32):
        raise PsdError("A depth this reader does not know: %d." % depth)

    at = 26
    palette, at = _section(data, at)
    _resources, at = _section(data, at)
    _layers, at = _section(data, at, wide=version == 2)
    if at + 2 > len(data):
        raise PsdError(
            "This file was saved without a flattened copy inside it, so there "
            "is nothing here that can be shown without reading the layers."
        )

    compression, = struct.unpack(">H", data[at:at + 2])
    at += 2
    planes = _planes(data, at, width, height, channels, depth, compression, version)

    notes: list[str] = []
    pixels = _as_rgba(planes, width * height, mode, channels, palette, notes)
    if depth in (16, 32):
        notes.append(
            "The file keeps more than eight bits a colour; the preview shows "
            "the top eight."
        )
    return {
        "width": width,
        "height": height,
        "pixels": pixels,
        "notes": notes,
        "mode": mode,
        "depth": depth,
        "channels": channels,
    }


def _section(data, at, wide=False):
    """One length-prefixed block, and where the next one starts."""
    if wide:
        length, = struct.unpack(">Q", data[at:at + 8])
        at += 8
    else:
        length, = struct.unpack(">I", data[at:at + 4])
        at += 4
    return data[at:at + length], at + length


def _planes(data, at, width, height, channels, depth, compression, version):
    """One byte plane per channel, narrowed to eight bits."""
    step = depth // 8 if depth >= 8 else 1
    row_bytes = width * step if depth >= 8 else (width + 7) // 8
    want = row_bytes * height

    if compression == 0:
        raw = [data[at + i * want:at + (i + 1) * want] for i in range(channels)]
    elif compression == 1:
        counts_at = at
        size = 4 if version == 2 else 2
        kind = ">%dI" % (height * channels) if version == 2 else ">%dH" % (height * channels)
        counts = struct.unpack(kind, data[counts_at:counts_at + height * channels * size])
        at = counts_at + height * channels * size
        raw = []
        for channel in range(channels):
            plane = bytearray()
            for row in range(height):
                length = counts[channel * height + row]
                plane += _unpack_bits(data[at:at + length], row_bytes)
                at += length
            raw.append(bytes(plane))
    elif compression in (2, 3):
        body = zlib.decompress(data[at:])
        raw = [body[i * want:(i + 1) * want] for i in range(channels)]
        if compression == 3:
            raw = [_undelta(plane, width, height, step) for plane in raw]
    else:
        raise PsdError("A compression this reader does not know: %d." % compression)

    if depth == 1:
        return [_from_bits(plane, width, height) for plane in raw]
    if step == 1:
        return raw
    # Wider samples are big-endian, so the top byte comes first.
    return [plane[0::step] for plane in raw]


def _unpack_bits(body, want):
    """PackBits, the run-length encoding Photoshop and Targa share."""
    out = bytearray()
    at = 0
    while len(out) < want and at < len(body):
        code = body[at]
        at += 1
        if code < 128:
            out += body[at:at + code + 1]
            at += code + 1
        elif code > 128:
            out += body[at:at + 1] * (257 - code)
            at += 1
    if len(out) < want:
        out += b"\x00" * (want - len(out))
    return out[:want]


def _undelta(plane, width, height, step):
    """The prediction zip compression applies along each row."""
    out = bytearray(plane)
    for row in range(height):
        start = row * width * step
        for x in range(1, width):
            i = start + x * step
            out[i] = (out[i] + out[i - step]) & 0xFF
    return bytes(out)


def _from_bits(plane, width, height):
    """A bitmap image, one bit a pixel, and 0 is white in this format."""
    out = bytearray(width * height)
    stride = (width + 7) // 8
    for row in range(height):
        base = row * stride
        for x in range(width):
            bit = (plane[base + (x >> 3)] >> (7 - (x & 7))) & 1
            out[row * width + x] = 0 if bit else 255
    return bytes(out)


def _as_rgba(planes, pixels, mode, channels, palette, notes):
    out = bytearray(pixels * 4)
    alpha = None
    if mode == RGB and channels >= 3:
        out[0::4] = planes[0]
        out[1::4] = planes[1]
        out[2::4] = planes[2]
        if channels >= 4:
            alpha = planes[3]
    elif mode in (GREY, BITMAP, DUOTONE, MULTICHANNEL):
        grey = planes[0]
        out[0::4] = grey
        out[1::4] = grey
        out[2::4] = grey
        if channels >= 2 and mode == GREY:
            alpha = planes[1]
    elif mode == INDEXED:
        entries = len(palette) // 3
        red = bytearray(pixels)
        green = bytearray(pixels)
        blue = bytearray(pixels)
        index = planes[0]
        for i, value in enumerate(index):
            if value < entries:
                red[i] = palette[value]
                green[i] = palette[entries + value]
                blue[i] = palette[entries * 2 + value]
        out[0::4] = red
        out[1::4] = green
        out[2::4] = blue
    elif mode == CMYK and channels >= 4:
        # Photoshop stores CMYK inverted, and this is the plain conversion
        # rather than a colour-managed one: a preview, not a proof.
        notes.append("Printing colours converted plainly, without a profile.")
        c, m, y, k = planes[0], planes[1], planes[2], planes[3]
        red = bytearray(pixels)
        green = bytearray(pixels)
        blue = bytearray(pixels)
        for i in range(pixels):
            black = k[i]
            red[i] = c[i] * black // 255
            green[i] = m[i] * black // 255
            blue[i] = y[i] * black // 255
        out[0::4] = red
        out[1::4] = green
        out[2::4] = blue
    else:
        raise PsdError("A colour mode this reader does not know: %d." % mode)

    out[3::4] = alpha if alpha is not None else b"\xff" * pixels
    return out

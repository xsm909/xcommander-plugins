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

"""TIFF — a container of tags, of which a picture is one arrangement.

**Why it is the awkward one.** There is no single TIFF: the tags say how many
bits a sample has, whether the picture is in strips or in tiles, which of half a
dozen compressions was used, whether a predictor was applied first, whether the
samples are interleaved or in separate planes, and what the numbers mean —
white-is-zero, black-is-zero, RGB, a palette, or printing inks. A reader that
assumes any of those works on the file it was written against and on nothing
else. So every one of them is read from the file here.

Two that are worth naming because they are what real files do:

- **macOS writes tiled TIFFs with a 512-pixel tile.** A 32-pixel picture from
  `sips` is 790 KB, all of it one tile, and a reader that only knows strips sees
  no picture data at all.
- **LZW here is not quite anybody else's LZW.** The codes grow one step early —
  the "early change" — and a decoder without it drifts a bit at a time and
  produces convincing noise rather than an error.
"""

from __future__ import annotations

import struct
import zlib

NONE, CCITT, G3, G4, LZW, OLD_JPEG, JPEG, DEFLATE, PACKBITS, DEFLATE_OLD = (
    1, 2, 3, 4, 5, 6, 7, 8, 32773, 32946
)

WHITE_IS_ZERO, BLACK_IS_ZERO, RGB, PALETTE, MASK, CMYK, YCBCR = 0, 1, 2, 3, 4, 5, 6

MAX_PIXELS = 40_000_000


class TiffError(Exception):
    """The file is not one, or is one this cannot read."""


def is_tiff(head: bytes) -> bool:
    return head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def read(data: bytes) -> dict:
    if len(data) < 8 or not is_tiff(data):
        raise TiffError("This is not a TIFF.")
    order = "<" if data[:2] == b"II" else ">"
    magic, = struct.unpack(order + "H", data[2:4])
    if magic == 43:
        raise TiffError("This is a BigTIFF, which this reader does not read.")
    if magic != 42:
        raise TiffError("A TIFF of a kind this reader does not know.")

    at, = struct.unpack(order + "I", data[4:8])
    tags, next_page = _directory(data, order, at)
    notes: list[str] = []
    if next_page:
        notes.append("The file has more than one page; the first is shown.")

    width = _one(tags, 256, 0)
    height = _one(tags, 257, 0)
    if width <= 0 or height <= 0:
        raise TiffError("This TIFF says it has no pixels.")
    if width * height > MAX_PIXELS:
        raise TiffError(
            "This picture is %d by %d — too big to open as a preview."
            % (width, height)
        )

    samples = _one(tags, 277, 1)
    bits = list(tags.get(258, [8] * samples))
    while len(bits) < samples:
        bits.append(bits[-1] if bits else 8)
    if len(set(bits)) != 1:
        raise TiffError("A TIFF whose channels are different sizes.")
    depth = bits[0]
    if depth not in (1, 2, 4, 8, 16, 32):
        raise TiffError("A depth this reader does not know: %d." % depth)
    if depth == 32 and _one(tags, 339, 1) == 3:
        raise TiffError("A TIFF of floating-point samples.")

    compression = _one(tags, 259, NONE)
    if compression in (CCITT, G3, G4, OLD_JPEG, JPEG):
        raise TiffError(
            "This TIFF is compressed in a way this reader does not know (%d)."
            % compression
        )
    photometric = _one(tags, 262, BLACK_IS_ZERO if samples == 1 else RGB)
    if photometric == YCBCR:
        raise TiffError("A TIFF in YCbCr, which this reader does not know.")
    planar = _one(tags, 284, 1)
    predictor = _one(tags, 317, 1)
    if predictor not in (1, 2):
        raise TiffError("A predictor this reader does not know: %d." % predictor)

    rows = _rows(data, order, tags, width, height, samples, depth,
                 compression, predictor, planar)
    pixels = _as_rgba(rows, width, height, samples, depth, photometric,
                      tags.get(320), _one(tags, 338, 0), notes)
    if depth > 8:
        notes.append(
            "The file keeps more than eight bits a colour; the preview shows "
            "the top eight."
        )
    return {"width": width, "height": height, "pixels": pixels, "notes": notes}


# ------------------------------------------------------------------- the tags


#: How many bytes each field type takes, by its number.
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
             11: 4, 12: 8}
TYPE_CODE = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d"}


def _directory(data, order, at):
    """One image file directory: every tag as a list of numbers."""
    count, = struct.unpack(order + "H", data[at:at + 2])
    at += 2
    tags = {}
    for _ in range(count):
        tag, kind, length = struct.unpack(order + "HHI", data[at:at + 8])
        body = data[at + 8:at + 12]
        at += 12
        size = TYPE_SIZE.get(kind)
        if size is None:
            continue
        total = size * length
        # Four bytes or fewer live in the entry itself; anything longer is an
        # offset to somewhere else in the file.
        if total > 4:
            where, = struct.unpack(order + "I", body)
            body = data[where:where + total]
        else:
            # Four bytes are always written, whatever the field needs; taking
            # all four asks `struct` for more numbers than there are.
            body = body[:total]
        code = TYPE_CODE.get(kind)
        if code is None:
            tags[tag] = [body[:total]]
        elif kind in (5, 10):
            tags[tag] = [x for x in struct.unpack(order + "%di" % (length * 2), body)]
        else:
            tags[tag] = list(struct.unpack(order + "%d%s" % (length, code), body))
    nxt, = struct.unpack(order + "I", data[at:at + 4])
    return tags, nxt


def _one(tags, tag, fallback):
    values = tags.get(tag)
    return values[0] if values else fallback


# ------------------------------------------------------------ the picture data


def _rows(data, order, tags, width, height, samples, depth, compression,
          predictor, planar):
    """The whole picture as one interleaved buffer of samples.

    Strips and tiles are the same thing with different arithmetic — a rectangle
    of the picture, compressed on its own — so they are read by one path.
    """
    per_row = (width * samples * depth + 7) // 8
    out = bytearray(per_row * height)

    tile_width = _one(tags, 322, 0)
    tile_height = _one(tags, 323, 0)
    if tile_width and tile_height:
        offsets = tags.get(324, [])
        counts = tags.get(325, [])
        across = (width + tile_width - 1) // tile_width
        down = (height + tile_height - 1) // tile_height
        planes = samples if planar == 2 else 1
        per_plane = across * down
        for index, (offset, count) in enumerate(zip(offsets, counts)):
            plane = index // per_plane if planar == 2 else 0
            place = index % per_plane
            left = (place % across) * tile_width
            top = (place // across) * tile_height
            body = _decompress(data[offset:offset + count], compression)
            body = _undo_predictor(body, predictor, tile_width,
                                   samples if planar == 1 else 1, depth)
            _place(out, body, width, height, per_row, left, top,
                   tile_width, tile_height, samples, depth, planar, plane)
        return out

    offsets = tags.get(273, [])
    counts = tags.get(279, [])
    rows_per_strip = _one(tags, 278, height)
    if rows_per_strip <= 0:
        rows_per_strip = height
    planes = samples if planar == 2 else 1
    strips_per_plane = (height + rows_per_strip - 1) // rows_per_strip
    for index, (offset, count) in enumerate(zip(offsets, counts)):
        plane = index // strips_per_plane if planar == 2 else 0
        place = index % strips_per_plane
        top = place * rows_per_strip
        body = _decompress(data[offset:offset + count], compression)
        body = _undo_predictor(body, predictor, width,
                               samples if planar == 1 else 1, depth)
        _place(out, body, width, height, per_row, 0, top,
               width, rows_per_strip, samples, depth, planar, plane)
    return out


def _place(out, body, width, height, per_row, left, top, tile_width,
           tile_height, samples, depth, planar, plane):
    """One decoded rectangle written into the picture."""
    step = depth // 8 if depth >= 8 else None
    row_bytes = (tile_width * (1 if planar == 2 else samples) * depth + 7) // 8
    for row in range(tile_height):
        y = top + row
        if y >= height:
            break
        source = body[row * row_bytes:(row + 1) * row_bytes]
        if not source:
            break
        if depth < 8:
            source = _spread(source, depth)
            step = 1
        if planar == 1:
            start = (y * per_row) + (left * samples * (step or 1))
            take = min(len(source), per_row - (left * samples * (step or 1)))
            out[start:start + take] = source[:take]
        else:
            # A plane at a time: every `samples`th sample of the row is this
            # one's.
            for x in range(min(tile_width, width - left)):
                at = ((y * width) + left + x) * samples * (step or 1)
                for byte in range(step or 1):
                    index = x * (step or 1) + byte
                    if index >= len(source):
                        break
                    out[at + plane * (step or 1) + byte] = source[index]


def _spread(body, depth):
    """Samples narrower than a byte, one to a byte and scaled to fill it."""
    out = bytearray()
    top = (1 << depth) - 1
    for byte in body:
        for shift in range(8 - depth, -1, -depth):
            value = (byte >> shift) & top
            out.append(value * 255 // top)
    return bytes(out)


def _decompress(body, compression):
    if compression == NONE:
        return body
    if compression == PACKBITS:
        return _unpack_bits(body)
    if compression in (DEFLATE, DEFLATE_OLD):
        return zlib.decompress(body)
    if compression == LZW:
        return _unlzw(body)
    raise TiffError("A compression this reader does not know: %d." % compression)


def _unpack_bits(body):
    """The same run-length encoding Photoshop uses."""
    out = bytearray()
    at = 0
    while at < len(body):
        code = body[at]
        at += 1
        if code < 128:
            out += body[at:at + code + 1]
            at += code + 1
        elif code > 128:
            out += body[at:at + 1] * (257 - code)
            at += 1
    return bytes(out)


def _unlzw(body):
    """TIFF's LZW, whose codes grow **one step early**.

    Without the early change the widths part company with the encoder's a code
    at a time and the picture comes out as convincing noise — no error, no
    stripe, just a different picture. It is the single thing to get right here.
    """
    pieces = []
    add = pieces.append
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    grow = table.append
    width = 9
    next_width = 511                              # where the code widens
    previous = None
    bits = 0
    held = 0
    # Locals and a list of pieces rather than a growing bytearray: this loop
    # runs once per code, which is millions of times on a scan, and every
    # attribute lookup in it is paid for that many times.
    #
    # **Measured, on a strip of 1 282 bytes repeated 500 times: 2.6 MB of
    # pixels a second**, and the tightening above did not move it — at that
    # size the 258-entry table being rebuilt every call is most of the work,
    # so the number is a floor rather than a real scan's. What it does
    # say is the order of magnitude: **a ten-megapixel LZW scan is seconds, not
    # milliseconds**, and the call limit is sixty.
    for byte in body:
        held = (held << 8) | byte
        bits += 8
        while bits >= width:
            bits -= width
            code = (held >> bits) & ((1 << width) - 1)
            if code == 256:                       # clear
                table = [bytes([i]) for i in range(256)] + [b"", b""]
                grow = table.append
                width = 9
                next_width = 511
                previous = None
                continue
            if code == 257:                       # end of information
                return b"".join(pieces)
            if previous is None:
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                grow(previous + entry[:1])
            else:
                entry = previous + previous[:1]
                grow(entry)
            add(entry)
            previous = entry
            # One earlier than the table actually needs it — the early change.
            if len(table) >= next_width and width < 12:
                width += 1
                next_width = (1 << width) - 1
    return b"".join(pieces)


def _undo_predictor(body, predictor, width, samples, depth):
    """Horizontal differencing, undone along each row."""
    if predictor != 2:
        return body
    out = bytearray(body)
    step = max(1, depth // 8)
    row_bytes = width * samples * step
    if depth == 8:
        for start in range(0, len(out), row_bytes):
            for i in range(start + samples, min(start + row_bytes, len(out))):
                out[i] = (out[i] + out[i - samples]) & 0xFF
    elif depth == 16:
        for start in range(0, len(out), row_bytes):
            for i in range(start + samples * 2, min(start + row_bytes, len(out)), 2):
                was = (out[i - samples * 2] << 8) | out[i - samples * 2 + 1]
                now = ((out[i] << 8) | out[i + 1]) + was
                out[i] = (now >> 8) & 0xFF
                out[i + 1] = now & 0xFF
    return bytes(out)


# ------------------------------------------------------------------- colours


def _as_rgba(body, width, height, samples, depth, photometric, palette,
             extra, notes):
    pixels = width * height
    step = max(1, depth // 8)
    # Everything is one byte a sample from here on: the top byte of a wide one,
    # which is exact for anything a screen can show.
    if step > 1:
        body = body[0::step]
    planes = [body[i::samples] for i in range(samples)]
    for index, plane in enumerate(planes):
        if len(plane) < pixels:
            planes[index] = plane + bytes(pixels - len(plane))

    out = bytearray(pixels * 4)
    alpha = planes[3] if (samples >= 4 and extra) else None

    if photometric in (BLACK_IS_ZERO, WHITE_IS_ZERO, MASK):
        grey = planes[0]
        if photometric != BLACK_IS_ZERO:
            grey = bytes(255 - value for value in grey)
        out[0::4] = grey
        out[1::4] = grey
        out[2::4] = grey
        if samples >= 2 and extra:
            alpha = planes[1]
    elif photometric == RGB:
        out[0::4] = planes[0]
        out[1::4] = planes[1]
        out[2::4] = planes[2]
    elif photometric == PALETTE:
        # The palette is three runs of sixteen-bit values, reds then greens
        # then blues — not triples, which is the mistake that comes out looking
        # like somebody shuffled the colours.
        table = palette or []
        entries = len(table) // 3
        red = bytearray(pixels)
        green = bytearray(pixels)
        blue = bytearray(pixels)
        index_plane = planes[0]
        for i, value in enumerate(index_plane[:pixels]):
            if value < entries:
                red[i] = table[value] >> 8
                green[i] = table[entries + value] >> 8
                blue[i] = table[entries * 2 + value] >> 8
        out[0::4] = red
        out[1::4] = green
        out[2::4] = blue
    elif photometric == CMYK and samples >= 4:
        notes.append("Printing colours converted plainly, without a profile.")
        c, m, y, k = planes[0], planes[1], planes[2], planes[3]
        red = bytearray(pixels)
        green = bytearray(pixels)
        blue = bytearray(pixels)
        for i in range(pixels):
            black = 255 - k[i]
            red[i] = (255 - c[i]) * black // 255
            green[i] = (255 - m[i]) * black // 255
            blue[i] = (255 - y[i]) * black // 255
        out[0::4] = red
        out[1::4] = green
        out[2::4] = blue
        alpha = None
    else:
        raise TiffError(
            "A colour arrangement this reader does not know: %d." % photometric
        )

    out[3::4] = alpha if alpha is not None else b"\xff" * pixels
    return out

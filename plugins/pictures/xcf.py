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

"""GIMP's own format, read far enough to show the picture.

**What an XCF is.** A header, then a list of layers and channels reached by
pointer. Every layer holds a *hierarchy* — the picture at full size and then
half size and half again, of which only the first level is ever wanted — and a
level is a grid of **64×64 tiles**. Each tile is compressed on its own, and
inside a tile every **byte plane is compressed separately**: all the reds, then
all the greens, then all the blues, then all the alphas. That last detail is the
one that catches people out; a tile is not a run of pixels.

**What this reader does and does not do.** It composites the visible layers with
their offsets, opacity and masks, in the ordinary "normal" way, and hands back
one RGBA picture. It does **not** know the twenty-odd blend modes, and it says
so out loud rather than showing you a picture that is wrong in a way you cannot
see — the same rule the 3D viewer keeps about a model it had to cut short.
"""

from __future__ import annotations

import struct
import zlib

#: Property numbers, from GIMP's own `xcf-private.h`. Only the ones read here.
PROP_END = 0
PROP_COLORMAP = 1
PROP_OPACITY = 6
PROP_MODE = 7
PROP_VISIBLE = 8
PROP_APPLY_MASK = 11
PROP_OFFSETS = 15
PROP_COMPRESSION = 17
PROP_GROUP_ITEM = 29
PROP_FLOAT_OPACITY = 33

#: Bytes per sample for each precision GIMP writes. The two 8-bit ones are what
#: every ordinary file uses; the wider ones are read by taking the top byte of
#: each sample, which is exact for display and wrong for nothing anybody can see
#: on a screen.
SAMPLE_BYTES = {
    100: 1, 150: 1,          # 8-bit, linear and gamma
    200: 2, 250: 2,          # 16-bit integer
    500: 2, 550: 2,          # 16-bit float
    300: 4, 350: 4,          # 32-bit integer
    600: 4, 650: 4,          # 32-bit float
    700: 8, 750: 8,          # 64-bit float
}

#: Which precisions are integers, and so may be narrowed by taking a byte. A
#: float sample's top byte is exponent and sign, which is not a colour at all.
INTEGER_PRECISIONS = {100, 150, 200, 250, 300, 350}

#: Channels in a layer of each type: RGB, RGBA, grey, grey+alpha, indexed,
#: indexed+alpha.
LAYER_CHANNELS = {0: 3, 1: 4, 2: 1, 3: 2, 4: 1, 5: 2}

#: The one blend mode this composites properly. GIMP 3 writes 28 for "normal";
#: files older than 2.10 write 0 for the same thing.
NORMAL_MODES = {0, 28}

#: Bigger than this and the answer would be hundreds of megabytes over a pipe
#: for a preview nobody can see all of at once.
MAX_PIXELS = 40_000_000


class XcfError(Exception):
    """The file is not one, or is one this cannot read."""


def is_xcf(head: bytes) -> bool:
    return head[:9] == b"gimp xcf "


def read(data: bytes) -> dict:
    """The picture, as ``{width, height, pixels, notes}``.

    ``pixels`` is RGBA, eight bits a channel, top row first — the shape every
    picture in this application is in by the time the host sees it.
    """
    if not is_xcf(data):
        raise XcfError("This is not a GIMP file.")
    version = _version(data)
    at = 14
    width, height, base = struct.unpack(">III", data[at:at + 12])
    at += 12
    precision = 150
    if version >= 4:
        precision, = struct.unpack(">I", data[at:at + 4])
        at += 4
    if width * height > MAX_PIXELS:
        raise XcfError(
            "This picture is %d by %d — too big to open as a preview."
            % (width, height)
        )
    if precision not in SAMPLE_BYTES:
        raise XcfError("A precision this reader does not know: %d." % precision)

    notes: list[str] = []
    properties, at = _properties(data, at)
    compression = properties.get(PROP_COMPRESSION, b"\x00")[0]
    if compression not in (0, 1, 2):
        raise XcfError("A compression this reader does not know: %d." % compression)
    palette = _palette(properties.get(PROP_COLORMAP))

    pointer = ">Q" if version >= 11 else ">I"
    step = 8 if version >= 11 else 4
    layers, at = _pointers(data, at, pointer, step)

    canvas = bytearray(width * height * 4)
    drawn = 0
    unknown_modes = 0
    # The file lists its layers from the top down, and a picture is built from
    # the bottom up.
    for pointer_at in reversed(layers):
        layer = _layer(data, pointer_at, version, compression, precision, palette)
        if layer is None:
            continue
        if layer["mode"] not in NORMAL_MODES:
            unknown_modes += 1
        _over(canvas, width, height, layer)
        drawn += 1

    if drawn == 0:
        notes.append("Nothing in this file is visible.")
    if unknown_modes:
        notes.append(
            "%d layer(s) use a blend mode this reader does not know, and are "
            "laid on normally." % unknown_modes
        )
    if precision not in INTEGER_PRECISIONS:
        notes.append(
            "This file keeps its colours as floating point, which this reader "
            "narrows by hand; the colours may be out."
        )
    return {
        "width": width,
        "height": height,
        "pixels": canvas,
        "notes": notes,
        "layers": drawn,
        "precision": precision,
        "compression": compression,
        "version": version,
    }


def _version(data: bytes) -> int:
    mark = data[9:13]
    if mark == b"file":
        return 0
    try:
        return int(mark.lstrip(b"v"))
    except ValueError as failure:
        raise XcfError("A version this reader does not know: %r" % mark) from failure


def _pointers(data: bytes, at: int, pointer: str, step: int):
    out = []
    while True:
        value, = struct.unpack(pointer, data[at:at + step])
        at += step
        if value == 0:
            return out, at
        out.append(value)


def _properties(data: bytes, at: int):
    """Every property up to the end marker, by number."""
    out: dict[int, bytes] = {}
    while True:
        kind, length = struct.unpack(">II", data[at:at + 8])
        at += 8
        if kind == PROP_END:
            return out, at
        out[kind] = data[at:at + length]
        at += length


def _palette(body: bytes | None):
    if not body:
        return None
    count, = struct.unpack(">I", body[:4])
    return body[4:4 + count * 3]


def _layer(data, at, version, compression, precision, palette):
    """One layer as RGBA, or None where there is nothing to draw."""
    width, height, kind = struct.unpack(">III", data[at:at + 12])
    at += 12
    length, = struct.unpack(">I", data[at:at + 4])
    at += 4
    name = data[at:at + max(0, length - 1)].decode("utf-8", "replace")
    at += length
    properties, at = _properties(data, at)

    # A group holds no pixels of its own; what is in it is listed separately.
    if PROP_GROUP_ITEM in properties:
        return None
    if properties.get(PROP_VISIBLE, b"\x00\x00\x00\x01")[-1] == 0:
        return None

    pointer = ">Q" if version >= 11 else ">I"
    step = 8 if version >= 11 else 4
    hierarchy, = struct.unpack(pointer, data[at:at + step])
    mask_at, = struct.unpack(pointer, data[at + step:at + step * 2])
    if hierarchy == 0:
        return None

    channels = LAYER_CHANNELS.get(kind, 4)
    planes = _hierarchy(data, hierarchy, compression, precision, channels)
    if planes is None:
        return None
    rgba = _as_rgba(planes, width * height, kind, palette)

    opacity = 255
    if PROP_FLOAT_OPACITY in properties:
        value, = struct.unpack(">f", properties[PROP_FLOAT_OPACITY])
        opacity = max(0, min(255, round(value * 255)))
    elif PROP_OPACITY in properties:
        opacity, = struct.unpack(">I", properties[PROP_OPACITY])
        opacity = max(0, min(255, opacity))

    applies = properties.get(PROP_APPLY_MASK, b"\x00\x00\x00\x00")[-1] == 1
    if mask_at and applies:
        mask = _mask(data, mask_at, version, compression, precision, width, height)
        if mask is not None:
            for i, value in enumerate(mask):
                if value != 255:
                    rgba[i * 4 + 3] = rgba[i * 4 + 3] * value // 255

    if opacity != 255:
        for i in range(3, len(rgba), 4):
            rgba[i] = rgba[i] * opacity // 255

    offsets = properties.get(PROP_OFFSETS, b"\x00" * 8)
    x, y = struct.unpack(">ii", offsets[:8])
    mode = 0
    if PROP_MODE in properties:
        mode, = struct.unpack(">I", properties[PROP_MODE])
    return {
        "name": name,
        "width": width,
        "height": height,
        "x": x,
        "y": y,
        "mode": mode,
        "pixels": rgba,
    }


def _mask(data, at, version, compression, precision, width, height):
    """A layer mask, as one byte a pixel."""
    mask_width, mask_height = struct.unpack(">II", data[at:at + 8])
    if (mask_width, mask_height) != (width, height):
        return None
    step = 8 if version >= 11 else 4
    pointer = ">Q" if version >= 11 else ">I"
    length, = struct.unpack(">I", data[at + 8:at + 12])
    cursor = at + 12 + length
    _, cursor = _properties(data, cursor)
    hierarchy, = struct.unpack(pointer, data[cursor:cursor + step])
    if hierarchy == 0:
        return None
    planes = _hierarchy(data, hierarchy, compression, precision, 1)
    return None if planes is None else planes[0]


def _hierarchy(data, at, compression, precision, channels):
    """The top level of a hierarchy, as one byte plane per channel."""
    width, height, bpp = struct.unpack(">III", data[at:at + 12])
    level, = struct.unpack(">Q", data[at + 12:at + 20])
    if level == 0 or width == 0 or height == 0:
        return None
    sample = SAMPLE_BYTES[precision]
    # What the file says, not what the layer type implies: they agree, and where
    # they do not the file wins.
    planes_per_pixel = bpp
    take = [c * sample for c in range(channels)]
    return _level(data, level, width, height, planes_per_pixel, take, compression)


def _level(data, at, width, height, planes_per_pixel, take, compression):
    """Every tile of one level, unpacked into one byte plane per wanted byte."""
    level_width, level_height = struct.unpack(">II", data[at:at + 8])
    at += 8
    tiles = []
    while True:
        pointer, = struct.unpack(">Q", data[at:at + 8])
        at += 8
        if pointer == 0:
            break
        tiles.append(pointer)

    out = [bytearray(width * height) for _ in take]
    across = (width + 63) // 64
    for index, pointer in enumerate(tiles):
        left = (index % across) * 64
        top = (index // across) * 64
        tile_width = min(64, width - left)
        tile_height = min(64, height - top)
        if tile_width <= 0 or tile_height <= 0:
            continue
        planes = _tile(
            data, pointer, tile_width * tile_height, planes_per_pixel, compression
        )
        for slot, plane_index in enumerate(take):
            if plane_index >= len(planes):
                continue
            plane = planes[plane_index]
            target = out[slot]
            for row in range(tile_height):
                start = (top + row) * width + left
                target[start:start + tile_width] = (
                    plane[row * tile_width:(row + 1) * tile_width]
                )
    return out


def _tile(data, at, pixels, planes_per_pixel, compression):
    """One tile, as its byte planes.

    Each plane is the same byte of every pixel in the tile, which is why a
    picture comes out of this in strips of one colour rather than in pixels.
    """
    if compression == 1:
        planes = []
        for _ in range(planes_per_pixel):
            plane, at = _unpack_rle(data, at, pixels)
            planes.append(plane)
        return planes
    if compression == 2:
        # The length is not written down: the zlib stream itself says where it
        # ends, so it is fed until it stops asking.
        machine = zlib.decompressobj()
        body = machine.decompress(data[at:at + pixels * planes_per_pixel * 2 + 64])
        while len(body) < pixels * planes_per_pixel and not machine.eof:
            more = machine.decompress(b"")
            if not more:
                break
            body += more
        return [
            body[i * pixels:(i + 1) * pixels] for i in range(planes_per_pixel)
        ]
    return [
        data[at + i * pixels:at + (i + 1) * pixels] for i in range(planes_per_pixel)
    ]


def _unpack_rle(data, at, want):
    """One plane of one tile.

    Four opcodes, and the two long ones are what a plain run-length reader gets
    wrong: 127 and 128 are not runs of their own length, they are the escape to
    a sixteen-bit one.
    """
    out = bytearray()
    while len(out) < want:
        code = data[at]
        at += 1
        if code <= 126:
            out += data[at:at + 1] * (code + 1)
            at += 1
        elif code == 127:
            length = (data[at] << 8) | data[at + 1]
            at += 2
            out += data[at:at + 1] * length
            at += 1
        elif code == 128:
            length = (data[at] << 8) | data[at + 1]
            at += 2
            out += data[at:at + length]
            at += length
        else:
            length = 256 - code
            out += data[at:at + length]
            at += length
    return out, at


def _as_rgba(planes, pixels, kind, palette):
    """The planes of one layer, interleaved into RGBA."""
    out = bytearray(pixels * 4)
    if kind in (0, 1):                      # RGB, RGBA
        out[0::4] = planes[0]
        out[1::4] = planes[1]
        out[2::4] = planes[2]
        out[3::4] = planes[3] if kind == 1 else b"\xff" * pixels
    elif kind in (2, 3):                    # grey, grey with alpha
        grey = planes[0]
        out[0::4] = grey
        out[1::4] = grey
        out[2::4] = grey
        out[3::4] = planes[1] if kind == 3 else b"\xff" * pixels
    else:                                   # indexed, with and without alpha
        table = palette or b""
        entries = len(table) // 3
        index = planes[0]
        red = bytearray(pixels)
        green = bytearray(pixels)
        blue = bytearray(pixels)
        for i, value in enumerate(index):
            if value < entries:
                red[i] = table[value * 3]
                green[i] = table[value * 3 + 1]
                blue[i] = table[value * 3 + 2]
        out[0::4] = red
        out[1::4] = green
        out[2::4] = blue
        out[3::4] = planes[1] if kind == 5 else b"\xff" * pixels
    return out


def _over(canvas, width, height, layer):
    """One layer laid on what is already there, in the ordinary way.

    Two paths, and the reason is speed rather than taste: a layer that is opaque
    everywhere is rows of bytes and is copied as rows, which is the difference
    between a tenth of a second and several. Only where alpha is really in play
    does this go pixel by pixel.
    """
    pixels = layer["pixels"]
    lw, lh = layer["width"], layer["height"]
    x0, y0 = layer["x"], layer["y"]
    alpha = pixels[3::4]
    opaque = alpha.count(255) == len(alpha)

    for row in range(lh):
        y = y0 + row
        if not (0 <= y < height):
            continue
        left = max(0, -x0)
        right = min(lw, width - x0)
        if right <= left:
            continue
        source = (row * lw + left) * 4
        target = ((y * width) + x0 + left) * 4
        count = right - left
        if opaque:
            canvas[target:target + count * 4] = pixels[source:source + count * 4]
            continue
        for i in range(count):
            s = source + i * 4
            a = pixels[s + 3]
            if a == 0:
                continue
            t = target + i * 4
            if a == 255:
                canvas[t:t + 4] = pixels[s:s + 4]
                continue
            was = canvas[t + 3]
            out = a + was * (255 - a) // 255
            for c in range(3):
                canvas[t + c] = (
                    pixels[s + c] * a + canvas[t + c] * was * (255 - a) // 255
                ) // max(1, out)
            canvas[t + 3] = out

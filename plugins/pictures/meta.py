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

"""What a photograph says about itself.

**Every road ends at the same place.** A JPEG carries EXIF in an `APP1` segment,
a TIFF *is* the same directory with a file header in front of it, a PNG keeps it
in an `eXIf` chunk, a WebP in a RIFF chunk of that name, and a HEIC in an item
inside its `meta` box — but all five hand over the identical thing: a TIFF
directory. So the container readers below are each a dozen lines whose only job
is to find those bytes, and one reader understands them.

**Only a fraction of it is shown, and choosing which is the work.** EXIF has
several hundred tags and a panel listing all of them is a hex dump with names on
it. What is here is what somebody looking at a photograph wants: which camera
and lens, the exposure, when, where, and who made it — with everything the file
still holds counted in a line at the foot rather than silently dropped.

Nothing is decoded. The picture is drawn by the machine's own engine or by the
readers next door; this only ever reads a header.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any, Dict, List, Optional, Tuple

# -- the TIFF directory, which is what EXIF actually is ---------------------

#: Bytes per component, by TIFF type. 0 for the ones nothing here reads.
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_INTEROP_IFD = 0xA005


class Exif:
    """One file's tags, by directory."""

    def __init__(self) -> None:
        self.main: Dict[int, Any] = {}
        self.exif: Dict[int, Any] = {}
        self.gps: Dict[int, Any] = {}
        self.thumbnail: Dict[int, Any] = {}

    @property
    def count(self) -> int:
        return len(self.main) + len(self.exif) + len(self.gps)

    def get(self, tag: int, where: str = "exif") -> Any:
        table = {"main": self.main, "exif": self.exif, "gps": self.gps}[where]
        return table.get(tag)

    def any(self, *tags: Tuple[str, int]) -> Any:
        for where, tag in tags:
            value = self.get(tag, where)
            if value not in (None, ""):
                return value
        return None


def read_tiff_tags(data: bytes, base: int = 0) -> Optional[Exif]:
    """A TIFF header and every directory under it, as far as it can be read."""
    if len(data) < base + 8:
        return None
    order = data[base:base + 2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        return None
    magic, first = struct.unpack_from(endian + "HI", data, base + 2)
    if magic not in (42, 43):  # 43 is BigTIFF, whose directories differ
        return None
    if magic == 43:
        return None

    found = Exif()
    seen: set = set()

    def directory(offset: int) -> Dict[int, Any]:
        out: Dict[int, Any] = {}
        at = base + offset
        if offset in seen or at + 2 > len(data) or offset <= 0:
            return out
        seen.add(offset)
        (count,) = struct.unpack_from(endian + "H", data, at)
        if count > 4096:
            return out
        for i in range(count):
            entry = at + 2 + i * 12
            if entry + 12 > len(data):
                break
            tag, kind, length = struct.unpack_from(endian + "HHI", data, entry)
            value = _value(data, base, endian, kind, length, entry + 8)
            if value is not None:
                out[tag] = value
        return out

    found.main = directory(first)
    for tag, name in ((_EXIF_IFD, "exif"), (_GPS_IFD, "gps")):
        pointer = found.main.get(tag)
        if isinstance(pointer, (int, float)):
            setattr(found, name, directory(int(pointer)))
    # An IFD1 holds the thumbnail's own tags; counted, never shown.
    at = base + first
    if at + 2 <= len(data):
        (count,) = struct.unpack_from(endian + "H", data, at)
        tail = at + 2 + count * 12
        if tail + 4 <= len(data):
            (nxt,) = struct.unpack_from(endian + "I", data, tail)
            if nxt:
                found.thumbnail = directory(nxt)
    return found


def _value(data: bytes, base: int, endian: str, kind: int, count: int, at: int) -> Any:
    size = _SIZES.get(kind, 0)
    if size == 0 or count > 1 << 20:
        return None
    total = size * count
    if total > 4:
        (offset,) = struct.unpack_from(endian + "I", data, at)
        at = base + offset
    if at < 0 or at + total > len(data):
        return None
    raw = data[at:at + total]

    if kind == 2:  # ASCII
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    if kind in (1, 6, 7):  # BYTE, SBYTE, UNDEFINED
        return raw
    codes = {3: "H", 8: "h", 4: "I", 9: "i", 11: "f", 12: "d"}
    if kind in codes:
        values = list(struct.unpack(endian + codes[kind] * count, raw))
        return values[0] if count == 1 else values
    if kind in (5, 10):  # RATIONAL, SRATIONAL
        code = "II" if kind == 5 else "ii"
        pairs = struct.unpack(endian + code * count, raw)
        out = [(pairs[i], pairs[i + 1]) for i in range(0, len(pairs), 2)]
        return out[0] if count == 1 else out
    return None


# -- finding those bytes in each container ---------------------------------


class Picture:
    """What one file turned out to be, and what it had to say."""

    def __init__(self, kind: str = ""):
        self.kind = kind
        self.width = 0
        self.height = 0
        self.depth = ""
        self.exif: Optional[Exif] = None
        self.xmp: str = ""
        self.text: List[Tuple[str, str]] = []
        self.notes: List[str] = []


def read(data: bytes) -> Picture:
    """Whatever this file will say, whichever of the formats it is."""
    if data[:2] == b"\xff\xd8":
        return _jpeg(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png(data)
    if data[:2] in (b"II", b"MM") and len(data) > 4:
        return _tiff(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    if data[4:8] == b"ftyp":
        return _heif(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _gif(data)
    if data[:2] == b"BM":
        return _bmp(data)
    if data[:4] == b"8BPS":
        return _psd(data)
    if data[:9] == b"gimp xcf ":
        return _xcf(data)
    return Picture()


def _jpeg(data: bytes) -> Picture:
    found = Picture("JPEG")
    at = 2
    n = len(data)
    while at + 4 <= n:
        if data[at] != 0xFF:
            at += 1
            continue
        marker = data[at + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        if marker == 0xD9 or marker == 0xDA:  # end, or the scan itself
            break
        (length,) = struct.unpack_from(">H", data, at + 2)
        body = data[at + 4:at + 2 + length]
        if marker == 0xE1:
            if body[:6] == b"Exif\0\0":
                found.exif = found.exif or read_tiff_tags(body, 6)
            elif body[:28] == b"http://ns.adobe.com/xap/1.0/" and not found.xmp:
                found.xmp = body[29:].decode("utf-8", "replace")
        elif marker == 0xFE:
            text = body.decode("utf-8", "replace").strip()
            if text:
                found.text.append(("Comment", text))
        elif marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if len(body) >= 6:
                precision, height, width, components = struct.unpack_from(">BHHB", body, 0)
                found.height, found.width = height, width
                found.depth = "%d bits, %s" % (
                    precision * components,
                    {1: "greyscale", 3: "colour", 4: "CMYK"}.get(components, "%d channels" % components),
                )
                if marker in (0xC2, 0xC6, 0xCA, 0xCE):
                    found.notes.append("progressive")
        at += 2 + length
    return found


def _png(data: bytes) -> Picture:
    found = Picture("PNG")
    at = 8
    n = len(data)
    while at + 8 <= n:
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4:at + 8]
        body = data[at + 8:at + 8 + length]
        if length > n:
            break
        if kind == b"IHDR" and len(body) >= 13:
            width, height, bits, colour = struct.unpack_from(">IIBB", body, 0)
            found.width, found.height = width, height
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour, 1)
            found.depth = "%d bits, %s" % (
                bits * channels,
                {0: "greyscale", 2: "colour", 3: "palette",
                 4: "greyscale with alpha", 6: "colour with alpha"}.get(colour, "?"),
            )
            if body[12] == 1:
                found.notes.append("interlaced")
        elif kind == b"eXIf":
            found.exif = found.exif or read_tiff_tags(body)
        elif kind in (b"tEXt", b"zTXt", b"iTXt"):
            pair = _png_text(kind, body)
            if pair:
                if pair[0].lower() == "xml:com.adobe.xmp" and not found.xmp:
                    found.xmp = pair[1]
                else:
                    found.text.append(pair)
        elif kind == b"pHYs" and len(body) >= 9:
            x, y, unit = struct.unpack_from(">IIB", body, 0)
            if unit == 1 and x and y:
                found.text.append(("Resolution", "%d × %d dpi"
                                   % (round(x * 0.0254), round(y * 0.0254))))
        elif kind == b"IDAT":
            break  # everything worth reading stands before the pixels
        at += 12 + length
    return found


def _png_text(kind: bytes, body: bytes) -> Optional[Tuple[str, str]]:
    try:
        if kind == b"tEXt":
            key, value = body.split(b"\0", 1)
            return key.decode("latin-1"), value.decode("latin-1", "replace")
        if kind == b"zTXt":
            key, rest = body.split(b"\0", 1)
            return key.decode("latin-1"), zlib.decompress(rest[1:]).decode("utf-8", "replace")
        key, rest = body.split(b"\0", 1)
        compressed, method = rest[0], rest[1]
        _language, rest = rest[2:].split(b"\0", 1)
        _translated, text = rest.split(b"\0", 1)
        if compressed and method == 0:
            text = zlib.decompress(text)
        return key.decode("utf-8", "replace"), text.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a damaged chunk is one missing fact
        return None


def _tiff(data: bytes) -> Picture:
    found = Picture("TIFF")
    found.exif = read_tiff_tags(data)
    if found.exif:
        width = found.exif.main.get(0x0100)
        height = found.exif.main.get(0x0101)
        bits = found.exif.main.get(0x0102)
        if isinstance(width, (int, float)):
            found.width = int(width)
        if isinstance(height, (int, float)):
            found.height = int(height)
        if isinstance(bits, list):
            found.depth = "%d bits" % sum(int(b) for b in bits)
        elif isinstance(bits, (int, float)):
            found.depth = "%d bits" % int(bits)
    return found


def _webp(data: bytes) -> Picture:
    found = Picture("WebP")
    at = 12
    n = min(len(data), struct.unpack_from("<I", data, 4)[0] + 8)
    while at + 8 <= n:
        kind = data[at:at + 4]
        (length,) = struct.unpack_from("<I", data, at + 4)
        body = data[at + 8:at + 8 + length]
        if kind == b"VP8X" and len(body) >= 10:
            found.width = int.from_bytes(body[4:7], "little") + 1
            found.height = int.from_bytes(body[7:10], "little") + 1
            if body[0] & 0x02:
                found.notes.append("animated")
        elif kind == b"VP8 " and len(body) >= 10 and not found.width:
            found.width = struct.unpack_from("<H", body, 6)[0] & 0x3FFF
            found.height = struct.unpack_from("<H", body, 8)[0] & 0x3FFF
        elif kind == b"VP8L" and len(body) >= 5 and not found.width:
            bits = int.from_bytes(body[1:5], "little")
            found.width = (bits & 0x3FFF) + 1
            found.height = ((bits >> 14) & 0x3FFF) + 1
        elif kind == b"EXIF":
            found.exif = found.exif or read_tiff_tags(
                body[6:] if body[:6] == b"Exif\0\0" else body)
        elif kind == b"XMP " and not found.xmp:
            found.xmp = body.decode("utf-8", "replace")
        at += 8 + length + (length & 1)
    return found


def _heif(data: bytes) -> Picture:
    """A HEIC or an AVIF: the EXIF is an *item* inside the `meta` box.

    Three boxes have to be read together and none of them is where a reader
    would first look: `iinf` says which item is the EXIF, `iloc` says where in
    the file it sits, and `ispe` — buried in the properties — is the only place
    the picture's size is written down at all.
    """
    brand = data[8:12].decode("latin-1", "replace").strip()
    found = Picture("AVIF" if brand.startswith("avi") else "HEIF")
    meta = _box(data, b"meta", 0, len(data))
    if meta is None:
        return found
    start, end = meta
    start += 4  # meta is a full box: a version and its flags

    # Which item is the EXIF, by its four-character type.
    exif_item = None
    iinf = _box(data, b"iinf", start, end)
    if iinf:
        at, stop = iinf
        version = data[at]
        at += 4
        at += 2 if version == 0 else 4
        while at + 8 <= stop:
            (length,) = struct.unpack_from(">I", data, at)
            if length < 8 or at + length > stop:
                break
            if data[at + 4:at + 8] == b"infe":
                infe = data[at + 8:at + length]
                if infe and infe[0] >= 2:
                    item = struct.unpack_from(">H", infe, 4)[0] if infe[0] == 2 \
                        else struct.unpack_from(">I", infe, 4)[0]
                    kind = infe[8:12] if infe[0] == 2 else infe[10:14]
                    if kind == b"Exif":
                        exif_item = item
            at += length

    if exif_item is not None:
        placed = _iloc(data, start, end).get(exif_item)
        if placed:
            offset, length = placed
            body = data[offset:offset + length]
            # An EXIF item begins with the offset to the TIFF header inside it.
            if len(body) > 4:
                skip = struct.unpack_from(">I", body, 0)[0] + 4
                found.exif = read_tiff_tags(body, skip if skip < len(body) else 0)

    ispe = _find(data, b"ispe", start, end)
    if ispe:
        at, stop = ispe
        if at + 12 <= stop:
            found.width, found.height = struct.unpack_from(">II", data, at + 4)
    return found


def _iloc(data: bytes, start: int, end: int) -> Dict[int, Tuple[int, int]]:
    """Item id → where it is in the file. Only the ordinary construction."""
    found: Dict[int, Tuple[int, int]] = {}
    box = _box(data, b"iloc", start, end)
    if box is None:
        return found
    at, stop = box
    version = data[at]
    at += 4
    sizes = data[at]
    lengths = data[at + 1]
    offset_size, length_size = sizes >> 4, sizes & 15
    base_size, index_size = lengths >> 4, lengths & 15
    at += 2
    if version < 2:
        count = struct.unpack_from(">H", data, at)[0]
        at += 2
    else:
        count = struct.unpack_from(">I", data, at)[0]
        at += 4
    for _ in range(min(count, 4096)):
        if at + 8 > stop:
            break
        if version < 2:
            item = struct.unpack_from(">H", data, at)[0]
            at += 2
        else:
            item = struct.unpack_from(">I", data, at)[0]
            at += 4
        if version >= 1:
            at += 2  # construction method
        at += 2  # data reference index
        at += base_size
        extents = struct.unpack_from(">H", data, at)[0]
        at += 2
        first = None
        total = 0
        for _extent in range(min(extents, 64)):
            at += index_size if version >= 1 and index_size else 0
            offset = _uint(data, at, offset_size)
            at += offset_size
            length = _uint(data, at, length_size)
            at += length_size
            if first is None:
                first = offset
            total += length
        if first is not None:
            found[item] = (first, total)
    return found


def _uint(data: bytes, at: int, size: int) -> int:
    if size == 0 or at + size > len(data):
        return 0
    return int.from_bytes(data[at:at + size], "big")


def _box(data: bytes, want: bytes, start: int, end: int) -> Optional[Tuple[int, int]]:
    """The body of the first [want] box among the children of this range."""
    at = start
    while at + 8 <= end:
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4:at + 8]
        body = at + 8
        if length == 1:
            if at + 16 > end:
                return None
            length = struct.unpack_from(">Q", data, at + 8)[0]
            body = at + 16
        if length == 0:
            length = end - at
        if length < 8 or at + length > end:
            return None
        if kind == want:
            return body, at + length
        at += length
    return None


def _find(data: bytes, want: bytes, start: int, end: int,
          depth: int = 0) -> Optional[Tuple[int, int]]:
    """[want] anywhere under this range — for a box nested three deep."""
    if depth > 6:
        return None
    at = start
    while at + 8 <= end:
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4:at + 8]
        body = at + 8
        if length == 1:
            if at + 16 > end:
                return None
            length = struct.unpack_from(">Q", data, at + 8)[0]
            body = at + 16
        if length == 0:
            length = end - at
        if length < 8 or at + length > end:
            return None
        if kind == want:
            return body, at + length
        if kind in (b"iprp", b"ipco", b"meta", b"moov", b"trak", b"mdia"):
            inner = _find(data, want, body + (4 if kind == b"meta" else 0),
                          at + length, depth + 1)
            if inner:
                return inner
        at += length
    return None


def _gif(data: bytes) -> Picture:
    found = Picture("GIF")
    if len(data) >= 10:
        found.width, found.height = struct.unpack_from("<HH", data, 6)
        found.depth = "%d colours" % (2 ** ((data[10] & 7) + 1))
    return found


def _bmp(data: bytes) -> Picture:
    found = Picture("BMP")
    if len(data) >= 30:
        found.width, found.height = struct.unpack_from("<ii", data, 18)
        found.height = abs(found.height)
        found.depth = "%d bits" % struct.unpack_from("<H", data, 28)[0]
    return found


def _psd(data: bytes) -> Picture:
    found = Picture("Photoshop")
    if len(data) >= 26:
        channels, height, width, depth, mode = struct.unpack_from(">HIIHH", data, 12)
        found.width, found.height = width, height
        found.depth = "%d bits, %d channel(s), %s" % (
            depth, channels,
            {0: "bitmap", 1: "greyscale", 2: "indexed", 3: "RGB", 4: "CMYK",
             7: "multichannel", 8: "duotone", 9: "Lab"}.get(mode, "mode %d" % mode),
        )
    return found


def _xcf(data: bytes) -> Picture:
    found = Picture("GIMP")
    if len(data) >= 22:
        found.width, found.height = struct.unpack_from(">II", data, 14)
        found.depth = {0: "RGB", 1: "greyscale", 2: "indexed"}.get(
            struct.unpack_from(">I", data, 22)[0] if len(data) >= 26 else -1, "")
    return found


# -- turning tags into sentences -------------------------------------------


def rational(value: Any) -> Optional[float]:
    if isinstance(value, tuple) and len(value) == 2 and value[1]:
        return value[0] / value[1]
    if isinstance(value, (int, float)):
        return float(value)
    return None


def shutter(value: Any) -> str:
    seconds = rational(value)
    if seconds is None or seconds <= 0:
        return ""
    if seconds >= 1:
        return ("%g s" % round(seconds, 1))
    return "1/%d s" % round(1 / seconds)


def aperture(value: Any) -> str:
    f = rational(value)
    return "" if not f else "f/%g" % round(f, 1)


def millimetres(value: Any) -> str:
    mm = rational(value)
    return "" if not mm else "%g mm" % round(mm, 1)


def degrees(value: Any, reference: Any) -> str:
    """A GPS coordinate as a number somebody can paste into a map."""
    if not isinstance(value, list) or len(value) < 3:
        return ""
    parts = [rational(v) or 0 for v in value[:3]]
    total = parts[0] + parts[1] / 60 + parts[2] / 3600
    if isinstance(reference, str) and reference.upper() in ("S", "W"):
        total = -total
    return "%.6f" % total


#: EXIF's own numberings, in words. Only the ones a photograph really uses.
FLASH = {0: "did not fire", 1: "fired", 5: "fired, no return", 7: "fired, returned",
         9: "fired, compulsory", 16: "off", 24: "did not fire, auto",
         25: "fired, auto", 29: "fired, auto, no return", 31: "fired, auto, returned",
         32: "no flash function", 89: "fired, auto, red-eye"}
METERING = {0: "unknown", 1: "average", 2: "centre-weighted", 3: "spot",
            4: "multi-spot", 5: "pattern", 6: "partial", 255: "other"}
PROGRAM = {0: "not defined", 1: "manual", 2: "normal", 3: "aperture priority",
           4: "shutter priority", 5: "creative", 6: "action", 7: "portrait",
           8: "landscape"}
WHITE_BALANCE = {0: "automatic", 1: "manual"}
ORIENTATION = {1: "as it stands", 2: "mirrored", 3: "turned round",
               4: "mirrored and turned round", 5: "mirrored and turned left",
               6: "turned right", 7: "mirrored and turned right",
               8: "turned left"}


def named(table: Dict[int, str], value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return table.get(int(value), "")


def when(value: Any, offset: Any = None) -> str:
    """`2026:08:30 17:04:11` as a date somebody wrote rather than a format."""
    if not isinstance(value, str) or len(value) < 19:
        return value if isinstance(value, str) else ""
    date = value[:10].replace(":", "-")
    text = "%s %s" % (date, value[11:19])
    if isinstance(offset, str) and offset.strip():
        text += " " + offset.strip()
    return text


def size(count: int) -> str:
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if count >= step:
            return "%.1f %s" % (count / step, unit)
    return "%d bytes" % count


def megapixels(width: int, height: int) -> str:
    total = width * height
    return "" if total <= 0 else "%.1f Mpx" % (total / 1e6)


__all__ = ["Exif", "Picture", "read", "read_tiff_tags"]

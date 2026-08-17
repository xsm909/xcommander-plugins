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

"""Pixels back into a picture the host can decode.

**Why a picture at all, rather than the pixels themselves.** The host draws
whatever a plugin sends as `image`, and it decodes PNG on every platform there
is. Sending raw bytes would need a new shape in the contract *and* would be
four bytes a pixel over the pipe where this is closer to one. The whole cost is
`zlib`, which the shipped Python has.

Rows are written unfiltered. A filter would compress a photograph better, but
every filter is arithmetic per byte, and per byte in Python is seconds for a
picture this size — see the note in the module that measured it.
"""

from __future__ import annotations

import struct
import zlib


def write(width: int, height: int, rgba: bytes, level: int = 6) -> bytes:
    """One RGBA picture as PNG bytes."""
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        raw += b"\x00"
        raw += rgba[row * stride:(row + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )

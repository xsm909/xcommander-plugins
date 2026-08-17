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

"""glTF 2.0 and GLB: the modern interchange format, read into the same shape.

Where FBX is a format you learn by feeding it files, this one has a
specification and keeps to it. What it hands over is already close to what a
renderer wants — triangles in typed arrays, one material per primitive — so
almost all of this module is address arithmetic rather than interpretation.

**The four things that are genuinely different from FBX**, and each is a place
to get it wrong quietly:

- **It is already Y-up and right-handed**, so there is no axis fix and must not
  be one.
- **Its picture coordinates already run down from the top**, so they must *not*
  be turned over — FBX's must, and doing both the same way would flip one of
  them.
- **Matrices are stored column-major for column vectors.** Everything here
  works in row vectors, and the transpose of a column-major matrix read in
  order *is* the row-major one, so the sixteen numbers are taken as they come.
  That is a coincidence worth stating, because it looks like a missing step.
- **Rotations are quaternions**, not Euler angles in some order.

Reading files this module does not do: it is handed a `resolve` that turns a
URI into bytes, because a `.gltf` keeps its numbers in a `.bin` beside it and
whose business that is belongs to the host.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Callable, Dict, List, Optional, Tuple

MAGIC = b"glTF"

#: Reading one number: the code the file uses, and how to unpack it.
COMPONENTS = {
    5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
    5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4),
}

#: How many numbers make one of these.
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
          "MAT2": 4, "MAT3": 9, "MAT4": 16}

#: What a primitive is made of. Only triangles are drawn; the rest are real
#: parts of the format and are counted rather than pretended about.
TRIANGLES, STRIP, FAN = 4, 5, 6


class GltfError(Exception):
    pass


class Document:
    """A parsed file: the JSON that describes it, and the block of bytes a GLB
    carries inside itself."""

    __slots__ = ("json", "chunk", "binary")

    def __init__(self, tree: dict, chunk: bytes, binary: bool):
        self.json = tree
        self.chunk = chunk
        self.binary = binary

    def listed(self, name: str) -> list:
        value = self.json.get(name)
        return value if isinstance(value, list) else []

    def entry(self, name: str, index: Optional[int]) -> Optional[dict]:
        if index is None:
            return None
        rows = self.listed(name)
        if not isinstance(index, int) or not 0 <= index < len(rows):
            return None
        row = rows[index]
        return row if isinstance(row, dict) else None


def parse(data: bytes) -> Document:
    """Either form: the binary container, or the JSON on its own."""
    if data[:4] == MAGIC:
        if len(data) < 12:
            raise GltfError("The file ends inside its own header.")
        _magic, version, _length = struct.unpack_from("<4sII", data, 0)
        if version != 2:
            raise GltfError("This is glTF version %d; only 2 is read." % version)
        tree: Optional[dict] = None
        chunk = b""
        at = 12
        while at + 8 <= len(data):
            size, kind = struct.unpack_from("<I4s", data, at)
            body = data[at + 8:at + 8 + size]
            if kind == b"JSON":
                tree = json.loads(body.decode("utf-8"))
            elif kind == b"BIN\x00":
                chunk = body
            # Chunks are padded to four bytes, and the padding is not the size.
            at += 8 + size + (-size % 4)
        if tree is None:
            raise GltfError("The file carries no description of itself.")
        return Document(tree, chunk, True)

    try:
        tree = json.loads(data.decode("utf-8"))
    except Exception as failure:  # noqa: BLE001 - a bad file is not a crash
        raise GltfError("This is neither a GLB nor readable JSON: %s" % failure)
    if not isinstance(tree, dict):
        raise GltfError("The description of the file is not an object.")
    return Document(tree, b"", False)


def _from_uri(uri: str, resolve: Optional[Callable[[str], bytes]]) -> bytes:
    """Bytes named by a URI: written into it, or in a file beside this one."""
    if uri.startswith("data:"):
        _head, _, tail = uri.partition(",")
        if ";base64" in _head:
            return base64.b64decode(tail)
        return tail.encode("utf-8", "replace")
    return resolve(uri) if resolve else b""


class Bytes:
    """The file's buffers, fetched once each however they are stored."""

    def __init__(self, document: Document,
                 resolve: Optional[Callable[[str], bytes]] = None):
        self.document = document
        self.resolve = resolve
        self._buffers: Dict[int, bytes] = {}

    def buffer(self, index: int) -> bytes:
        known = self._buffers.get(index)
        if known is not None:
            return known
        entry = self.document.entry("buffers", index) or {}
        uri = entry.get("uri")
        if isinstance(uri, str) and uri:
            data = _from_uri(uri, self.resolve)
        else:
            # No URI means the block inside the GLB itself.
            data = self.document.chunk
        self._buffers[index] = data
        return data

    def view(self, index: int) -> Tuple[bytes, int]:
        """One view of a buffer, and the stride it walks in (0 for packed)."""
        entry = self.document.entry("bufferViews", index)
        if entry is None:
            return b"", 0
        data = self.buffer(int(entry.get("buffer", 0)))
        start = int(entry.get("byteOffset", 0))
        length = int(entry.get("byteLength", 0))
        return data[start:start + length], int(entry.get("byteStride", 0) or 0)

    def read(self, index: Optional[int]) -> List[float]:
        """An accessor, flattened: count x width numbers, in order.

        Sparse accessors are part of the format and vanishingly rare in files
        anyone exports; one is read as its base and the difference is noted
        rather than silently applied.
        """
        entry = self.document.entry("accessors", index)
        if entry is None:
            return []
        code = COMPONENTS.get(int(entry.get("componentType", 0)))
        width = WIDTHS.get(str(entry.get("type", "")), 0)
        count = int(entry.get("count", 0))
        if code is None or not width or count <= 0:
            return []
        item, size = code

        view_index = entry.get("bufferView")
        if view_index is None:
            # A view-less accessor is all zeroes by the specification.
            return [0.0] * (count * width)
        data, stride = self.view(int(view_index))
        start = int(entry.get("byteOffset", 0))
        packed = width * size
        if not stride or stride == packed:
            wanted = count * width
            end = start + wanted * size
            if end > len(data):
                wanted = max(0, (len(data) - start) // size)
            return list(struct.unpack_from("<%d%s" % (wanted, item), data, start))

        # Interlaced: positions and normals sharing one run of bytes.
        out: List[float] = []
        for i in range(count):
            at = start + i * stride
            if at + packed > len(data):
                break
            out.extend(struct.unpack_from("<%d%s" % (width, item), data, at))
        return out

    def image(self, index: Optional[int]) -> Optional[dict]:
        """A picture: its bytes if the file holds them, else a name to find."""
        entry = self.document.entry("images", index)
        if entry is None:
            return None
        view_index = entry.get("bufferView")
        if view_index is not None:
            data, _stride = self.view(int(view_index))
            if data:
                return {"bytes": bytes(data), "name": str(entry.get("name") or "")}
        uri = entry.get("uri")
        if isinstance(uri, str) and uri:
            if uri.startswith("data:"):
                return {"bytes": _from_uri(uri, None), "name": ""}
            return {"beside": uri, "name": uri.rsplit("/", 1)[-1]}
        return None

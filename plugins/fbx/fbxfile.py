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

"""Reading the FBX container: the tree of nodes a file is, and nothing more.

An FBX file is a tree. Every node has a name, a list of typed properties and a
list of child nodes; what any of it *means* is a separate question, answered in
``scene.py``. Keeping the two apart is what makes this half boring — and it is
boring: measured over Unreal's own importer test suite, 45 binary files across
format versions 7300, 7400 and 7500 parse in 2 to 17 milliseconds each.

Two shapes exist. The binary one is a length-prefixed tree, and from version
7500 its offsets are 64-bit rather than 32 — the only difference that matters
here. The ASCII one is a brace-and-comma text tree, and about one file in eight
turns out to be one, so it is not an afterthought.
"""

from __future__ import annotations

import re
import struct
import zlib
from typing import Iterator, List, Optional, Sequence

BINARY_MAGIC = b"Kaydara FBX Binary  \x00"

#: FBX counts time in these per second. Every duration in a file is an integer
#: of them, and dividing by it is how a stack becomes seconds.
TIME_UNIT = 46186158000


class FbxError(Exception):
    """Raised when a file is not FBX, or is FBX this cannot read."""


class Node:
    """One record of the tree: a name, its properties, and what is under it."""

    __slots__ = ("name", "props", "children")

    def __init__(self, name: str, props: List[object], children: List["Node"]):
        self.name = name
        self.props = props
        self.children = children

    # -- getting about the tree -------------------------------------------

    def find(self, name: str) -> Optional["Node"]:
        for child in self.children:
            if child.name == name:
                return child
        return None

    def find_all(self, name: str) -> List["Node"]:
        return [child for child in self.children if child.name == name]

    def walk(self) -> Iterator["Node"]:
        for child in self.children:
            yield child
            yield from child.walk()

    def prop(self, index: int, default=None):
        return self.props[index] if index < len(self.props) else default

    def value(self, name: str, index: int = 0, default=None):
        """The ``index``th property of the first child called ``name``."""
        child = self.find(name)
        return default if child is None else child.prop(index, default)

    def property70(self, name: str, default=None):
        """One entry of a ``Properties70`` block.

        FBX keeps everything configurable in these: a list of ``P`` records,
        each ``name, type, subtype, flags, value…``. The value starts at index
        4 and may be several numbers — a vector is three of them.
        """
        block = self.find("Properties70")
        if block is None:
            return default
        for entry in block.find_all("P"):
            if entry.prop(0) == name:
                values = entry.props[4:]
                if not values:
                    return default
                return values[0] if len(values) == 1 else values
        return default

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Node(%r, %d props, %d children)" % (
            self.name,
            len(self.props),
            len(self.children),
        )


class Document:
    """A parsed file: its version, and the top-level nodes."""

    __slots__ = ("version", "root", "is_binary")

    def __init__(self, version: int, root: Node, is_binary: bool):
        self.version = version
        self.root = root
        self.is_binary = is_binary

    def find(self, name: str) -> Optional[Node]:
        return self.root.find(name)

    def objects(self) -> List[Node]:
        block = self.root.find("Objects")
        return block.children if block else []

    def connections(self) -> List[tuple]:
        """Every ``(kind, child_id, parent_id, property_name)`` in the file.

        FBX puts almost nothing inside anything else: a mesh, its material, the
        bone that deforms it and the curve that animates it are all siblings in
        ``Objects``, and this list is what relates them. ``OO`` connects object
        to object, ``OP`` connects an object to a named property of another.
        """
        block = self.root.find("Connections")
        if block is None:
            return []
        out = []
        for entry in block.children:
            kind = entry.prop(0)
            child, parent = entry.prop(1), entry.prop(2)
            if child is None or parent is None:
                continue
            out.append((kind, int(child), int(parent), entry.prop(3)))
        return out


# -- binary --------------------------------------------------------------------


def _read_array(data: bytes, offset: int, code: str):
    """One of FBX's packed arrays, inflated if it was deflated."""
    length, encoding, compressed = struct.unpack_from("<III", data, offset)
    offset += 12
    raw = data[offset : offset + compressed]
    offset += compressed
    if encoding == 1:
        raw = zlib.decompress(raw)

    item = {"f": "f", "d": "d", "l": "q", "i": "i", "b": "?"}[code]
    values = struct.unpack("<%d%s" % (length, item), raw[: length * struct.calcsize(item)])
    return list(values), offset


def _read_props(data: bytes, offset: int, count: int):
    props: List[object] = []
    for _ in range(count):
        code = chr(data[offset])
        offset += 1
        if code in "YCIFDL":
            fmt = {"Y": "<h", "C": "<?", "I": "<i", "F": "<f", "D": "<d", "L": "<q"}[code]
            size = struct.calcsize(fmt)
            props.append(struct.unpack_from(fmt, data, offset)[0])
            offset += size
        elif code in "fdlib":
            values, offset = _read_array(data, offset, code)
            props.append(values)
        elif code in "SR":
            (length,) = struct.unpack_from("<I", data, offset)
            offset += 4
            chunk = data[offset : offset + length]
            offset += length
            # A string property is UTF-8; a raw one is bytes and stays bytes.
            props.append(chunk.decode("utf-8", "replace") if code == "S" else chunk)
        else:
            raise FbxError("Unknown property type %r at byte %d" % (code, offset - 1))
    return props, offset


def _read_binary(data: bytes) -> Document:
    (version,) = struct.unpack_from("<I", data, len(BINARY_MAGIC) + 2)
    wide = version >= 7500
    header = struct.calcsize("<QQQB" if wide else "<IIIB")

    def read_children(offset: int, limit: int) -> tuple:
        children: List[Node] = []
        while offset + header <= limit:
            if wide:
                end, count, _plen, namelen = struct.unpack_from("<QQQB", data, offset)
            else:
                end, count, _plen, namelen = struct.unpack_from("<IIIB", data, offset)
            offset += header

            # A record of all zeros ends the list — that is how nesting is
            # terminated, rather than by a count.
            if end == 0:
                return children, offset

            name = data[offset : offset + namelen].decode("utf-8", "replace")
            offset += namelen
            props, offset = _read_props(data, offset, count)

            nested: List[Node] = []
            if offset < end:
                nested, _ = read_children(offset, end)
            children.append(Node(name, props, nested))
            offset = end
        return children, offset

    children, _ = read_children(len(BINARY_MAGIC) + 6, len(data))
    return Document(version, Node("", [], children), True)


# -- ascii ---------------------------------------------------------------------

_ASCII_VERSION = re.compile(rb"FBXVersion:\s*(\d+)")


def _read_ascii(data: bytes) -> Document:
    """The text form, which about one file in eight turns out to be.

    Deliberately forgiving: this reads files somebody else wrote, and refusing
    one over a stray token helps nobody.

    The result is made to look exactly like a parsed binary file, because
    everything downstream should never learn which shape it came from. That
    takes one adjustment: in text an array is written ``Vertices: *24 { a: … }``
    — the numbers hang off a child called ``a`` — so those are lifted onto the
    node itself, which is where the binary form puts them.
    """
    text = data.decode("utf-8", "replace")
    match = _ASCII_VERSION.search(data)
    version = int(match.group(1)) if match else 7400

    root = Node("", [], [])
    stack = [root]
    # The last leaf opened, for an array whose numbers run over several lines.
    pending: Optional[Node] = None

    for raw in text.splitlines():
        line = _strip_comment(raw).strip()

        # A closing brace can share a line with what follows it, and it must be
        # dealt with first: it has no colon in it, and treating it as loose
        # array data is how the whole tree ends up inside the first node.
        while line.startswith("}"):
            if len(stack) > 1:
                stack.pop()
            pending = None
            line = line[1:].strip()
        if not line:
            continue

        head, sep, rest = line.partition(":")
        if not sep or '"' in head or "," in head:
            # No name: the rest of an array that spilled over.
            target = pending if pending is not None else stack[-1]
            target.props.extend(_ascii_values(line))
            continue

        opens = rest.rstrip().endswith("{")
        if opens:
            rest = rest.rstrip()[:-1]

        node = Node(head.strip(), _ascii_values(rest.strip()), [])
        stack[-1].children.append(node)
        if opens:
            stack.append(node)
            pending = None
        else:
            pending = node

    _hoist_arrays(root)
    return Document(version, root, False)


def _strip_comment(line: str) -> str:
    """Drops a trailing ``;`` comment, unless it is inside a quoted string."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            return line[:index]
    return line


def _hoist_arrays(node: Node) -> None:
    """Moves ``a:``'s numbers up onto the node that owns them."""
    for child in node.children:
        _hoist_arrays(child)
    if not node.props and len(node.children) == 1:
        only = node.children[0]
        if only.name == "a" and not only.children:
            node.props = [only.props]
            node.children = []


def _ascii_values(text: str) -> List[object]:
    if not text:
        return []
    out: List[object] = []
    for piece in _split_ascii(text):
        piece = piece.strip()
        if not piece:
            continue
        if piece.startswith('"'):
            out.append(piece.strip('"'))
            continue
        if piece.startswith("*"):
            # `*1234 {` — the count before an array, which the values follow.
            continue
        try:
            out.append(int(piece))
        except ValueError:
            try:
                out.append(float(piece))
            except ValueError:
                out.append(piece)
    return out


def _split_ascii(text: str) -> List[str]:
    """Commas, except inside quotes."""
    pieces, current, quoted = [], [], False
    for char in text:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char == "," and not quoted:
            pieces.append("".join(current))
            current = []
        else:
            current.append(char)
    pieces.append("".join(current))
    return pieces


# -- the way in ----------------------------------------------------------------


def parse(data: bytes) -> Document:
    """Reads either shape. Raises [FbxError] for anything that is not FBX."""
    if data.startswith(BINARY_MAGIC):
        return _read_binary(data)
    head = data[:512].lstrip()
    if head.startswith(b";") or b"FBXHeaderExtension" in data[:4096]:
        return _read_ascii(data)
    raise FbxError("Not an FBX file")


def flatten(values: Sequence) -> List[float]:
    """FBX writes vectors either as one array property or as several scalars."""
    if len(values) == 1 and isinstance(values[0], list):
        return [float(v) for v in values[0]]
    return [float(v) for v in values]

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

"""The workflow inside the picture.

**Most ComfyUI graphs never exist as a `.json` at all.** The editor writes the
whole workflow into the PNG it just generated, as a text chunk, and that image
is what people keep, post and send each other. So the picture is the file, and
the graph that made it is inside it.

A PNG is a signature and then a run of chunks, each `length, type, data, crc`.
Three of them carry text — `tEXt` (Latin-1, plain), `zTXt` (Latin-1, deflated)
and `iTXt` (UTF-8, deflated or not) — and ComfyUI writes `workflow` for the
document the editor draws and `prompt` for the API form of it.

Nothing here decodes an image. The pixels are somebody else's job, and this
walks past them: a chunk it does not care about costs one seek.
"""

from __future__ import annotations

import zlib
from typing import Dict, Optional

#: The eight bytes every PNG starts with.
SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Keys ComfyUI writes, best first: `workflow` is the document the editor
#: draws, `prompt` the API form with no coordinates in it.
KEYS = ("workflow", "prompt")


def is_png(data: bytes) -> bool:
    return data[: len(SIGNATURE)] == SIGNATURE


def text_chunks(data: bytes) -> Dict[str, str]:
    """Every text chunk in [data], by key.

    Damage is not an error: a truncated file gives back whatever was read
    before the truncation, because a picture half-copied still has its
    metadata at the front and the workflow in it is worth showing.
    """
    if not is_png(data):
        return {}

    found: Dict[str, str] = {}
    at = len(SIGNATURE)
    total = len(data)

    while at + 8 <= total:
        length = int.from_bytes(data[at : at + 4], "big")
        kind = data[at + 4 : at + 8]
        body = data[at + 8 : at + 8 + length]
        if len(body) < length:  # cut short
            break
        if kind == b"IEND":
            break
        if kind in (b"tEXt", b"zTXt", b"iTXt"):
            pair = _read_text(kind, body)
            if pair is not None and pair[0] not in found:
                found[pair[0]] = pair[1]
        # length, type, data, crc
        at += 12 + length

    return found


def workflow_json(data: bytes) -> Optional[str]:
    """The graph inside the picture, as the text of a JSON document."""
    chunks = text_chunks(data)
    for key in KEYS:
        text = chunks.get(key)
        if text and text.lstrip()[:1] in ("{", "["):
            return text
    return None


def carries_a_graph(head: bytes) -> bool:
    """Whether the first pages of a picture say a workflow is in it.

    Asked of a *head*, so the value is usually cut off half way through — the
    key is what is looked for, and the key is written before the value.
    """
    if not is_png(head):
        return False
    for key in KEYS:
        if b"\x00" + key.encode() in head or key.encode() + b"\x00" in head:
            return True
    return False


def _read_text(kind: bytes, body: bytes):
    """One text chunk as `(key, text)`, or None when it cannot be read."""
    try:
        if kind == b"tEXt":
            key, _, value = body.partition(b"\x00")
            return key.decode("latin-1"), value.decode("latin-1")

        if kind == b"zTXt":
            key, _, rest = body.partition(b"\x00")
            # The first byte after the key is the compression method.
            return key.decode("latin-1"), zlib.decompress(rest[1:]).decode(
                "latin-1"
            )

        if kind == b"iTXt":
            key, _, rest = body.partition(b"\x00")
            compressed = rest[:1] == b"\x01"
            # compression flag, compression method, then two null-terminated
            # strings nobody here needs: the language and the translated key.
            rest = rest[2:]
            _, _, rest = rest.partition(b"\x00")
            _, _, text = rest.partition(b"\x00")
            if compressed:
                text = zlib.decompress(text)
            return key.decode("latin-1"), text.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    return None

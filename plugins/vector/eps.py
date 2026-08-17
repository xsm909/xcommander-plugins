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

"""Encapsulated PostScript, run rather than parsed. See `ps` for the machine.

**Three things are this file's own**, and none of them are in the language:

1. **The bounding box.** A PostScript program has no size; an EPS is required to
   declare one in a comment, `%%BoundingBox`, and that comment *is* the drawing's
   size. Without it there is nothing to fit to the window.
2. **Which way up.** PostScript counts from the bottom left and every drawing on
   a screen counts from the top left, so the whole thing is turned over once,
   here, where the box is known.
3. **The wrapper.** A file that came from a Windows tool may be a small binary
   header with the PostScript inside it and a picture of the drawing beside it,
   for applications that could not run PostScript. Ours can, so the program is
   what is taken — but the header has to be stepped over first.
"""

from __future__ import annotations

import base64
import re
import struct

import ps

MAX_SHAPES = 20000


class EpsError(Exception):
    """The file is not one, or is one this cannot run."""


def is_eps(head: bytes) -> bool:
    return head[:2] == b"%!" or head[:4] == b"\xc5\xd0\xd3\xc6"


def read(data: bytes) -> dict:
    body, preview = _unwrap(data)
    if not body.lstrip()[:2] == b"%!":
        raise EpsError("This is not an EPS.")
    text = body.decode("latin-1", errors="replace")

    box = _bounding_box(text)
    if box is None:
        raise EpsError(
            "This EPS does not say how big it is, and a drawing without a size "
            "cannot be shown."
        )
    left, bottom, right, top = box
    width, height = right - left, top - bottom
    if width <= 0 or height <= 0:
        raise EpsError("This EPS says it has no size.")

    notes: list[str] = []
    machine = ps.Machine(notes)
    # The one transform this file owns: PostScript counts up from the bottom
    # left corner of the box, and a drawing on a screen counts down from the
    # top left.
    machine.matrix = (1.0, 0.0, 0.0, -1.0, -left, top)
    try:
        machine.run(ps.tokenise(text))
    except ps.PostScriptError as failure:
        if not machine.shapes:
            raise EpsError(str(failure)) from failure
        notes.append("The file stopped early: %s" % failure)

    if machine.unknown:
        # Named, not counted: which operators a file wanted is the one thing
        # that says what is missing from the picture.
        missing = sorted(machine.unknown)
        notes.append(
            "%d operator(s) this reader does not know were skipped: %s%s."
            % (
                len(missing),
                ", ".join(missing[:8]),
                "…" if len(missing) > 8 else "",
            )
        )
    if not machine.shapes:
        raise EpsError(
            "Nothing in this EPS drew anything this reader understands."
            + (" It carries a picture of itself, which is not read." if preview
               else "")
        )

    shapes = machine.shapes[:MAX_SHAPES]
    if len(machine.shapes) > MAX_SHAPES:
        notes.append("%d shapes of %d shown." % (MAX_SHAPES, len(machine.shapes)))
    return {
        "width": width,
        "height": height,
        "shapes": [_packed(shape) for shape in shapes],
        "notes": notes,
    }


def _unwrap(data: bytes):
    """The PostScript itself, and whether a picture came with it.

    A DOS EPS is a thirty-byte header of offsets: where the program is, and
    where the preview picture is. Everything else is a plain text file.
    """
    if data[:4] != b"\xc5\xd0\xd3\xc6":
        return data, False
    ps_at, ps_length, wmf_at, _wmf, tiff_at, _tiff = struct.unpack(
        "<IIIIII", data[4:28]
    )
    return data[ps_at:ps_at + ps_length], bool(wmf_at or tiff_at)


def _bounding_box(text: str):
    """`%%BoundingBox`, preferring the high-resolution one where there is one.

    `(atend)` is legal and means it is written after the drawing, so the whole
    file is searched rather than only its head.
    """
    best = None
    for name in ("%%HiResBoundingBox:", "%%BoundingBox:"):
        for match in re.finditer(re.escape(name) + r"([^\r\n]*)", text):
            numbers = re.findall(r"[-+]?[\d.]+", match.group(1))
            if len(numbers) >= 4:
                try:
                    return [float(value) for value in numbers[:4]]
                except ValueError:
                    continue
    return best


def _packed(shape):
    """One shape in the shape the host reads."""
    return {
        "verbs": base64.b64encode(bytes(shape["verbs"])).decode("ascii"),
        "points": base64.b64encode(
            struct.pack("<%df" % len(shape["points"]), *shape["points"])
        ).decode("ascii"),
        "fill": shape["fill"],
        "stroke": shape["stroke"],
        "strokeWidth": shape["strokeWidth"],
        "evenOdd": False,
        "cap": "butt",
        "join": "miter",
    }

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

"""Drawings made of shapes, drawn as shapes.

A vector file is worth having because it stays sharp however far into it you
go, and that is only true if the shapes reach the canvas as shapes. So this
does not rasterise anything: it flattens the file into a list of paths with
their colours already worked out, and the host draws them — the same division
the 3D viewer keeps, where the plugin does the arcana and the host draws.

SVG today. EPS and the rest as they are added; `.ai` is not on that list,
because a modern Illustrator file is a PDF and reading one is a different and
much larger job than reading a list of shapes.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xcommander import Plugin, error  # noqa: E402

import eps  # noqa: E402
import svg  # noqa: E402

plugin = Plugin("org.xcommander.vector", "Vector graphics")

#: A drawing is text and compresses well; the cap is for the one that is a map
#: of a country.
MAX_BYTES = 64 << 20

READERS = {
    "svg": (svg.read, svg.SvgError, "SVG"),
    "eps": (eps.read, eps.EpsError, "EPS"),
    "epsf": (eps.read, eps.EpsError, "EPS"),
    "epsi": (eps.read, eps.EpsError, "EPS"),
    "ps": (eps.read, eps.EpsError, "PostScript"),
}


@plugin.viewer(
    "vector.drawing",
    "Drawing",
    extensions=sorted(READERS),
    # Above the text viewer, which has always claimed .svg — a drawing opens on
    # F3 and its own text is one Shift+F3 away, which is the right way round.
    priority=20,
    produces="drawing",
)
def drawing(url: str) -> dict:
    started = time.time()
    extension = url.rsplit(".", 1)[-1].lower()
    reader = READERS.get(extension)
    if reader is None:
        return error("This viewer does not read a .%s." % extension)
    read, refusal, what = reader

    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return error("The file could not be read: %s" % failure)
    if not raw:
        return error("The file is empty.")

    try:
        found = read(raw)
    except refusal as failure:
        return error(str(failure))
    except Exception as failure:  # noqa: BLE001
        return error("This %s file could not be read: %s" % (what, failure))

    notes = list(found["notes"])
    plugin.log(
        "%s %gx%g, %d shape(s), %.2fs"
        % (extension, found["width"], found["height"],
           len(found["shapes"]), time.time() - started)
    )
    return {
        "kind": "vector",
        "width": found["width"],
        "height": found["height"],
        "shapes": found["shapes"],
        "detail": " ".join(notes) if notes else None,
    }


plugin.run()

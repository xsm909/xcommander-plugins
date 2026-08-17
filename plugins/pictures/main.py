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

"""The raster formats the machine's own decoder will not open.

**Why this exists at all.** The host draws a picture by handing the file to the
engine, and the engine's list of formats is not the same on every machine —
measured 2026-08-17, macOS reads Photoshop, Targa, TIFF, HEIC and JPEG 2000
through the system, where Skia on its own knows four formats. GIMP's own `.xcf`
is read by nobody anywhere, which is why it is the first one here.

**The shape, and it needed nothing new in the contract.** The reader composites
the file into one RGBA picture, packs it as PNG, and returns it as an ordinary
`image` — so F3, the Ctrl+Q panel viewport and a tool pointing at a file all
show it, and the host's picture canvas gives it 1:1 and zoom for free. The same
plan the 3D viewer follows: the plugin does the arcana, the host draws.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xcommander import Plugin, error, image  # noqa: E402

import png  # noqa: E402
import psd  # noqa: E402
import tga  # noqa: E402
import tiff  # noqa: E402
import xcf  # noqa: E402

plugin = Plugin("org.xcommander.pictures", "Pictures")

#: A GIMP file keeps every layer at full size, so it is several times the
#: picture. This is generous on purpose and still finite.
MAX_BYTES = 512 << 20

#: The readers, by extension. One viewer rather than one per format — which
#: file it is, is the file's business, not a menu the reader has to be picked
#: from.
READERS = {
    "xcf": (xcf.read, xcf.XcfError, "GIMP"),
    "psd": (psd.read, psd.PsdError, "Photoshop"),
    "psb": (psd.read, psd.PsdError, "Photoshop"),
    "tga": (tga.read, tga.TgaError, "Targa"),
    "tif": (tiff.read, tiff.TiffError, "TIFF"),
    "tiff": (tiff.read, tiff.TiffError, "TIFF"),
    "targa": (tga.read, tga.TgaError, "Targa"),
    "tpic": (tga.read, tga.TgaError, "Targa"),
}


@plugin.viewer(
    "pictures.raster",
    "Picture",
    extensions=sorted(READERS),
    priority=20,
)
def picture(url: str) -> dict:
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

    # Packing is the slow half on a big picture, and it is worth less there:
    # measured on a 3840x2160 Photoshop file, the careful setting costs a
    # second and saves a tenth of the bytes.
    pixels = found["width"] * found["height"]
    body = png.write(
        found["width"], found["height"], found["pixels"],
        level=1 if pixels > 4_000_000 else 6,
    )
    content = image(body)
    # What was drawn, and anything that was not — a preview that mentions
    # neither is telling the reader they have seen the file.
    notes = list(found["notes"])
    content["detail"] = " ".join(notes) if notes else None
    plugin.log(
        "%s %dx%d, %.2fs, %d KB"
        % (
            extension,
            found["width"],
            found["height"],
            time.time() - started,
            len(body) >> 10,
        )
    )
    return content


plugin.run()

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

from xcommander import (  # noqa: E402
    Plugin,
    error,
    fact,
    fact_group,
    facts,
    image,
)

import meta  # noqa: E402
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


#: Formats the machine's own decoder may well read where this one cannot.
#:
#: Measured 2026-08-17: macOS reads Photoshop, Targa and TIFF through the
#: system; Windows reads TIFF (and HEIC, with the extension installed) through
#: WIC. So a file this reader refuses — a fax-compressed TIFF, a JPEG inside a
#: TIFF, a Photoshop file saved without its flattened copy — is worth handing
#: over rather than refusing outright. GIMP's own format is not in the list
#: because nothing anywhere reads it.
HAND_OVER = {"tif", "tiff", "psd", "psb", "tga", "targa", "tpic"}

#: How much is worth sending on the chance that the engine can read it.
MAX_HANDOVER = 64 << 20


def answer(extension: str, raw: bytes) -> dict:
    """The content for one file, given its bytes. Separate from the viewer so
    that it can be checked without a host on the other end of a pipe."""
    reader = READERS.get(extension)
    if reader is None:
        return error("This viewer does not read a .%s." % extension)
    read, refusal, what = reader
    if not raw:
        return error("The file is empty.")

    try:
        found = read(raw)
    except refusal as failure:
        # **Ask the machine before giving up.** What each engine reads is not
        # the same on every machine and not the same as what this reads, and a
        # picture shown by somebody else's decoder is still the picture. If it
        # cannot either, the canvas says so in one sentence.
        if extension in HAND_OVER and len(raw) <= MAX_HANDOVER:
            content = image(raw, mime_type=_mime(extension))
            content["detail"] = (
                "%s — shown by this machine's own decoder instead." % failure
            )
            return content
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
    return content


def _mime(extension: str) -> str:
    return {
        "tif": "image/tiff", "tiff": "image/tiff",
        "psd": "image/vnd.adobe.photoshop",
        "psb": "image/vnd.adobe.photoshop",
    }.get(extension, "application/octet-stream")


@plugin.viewer(
    "pictures.raster",
    "Picture",
    extensions=sorted(READERS),
    priority=20,
)
def picture(url: str) -> dict:
    started = time.time()
    extension = url.rsplit(".", 1)[-1].lower()
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return error("The file could not be read: %s" % failure)

    content = answer(extension, raw)
    plugin.log("%s %s, %.2fs" % (extension, content.get("kind"), time.time() - started))
    return content


# -- what a picture says about itself --------------------------------------

#: Every raster format worth asking about, whether or not this plugin is the
#: one that draws it. That is the whole point of a describer being its own
#: contribution: a JPEG is decoded by the machine's engine through a plugin
#: that runs no Python at all, and its EXIF still has to come from somewhere.
DESCRIBED = [
    "jpg", "jpeg", "jpe", "jfif", "png", "gif", "bmp", "webp",
    "heic", "heif", "avif", "tif", "tiff", "psd", "psb", "tga", "targa",
    "xcf", "dng", "cr2", "nef", "arw", "orf", "rw2", "raf",
]

#: The head of a file is where every one of these formats keeps what it says
#: about itself — EXIF stands before the pixels in all of them. A raw file is
#: the exception worth naming: its EXIF is a TIFF directory at the front, so
#: this is enough there too.
HEAD_BYTES = 4 << 20


@plugin.describer(
    "pictures.about",
    "About this picture",
    extensions=DESCRIBED,
)
def about(url: str) -> dict:
    started = time.time()
    name = url.rsplit("/", 1)[-1]
    try:
        raw = plugin.read_file(url, max_bytes=HEAD_BYTES)
    except Exception as failure:  # noqa: BLE001
        return facts([], note="The file could not be read: %s" % failure)
    if not raw:
        return facts([], note="The file is empty.")

    whole = (plugin.stat(url) or {}).get("size")
    try:
        found = meta.read(raw)
    except Exception as failure:  # noqa: BLE001 - a damaged header is not a crash
        return facts(
            [_file_group(name, whole, meta.Picture())],
            note="This file's header could not be read: %s" % failure,
        )

    groups = [
        _file_group(name, whole, found),
        _camera_group(found),
        _exposure_group(found),
        _when_group(found),
        _where_group(found),
        _made_group(found),
    ]
    plugin.log("%s: %d tag(s), %.2fs"
               % (name, found.exif.count if found.exif else 0, time.time() - started))
    return facts([g for g in groups if g], note=_note(found, len(raw), whole))


def _file_group(name: str, whole, found) -> dict:
    rows = [fact("Name", name)]
    if isinstance(whole, int):
        rows.append(fact("Size", meta.size(whole)))
    if found.kind:
        rows.append(fact("Format", found.kind))
    if found.width and found.height:
        rows.append(fact("Dimensions", "%d × %d" % (found.width, found.height)))
        pixels = meta.megapixels(found.width, found.height)
        if pixels:
            rows.append(fact("Pixels", pixels))
    if found.depth:
        rows.append(fact("Colour", found.depth))
    if found.notes:
        rows.append(fact("Also", ", ".join(found.notes)))
    exif = found.exif
    if exif:
        turned = meta.named(meta.ORIENTATION, exif.get(0x0112, "main"))
        if turned and turned != "as it stands":
            rows.append(fact("Orientation", turned))
    return fact_group("Picture", rows)


def _camera_group(found) -> dict:
    exif = found.exif
    if not exif:
        return {}
    rows = [
        fact("Make", exif.get(0x010F, "main") or ""),
        fact("Model", exif.get(0x0110, "main") or ""),
        fact("Lens", exif.any(("exif", 0xA434), ("exif", 0xA433)) or ""),
        fact("Serial", exif.get(0xA431) or ""),
    ]
    return fact_group("Camera", [r for r in rows if r["value"]])


def _exposure_group(found) -> dict:
    exif = found.exif
    if not exif:
        return {}
    iso = exif.any(("exif", 0x8827), ("exif", 0x8833))
    if isinstance(iso, list):
        iso = iso[0] if iso else None
    bias = meta.rational(exif.get(0x9204))
    rows = [
        fact("Shutter", meta.shutter(exif.get(0x829A))),
        fact("Aperture", meta.aperture(exif.get(0x829D))),
        fact("ISO", "" if iso is None else str(int(iso))),
        fact("Focal length", meta.millimetres(exif.get(0x920A))),
        # Only where it says something the line above did not: on a
        # full-frame body the two are the same number twice.
        fact("Full-frame equivalent", _equivalent(exif)),
        fact("Exposure bias", "" if bias in (None, 0) else "%+g EV" % round(bias, 2)),
        fact("Program", meta.named(meta.PROGRAM, exif.get(0x8822))),
        fact("Metering", meta.named(meta.METERING, exif.get(0x9207))),
        fact("Flash", meta.named(meta.FLASH, exif.get(0x9209))),
        fact("White balance", meta.named(meta.WHITE_BALANCE, exif.get(0xA403))),
    ]
    return fact_group("Exposure", [r for r in rows if r["value"]])


def _equivalent(exif) -> str:
    """The 35 mm equivalent, unless it is the focal length again."""
    equivalent = meta.millimetres(exif.get(0xA405))
    return "" if equivalent == meta.millimetres(exif.get(0x920A)) else equivalent


def _when_group(found) -> dict:
    exif = found.exif
    if not exif:
        return {}
    offset = exif.get(0x9011) or exif.get(0x9010)
    rows = [
        fact("Taken", meta.when(exif.get(0x9003), offset)),
        fact("Digitised", meta.when(exif.get(0x9004), exif.get(0x9012))),
        fact("Changed", meta.when(exif.get(0x0132, "main"))),
    ]
    # **The same instant three times is one fact.** A camera writes all three
    # and a converter rewrites the last, so they usually differ by a time zone
    # or by nothing at all — and three lines saying one thing is the noise this
    # panel is supposed to be free of. Compared to the second, ignoring the
    # offset, because that is what "the same moment" means here.
    kept = []
    for row in rows:
        if not row["value"]:
            continue
        if any(row["value"][:19] == other["value"][:19] for other in kept):
            continue
        kept.append(row)
    return fact_group("When", kept)


def _where_group(found) -> dict:
    exif = found.exif
    if not exif or not exif.gps:
        return {}
    latitude = meta.degrees(exif.get(0x0002, "gps"), exif.get(0x0001, "gps"))
    longitude = meta.degrees(exif.get(0x0004, "gps"), exif.get(0x0003, "gps"))
    altitude = meta.rational(exif.get(0x0006, "gps"))
    if altitude is not None and exif.get(0x0005, "gps") == b"\x01":
        altitude = -altitude
    rows = [
        fact("Latitude", latitude),
        fact("Longitude", longitude),
        fact("Altitude", "" if altitude is None else "%d m" % round(altitude)),
    ]
    if latitude and longitude:
        # One line to copy into whatever the reader uses for maps. Not a link:
        # a viewer that quietly sends where a photograph was taken to somebody
        # else's server is not a viewer, and this application asks first.
        rows.append(fact("Coordinates", "%s, %s" % (latitude, longitude)))
    return fact_group("Where", [r for r in rows if r["value"]])


def _made_group(found) -> dict:
    exif = found.exif
    rows = []
    if exif:
        rows = [
            fact("Software", exif.get(0x0131, "main") or ""),
            fact("Artist", exif.get(0x013B, "main") or ""),
            fact("Copyright", exif.get(0x8298, "main") or ""),
            fact("Description", exif.get(0x010E, "main") or "", wide=True),
            fact("Comment", _user_comment(exif.get(0x9286)), wide=True),
        ]
    for key, value in found.text[:12]:
        rows.append(fact(key, value, wide=len(value) > 40))
    return fact_group("Made with", [r for r in rows if r["value"]])


def _user_comment(value) -> str:
    """`UserComment` says its own encoding in the first eight bytes."""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes) or len(value) < 8:
        return ""
    head, body = value[:8], value[8:]
    if head.startswith(b"UNICODE"):
        return body.decode("utf-16-be", "replace").strip("\0 ")
    if head.startswith(b"ASCII"):
        return body.decode("ascii", "replace").strip("\0 ")
    return body.decode("utf-8", "replace").strip("\0 ")


def _note(found, read: int, whole) -> str:
    remarks = []
    if found.exif is None and not found.text and not found.xmp:
        remarks.append("This file carries nothing beyond its own header.")
    elif found.exif is not None:
        # What is above is a choice out of what is there, and saying how much
        # was left out is the difference between a summary and a claim.
        remarks.append("%d tag(s) in the file." % found.exif.count)
    if found.xmp:
        remarks.append("It also carries XMP, which is not read here.")
    if isinstance(whole, int) and whole > read:
        remarks.append("Read the first %s of it." % meta.size(read))
    return " ".join(remarks) if remarks else ""


if __name__ == "__main__":
    plugin.run()

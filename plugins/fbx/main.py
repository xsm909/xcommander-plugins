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

"""FBX — looking inside Autodesk's interchange format.

Step 0: what is in this file. F3 on a `.fbx` answers with its meshes, its
skeleton, its clips and how long they run, before any of it is drawn.

That is worth having on its own — half of what anyone wants from a folder of
assets is "which of these is the animated one" — and it is also the floor the
rest is built on: the same parser and the same object graph feed the model
viewer that comes next.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import base64  # noqa: E402
import hashlib  # noqa: E402
import struct  # noqa: E402
from urllib.parse import quote  # noqa: E402

from xcommander import Plugin, error, markdown  # noqa: E402

import animation  # noqa: E402
import fbxfile  # noqa: E402
import geometry  # noqa: E402
from scene import Scene, summarise  # noqa: E402

plugin = Plugin("org.xcommander.fbx", "FBX")

#: Files are read whole. The largest FBX anyone has pointed this at is a
#: megabyte and a half; the cap is for the one that is not.
MAX_BYTES = 256 << 20


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%d %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def thousands(value: int) -> str:
    return "{:,}".format(value).replace(",", " ")


def _load(url: str):
    """The file, parsed, or the content that explains why not."""
    data = plugin.read_file(url, max_bytes=MAX_BYTES)
    if not data:
        return None, error("The file is empty, or could not be read.")
    try:
        return (fbxfile.parse(data), len(data)), None
    except fbxfile.FbxError as failure:
        return None, error(str(failure))
    except Exception as failure:  # noqa: BLE001 - a malformed file is not a crash
        return None, error("This file could not be read as FBX: %s" % failure)


def _b64_floats(values) -> str:
    return base64.b64encode(geometry.pack_floats(values)).decode("ascii")


#: Pictures are sent whole, and a model with a 4K texture on every material
#: would otherwise put tens of megabytes through a pipe meant for a preview.
MAX_PICTURE_BYTES = 16 << 20


def _beside(url: str, relative: str) -> str:
    """The URL of a file named relative to the one being read.

    A texture that is not in the file is named the way the machine that wrote
    the file saw it — `..\\textures\\skin.png`, or an absolute path on a disk
    that belongs to somebody else. Only the relative form is followed, and only
    downwards from the folder the FBX is in; the name on its own is tried too,
    because a `.fbm` folder an exporter promised is very often not there.
    """
    folder = url.rsplit("/", 1)[0] if "/" in url else url
    return folder + "/" + quote(relative.lstrip("./"))


def _pictures(url: str, parts: list) -> list:
    """Every bitmap the meshes want, read once, and each mesh told which.

    Two meshes made of one material share one picture; a file that carries its
    own bitmaps needs nothing read at all.
    """
    images: list = []
    known: dict = {}
    spent = 0

    for mesh in parts:
        mesh["image"] = -1
        picture = mesh.get("picture")
        if not picture:
            continue

        data = picture.get("bytes")
        name = picture.get("name") or picture.get("beside") or ""
        if data is None:
            wanted = picture.get("beside") or ""
            if not wanted:
                continue
            # As written, then by its bare name in the same folder as the FBX.
            tries = [wanted]
            if "/" in wanted:
                tries.append(wanted.rsplit("/", 1)[1])
            for attempt in tries:
                try:
                    data = plugin.read_file(_beside(url, attempt),
                                            max_bytes=MAX_PICTURE_BYTES - spent)
                except Exception:  # noqa: BLE001 - a missing texture is not a crash
                    data = None
                if data:
                    name = attempt
                    break
            if not data:
                continue

        key = hashlib.sha1(data).hexdigest()
        if key in known:
            mesh["image"] = known[key]
            continue
        if spent + len(data) > MAX_PICTURE_BYTES:
            continue
        spent += len(data)
        known[key] = len(images)
        mesh["image"] = len(images)
        images.append({
            "name": name.rsplit("/", 1)[-1],
            "data": base64.b64encode(data).decode("ascii"),
        })

    return images


def _skin(scene, parts: list, fix: list) -> dict:
    """Weights per vertex and a matrix per joint per frame, for every clip.

    Kept per mesh: two meshes rarely share a skeleton exactly, and pooling
    their joints would make an index space that nobody can check against
    anything.
    """
    for mesh in parts:
        clusters = animation.clusters_of(scene, mesh["geometryId"])
        mesh["clusters"] = clusters
        if not clusters:
            continue

        pulls = animation.influences(clusters, mesh["sourceCount"])
        indices, weights = [], []
        for source in mesh["sources"]:
            entry = pulls[source] if 0 <= source < len(pulls) else []
            for slot in range(animation.MAX_INFLUENCES):
                if slot < len(entry):
                    indices.append(entry[slot][0])
                    weights.append(entry[slot][1])
                else:
                    indices.append(0)
                    weights.append(0.0)
        mesh["jointIndices"] = indices
        mesh["jointWeights"] = weights

    clips = []
    for stack in scene.of_kind("AnimationStack"):
        baked = animation.bake(scene, stack, parts, fix)
        if baked is None or not any(baked["tracks"]):
            continue
        clips.append(baked)
    return {"clips": clips}


def mesh3d(meshes: list, up_axis: str, unit_scale: float, triangles: int,
           clips: list, images: list) -> dict:
    """The content the host draws.

    Numbers travel as packed binary rather than as JSON arrays: a mesh of
    twenty thousand triangles is a quarter of a million numbers, and written
    out as digits that is megabytes of text to parse before anything appears.
    """
    return {
        "kind": "mesh3d",
        "upAxis": up_axis,
        "unitScale": unit_scale,
        "triangles": triangles,
        "clips": [
            {
                "name": clip["name"],
                "frames": clip["frames"],
                "fps": clip["fps"],
                "seconds": clip["seconds"],
                "tracks": [
                    "" if track is None else _b64_floats(track)
                    for track in clip["tracks"]
                ],
            }
            for clip in clips
        ],
        "images": images,
        "meshes": [
            {
                "name": mesh["name"],
                "color": mesh["color"],
                #: Which of `images` this mesh is painted with, or −1 for none.
                "image": mesh.get("image", -1),
                "uvs": _b64_floats(mesh.get("uvs") or []),
                "joints": len(mesh.get("clusters") or []),
                "jointIndices": base64.b64encode(
                    struct.pack("<%dH" % len(mesh.get("jointIndices") or []),
                                *(mesh.get("jointIndices") or []))).decode("ascii"),
                "jointWeights": _b64_floats(mesh.get("jointWeights") or []),
                "positions": base64.b64encode(
                    geometry.pack_floats(mesh["positions"])).decode("ascii"),
                "normals": base64.b64encode(
                    geometry.pack_floats(mesh["normals"])).decode("ascii"),
                "indices": base64.b64encode(
                    geometry.pack_indices(mesh["indices"])).decode("ascii"),
            }
            for mesh in meshes
        ],
    }


@plugin.viewer("fbx.model", "Model", extensions=["fbx"], priority=20)
def model(url: str) -> dict:
    loaded, refusal = _load(url)
    if refusal is not None:
        return refusal
    document, _size = loaded

    scene = Scene(document)
    try:
        parts, note = geometry.meshes(scene)
    except Exception as failure:  # noqa: BLE001 - one odd file is not a crash
        return error("The geometry in this file could not be read: %s" % failure)

    if not parts:
        return error(
            "This file carries no mesh to draw. Shift+F3 lists what it does "
            "carry — a skeleton and its animation, most likely."
        )

    images = _pictures(url, parts)
    skinned = _skin(scene, parts, geometry._axis_fix(scene))
    return mesh3d(parts, scene.up_axis(), scene.unit_scale(), note["triangles"],
                  skinned["clips"], images)


@plugin.viewer("fbx.contents", "FBX contents", extensions=["fbx"], priority=10)
def contents(url: str) -> dict:
    loaded, refusal = _load(url)
    if refusal is not None:
        return refusal
    document, size = loaded
    return markdown(_report(summarise(Scene(document)), size))


def _report(facts: dict, size: int) -> str:
    lines = []

    version = facts["version"]
    lines.append("# FBX %d.%d" % (version // 1000, (version % 1000) // 100))
    lines.append("")
    lines.append("| | |")
    lines.append("| --- | --- |")
    lines.append("| Form | %s |" % ("binary" if facts["binary"] else "text"))
    lines.append("| Size | %s |" % human(size))
    lines.append("| Written by | %s |" % facts["creator"])
    lines.append("| Up axis | %s |" % facts["upAxis"])
    lines.append("| Units | %g cm |" % facts["unitScale"])
    lines.append("| Frame rate | %g fps |" % facts["frameRate"])
    lines.append("| Objects | %s, %s connections |"
                 % (thousands(facts["objects"]), thousands(facts["connections"])))
    if facts["models"]:
        kinds = ", ".join(
            "%s %d" % (name, count)
            for name, count in sorted(facts["models"].items(), key=lambda kv: -kv[1])
        )
        lines.append("| Nodes | %s |" % kinds)
    lines.append("")

    meshes = facts["meshes"]
    if meshes:
        total = sum(m["triangles"] for m in meshes)
        lines.append("## Geometry — %s triangles in %d mesh(es)"
                     % (thousands(total), len(meshes)))
        lines.append("")
        lines.append("| Mesh | Vertices | Polygons | Triangles |")
        lines.append("| --- | ---: | ---: | ---: |")
        for mesh in meshes[:40]:
            lines.append("| %s | %s | %s | %s |" % (
                mesh["name"], thousands(mesh["vertices"]),
                thousands(mesh["polygons"]), thousands(mesh["triangles"])))
        if len(meshes) > 40:
            lines.append("| … and %d more | | | |" % (len(meshes) - 40))
        lines.append("")
    else:
        lines.append("## Geometry")
        lines.append("")
        lines.append("Nothing to draw: this file carries no mesh.")
        lines.append("")

    if facts["joints"] or facts["skins"]:
        lines.append("## Skeleton")
        lines.append("")
        lines.append("%d joint(s), %d skin(s) binding %d cluster(s)."
                     % (facts["joints"], facts["skins"], facts["clusters"]))
        lines.append("")

    clips = facts["clips"]
    if clips:
        lines.append("## Animation")
        lines.append("")
        lines.append("| Clip | Length | Layers | Curves | Keys |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for clip in clips:
            lines.append("| %s | %.2f s | %d | %s | %s |" % (
                clip["name"], clip["seconds"], clip["layers"],
                thousands(clip["curves"]), thousands(clip["keys"])))
        lines.append("")

    if facts["materials"] or facts["textures"]:
        lines.append("## Surfaces")
        lines.append("")
        note = "%d material(s), %d texture(s)" % (facts["materials"], facts["textures"])
        if facts["embedded"]:
            note += " — %d embedded in the file, %s" % (
                facts["embedded"], human(facts["embeddedBytes"]))
        lines.append(note + ".")
        lines.append("")

    return "\n".join(lines)


plugin.run()

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
import gltffile  # noqa: E402
import gltfanim  # noqa: E402
import gltfscene  # noqa: E402
import objfile  # noqa: E402
from scene import Scene, summarise  # noqa: E402

#: What this can be pointed at. One viewer for all of them: which format a file
#: is, is the reader's business and nobody else's.
MODELS = ["fbx", "glb", "gltf", "obj"]

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


def _sibling(url: str, name: str) -> str:
    folder = url.rsplit("/", 1)[0] if "/" in url else url
    return folder + "/" + quote(name.lstrip("./"))


class Model:
    """One file, read: whichever format it is, asked the same three questions.

    The whole of the difference between formats lives behind this. What comes
    out of `meshes` is one shape of dict, so the payload, the pictures, the
    truncation report and every line of the host are the same for all of them.
    """

    def __init__(self, url: str, data: bytes):
        self.url = url
        self.size = len(data)
        head = data.lstrip()[:1]
        if url.lower().rsplit(".", 1)[-1] == "obj":
            self.kind = "obj"
            self.data = data
            self.parts = None
        elif data[:4] == gltffile.MAGIC or head == b"{":
            self.kind = "gltf"
            self.document = gltffile.parse(data)
            self.held = gltffile.Bytes(
                self.document,
                lambda uri: plugin.read_file(_sibling(url, uri),
                                             max_bytes=MAX_BYTES),
            )
        else:
            self.kind = "fbx"
            self.document = fbxfile.parse(data)
            self.scene = Scene(self.document)

    def meshes(self):
        if self.kind == "obj":
            self.parts, note = objfile.meshes(
                self.data,
                lambda name: plugin.read_file(_sibling(self.url, name),
                                              max_bytes=MAX_BYTES),
            )
            return self.parts, note
        if self.kind == "gltf":
            return gltfscene.meshes(self.document, self.held)
        return geometry.meshes(self.scene)

    def facts(self) -> dict:
        if self.kind == "obj":
            if self.parts is None:
                self.meshes()
            return objfile.summarise(self.data, self.parts or [])
        if self.kind == "gltf":
            return gltfscene.summarise(self.document, self.held, self.size)
        return summarise(self.scene)

    def clips(self, parts: list) -> list:
        """What moves. OBJ has nothing that can."""
        if self.kind == "obj":
            return []
        if self.kind == "gltf":
            return gltfanim.clips(self.document, self.held, parts)
        return _skin(self.scene, parts, geometry._axis_fix(self.scene))["clips"]


def _load(url: str):
    """The file, parsed, or the content that explains why not."""
    data = plugin.read_file(url, max_bytes=MAX_BYTES)
    if not data:
        return None, error("The file is empty, or could not be read.")
    try:
        return Model(url, data), None
    except (fbxfile.FbxError, gltffile.GltfError) as failure:
        return None, error(str(failure))
    except Exception as failure:  # noqa: BLE001 - a malformed file is not a crash
        return None, error("This file could not be read as a model: %s" % failure)


def _b64_floats(values) -> str:
    return base64.b64encode(geometry.pack_floats(values)).decode("ascii")


#: Pictures are sent whole, and a model with a 4K texture on every material
#: would otherwise put tens of megabytes through a pipe meant for a preview.
MAX_PICTURE_BYTES = 16 << 20

#: What the host can actually decode, by the first bytes of the file. A real
#: model here ships a 2 MB Targa, which Flutter has no decoder for: sending it
#: costs the pipe two megabytes and the host an error, and the mesh falls back
#: to its material colour either way. So it is not sent.
_DECODABLE = (
    b"\x89PNG",          # PNG
    b"\xff\xd8\xff",     # JPEG
    b"GIF8",             # GIF
    b"BM",               # BMP
    b"RIFF",             # WebP, checked further below
)


def _readable(data: bytes) -> bool:
    if data[:4] == b"RIFF":
        return data[8:12] == b"WEBP"
    return any(data.startswith(magic) for magic in _DECODABLE)


def _rooted(name: str) -> bool:
    """Whether a name is a place on somebody else's machine.

    FBX has a field called `RelativeFilename` and real files put absolute paths
    in it: the model that taught this one says
    `C:/Temp/CharacterCreator4Temp/FbxWorkingDirectory/skull_Diffuse.png`.
    Such a name says nothing about where the picture is now, so it is only ever
    used for the name at the end of it.
    """
    return name.startswith("/") or (len(name) > 1 and name[1] == ":")


def _places(url: str, picture: dict) -> list:
    """Where to look for a picture that is not inside the file, in order.

    Relative as written, if it really is relative and points downward; then the
    bare name in the folder the model is in, because a `.fbm` folder an
    exporter promised is very often not there; then a `textures` folder beside
    the model and beside its own folder, which is how a downloaded model is
    laid out — `source/thing.fbx` with `textures/` next to `source`.
    """
    folder = url.rsplit("/", 1)[0] if "/" in url else url
    above = folder.rsplit("/", 1)[0] if "/" in folder else folder
    wanted = picture.get("beside") or ""
    name = picture.get("name") or wanted.rsplit("/", 1)[-1]

    out = []
    if wanted and not _rooted(wanted) and ".." not in wanted:
        out.append(folder + "/" + quote(wanted.lstrip("./")))
    if name:
        for place in (folder, folder + "/textures", above + "/textures"):
            candidate = place + "/" + quote(name)
            if candidate not in out:
                out.append(candidate)
    return out


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
            for attempt in _places(url, picture):
                try:
                    data = plugin.read_file(attempt,
                                            max_bytes=MAX_PICTURE_BYTES - spent)
                except Exception:  # noqa: BLE001 - a missing texture is not a crash
                    data = None
                if data:
                    break
            if not data:
                continue

        key = hashlib.sha1(data).hexdigest()
        if key in known:
            mesh["image"] = known[key]
            continue
        if spent + len(data) > MAX_PICTURE_BYTES or not _readable(data):
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
            # A mesh nothing skins still moves if its own node does — a
            # propeller, a door, a lid. It is sent as a skin of one bone
            # pulling on every vertex, so the host draws it by the same
            # arithmetic and knows nothing about the difference. Only when
            # something in the file actually moves it: weights on a model that
            # never budges are bytes for nothing.
            if animation.moves(scene, mesh.get("modelId")):
                mesh["rigid"] = True
                count = len(mesh["positions"]) // 3
                mesh["jointIndices"] = [0] * (count * animation.MAX_INFLUENCES)
                mesh["jointWeights"] = [
                    1.0 if slot == 0 else 0.0
                    for _ in range(count)
                    for slot in range(animation.MAX_INFLUENCES)
                ]
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


def mesh3d(meshes: list, up_axis: str, unit_scale: float, note: dict,
           clips: list, images: list) -> dict:
    """The content the host draws.

    Numbers travel as packed binary rather than as JSON arrays: a mesh of
    twenty thousand triangles is a quarter of a million numbers, and written
    out as digits that is megabytes of text to parse before anything appears.
    """
    held = note.get("held", note["triangles"])
    short = held > note["triangles"]
    return {
        "kind": "mesh3d",
        "upAxis": up_axis,
        "unitScale": unit_scale,
        "triangles": note["triangles"],
        # A preview that quietly shows a third of a model is a preview that
        # lies. There is a cap, it is reached by real files, and when it is
        # reached the view has to be able to say so.
        "truncated": short,
        "detail": ("%s of the file's %s triangles"
                   % (thousands(note["triangles"]), thousands(held))) if short else "",
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
                # Only where there is a picture to read: on a model with none
                # they are eight bytes a vertex saying nothing, and on a
                # faceted one — where no two corners are ever shared — that is
                # a fifth of everything sent.
                "uvs": _b64_floats(mesh["uvs"]) if mesh.get("image", -1) >= 0
                       and mesh.get("uvs") else "",
                "joints": len(mesh.get("clusters") or [])
                          or mesh.get("joints")
                          or (1 if mesh.get("rigid") else 0),
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


@plugin.viewer("fbx.model", "Model", extensions=MODELS, priority=20)
def model(url: str) -> dict:
    loaded, refusal = _load(url)
    if refusal is not None:
        return refusal

    try:
        parts, note = loaded.meshes()
    except Exception as failure:  # noqa: BLE001 - one odd file is not a crash
        return error("The geometry in this file could not be read: %s" % failure)

    if not parts:
        return error(
            "This file carries no mesh to draw. Shift+F3 lists what it does "
            "carry — a skeleton and its animation, most likely."
        )

    images = _pictures(url, parts)
    facts = loaded.facts()
    return mesh3d(parts, facts["upAxis"], facts["unitScale"], note,
                  loaded.clips(parts), images)


@plugin.viewer("fbx.contents", "What is in the file", extensions=MODELS,
               priority=10)
def contents(url: str) -> dict:
    loaded, refusal = _load(url)
    if refusal is not None:
        return refusal
    return markdown(_report(loaded.facts(), loaded.size, loaded.kind))


def _report(facts: dict, size: int, kind: str = "fbx") -> str:
    lines = []

    version = facts["version"]
    if kind == "gltf":
        lines.append("# glTF 2.0")
    elif kind == "obj":
        lines.append("# Wavefront OBJ")
    else:
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

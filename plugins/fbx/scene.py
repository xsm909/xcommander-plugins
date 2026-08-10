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

"""What the tree in an FBX file *means*.

``fbxfile`` gives back a tree of nodes; almost nothing in FBX is nested inside
the thing it belongs to. A mesh, the material painted on it, the bone that
deforms it and the curve that animates it are all siblings in ``Objects``, and
a separate ``Connections`` list says which belongs to which. This module builds
that graph and answers questions about it.

Step 0 asks only one question — what is in this file — but the graph is the
same one the mesh reader and the animation reader will use, so it lives here
from the start.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from fbxfile import TIME_UNIT, Document, Node

#: What ``TimeMode`` in the global settings means, in frames per second. The
#: gaps are modes that carry their own rate or are not rates at all.
TIME_MODES = {
    1: 120.0, 2: 100.0, 3: 60.0, 4: 50.0, 5: 48.0, 6: 30.0, 7: 30.0, 8: 30.0,
    9: 29.97, 10: 29.97, 11: 25.0, 12: 24.0, 13: 23.976, 14: 96.0, 15: 72.0,
    16: 59.94, 17: 119.88,
}

AXIS_NAMES = {0: "X", 1: "Y", 2: "Z"}


class Obj:
    """One entry of ``Objects``: what it is, what it is called, and its node."""

    __slots__ = ("id", "name", "kind", "subkind", "node")

    def __init__(self, ident: int, name: str, kind: str, subkind: str, node: Node):
        self.id = ident
        self.name = name
        #: The record's own name — ``Model``, ``Geometry``, ``Deformer``…
        self.kind = kind
        #: What sort of one — ``Mesh``, ``LimbNode``, ``Skin``, ``Cluster``…
        self.subkind = subkind
        self.node = node

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Obj(%d, %r, %s/%s)" % (self.id, self.name, self.kind, self.subkind)


def split_name(raw: object) -> tuple:
    """The name and class out of an object's name property.

    The two shapes disagree, and this is the only place that may know it:
    binary writes ``pCube1\\x00\\x01Model`` and text writes ``Model::pCube1``.
    """
    text = raw if isinstance(raw, str) else ""
    if "\x00\x01" in text:
        name, _, cls = text.partition("\x00\x01")
        return name, cls
    if "::" in text:
        cls, _, name = text.partition("::")
        return name, cls
    return text, ""


class Scene:
    """The object graph of one document."""

    def __init__(self, document: Document):
        self.document = document
        self.objects: Dict[int, Obj] = {}
        #: parent id -> children ids, and the reverse. Both are wanted: a mesh
        #: looks up, a skeleton walks down.
        self.children: Dict[int, List[int]] = {}
        self.parents: Dict[int, List[int]] = {}
        #: (child id, property name) for OP connections — how an animation
        #: curve says *which* property it drives.
        self.properties: Dict[int, List[tuple]] = {}

        for node in document.objects():
            ident = node.prop(0)
            if not isinstance(ident, int):
                continue
            name, _cls = split_name(node.prop(1))
            self.objects[ident] = Obj(ident, name, node.name, str(node.prop(2) or ""), node)

        for kind, child, parent, prop in document.connections():
            self.children.setdefault(parent, []).append(child)
            self.parents.setdefault(child, []).append(parent)
            if kind == "OP" and prop:
                self.properties.setdefault(parent, []).append((child, prop))

    # -- getting about ----------------------------------------------------

    def of_kind(self, kind: str, subkind: Optional[str] = None) -> List[Obj]:
        return [
            obj
            for obj in self.objects.values()
            if obj.kind == kind and (subkind is None or obj.subkind == subkind)
        ]

    def children_of(self, ident: int, kind: Optional[str] = None) -> List[Obj]:
        out = []
        for child in self.children.get(ident, ()):
            obj = self.objects.get(child)
            if obj is not None and (kind is None or obj.kind == kind):
                out.append(obj)
        return out

    def parents_of(self, ident: int, kind: Optional[str] = None) -> List[Obj]:
        out = []
        for parent in self.parents.get(ident, ()):
            obj = self.objects.get(parent)
            if obj is not None and (kind is None or obj.kind == kind):
                out.append(obj)
        return out

    # -- the shape of the world -------------------------------------------

    @property
    def settings(self) -> Optional[Node]:
        return self.document.find("GlobalSettings")

    def unit_scale(self) -> float:
        """Centimetres per unit. FBX is usually written in centimetres."""
        settings = self.settings
        value = settings.property70("UnitScaleFactor") if settings else None
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

    def up_axis(self) -> str:
        settings = self.settings
        if settings is None:
            return "Y"
        axis = settings.property70("UpAxis", 1)
        sign = settings.property70("UpAxisSign", 1)
        name = AXIS_NAMES.get(int(axis) if isinstance(axis, (int, float)) else 1, "Y")
        return ("-" if isinstance(sign, (int, float)) and sign < 0 else "") + name

    def frame_rate(self) -> float:
        settings = self.settings
        if settings is None:
            return 30.0
        mode = settings.property70("TimeMode", 6)
        if isinstance(mode, (int, float)) and int(mode) in TIME_MODES:
            return TIME_MODES[int(mode)]
        custom = settings.property70("CustomFrameRate", 30.0)
        try:
            rate = float(custom)
        except (TypeError, ValueError):
            return 30.0
        return rate if rate > 0 else 30.0

    def creator(self) -> str:
        header = self.document.find("FBXHeaderExtension")
        value = self.document.root.value("Creator")
        if value is None and header is not None:
            value = header.value("Creator")
        return str(value or "unknown")


# -- counting what is in a file -----------------------------------------------


def polygon_counts(node: Node) -> tuple:
    """``(vertices, polygons, triangles)`` for one ``Geometry``.

    FBX stores polygons as a flat index list where **the last index of each
    polygon is bitwise-negated** — that is the only marker of where one ends.
    Triangles are what a fan triangulation would produce, which is what a
    preview will draw.
    """
    verts = node.find("Vertices")
    index = node.find("PolygonVertexIndex")
    vertex_count = len(verts.prop(0) or ()) // 3 if verts and isinstance(verts.prop(0), list) else 0
    if not index or not isinstance(index.prop(0), list):
        return vertex_count, 0, 0

    polygons = triangles = corners = 0
    for value in index.prop(0):
        corners += 1
        if value < 0:
            polygons += 1
            triangles += max(0, corners - 2)
            corners = 0
    return vertex_count, polygons, triangles


def stack_seconds(stack: Node) -> float:
    start = stack.property70("LocalStart", 0)
    stop = stack.property70("LocalStop", 0)
    try:
        return max(0.0, (float(stop) - float(start)) / TIME_UNIT)
    except (TypeError, ValueError):
        return 0.0


def summarise(scene: Scene) -> dict:
    """Everything step 0 shows: what the file holds, counted."""
    meshes = []
    for obj in scene.of_kind("Geometry"):
        if obj.subkind not in ("Mesh", ""):
            continue
        vertices, polygons, triangles = polygon_counts(obj.node)
        if vertices == 0 and polygons == 0:
            continue
        # A geometry is nameless as often as not; the model holding it is not.
        holders = [m.name for m in scene.parents_of(obj.id, "Model") if m.name]
        meshes.append({
            "name": obj.name or (holders[0] if holders else "(unnamed)"),
            "vertices": vertices,
            "polygons": polygons,
            "triangles": triangles,
        })
    meshes.sort(key=lambda m: -m["triangles"])

    clips = []
    for stack in scene.of_kind("AnimationStack"):
        layers = scene.children_of(stack.id, "AnimationLayer")
        curves = 0
        keys = 0
        for layer in layers:
            for curve_node in scene.children_of(layer.id, "AnimationCurveNode"):
                for curve in scene.children_of(curve_node.id, "AnimationCurve"):
                    curves += 1
                    times = curve.node.find("KeyTime")
                    if times and isinstance(times.prop(0), list):
                        keys += len(times.prop(0))
        clips.append({
            "name": stack.name or "(unnamed)",
            "seconds": stack_seconds(stack.node),
            "layers": len(layers),
            "curves": curves,
            "keys": keys,
        })

    videos = scene.of_kind("Video")
    embedded = 0
    embedded_bytes = 0
    for video in videos:
        content = video.node.find("Content")
        if content is not None and content.props:
            data = content.prop(0)
            if isinstance(data, (bytes, bytearray)) and data:
                embedded += 1
                embedded_bytes += len(data)

    models = {}
    for model in scene.of_kind("Model"):
        models[model.subkind or "(none)"] = models.get(model.subkind or "(none)", 0) + 1

    skins = scene.of_kind("Deformer", "Skin")
    clusters = [
        obj for obj in scene.objects.values()
        if obj.kind in ("Deformer", "SubDeformer") and obj.subkind == "Cluster"
    ]
    joints = scene.of_kind("Model", "LimbNode")

    return {
        "version": scene.document.version,
        "binary": scene.document.is_binary,
        "creator": scene.creator(),
        "unitScale": scene.unit_scale(),
        "upAxis": scene.up_axis(),
        "frameRate": scene.frame_rate(),
        "objects": len(scene.objects),
        "connections": len(scene.document.connections()),
        "models": models,
        "meshes": meshes,
        "materials": len(scene.of_kind("Material")),
        "textures": len(scene.of_kind("Texture")),
        "embedded": embedded,
        "embeddedBytes": embedded_bytes,
        "skins": len(skins),
        "clusters": len(clusters),
        "joints": len(joints),
        "clips": clips,
    }

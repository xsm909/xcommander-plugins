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

"""Turning FBX geometry into triangles somebody can draw.

Three jobs, and each is a place files go wrong:

- **placing** a mesh, which means composing FBX's nine-part transform chain up
  the node hierarchy;
- **triangulating** polygons, which are stored as a flat index list where the
  last corner of each polygon is bitwise-negated;
- **normals**, which come in four combinations of mapping and reference mode,
  and are missing often enough that computing them has to work too.

The result is world-space triangles with a normal each, in a right-handed
Y-up space whatever the file said — the host draws, and should not have to know
that some files are Z-up.
"""

from __future__ import annotations

import math
import struct
from typing import Dict, List, Optional, Tuple

from fbxfile import Node
from scene import Obj, Scene

# -- 4x4 matrices, row-major, as flat lists of 16 ----------------------------

IDENTITY = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def multiply(a: List[float], b: List[float]) -> List[float]:
    out = [0.0] * 16
    for row in range(4):
        for col in range(4):
            out[row * 4 + col] = (
                a[row * 4 + 0] * b[0 * 4 + col]
                + a[row * 4 + 1] * b[1 * 4 + col]
                + a[row * 4 + 2] * b[2 * 4 + col]
                + a[row * 4 + 3] * b[3 * 4 + col]
            )
    return out


def translation(x: float, y: float, z: float) -> List[float]:
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]


def scaling(x: float, y: float, z: float) -> List[float]:
    return [x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1]


def rotation(degrees: Tuple[float, float, float], order: int = 0) -> List[float]:
    """Euler angles in degrees, in FBX's own rotation orders.

    The order is read the way it is named: `eEulerXYZ` turns about X first.
    FBX's own documentation writes that chain backwards — `R = Rz * Ry * Rx` —
    because it multiplies column vectors, and everything here is a row vector
    meeting its matrices left to right. Same rotation, written the other way
    round; writing it the documented way round is a rotation nobody asked for.
    """
    x, y, z = (math.radians(v) for v in degrees)
    sx, cx = math.sin(x), math.cos(x)
    sy, cy = math.sin(y), math.cos(y)
    sz, cz = math.sin(z), math.cos(z)

    rx = [1, 0, 0, 0, 0, cx, sx, 0, 0, -sx, cx, 0, 0, 0, 0, 1]
    ry = [cy, 0, -sy, 0, 0, 1, 0, 0, sy, 0, cy, 0, 0, 0, 0, 1]
    rz = [cz, sz, 0, 0, -sz, cz, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]

    sequence = {
        0: (rx, ry, rz), 1: (rx, rz, ry), 2: (ry, rz, rx),
        3: (ry, rx, rz), 4: (rz, rx, ry), 5: (rz, ry, rx),
    }.get(order, (rx, ry, rz))
    return multiply(multiply(sequence[0], sequence[1]), sequence[2])


def inverse_rotation(m: List[float]) -> List[float]:
    """A rotation undone, which for a rotation is its transpose."""
    return [
        m[0], m[4], m[8], 0,
        m[1], m[5], m[9], 0,
        m[2], m[6], m[10], 0,
        0, 0, 0, 1,
    ]


def transform_point(m: List[float], x: float, y: float, z: float) -> Tuple[float, float, float]:
    return (
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14],
    )


def transform_direction(m: List[float], x: float, y: float, z: float) -> Tuple[float, float, float]:
    """Without the translation. Not the inverse transpose — good enough for a
    preview, and wrong only under non-uniform scale."""
    return (
        m[0] * x + m[4] * y + m[8] * z,
        m[1] * x + m[5] * y + m[9] * z,
        m[2] * x + m[6] * y + m[10] * z,
    )


def _vector(node: Node, name: str, default=(0.0, 0.0, 0.0)) -> Tuple[float, float, float]:
    value = node.property70(name)
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (float(value[0]), float(value[1]), float(value[2]))
    return default


def compose(node: Node, t, r, s) -> List[float]:
    """One node's own transform, with its three animatable parts supplied.

    Nine parts, not three. Most files leave the offsets and pivots at zero, but
    the ones that do not are exactly the ones that come out of Maya looking
    right everywhere except here.

    **The chain is written in the order things happen**: scaled about the
    scaling pivot, turned about the rotation pivot, and only then put where the
    file says. FBX's own documentation prints the same nine parts backwards,
    `T * Roff * Rp * … * Sp⁻¹`, because it multiplies column vectors; every
    matrix here is met by a row vector from the left. Writing it the documented
    way round leaves a node's own translation to be rotated and scaled by that
    same node — and a preview frames whatever it is handed, so a single mesh
    thrown thousands of units away still fills the window and looks perfectly
    correct. It was found by measuring a cube, not by looking at one.
    """
    order = node.property70("RotationOrder", 0)
    order = int(order) if isinstance(order, (int, float)) else 0

    roff = _vector(node, "RotationOffset")
    rpiv = _vector(node, "RotationPivot")
    soff = _vector(node, "ScalingOffset")
    spiv = _vector(node, "ScalingPivot")
    pre = _vector(node, "PreRotation")
    post = _vector(node, "PostRotation")

    m = translation(*(-v for v in spiv))
    m = multiply(m, scaling(*s))
    m = multiply(m, translation(*spiv))
    m = multiply(m, translation(*soff))
    m = multiply(m, translation(*(-v for v in rpiv)))
    m = multiply(m, inverse_rotation(rotation(post, order)))
    m = multiply(m, rotation(r, order))
    m = multiply(m, rotation(pre, order))
    m = multiply(m, translation(*rpiv))
    m = multiply(m, translation(*roff))
    m = multiply(m, translation(*t))
    return m


def local_transform(node: Node) -> List[float]:
    """One node's own transform, as the file has it standing still."""
    return compose(
        node,
        _vector(node, "Lcl Translation"),
        _vector(node, "Lcl Rotation"),
        _vector(node, "Lcl Scaling", (1.0, 1.0, 1.0)),
    )


def geometric_transform(node: Node) -> List[float]:
    """The extra transform that moves a model's geometry but not its children.

    Maya has no such idea, so it is usually identity; when it is not, ignoring
    it puts the mesh somewhere the file never said.
    """
    t = _vector(node, "GeometricTranslation")
    r = _vector(node, "GeometricRotation")
    s = _vector(node, "GeometricScaling", (1.0, 1.0, 1.0))
    if t == (0, 0, 0) and r == (0, 0, 0) and s == (1, 1, 1):
        return IDENTITY
    return multiply(multiply(scaling(*s), rotation(r)), translation(*t))


# -- layers ------------------------------------------------------------------


def _layer_values(layer: Optional[Node], name: str) -> Tuple[List[float], List[int], str, str]:
    if layer is None:
        return [], [], "", ""
    direct = layer.find(name)
    index = layer.find(name + "Index")
    mapping = str(layer.value("MappingInformationType") or "")
    reference = str(layer.value("ReferenceInformationType") or "")
    values = direct.prop(0) if direct and isinstance(direct.prop(0), list) else []
    indices = index.prop(0) if index and isinstance(index.prop(0), list) else []
    return values, indices, mapping, reference


class _Normals:
    """Answers "what is the normal at this corner", however the file said it."""

    def __init__(self, geometry: Node):
        self.values, self.indices, self.mapping, self.reference = _layer_values(
            geometry.find("LayerElementNormal"), "Normals"
        )
        self.usable = bool(self.values)

    def at(self, corner: int, vertex: int) -> Optional[Tuple[float, float, float]]:
        if not self.usable:
            return None
        if self.mapping == "ByVertice" or self.mapping == "ByVertex":
            key = vertex
        elif self.mapping == "ByPolygonVertex":
            key = corner
        else:
            return None
        if self.reference in ("IndexToDirect", "Index"):
            if key >= len(self.indices):
                return None
            key = self.indices[key]
        base = key * 3
        if base + 2 >= len(self.values):
            return None
        return (self.values[base], self.values[base + 1], self.values[base + 2])


# -- meshes ------------------------------------------------------------------


def _normalise(x: float, y: float, z: float) -> Tuple[float, float, float]:
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        return (0.0, 1.0, 0.0)
    return (x / length, y / length, z / length)


class MeshBuilder:
    """Collects triangle corners, sharing the ones that are truly identical."""

    def __init__(self):
        self.positions: List[float] = []
        self.normals: List[float] = []
        self.indices: List[int] = []
        #: Which vertex of the *file* each of ours came from. Sharing corners
        #: renumbers everything, and skin weights are given against the
        #: original numbering — without this they would land on the wrong
        #: vertices, which looks like a badly rigged model rather than a bug.
        self.sources: List[int] = []
        self._seen: Dict[tuple, int] = {}

    def corner(self, position: Tuple[float, float, float], normal: Tuple[float, float, float],
               source: int = -1) -> int:
        # Rounded, because two corners of the same seam differ in the last bit
        # and sharing them is the difference between 50 000 vertices and 12 000.
        key = (
            round(position[0], 5), round(position[1], 5), round(position[2], 5),
            round(normal[0], 3), round(normal[1], 3), round(normal[2], 3),
        )
        index = self._seen.get(key)
        if index is None:
            index = len(self.positions) // 3
            self._seen[key] = index
            self.positions.extend(position)
            self.normals.extend(normal)
            self.sources.append(source)
        return index

    def triangle(self, a: int, b: int, c: int) -> None:
        self.indices.extend((a, b, c))


def _global_transform(scene: Scene, model_id: int, cache: Dict[int, List[float]]) -> List[float]:
    known = cache.get(model_id)
    if known is not None:
        return known

    obj = scene.objects.get(model_id)
    if obj is None or obj.kind != "Model":
        return IDENTITY

    # Guard against a cycle before recursing: a malformed file must not hang
    # the plugin, and the host has a sixty-second patience.
    cache[model_id] = IDENTITY
    parents = [p for p in scene.parents_of(model_id, "Model")]
    parent = _global_transform(scene, parents[0].id, cache) if parents else IDENTITY

    result = multiply(local_transform(obj.node), parent)
    cache[model_id] = result
    return result


def _colour_of(scene: Scene, model_id: int) -> str:
    for material in scene.children_of(model_id, "Material"):
        value = material.node.property70("DiffuseColor")
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return "#%02X%02X%02X" % tuple(
                max(0, min(255, int(round(float(c) * 255)))) for c in value[:3]
            )
    return ""


def _axis_fix(scene: Scene) -> List[float]:
    """Whatever the file calls up, the answer is Y.

    A preview that draws a Z-up file lying on its face is a preview nobody
    trusts, and the host has no business knowing which files those are.
    """
    settings = scene.settings
    if settings is None:
        return IDENTITY
    up = settings.property70("UpAxis", 1)
    up = int(up) if isinstance(up, (int, float)) else 1
    if up == 2:  # Z is up: tip it back a quarter turn about X.
        return rotation((-90.0, 0.0, 0.0))
    if up == 0:  # X is up, which is rare and still has to land somewhere.
        return rotation((0.0, 0.0, 90.0))
    return IDENTITY


def meshes(scene: Scene, max_triangles: int = 400000) -> Tuple[List[dict], dict]:
    """Every drawable mesh in the file, placed, triangulated and shaded flat.

    Returns the meshes and a note of what had to be left out, because a preview
    that silently drops half a model is worse than one that says so.
    """
    cache: Dict[int, List[float]] = {}
    fix = _axis_fix(scene)
    out: List[dict] = []
    total = 0
    dropped = 0

    for geometry in scene.of_kind("Geometry"):
        if geometry.subkind not in ("Mesh", ""):
            continue
        vertices_node = geometry.node.find("Vertices")
        index_node = geometry.node.find("PolygonVertexIndex")
        if not vertices_node or not index_node:
            continue
        vertices = vertices_node.prop(0)
        corners = index_node.prop(0)
        if not isinstance(vertices, list) or not isinstance(corners, list):
            continue

        holders = scene.parents_of(geometry.id, "Model")
        holder = holders[0] if holders else None
        unfixed = IDENTITY
        if holder is not None:
            unfixed = multiply(
                geometric_transform(holder.node),
                _global_transform(scene, holder.id, cache),
            )
        placement = multiply(unfixed, fix)

        normals = _Normals(geometry.node)
        builder = MeshBuilder()
        polygon: List[Tuple[int, int]] = []
        triangles = 0

        for position, raw in enumerate(corners):
            vertex = ~raw if raw < 0 else raw
            polygon.append((position, vertex))
            if raw >= 0:
                continue

            triangles += max(0, len(polygon) - 2)
            if total + triangles > max_triangles:
                dropped += 1
                polygon = []
                break

            placed = []
            for corner_index, vertex_index in polygon:
                base = vertex_index * 3
                if base + 2 >= len(vertices):
                    placed.append(None)
                    continue
                point = transform_point(
                    placement, vertices[base], vertices[base + 1], vertices[base + 2]
                )
                given = normals.at(corner_index, vertex_index)
                if given is not None:
                    direction = _normalise(*transform_direction(placement, *given))
                else:
                    direction = None
                placed.append((point, direction, vertex_index))

            if any(entry is None for entry in placed) or len(placed) < 3:
                polygon = []
                continue

            # A polygon with no normals of its own gets the one its own plane
            # implies, which is what flat shading means.
            if placed[0][1] is None:
                (ax, ay, az) = placed[0][0]
                (bx, by, bz) = placed[1][0]
                (cx, cy, cz) = placed[2][0]
                ux, uy, uz = bx - ax, by - ay, bz - az
                vx, vy, vz = cx - ax, cy - ay, cz - az
                face = _normalise(
                    uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                )
                placed = [(point, face, source) for point, _, source in placed]

            fan = [
                builder.corner(point, direction, source)
                for point, direction, source in placed
            ]
            for i in range(1, len(fan) - 1):
                builder.triangle(fan[0], fan[i], fan[i + 1])
            polygon = []

        if not builder.indices:
            continue
        total += len(builder.indices) // 3
        out.append({
            "name": (holder.name if holder and holder.name else geometry.name) or "(unnamed)",
            "positions": builder.positions,
            "normals": builder.normals,
            "indices": builder.indices,
            "sources": builder.sources,
            "sourceCount": len(vertices) // 3,
            "geometryId": geometry.id,
            # Where the mesh was placed, before the axis fix — the skinning
            # maths needs to undo exactly this and no more.
            "placement_no_fix": unfixed,
            "color": _colour_of(scene, holder.id) if holder else "",
        })

    return out, {"droppedMeshes": dropped, "triangles": total}


def pack_floats(values: List[float]) -> bytes:
    return struct.pack("<%df" % len(values), *values)


def pack_indices(values: List[int]) -> bytes:
    return struct.pack("<%dI" % len(values), *values)

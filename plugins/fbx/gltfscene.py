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

"""A glTF scene, turned into the meshes the host already knows how to draw.

**The point of this file is that it produces exactly what `geometry.meshes`
produces** — the same list of dicts, the same keys — so nothing downstream of it
knows which format the model came out of. The payload assembly, the picture
finding, the truncation report and the whole of the host are untouched.

What that costs is one place per format that has to be right about its own
conventions, and this is glTF's:

- already Y-up and right-handed, so no axis fix;
- picture coordinates already run down from the top, so no turning over;
- matrices are stored column-major for column vectors, which read in order is
  the row-major matrix for row vectors — the same sixteen numbers;
- rotations are quaternions.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import geometry
from geometry import IDENTITY, MeshBuilder, multiply, transform_direction, transform_point
from gltffile import Bytes, Document, FAN, STRIP, TRIANGLES


def _quaternion(x: float, y: float, z: float, w: float) -> List[float]:
    """A rotation as a row-vector matrix, from the four numbers glTF stores."""
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return [
        1 - 2 * (yy + zz), 2 * (xy + wz), 2 * (xz - wy), 0.0,
        2 * (xy - wz), 1 - 2 * (xx + zz), 2 * (yz + wx), 0.0,
        2 * (xz + wy), 2 * (yz - wx), 1 - 2 * (xx + yy), 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def local_of(node: dict) -> List[float]:
    """One node's own transform, whichever way it is written.

    A node gives either a matrix or the three parts; the specification says it
    may not give both, and a file that does is read by its matrix.
    """
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) >= 16:
        return [float(v) for v in matrix[:16]]

    out = IDENTITY
    scale = node.get("scale")
    if isinstance(scale, list) and len(scale) >= 3:
        out = geometry.scaling(float(scale[0]), float(scale[1]), float(scale[2]))
    rotation = node.get("rotation")
    if isinstance(rotation, list) and len(rotation) >= 4:
        out = multiply(out, _quaternion(*(float(v) for v in rotation[:4])))
    move = node.get("translation")
    if isinstance(move, list) and len(move) >= 3:
        out = multiply(out, geometry.translation(
            float(move[0]), float(move[1]), float(move[2])))
    return out


def placements(document: Document) -> Dict[int, List[float]]:
    """Where every node ends up, walked down from the scene's roots.

    Down rather than up: glTF writes children, not parents, so a node does not
    know where it hangs from. A node reached twice — which the format forbids
    and files still do — keeps the first place it was given, and a cycle cannot
    spin because a node is only walked into once.
    """
    nodes = document.listed("nodes")
    scene = document.entry("scenes", document.json.get("scene", 0)) or {}
    roots = scene.get("nodes")
    if not isinstance(roots, list) or not roots:
        # No scene worth the name: take every node that nobody claims a child.
        claimed = set()
        for node in nodes:
            if isinstance(node, dict):
                for child in node.get("children") or []:
                    claimed.add(int(child))
        roots = [i for i in range(len(nodes)) if i not in claimed]

    out: Dict[int, List[float]] = {}
    stack = [(int(i), IDENTITY) for i in reversed(roots)]
    while stack:
        index, parent = stack.pop()
        if index in out or not 0 <= index < len(nodes):
            continue
        node = nodes[index] if isinstance(nodes[index], dict) else {}
        here = multiply(local_of(node), parent)
        out[index] = here
        for child in reversed(node.get("children") or []):
            stack.append((int(child), here))
    return out


def bind_of(document: Document, held: Bytes, skin: Optional[int],
            where: Dict[int, List[float]]) -> Optional[List[float]]:
    """Where a skin leaves its mesh when nothing is animating it.

    `IBM · G` for any joint, the two being inverses of one another at bind time
    but for whatever the file has been scaled or moved by since — which is the
    whole of what this is for. The first joint that answers is enough: a rig
    whose joints disagree about it is a rig with a broken bind pose, and this is
    a preview.
    """
    entry = document.entry("skins", skin)
    if entry is None:
        return None
    joints = [int(j) for j in (entry.get("joints") or [])]
    flat = held.read(entry.get("inverseBindMatrices"))
    for slot, joint in enumerate(joints):
        if (slot + 1) * 16 > len(flat):
            break
        ibm = list(flat[slot * 16:(slot + 1) * 16])
        return multiply(ibm, where.get(joint, IDENTITY))
    return None


def _colour(material: Optional[dict]) -> str:
    if not material:
        return ""
        # Nothing said is nothing said: the host puts its own colour on it.
    pbr = material.get("pbrMetallicRoughness") or {}
    base = pbr.get("baseColorFactor")
    if not isinstance(base, list) or len(base) < 3:
        return ""
    # glTF writes colour in linear light and every picture here is drawn in
    # sRGB, so it is brought across rather than copied.
    return "#%02X%02X%02X" % tuple(
        max(0, min(255, int(round(_srgb(float(c)) * 255)))) for c in base[:3]
    )


def _srgb(value: float) -> float:
    if value <= 0.0031308:
        return max(0.0, 12.92 * value)
    return min(1.0, 1.055 * (value ** (1 / 2.4)) - 0.055)


def _picture(document: Document, held: Bytes, material: Optional[dict]) -> Optional[dict]:
    """The bitmap a material is coloured from, if it has one."""
    if not material:
        return None
    pbr = material.get("pbrMetallicRoughness") or {}
    slot = pbr.get("baseColorTexture")
    if not isinstance(slot, dict):
        return None
    texture = document.entry("textures", slot.get("index"))
    if texture is None:
        return None
    picture = held.image(texture.get("source"))
    if picture is None:
        return None

    # KHR_texture_transform, which is how glTF says what FBX says with Scaling
    # and Translation — and it is a multiplier here, not a tile size.
    laid = ((slot.get("extensions") or {}).get("KHR_texture_transform")) or {}
    scale = laid.get("scale")
    offset = laid.get("offset")
    if isinstance(scale, list) and len(scale) >= 2:
        picture["scale"] = (1.0 / float(scale[0]) if float(scale[0]) else 1.0,
                           1.0 / float(scale[1]) if float(scale[1]) else 1.0)
    if isinstance(offset, list) and len(offset) >= 2:
        picture["offset"] = (float(offset[0]), float(offset[1]))
    return picture


def _fan(corners: List[int], mode: int) -> List[Tuple[int, int, int]]:
    """A primitive's corners as triangles, in whichever way it lists them."""
    out = []
    if mode == TRIANGLES:
        for i in range(0, len(corners) - 2, 3):
            out.append((corners[i], corners[i + 1], corners[i + 2]))
    elif mode == STRIP:
        for i in range(len(corners) - 2):
            a, b, c = corners[i], corners[i + 1], corners[i + 2]
            out.append((a, b, c) if i % 2 == 0 else (b, a, c))
    elif mode == FAN:
        for i in range(1, len(corners) - 1):
            out.append((corners[0], corners[i], corners[i + 1]))
    return out


def meshes(document: Document, held: Bytes,
           max_triangles: int = 400000) -> Tuple[List[dict], dict]:
    """Every drawable primitive in the file, placed and triangulated.

    One entry per primitive rather than per mesh, which is what glTF already
    is: a primitive carries one material, and one material is one drawing call.
    That is the same split `geometry.meshes` has to work for by hand.
    """
    where = placements(document)
    nodes = document.listed("nodes")
    out: List[dict] = []
    total = 0
    dropped = 0
    held_triangles = 0

    for index in sorted(where):
        node = nodes[index] if isinstance(nodes[index], dict) else {}
        mesh = document.entry("meshes", node.get("mesh"))
        if mesh is None:
            continue

        # What gets baked into the vertices, and it is not always the node's
        # own place.
        #
        # The specification says a skinned mesh is not placed by its node — its
        # vertices are in the skeleton's space, and the joints put them
        # somewhere. But the host is handed positions *and* a matrix per joint,
        # and both have to speak about the same space or the model is drawn at
        # one scale and posed at another. It was: a character whose vertices
        # spanned ±95 posed into ±1, so the framing, which is worked out from
        # the vertices, zoomed out a hundredfold and drew him a pixel high.
        #
        # So the rig's own resting transform is baked in and taken back out of
        # every joint matrix, exactly as the FBX side does with a mesh's
        # placement. `IBM · G` at rest is that transform by construction, and
        # taking it from the skin rather than from the node means it agrees with
        # the matrices whatever the node happens to say. On the model this was
        # measured on the two are the same 0.0107 either way.
        placement = where[index]
        if node.get("skin") is not None:
            placement = bind_of(document, held, node.get("skin"), where) or placement
        name = str(node.get("name") or mesh.get("name") or "") or "(unnamed)"
        parts = mesh.get("primitives")
        if not isinstance(parts, list):
            continue

        for slot, primitive in enumerate(parts):
            if not isinstance(primitive, dict):
                continue
            mode = int(primitive.get("mode", TRIANGLES))
            attributes = primitive.get("attributes") or {}
            positions = held.read(attributes.get("POSITION"))
            if not positions:
                continue
            normals = held.read(attributes.get("NORMAL"))
            uvs = held.read(attributes.get("TEXCOORD_0"))

            corners = [int(v) for v in held.read(primitive.get("indices"))]
            if not corners:
                corners = list(range(len(positions) // 3))
            triangles = _fan(corners, mode)
            held_triangles += len(triangles)
            if mode not in (TRIANGLES, STRIP, FAN) or not triangles:
                # Lines and points are real parts of the format and nothing
                # here can draw them; saying so beats drawing nothing.
                dropped += 1
                continue
            if total + len(triangles) > max_triangles:
                triangles = triangles[:max(0, max_triangles - total)]
                dropped += 1

            skin = node.get("skin")
            builder = MeshBuilder()
            for triangle in triangles:
                fan = []
                for corner in triangle:
                    base = corner * 3
                    if base + 2 >= len(positions):
                        fan = []
                        break
                    point = transform_point(placement, positions[base],
                                            positions[base + 1], positions[base + 2])
                    if base + 2 < len(normals):
                        direction = geometry._normalise(*transform_direction(
                            placement, normals[base], normals[base + 1],
                            normals[base + 2]))
                    else:
                        direction = None
                    at = corner * 2
                    # Already the right way up: glTF counts its picture
                    # coordinates down from the top, as pictures are drawn.
                    picture = ((uvs[at], uvs[at + 1])
                               if at + 1 < len(uvs) else (0.0, 0.0))
                    fan.append((point, direction, picture, corner))
                if len(fan) < 3:
                    continue
                if fan[0][1] is None:
                    (ax, ay, az) = fan[0][0]
                    (bx, by, bz) = fan[1][0]
                    (cx, cy, cz) = fan[2][0]
                    ux, uy, uz = bx - ax, by - ay, bz - az
                    vx, vy, vz = cx - ax, cy - ay, cz - az
                    face = geometry._normalise(uy * vz - uz * vy,
                                               uz * vx - ux * vz,
                                               ux * vy - uy * vx)
                    fan = [(p, face, uv, source) for p, _n, uv, source in fan]
                made = [builder.corner(p, n, uv, source) for p, n, uv, source in fan]
                builder.triangle(made[0], made[1], made[2])

            if not builder.indices:
                continue
            material = document.entry("materials", primitive.get("material"))
            picture = _picture(document, held, material)
            if picture:
                geometry._lay(builder.uvs, picture)
            total += len(builder.indices) // 3
            entry = {
                "name": name if len(parts) < 2 else "%s · %d" % (name, slot),
                "positions": builder.positions,
                "normals": builder.normals,
                "uvs": builder.uvs,
                "indices": builder.indices,
                "sources": builder.sources,
                "sourceCount": len(positions) // 3,
                "geometryId": index * 1000 + slot,
                "modelId": index,
                "placement_no_fix": placement,
                "color": _colour(material),
                "picture": picture,
            }
            # What pulls on it, if anything does. Kept here because this is the
            # only place that knows where each of our vertices came from.
            if skin is not None:
                import gltfanim  # here, because it reads this module in turn
                bones, pulls = gltfanim.influences(
                    held, primitive, builder.sources, len(builder.positions) // 3)
                if bones:
                    entry["skin"] = int(skin)
                    entry["jointIndices"] = bones
                    entry["jointWeights"] = pulls
                    joints = document.entry("skins", int(skin)) or {}
                    entry["joints"] = len(joints.get("joints") or [])
                    entry["bones"], entry["boneParents"] = gltfanim.skeleton(
                        document, held, int(skin), placement)
            out.append(entry)

    return out, {"droppedMeshes": dropped, "triangles": total,
                 "held": max(total, held_triangles)}


def summarise(document: Document, held: Bytes, size: int) -> dict:
    """What is in the file, for the report Shift+F3 shows."""
    counted = []
    for mesh in document.listed("meshes"):
        if not isinstance(mesh, dict):
            continue
        triangles = 0
        vertices = 0
        for primitive in mesh.get("primitives") or []:
            if not isinstance(primitive, dict):
                continue
            attributes = primitive.get("attributes") or {}
            entry = document.entry("accessors", attributes.get("POSITION")) or {}
            vertices += int(entry.get("count", 0))
            indexed = document.entry("accessors", primitive.get("indices"))
            count = int(indexed.get("count", 0)) if indexed else int(entry.get("count", 0))
            triangles += count // 3
        counted.append({"name": str(mesh.get("name") or "(unnamed)"),
                        "vertices": vertices, "polygons": triangles,
                        "triangles": triangles})

    embedded = 0
    embedded_bytes = 0
    for i, image in enumerate(document.listed("images")):
        if not isinstance(image, dict):
            continue
        picture = held.image(i)
        if picture and "bytes" in picture:
            embedded += 1
            embedded_bytes += len(picture["bytes"])

    asset = document.json.get("asset") or {}
    return {
        "version": 2000,
        "binary": document.binary,
        "creator": str(asset.get("generator") or "unknown"),
        "unitScale": 100.0,  # glTF is metres; the rest of this speaks centimetres
        "upAxis": "Y",
        "frameRate": 30.0,
        "objects": sum(len(document.listed(name)) for name in
                       ("nodes", "meshes", "materials", "textures", "images",
                        "skins", "animations")),
        "connections": len(document.listed("nodes")),
        "models": {"Mesh": len(document.listed("meshes")),
                   "Node": len(document.listed("nodes"))},
        "meshes": counted,
        "materials": len(document.listed("materials")),
        "textures": len(document.listed("textures")),
        "embedded": embedded,
        "embeddedBytes": embedded_bytes,
        "skins": len(document.listed("skins")),
        "clusters": sum(len((s or {}).get("joints") or [])
                        for s in document.listed("skins") if isinstance(s, dict)),
        "joints": sum(len((s or {}).get("joints") or [])
                      for s in document.listed("skins") if isinstance(s, dict)),
        "clips": [{"name": str((a or {}).get("name") or "(unnamed)"),
                   "seconds": 0.0, "layers": 1,
                   "curves": len((a or {}).get("channels") or []),
                   "keys": 0}
                  for a in document.listed("animations") if isinstance(a, dict)],
    }

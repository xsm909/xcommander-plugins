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

"""Wavefront OBJ, and the `.mtl` beside it.

The oldest and simplest of them: one line per thing, in text. No animation, no
skeleton, no hierarchy — a bag of polygons with names on it. What it does have
and what has to be right:

- **indices count from one, and may be negative**, meaning "so many back from
  here". A file that uses the negative form and a reader that does not is off by
  the whole model.
- **a face corner is `v/vt/vn`** with either of the last two missing, so
  `1//2` is a corner with a normal and no picture coordinate.
- **faces may have any number of corners**, and are fanned like FBX's.
- **`usemtl` cuts the mesh**, exactly as a material slot does in the other two
  formats, so one file arrives as several meshes.
- the picture coordinates run **up** from the bottom, as FBX's do, so they are
  turned over here.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import geometry
from geometry import MeshBuilder, IDENTITY


def _numbers(parts: List[str], count: int) -> List[float]:
    out = []
    for text in parts[:count]:
        try:
            out.append(float(text))
        except ValueError:
            out.append(0.0)
    while len(out) < count:
        out.append(0.0)
    return out


def _index(text: str, held: int) -> Optional[int]:
    """One index of a face corner, counted from one or from the end."""
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    if value > 0:
        return value - 1
    if value < 0:
        return held + value
    return None


def materials(text: str) -> Dict[str, dict]:
    """What an `.mtl` says: a colour and a picture per name.

    Only the diffuse pair is read — `Kd` and `map_Kd`. A preview draws a colour
    and one picture; the rest of what an mtl can say is for a renderer.
    """
    out: Dict[str, dict] = {}
    current: Optional[dict] = None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        word = parts[0].lower()
        if word == "newmtl" and len(parts) > 1:
            current = out.setdefault(" ".join(parts[1:]), {})
        elif current is None:
            continue
        elif word == "kd" and len(parts) >= 4:
            r, g, b = _numbers(parts[1:], 3)
            current["color"] = "#%02X%02X%02X" % tuple(
                max(0, min(255, int(round(v * 255)))) for v in (r, g, b))
        elif word in ("map_kd", "map_ka") and len(parts) > 1:
            # An mtl may put options before the name; the name is the last part.
            name = parts[-1].replace("\\", "/")
            current.setdefault("picture", {"beside": name,
                                           "name": name.rsplit("/", 1)[-1]})
    return out


def meshes(data: bytes, resolve: Optional[Callable[[str], bytes]] = None,
           max_triangles: int = 400000) -> Tuple[List[dict], dict]:
    """Every group of faces in the file, cut by material, in the usual shape."""
    text = data.decode("utf-8", "replace")

    points: List[float] = []
    uvs: List[float] = []
    normals: List[float] = []
    named: Dict[str, dict] = {}
    builders: Dict[Tuple[str, str], MeshBuilder] = {}
    order: List[Tuple[str, str]] = []
    group = ""
    material = ""
    held = 0
    total = 0
    dropped = 0

    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        word = parts[0].lower()

        if word == "v":
            points.extend(_numbers(parts[1:], 3))
        elif word == "vt":
            u, v = _numbers(parts[1:], 2)
            # Up from the bottom in the file, down from the top for a picture.
            uvs.extend((u, 1.0 - v))
        elif word == "vn":
            normals.extend(_numbers(parts[1:], 3))
        elif word in ("o", "g"):
            group = " ".join(parts[1:]) if len(parts) > 1 else ""
        elif word == "usemtl":
            material = " ".join(parts[1:]) if len(parts) > 1 else ""
        elif word == "mtllib" and resolve is not None:
            for name in parts[1:]:
                try:
                    named.update(materials(resolve(name).decode("utf-8", "replace")))
                except Exception:  # noqa: BLE001 - a missing mtl is not a crash
                    pass
        elif word == "f" and len(parts) >= 4:
            held += 1
            corners = []
            for corner in parts[1:]:
                bits = corner.split("/")
                v = _index(bits[0], len(points) // 3)
                t = _index(bits[1], len(uvs) // 2) if len(bits) > 1 else None
                n = _index(bits[2], len(normals) // 3) if len(bits) > 2 else None
                if v is None or not 0 <= v * 3 + 2 < len(points):
                    corners = []
                    break
                point = (points[v * 3], points[v * 3 + 1], points[v * 3 + 2])
                picture = (uvs[t * 2], uvs[t * 2 + 1]) \
                    if t is not None and 0 <= t * 2 + 1 < len(uvs) else (0.0, 0.0)
                direction = (normals[n * 3], normals[n * 3 + 1], normals[n * 3 + 2]) \
                    if n is not None and 0 <= n * 3 + 2 < len(normals) else None
                corners.append((point, direction, picture, v))
            if len(corners) < 3:
                continue

            triangles = len(corners) - 2
            if total + triangles > max_triangles:
                dropped += 1
                continue

            if corners[0][1] is None:
                (ax, ay, az) = corners[0][0]
                (bx, by, bz) = corners[1][0]
                (cx, cy, cz) = corners[2][0]
                ux, uy, uz = bx - ax, by - ay, bz - az
                vx, vy, vz = cx - ax, cy - ay, cz - az
                face = geometry._normalise(uy * vz - uz * vy, uz * vx - ux * vz,
                                           ux * vy - uy * vx)
                corners = [(p, face, uv, s) for p, _n, uv, s in corners]

            key = (group, material)
            builder = builders.get(key)
            if builder is None:
                builder = builders[key] = MeshBuilder()
                order.append(key)
            fan = [builder.corner(p, geometry._normalise(*n), uv, s)
                   for p, n, uv, s in corners]
            for i in range(1, len(fan) - 1):
                builder.triangle(fan[0], fan[i], fan[i + 1])
            total += triangles

    out = []
    for slot, key in enumerate(order):
        builder = builders[key]
        if not builder.indices:
            continue
        group, material = key
        surface = named.get(material) or {}
        picture = dict(surface["picture"]) if surface.get("picture") else None
        if picture:
            geometry._lay(builder.uvs, picture)
        name = " · ".join(part for part in (group, material) if part) or "(unnamed)"
        out.append({
            "name": name,
            "positions": builder.positions,
            "normals": builder.normals,
            "uvs": builder.uvs,
            "indices": builder.indices,
            "sources": builder.sources,
            "sourceCount": len(points) // 3,
            "geometryId": slot,
            "modelId": None,
            "placement_no_fix": list(IDENTITY),
            "color": surface.get("color", ""),
            "picture": picture,
        })

    return out, {"droppedMeshes": dropped, "triangles": total,
                 "held": max(total, held)}


def summarise(data: bytes, parts: List[dict]) -> dict:
    """What is in the file, for the report."""
    return {
        "version": 0,
        "binary": False,
        "creator": "unknown",
        "unitScale": 1.0,
        "upAxis": "Y",
        "frameRate": 30.0,
        "objects": len(parts),
        "connections": 0,
        "models": {"Mesh": len(parts)},
        "meshes": [{"name": mesh["name"],
                    "vertices": len(mesh["positions"]) // 3,
                    "polygons": len(mesh["indices"]) // 3,
                    "triangles": len(mesh["indices"]) // 3}
                   for mesh in parts],
        "materials": len({mesh["color"] for mesh in parts if mesh["color"]}),
        "textures": len([1 for mesh in parts if mesh["picture"]]),
        "embedded": 0,
        "embeddedBytes": 0,
        "skins": 0,
        "clusters": 0,
        "joints": 0,
        "clips": [],
    }

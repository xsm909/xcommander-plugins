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

"""glTF skins and clips, baked into the frames the host already plays.

The arithmetic is shorter than FBX's and the reason is worth saying: glTF hands
over the inverse bind matrix directly, so there is none of the "what is
`Transform` really" archaeology that cost a session there. What a joint does at
a moment is

    skin(t) = IBM · G(t)

with `G` the joint node's global transform — and both are written here as row
vectors, so they meet left to right in that order.

To that goes one thing more, and it is not decoration. The host is handed
positions *and* a matrix per joint, and the two must speak about the same space.
A real character's vertices spanned ±95 and posed into ±1, because the file
scales its whole rig at the root and the inverse bind matrices were written
before that. So whatever resting transform is baked into the vertices is taken
back out of every joint matrix here: they cancel exactly, the pose is what the
file describes, and **at rest the matrix comes out the identity** — the same
invariant the FBX side is checked by.

Three differences from FBX that have to be got right:

- **times are seconds**, not units of 1/46186158000 of one;
- **rotations are quaternions**, so between two keys they are turned along the
  short way round rather than mixed number by number — mixing them shrinks the
  rotation towards the middle and makes a limb wobble;
- **`CUBICSPLINE` keeps three values per key**, the tangent before, the value,
  and the tangent after.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import geometry
from animation import invert
from geometry import IDENTITY, multiply
from gltffile import Bytes, Document
from gltfscene import local_of

#: Sampled, as FBX's are, and for the same reason: the host plays frames.
MAX_FRAMES = 300
SAMPLE_FPS = 30.0
MAX_INFLUENCES = 4


def _slerp(a: List[float], b: List[float], blend: float) -> List[float]:
    """Between two rotations, the short way round."""
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0.0:
        b = [-x for x in b]
        dot = -dot
    if dot > 0.9995:
        out = [x + (y - x) * blend for x, y in zip(a, b)]
    else:
        angle = math.acos(max(-1.0, min(1.0, dot)))
        sin = math.sin(angle)
        first = math.sin((1 - blend) * angle) / sin
        second = math.sin(blend * angle) / sin
        out = [x * first + y * second for x, y in zip(a, b)]
    length = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / length for x in out]


class Sampler:
    """One channel's keys, and how to read between them."""

    __slots__ = ("times", "values", "width", "kind", "_at")

    def __init__(self, times: List[float], values: List[float], width: int,
                 kind: str):
        self.times = times
        self.values = values
        self.width = width
        self.kind = kind or "LINEAR"
        self._at = 0

    def at(self, moment: float) -> Optional[List[float]]:
        times, width = self.times, self.width
        if not times:
            return None
        step = 3 if self.kind == "CUBICSPLINE" else 1

        def value(key: int, part: int = 1) -> List[float]:
            base = (key * step + (part if step == 3 else 0)) * width
            return list(self.values[base:base + width])

        if moment <= times[0]:
            return value(0)
        if moment >= times[-1]:
            return value(len(times) - 1)

        low = self._at if 0 <= self._at < len(times) - 1 and times[self._at] <= moment else 0
        if times[low] > moment:
            low = 0
        while low + 1 < len(times) - 1 and times[low + 1] <= moment:
            low += 1
        self._at = low
        high = low + 1

        span = times[high] - times[low]
        if span <= 0:
            return value(low)
        blend = (moment - times[low]) / span

        if self.kind == "STEP":
            return value(low)
        if self.kind == "CUBICSPLINE":
            p0, p1 = value(low), value(high)
            m0 = [v * span for v in value(low, 2)]
            m1 = [v * span for v in value(high, 0)]
            u2, u3 = blend * blend, blend * blend * blend
            return [
                (2 * u3 - 3 * u2 + 1) * p0[i] + (u3 - 2 * u2 + blend) * m0[i]
                + (-2 * u3 + 3 * u2) * p1[i] + (u3 - u2) * m1[i]
                for i in range(width)
            ]
        if width == 4:
            return _slerp(value(low), value(high), blend)
        return [a + (b - a) * blend
                for a, b in zip(value(low), value(high))]


class Clip:
    """One animation: which node each channel drives, and over what span."""

    def __init__(self, document: Document, held: Bytes, entry: dict):
        self.name = str(entry.get("name") or "(unnamed)")
        #: node -> path -> Sampler
        self.driven: Dict[int, Dict[str, Sampler]] = {}
        self.first: Optional[float] = None
        self.last: Optional[float] = None
        self.morphs = 0

        samplers = entry.get("samplers") or []
        for channel in entry.get("channels") or []:
            if not isinstance(channel, dict):
                continue
            target = channel.get("target") or {}
            node = target.get("node")
            path = str(target.get("path") or "")
            index = channel.get("sampler")
            if node is None or not isinstance(index, int) or index >= len(samplers):
                continue
            if path == "weights":
                # Morph targets. Nothing here draws them, and counting them is
                # how the report can say so instead of the model looking wrong.
                self.morphs += 1
                continue
            sampler = samplers[index]
            if not isinstance(sampler, dict):
                continue
            times = held.read(sampler.get("input"))
            values = held.read(sampler.get("output"))
            if not times or not values:
                continue
            width = 4 if path == "rotation" else 3
            self.driven.setdefault(int(node), {})[path] = Sampler(
                times, values, width, str(sampler.get("interpolation") or "LINEAR"))
            self.first = times[0] if self.first is None else min(self.first, times[0])
            self.last = times[-1] if self.last is None else max(self.last, times[-1])

    def local(self, index: int, node: dict, moment: float) -> List[float]:
        """One node's own transform at a moment, or where the file parks it."""
        driven = self.driven.get(index)
        if not driven:
            return local_of(node)

        out = IDENTITY
        scale = driven["scale"].at(moment) if "scale" in driven else node.get("scale")
        if isinstance(scale, list) and len(scale) >= 3:
            out = geometry.scaling(float(scale[0]), float(scale[1]), float(scale[2]))
        rotation = (driven["rotation"].at(moment) if "rotation" in driven
                    else node.get("rotation"))
        if isinstance(rotation, list) and len(rotation) >= 4:
            from gltfscene import _quaternion
            out = multiply(out, _quaternion(*(float(v) for v in rotation[:4])))
        move = (driven["translation"].at(moment) if "translation" in driven
                else node.get("translation"))
        if isinstance(move, list) and len(move) >= 3:
            out = multiply(out, geometry.translation(
                float(move[0]), float(move[1]), float(move[2])))
        return out


def _parents(document: Document) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for index, node in enumerate(document.listed("nodes")):
        if not isinstance(node, dict):
            continue
        for child in node.get("children") or []:
            out.setdefault(int(child), index)
    return out


def influences(held: Bytes, primitive: dict, sources: List[int],
               count: int) -> Tuple[List[int], List[float]]:
    """Four bones per vertex, in our own numbering rather than the file's.

    Sharing identical corners renumbers every vertex, so the weights — which
    are given against the file's numbering — have to be fetched through the
    record of where each of ours came from. The same trap as FBX's, and it
    looks like a badly rigged model rather than a bug.
    """
    attributes = primitive.get("attributes") or {}
    joints = held.read(attributes.get("JOINTS_0"))
    weights = held.read(attributes.get("WEIGHTS_0"))
    if not joints or not weights:
        return [], []

    out_joints: List[int] = []
    out_weights: List[float] = []
    for source in sources:
        base = source * MAX_INFLUENCES
        pulls = []
        for slot in range(MAX_INFLUENCES):
            at = base + slot
            weight = float(weights[at]) if at < len(weights) else 0.0
            if weight > 1e-6 and at < len(joints):
                pulls.append((int(joints[at]), weight))
        total = sum(weight for _j, weight in pulls) or 1.0
        for slot in range(MAX_INFLUENCES):
            if slot < len(pulls):
                out_joints.append(pulls[slot][0])
                out_weights.append(pulls[slot][1] / total)
            else:
                out_joints.append(0)
                out_weights.append(0.0)
    return out_joints, out_weights


def clips(document: Document, held: Bytes, parts: List[dict]) -> List[dict]:
    """Every animation in the file, baked per mesh, ready for the host."""
    animations = document.listed("animations")
    if not animations:
        return []
    nodes = document.listed("nodes")
    parents = _parents(document)

    # The inverse bind matrices of every skin, read once.
    binds: Dict[int, List[List[float]]] = {}
    for index, skin in enumerate(document.listed("skins")):
        if not isinstance(skin, dict):
            continue
        flat = held.read(skin.get("inverseBindMatrices"))
        joints = [int(j) for j in (skin.get("joints") or [])]
        binds[index] = [
            list(flat[i * 16:(i + 1) * 16]) if (i + 1) * 16 <= len(flat)
            else list(IDENTITY)
            for i in range(len(joints))
        ]

    out = []
    for entry in animations:
        if not isinstance(entry, dict):
            continue
        clip = Clip(document, held, entry)
        if clip.first is None or clip.last is None or clip.last <= clip.first:
            continue
        seconds = clip.last - clip.first
        frames = max(2, min(MAX_FRAMES, int(round(seconds * SAMPLE_FPS)) + 1))
        step = seconds / (frames - 1)

        work = []
        tracks: List[Optional[List[float]]] = []
        for mesh in parts:
            skin = mesh.get("skin")
            joints = None if skin is None else document.entry("skins", skin)
            if joints is None:
                tracks.append(None)
                work.append(None)
                continue
            matrices: List[float] = []
            tracks.append(matrices)
            # The resting transform baked into this mesh's vertices, taken back
            # out of every joint matrix. The two cancel exactly, so what the
            # host draws is the pose the file describes whatever was baked in —
            # and at rest the matrix comes out the identity, which is the same
            # invariant the FBX side is checked by.
            work.append((matrices, [int(j) for j in (joints.get("joints") or [])],
                         binds.get(int(skin)) or [],
                         invert(mesh.get("placement_no_fix") or IDENTITY)))

        for frame in range(frames):
            moment = clip.first + step * frame
            where: Dict[int, List[float]] = {}

            def global_of(index: int) -> List[float]:
                known = where.get(index)
                if known is not None:
                    return known
                node = nodes[index] if 0 <= index < len(nodes) else {}
                node = node if isinstance(node, dict) else {}
                where[index] = IDENTITY  # break a cycle before walking up
                parent = parents.get(index)
                above = global_of(parent) if parent is not None else IDENTITY
                here = multiply(clip.local(index, node, moment), above)
                where[index] = here
                return here

            for job in work:
                if job is None:
                    continue
                matrices, joints, bind, undo = job
                for slot, joint in enumerate(joints):
                    ibm = bind[slot] if slot < len(bind) else IDENTITY
                    matrices.extend(multiply(undo, multiply(ibm, global_of(joint))))

        if not any(tracks):
            continue
        out.append({
            "name": clip.name,
            "frames": frames,
            "fps": (frames - 1) / seconds if seconds > 0 else SAMPLE_FPS,
            "seconds": seconds,
            "tracks": tracks,
            "morphs": clip.morphs,
        })
    return out

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

"""Skins and clips: what moves, and where it is at each moment.

This is the part of FBX that everyone gets wrong, so it is written to be
checkable rather than clever. The whole of it rests on one identity:

    at the bind pose, every skinning matrix is the identity

which is asserted directly, on real files, in ``selftest.py``. If the order of
a multiplication is wrong — and there are six ways to get it wrong — that
assertion fails, instead of an arm bending the wrong way on the third file in
fifty and nobody noticing.

Why the matrices come out that simple is worth writing down, and so is the
thing that is *not* what the documentation says.

A cluster carries two matrices. ``TransformLink`` is the bone's global
transform at bind time — that one is as advertised. ``Transform`` is widely
described as the mesh's global transform at bind time, **and it is not**: it is
the mesh's bind transform expressed *in the bone's space*, so ``T · L`` is the
mesh's global transform and ``L⁻¹`` is already folded into it. Measured across
every skinned mesh in the corpus, ``T · L = M`` holds to machine precision
while ``T = M`` is out by tens of units. Undoing ``L`` a second time — which is
what the documented formula reads like — bends everything about a bone that is
not at the origin, and only on files whose bones are not at the origin.

So, with a vertex already in world space (placed by its mesh transform ``M``
and the axis fix ``F``):

    p_world_now = p_local · T · B(t) · F,   p_local = p_world · F⁻¹ · M⁻¹

    skin(t) = F⁻¹ · M⁻¹ · T · B(t) · F

At the bind pose ``B = L``, and since ``T · L = M`` the whole thing collapses
to the identity. That is the check the self-test runs, and it is what caught
the extra ``L⁻¹``.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from fbxfile import TIME_UNIT, Node
from geometry import IDENTITY, compose, local_transform, multiply
from scene import Obj, Scene

#: Frames are sampled, not interpolated on the fly by the host, so this caps
#: what a long clip costs. Ten seconds at thirty a second.
MAX_FRAMES = 300
SAMPLE_FPS = 30.0

#: How many bones may pull on one vertex. Four is what every renderer settles
#: on; the ones past that are always small and are folded into the rest.
MAX_INFLUENCES = 4


def invert(m: List[float]) -> List[float]:
    """Inverse of an affine transform. Enough: FBX has no projections in it."""
    a, b, c = m[0], m[1], m[2]
    d, e, f = m[4], m[5], m[6]
    g, h, i = m[8], m[9], m[10]

    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-20:
        return list(IDENTITY)
    inv = 1.0 / det

    r = [
        (e * i - f * h) * inv, (c * h - b * i) * inv, (b * f - c * e) * inv, 0.0,
        (f * g - d * i) * inv, (a * i - c * g) * inv, (c * d - a * f) * inv, 0.0,
        (d * h - e * g) * inv, (b * g - a * h) * inv, (a * e - b * d) * inv, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    x, y, z = m[12], m[13], m[14]
    r[12] = -(x * r[0] + y * r[4] + z * r[8])
    r[13] = -(x * r[1] + y * r[5] + z * r[9])
    r[14] = -(x * r[2] + y * r[6] + z * r[10])
    return r


def matrix_of(node: Optional[Node]) -> List[float]:
    """A 4x4 written out as sixteen numbers, as clusters store their poses."""
    if node is None or not node.props or not isinstance(node.prop(0), list):
        return list(IDENTITY)
    values = node.prop(0)
    if len(values) < 16:
        return list(IDENTITY)
    return [float(v) for v in values[:16]]


# -- curves ------------------------------------------------------------------


class Channel:
    """One animated number over time: its keys, and how to read between them.

    Linear between keys whatever the file says. FBX keeps tangents for cubic
    curves and this ignores them, which shows only on a clip sampled far more
    coarsely than it was keyed — and the sampling here is at least as fine.
    """

    __slots__ = ("times", "values")

    def __init__(self, times: List[int], values: List[float]):
        self.times = times
        self.values = values

    def at(self, time: int, default: float) -> float:
        if not self.times:
            return default
        if time <= self.times[0]:
            return self.values[0]
        if time >= self.times[-1]:
            return self.values[-1]

        low, high = 0, len(self.times) - 1
        while high - low > 1:
            middle = (low + high) // 2
            if self.times[middle] <= time:
                low = middle
            else:
                high = middle

        span = self.times[high] - self.times[low]
        if span <= 0:
            return self.values[low]
        blend = (time - self.times[low]) / span
        return self.values[low] + (self.values[high] - self.values[low]) * blend


def _channels_of(scene: Scene, curve_node: Obj) -> Dict[str, Channel]:
    """The X, Y and Z of one animated property."""
    out: Dict[str, Channel] = {}
    for curve_id, prop in scene.properties.get(curve_node.id, ()):
        curve = scene.objects.get(curve_id)
        if curve is None or curve.kind != "AnimationCurve":
            continue
        times = curve.node.find("KeyTime")
        values = curve.node.find("KeyValueFloat")
        if not times or not values:
            continue
        raw_times = times.prop(0)
        raw_values = values.prop(0)
        if not isinstance(raw_times, list) or not isinstance(raw_values, list):
            continue
        # `d|X` names the channel; the letter is all that matters.
        letter = str(prop).rsplit("|", 1)[-1].upper()[:1] or "X"
        count = min(len(raw_times), len(raw_values))
        out[letter] = Channel(
            [int(t) for t in raw_times[:count]],
            [float(v) for v in raw_values[:count]],
        )
    return out


class Animated:
    """Every curve in one layer, indexed by the node it drives."""

    def __init__(self, scene: Scene, layer_id: int):
        #: model id -> property name -> channel letter -> Channel
        self.by_model: Dict[int, Dict[str, Dict[str, Channel]]] = {}
        self.span: Tuple[Optional[int], Optional[int]] = (None, None)

        low = high = None
        for curve_node in scene.children_of(layer_id, "AnimationCurveNode"):
            channels = _channels_of(scene, curve_node)
            if not channels:
                continue
            for channel in channels.values():
                if channel.times:
                    low = channel.times[0] if low is None else min(low, channel.times[0])
                    high = channel.times[-1] if high is None else max(high, channel.times[-1])

            # Which property of which node this drives is on the connection
            # from the node to the curve node, not on either object.
            for model in scene.parents_of(curve_node.id, "Model"):
                for child_id, prop in scene.properties.get(model.id, ()):
                    if child_id != curve_node.id:
                        continue
                    self.by_model.setdefault(model.id, {})[str(prop)] = channels

        self.span = (low, high)

    def local(self, scene: Scene, model: Obj, time: int) -> List[float]:
        """The node's own transform at ``time``.

        Anything the clip does not animate keeps whatever the file says it is,
        which is why this reaches for the static value rather than a zero.
        """
        driven = self.by_model.get(model.id)
        if not driven:
            return local_transform(model.node)

        node = model.node

        def value(prop: str, fallback, index: int) -> float:
            channels = driven.get(prop)
            letter = "XYZ"[index]
            if channels and letter in channels:
                return channels[letter].at(time, fallback[index])
            return fallback[index]

        def vector(prop: str, default) -> Tuple[float, float, float]:
            static = node.property70(prop)
            if isinstance(static, (list, tuple)) and len(static) >= 3:
                fallback = [float(static[0]), float(static[1]), float(static[2])]
            else:
                fallback = list(default)
            return (value(prop, fallback, 0), value(prop, fallback, 1), value(prop, fallback, 2))

        # The same nine-part chain a standing-still node gets, so a pose and a
        # bind pose can never disagree about how a node is put together.
        return compose(
            node,
            vector("Lcl Translation", (0.0, 0.0, 0.0)),
            vector("Lcl Rotation", (0.0, 0.0, 0.0)),
            vector("Lcl Scaling", (1.0, 1.0, 1.0)),
        )


# -- skins -------------------------------------------------------------------


class Cluster:
    __slots__ = ("bone_id", "indices", "weights", "transform", "link")

    def __init__(self, bone_id: int, indices, weights, transform, link):
        self.bone_id = bone_id
        self.indices = indices
        self.weights = weights
        self.transform = transform
        self.link = link


def clusters_of(scene: Scene, geometry_id: int) -> List[Cluster]:
    out: List[Cluster] = []
    for skin in scene.children_of(geometry_id, "Deformer"):
        if skin.subkind not in ("Skin", ""):
            continue
        for cluster in scene.children_of(skin.id, "Deformer"):
            if cluster.subkind != "Cluster":
                continue
            bones = scene.children_of(cluster.id, "Model")
            if not bones:
                continue
            indices = cluster.node.find("Indexes")
            weights = cluster.node.find("Weights")
            out.append(Cluster(
                bones[0].id,
                indices.prop(0) if indices and isinstance(indices.prop(0), list) else [],
                weights.prop(0) if weights and isinstance(weights.prop(0), list) else [],
                matrix_of(cluster.node.find("Transform")),
                matrix_of(cluster.node.find("TransformLink")),
            ))
    return out


def influences(clusters: List[Cluster], source_count: int) -> List[List[Tuple[int, float]]]:
    """Per original vertex, which bones pull on it and how hard.

    Trimmed to the four strongest and renormalised, so what is dropped is
    spread over what is kept rather than shrinking the vertex towards nothing.
    """
    table: List[List[Tuple[int, float]]] = [[] for _ in range(source_count)]
    for joint, cluster in enumerate(clusters):
        pairs = min(len(cluster.indices), len(cluster.weights))
        for i in range(pairs):
            vertex = int(cluster.indices[i])
            weight = float(cluster.weights[i])
            if 0 <= vertex < source_count and weight > 1e-6:
                table[vertex].append((joint, weight))

    for vertex, pulls in enumerate(table):
        if len(pulls) > MAX_INFLUENCES:
            pulls.sort(key=lambda pair: -pair[1])
            del pulls[MAX_INFLUENCES:]
        total = sum(weight for _, weight in pulls)
        if total > 1e-9:
            table[vertex] = [(joint, weight / total) for joint, weight in pulls]
    return table


# -- baking ------------------------------------------------------------------


def _global_at(
    scene: Scene,
    animated: Animated,
    model_id: int,
    time: int,
    cache: Dict[int, List[float]],
) -> List[float]:
    known = cache.get(model_id)
    if known is not None:
        return known

    model = scene.objects.get(model_id)
    if model is None or model.kind != "Model":
        return list(IDENTITY)

    cache[model_id] = list(IDENTITY)  # break a cycle before recursing
    parents = scene.parents_of(model_id, "Model")
    parent = (
        _global_at(scene, animated, parents[0].id, time, cache)
        if parents
        else IDENTITY
    )
    result = multiply(animated.local(scene, model, time), parent)
    cache[model_id] = result
    return result


def bake(
    scene: Scene,
    stack: Obj,
    meshes: List[dict],
    fix: List[float],
) -> Optional[dict]:
    """One clip, sampled: a matrix per joint per frame, ready to be blended.

    ``meshes`` are the drawable meshes from ``geometry``; each carries the
    clusters that deform it and the transform it was placed by. What comes back
    is per mesh, because two meshes rarely share a skeleton exactly and pooling
    them would mean an index space nobody can check.
    """
    layers = scene.children_of(stack.id, "AnimationLayer")
    if not layers:
        return None
    animated = Animated(scene, layers[0].id)

    start = stack.node.property70("LocalStart", None)
    stop = stack.node.property70("LocalStop", None)
    try:
        first, last = int(start), int(stop)
    except (TypeError, ValueError):
        first, last = animated.span
    if first is None or last is None or last <= first:
        first, last = animated.span
    if first is None or last is None or last <= first:
        return None

    seconds = (last - first) / TIME_UNIT
    frames = max(2, min(MAX_FRAMES, int(round(seconds * SAMPLE_FPS)) + 1))
    step = (last - first) / (frames - 1)

    tracks = []
    for mesh in meshes:
        clusters = mesh.get("clusters") or []
        if not clusters:
            tracks.append(None)
            continue

        # Every frame's matrices for this mesh, laid end to end.
        matrices: List[float] = []
        before = invert(multiply(mesh["placement_no_fix"], fix))
        for frame in range(frames):
            time = int(first + step * frame)
            cache: Dict[int, List[float]] = {}
            for cluster in clusters:
                bone = _global_at(scene, animated, cluster.bone_id, time, cache)
                skin = multiply(
                    multiply(before, cluster.transform),
                    multiply(bone, fix),
                )
                matrices.extend(skin)
        tracks.append(matrices)

    return {
        "name": stack.name or "(unnamed)",
        "frames": frames,
        "fps": (frames - 1) / seconds if seconds > 0 else SAMPLE_FPS,
        "seconds": seconds,
        "tracks": tracks,
    }


def bind_pose_error(mesh: dict, clusters: List[Cluster], fix: List[float]) -> float:
    """How far the skinning is from doing nothing at all *at the bind pose*.

    Not "at frame zero" — a clip need not start at its bind pose, and checking
    that would be checking the file rather than the arithmetic. This substitutes
    the bind pose for the animated one, where every term must cancel:

        F⁻¹ · M⁻¹ · T · L · F  =  1

    Six orderings look plausible and only one is right; this is what tells them
    apart, and it is the difference between finding the mistake now and finding
    it as an arm bending backwards on the third file in fifty.
    """
    if not clusters:
        return 0.0
    before = invert(multiply(mesh["placement_no_fix"], fix))
    worst = 0.0
    for cluster in clusters:
        skin = multiply(
            multiply(before, cluster.transform),
            multiply(cluster.link, fix),
        )
        for row in range(4):
            for col in range(4):
                expected = 1.0 if row == col else 0.0
                worst = max(worst, abs(skin[row * 4 + col] - expected))
    return worst


def frames_to_seconds(units: int) -> float:
    return units / TIME_UNIT


def clamp_degrees(value: float) -> float:  # pragma: no cover - kept for clarity
    return math.fmod(value, 360.0)

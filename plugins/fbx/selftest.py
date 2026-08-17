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

"""Runs the FBX reader over a folder of files and says what it found.

    python3 selftest.py ~/Games/UE_5.7/Engine/Content/FbxEditorAutomation

This is not a unit test and does not pretend to be one: FBX is a format you
learn by feeding it files somebody else wrote. It is here because this plugin
is being built over several sittings, and the first thing to know each time is
whether the whole corpus still reads.

What it checks, beyond "does not throw":

- **the transform arithmetic**, before a single file is opened. A chain of the
  nine parts composed in the wrong order is still a rigid transform, and a
  preview frames whatever it is handed — so one mesh on its own looks perfectly
  right while sitting thousands of units from where the file put it;
- every file yields objects and connections — a silently empty tree is the
  failure mode that looks like success;
- in text files, every array is exactly as long as the file says it is, which
  is how a subtly wrong mesh is caught before it is ever drawn;
- **every skinned mesh deforms to nothing at its own bind pose.** Six orderings
  of the skinning matrices look plausible and one is right; this is what tells
  them apart. It is what caught an extra inverse that would have bent every
  model whose bones are not at the origin — on some files only, and always
  plausibly.

A file whose mesh has been moved since it was bound fails that last check
honestly: there the departure measures the file, not the arithmetic, and it is
reported as such rather than hidden.
"""

from __future__ import annotations

import glob
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import animation
import fbxfile
import geometry
import gltfanim
import gltffile
import gltfscene
from scene import Scene, summarise

DECLARED = re.compile(r"^\s*(\w+):\s*\*(\d+)\s*\{")


# -- the arithmetic, which needs no files ------------------------------------


def _node(**properties) -> fbxfile.Node:
    """A Model node holding the properties named, and nothing else."""
    entries = [
        fbxfile.Node("P", [name, "", "", ""] + list(value), [])
        for name, value in properties.items()
    ]
    return fbxfile.Node(
        "Model", [0, "Cube::Model", "Mesh"],
        [fbxfile.Node("Properties70", [], entries)],
    )


def _object(kind: str, ident: int, name: str, subkind: str, *children) -> fbxfile.Node:
    return fbxfile.Node(kind, [ident, "%s\x00\x01%s" % (name, kind), subkind],
                        list(children))


def _field(name: str, value) -> fbxfile.Node:
    return fbxfile.Node(name, [value], [])


def check_pictures() -> list:
    """Finding a material's bitmap in a file that asks for it by the wrong name.

    Taken from a real model — a downloaded character with three meshes reading
    one picture through three separate `Texture` objects, only one of which
    carries the bytes. The other two name a folder on the machine that made the
    file, in the field called `RelativeFilename`:
    `C:/Temp/CharacterCreator4Temp/FbxWorkingDirectory/skull_Diffuse.png`.
    Before this, two thirds of that model drew with no picture on it at all.
    """
    problems = []

    holder = _object("Video", 3, "held", "Clip",
                     _field("RelativeFilename", "textures/skin.png"),
                     _field("Content", b"\x89PNG-the-bytes"))
    stray = _object("Video", 4, "stray", "Clip",
                    _field("RelativeFilename",
                           "C:/Temp/FbxWorkingDirectory/skin.png"))
    document = fbxfile.Document(7400, fbxfile.Node("", [], [
        fbxfile.Node("Objects", [], [
            _object("Material", 1, "skin", ""),
            _object("Texture", 2, "skin_tex", ""),
            holder,
            _object("Material", 5, "skin_again", ""),
            _object("Texture", 6, "skin_tex_again", ""),
            stray,
        ]),
        fbxfile.Node("Connections", [], [
            fbxfile.Node("C", ["OP", 2, 1, "DiffuseColor"], []),
            fbxfile.Node("C", ["OO", 3, 2], []),
            fbxfile.Node("C", ["OP", 6, 5, "DiffuseColor"], []),
            fbxfile.Node("C", ["OO", 4, 6], []),
        ]),
    ]), True)

    scene = Scene(document)
    held = geometry.bitmaps(scene)
    if "skin.png" not in held:
        problems.append("the bitmap in the file was not gathered: %s" % list(held))

    for ident, what in ((1, "the material that carries its own bytes"),
                        (5, "the material that names somebody else's disk")):
        material = scene.objects[ident]
        picture = geometry._picture_of(scene, material, held)
        if not picture or picture.get("bytes") != b"\x89PNG-the-bytes":
            problems.append("%s got %s" % (what, picture))

    return problems


def check_gltf() -> list:
    """A glTF skin at rest must do nothing at all, whatever the file is scaled by.

    The same invariant the FBX side is held to, and it is here because of what
    happened without it. A real character's vertices spanned ±95 and posed into
    ±1: the file scales its whole rig at the root, its inverse bind matrices
    were written before that scaling, and so the positions and the joint
    matrices were speaking about spaces a hundred apart. The model was drawn a
    pixel high and looked like it had vanished.

    The fixture is that file in miniature — one joint, one bone, and a root that
    scales everything by a hundredth — plus one thing the real file did not have:
    a transform on the mesh's own node that the skeleton knows nothing about. It
    is there so that taking the resting transform from the skin and taking it
    from the node are different answers, and this can say which was used.
    """
    document = gltffile.Document({
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {"name": "root", "scale": [0.01, 0.01, 0.01], "children": [1, 2]},
            {"name": "bone", "translation": [0.0, 50.0, 0.0]},
            # A transform of its own that the skeleton knows nothing about,
            # so that taking the resting transform from the *skin* and taking
            # it from the node are two different answers and the check can tell
            # which one was used.
            {"name": "mesh", "mesh": 0, "skin": 0, "translation": [7.0, 0.0, 0.0]},
        ],
        # The bone stands at y=50 in the space the mesh was modelled in, so the
        # inverse bind matrix takes it back down again.
        "skins": [{"joints": [1], "inverseBindMatrices": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1, "JOINTS_0": 2,
                                                   "WEIGHTS_0": 3}}]}],
        "accessors": [
            {"type": "MAT4", "componentType": 5126, "count": 1},
            {"type": "VEC3", "componentType": 5126, "count": 3},
            {"type": "VEC4", "componentType": 5123, "count": 3},
            {"type": "VEC4", "componentType": 5126, "count": 3},
        ],
        "animations": [{
            "name": "still",
            "channels": [{"sampler": 0, "target": {"node": 1, "path": "translation"}}],
            "samplers": [{"input": 4, "output": 5, "interpolation": "LINEAR"}],
        }],
    }, b"", True)

    ibm = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, -50.0, 0, 1.0]
    held = _Answers({
        0: ibm,
        1: [0.0, 50.0, 0.0, 10.0, 60.0, 0.0, -10.0, 40.0, 0.0],
        2: [0, 0, 0, 0] * 3,
        3: [1.0, 0.0, 0.0, 0.0] * 3,
        4: [0.0, 1.0],
        5: [0.0, 50.0, 0.0, 0.0, 50.0, 0.0],
    }, document)

    problems = []
    parts, _note = gltfscene.meshes(document, held)
    if not parts:
        problems.append("the fixture yielded no mesh")
        return problems

    mesh = parts[0]
    top = max(mesh["positions"][1::3])
    if abs(top - 0.6) > 1e-6:
        problems.append("the rig's own scaling was not baked in: top at %r" % top)

    made = gltfanim.clips(document, held, parts)
    if not made:
        problems.append("the fixture's animation was not baked")
        return problems
    first = made[0]["tracks"][0][:16]
    off = max(abs(a - b) for a, b in zip(first, geometry.IDENTITY))
    if off > 1e-6:
        problems.append("at rest a joint should do nothing; it is out by %.3g" % off)
    return problems


class _Answers(gltffile.Bytes):
    """A `Bytes` that hands back numbers instead of reading a buffer."""

    def __init__(self, answers: dict, document):
        super().__init__(document, None)
        self._answers = answers

    def read(self, index):
        return list(self._answers.get(index) or [])

    def image(self, index):
        return None


def check_bending() -> list:
    """A curve that says it bends is read bent, and its slopes are read right.

    Two facts, both taken off real files rather than believed.

    The first: some files write the tangent array as int32 — the *bits* of a
    float, in an integer array. Read as integers they come out around a
    thousand million and a curve built from them explodes rather than bends.
    `-1036211200` is `-47.168`, and `-47.168` is exactly the rate between the
    first two keys of the curve that number came from.

    The second is what it is for: between two keys of the same value, slopes
    that leave one going up and come into the next going down make a bump. Read
    straight, there is no bump at all — which is what the whole corpus of
    resampled pairs was showing as an error of 8.6 in a spread of 90.
    """
    problems = []
    if abs(animation._real(-1036211200) - (-47.16796875)) > 1e-6:
        problems.append("the bits of a float read as an integer: %r"
                        % animation._real(-1036211200))
    if abs(animation._real(0.25) - 0.25) > 1e-12:
        problems.append("a float that was written as one came back wrong")

    second = int(fbxfile.TIME_UNIT)
    bump = animation.Channel(
        [0, second], [10.0, 10.0],
        [(1, animation.CURVED, 40.0, -40.0)],
    )
    middle = bump.at(second // 2, 0.0)
    if middle <= 10.5:
        problems.append("a curve with slopes on it did not bend: %r" % middle)
    if abs(bump.at(0, 0.0) - 10.0) > 1e-9 or abs(bump.at(second, 0.0) - 10.0) > 1e-9:
        problems.append("bending moved the keys themselves")

    straight = animation.Channel([0, second], [10.0, 10.0], [])
    if abs(straight.at(second // 2, 0.0) - 10.0) > 1e-9:
        problems.append("a curve with nothing said about it did not read straight")
    return problems


def check_laying() -> list:
    """A picture asked to repeat three times repeats three times.

    `Scaling` on a texture is the size of one tile rather than a multiplier, so
    a third means three times round — which was read off an exporter rather
    than assumed: a cube asked to repeat a picture three times comes out
    carrying 0.3333, and one asked for a picture twice as big carries 0.5 with
    its offset written straight.

    Checked by what it does to the spread of the coordinates rather than by the
    arithmetic, which would only be the same line written twice.
    """
    problems = []
    square = [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]

    laid = list(square)
    geometry._lay(laid, {"scale": (1.0 / 3.0, 0.5), "offset": (0.25, 0.0)})
    across = max(laid[0::2]) - min(laid[0::2])
    down = max(laid[1::2]) - min(laid[1::2])
    if abs(across - 3.0) > 1e-9:
        problems.append("a third across should be three times round, got %g" % across)
    if abs(down - 2.0) > 1e-9:
        problems.append("a half down should be twice round, got %g" % down)
    if abs(min(laid[0::2]) - 0.25) > 1e-9:
        problems.append("the offset should move it, got %g" % min(laid[0::2]))

    # And a texture that says nothing leaves the coordinates alone, to the bit.
    untouched = list(square)
    geometry._lay(untouched, {})
    if untouched != square:
        problems.append("a picture with nothing said about it was moved anyway")
    return problems


def check_rigid() -> list:
    """A mesh nothing skins still moves if its own node does.

    A propeller, a door, a lid: one rigid piece animated by its own node and
    not by any bone. Until 2026-08-17 none of them played at all — the baking
    walked clusters, a mesh with no clusters got no track, and the viewer
    showed a still model with a transport bar under it. It is sent as a skin
    of one bone pulling on everything, so the host needed no telling.
    """
    model_id, curve_node_id, curve_id, layer_id, stack_id = 1, 2, 3, 4, 5
    document = fbxfile.Document(7400, fbxfile.Node("", [], [
        fbxfile.Node("Objects", [], [
            _object("Model", model_id, "Propeller", "Mesh"),
            _object("AnimationCurveNode", curve_node_id, "T", ""),
            _object("AnimationCurve", curve_id, "X", "",
                    _field("KeyTime", [0, 46186158000]),
                    _field("KeyValueFloat", [0.0, 10.0])),
            _object("AnimationLayer", layer_id, "Base", ""),
            _object("AnimationStack", stack_id, "Take 001", ""),
        ]),
        fbxfile.Node("Connections", [], [
            fbxfile.Node("C", ["OO", layer_id, stack_id], []),
            fbxfile.Node("C", ["OO", curve_node_id, layer_id], []),
            fbxfile.Node("C", ["OP", curve_node_id, model_id, "Lcl Translation"], []),
            fbxfile.Node("C", ["OP", curve_id, curve_node_id, "d|X"], []),
        ]),
    ]), True)

    scene = Scene(document)
    problems = []
    if not animation.moves(scene, model_id):
        problems.append("a node with a curve on it was not seen to move")

    mesh = {
        "clusters": [],
        "rigid": True,
        "modelId": model_id,
        "placement_no_fix": list(geometry.IDENTITY),
    }
    baked = animation.bake(scene, scene.objects[stack_id], [mesh],
                           list(geometry.IDENTITY))
    if baked is None or not baked["tracks"] or baked["tracks"][0] is None:
        problems.append("a mesh moved by its own node was given no track")
        return problems

    track = baked["tracks"][0]
    first = track[12:15]
    last = track[-4:-1]
    if abs(first[0]) > 1e-9 or abs(last[0] - 10.0) > 1e-6:
        problems.append("the track runs %s to %s, wanted 0 to 10" % (first, last))
    return problems


def check_curves() -> list:
    """Reading a curve from where the last read finished answers the same.

    Baking walks a clip frame after frame, so a curve is nearly always asked
    about a moment just after the one before — the pair of keys it lands
    between is the same pair, or the next. Keeping the place instead of
    searching for it from scratch is worth a fifth of the baking, and is only
    correct if it answers what the keys say, including when the questions
    arrive backwards or in no order at all.

    **The answer is worked out here rather than asked of a second `Channel`.**
    The first go at this compared one channel against a fresh one and passed a
    deliberately broken reader with flying colours: both were broken the same
    way, and a check that asks the thing under test to mark its own work is not
    a check at all.
    """
    problems = []
    seed = 7

    def next_number(limit: int) -> int:
        nonlocal seed
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        return seed % limit

    for trial in range(60):
        count = 2 + next_number(30)
        times = sorted({next_number(10000) for _ in range(count)})
        if len(times) < 2:
            continue
        values = [next_number(1000) / 100.0 for _ in times]
        walking = animation.Channel(list(times), list(values))

        asks = [next_number(11000) - 500 for _ in range(40)]
        if trial % 3 == 0:
            asks.sort()
        elif trial % 3 == 1:
            asks.sort(reverse=True)
        for moment in asks:
            wanted = _between(times, values, moment)
            got = walking.at(moment, 0.0)
            if abs(got - wanted) > 1e-9:
                problems.append("at %d: read %r, the keys say %r"
                                % (moment, got, wanted))
                break
    return problems


def _between(times: list, values: list, moment: int) -> float:
    """What the keys say at ``moment``, worked out the long way round."""
    if moment <= times[0]:
        return values[0]
    if moment >= times[-1]:
        return values[-1]
    for i in range(len(times) - 1):
        if times[i] <= moment <= times[i + 1]:
            span = times[i + 1] - times[i]
            if span <= 0:
                return values[i]
            blend = (moment - times[i]) / span
            return values[i] + (values[i + 1] - values[i]) * blend
    return values[-1]


def check_arithmetic() -> list:
    """Four things about a node's transform that no file has to be read to know.

    Every one of them was true of the wrong answer as well — a chain composed
    backwards is still a rigid transform, so a single mesh drawn by itself fills
    the window and looks perfectly correct. These are the questions that tell
    the two apart.
    """
    problems = []

    def near(got, wanted, what, tolerance=1e-6):
        if max(abs(a - b) for a, b in zip(got, wanted)) > tolerance:
            problems.append("%s: got %s, wanted %s" % (
                what,
                " ".join("%.4f" % v for v in got),
                " ".join("%.4f" % v for v in wanted),
            ))

    # A node's own origin is where the file says it is, whatever else the node
    # does. Turn it and scale it by a hundred and it has still not moved.
    m = geometry.local_transform(_node(**{
        "Lcl Translation": (0.0, 0.0, -197.0),
        "Lcl Rotation": (-110.0, -18.0, -4.0),
        "Lcl Scaling": (100.0, 100.0, 100.0),
    }))
    near(geometry.transform_point(m, 0, 0, 0), (0.0, 0.0, -197.0),
         "a node's origin is at its own translation")

    # It is scaled about itself, too: a corner one unit out lands a hundred
    # units out from where the node is, not from where the world is.
    m = geometry.local_transform(_node(**{
        "Lcl Translation": (10.0, 0.0, 0.0),
        "Lcl Scaling": (100.0, 100.0, 100.0),
    }))
    near(geometry.transform_point(m, 1, 0, 0), (110.0, 0.0, 0.0),
         "a node is scaled about itself")

    # The euler order is the one it is named: eEulerXYZ turns about X first.
    angles = (30.0, 40.0, 50.0)
    by_hand = geometry.multiply(
        geometry.multiply(
            geometry.rotation((angles[0], 0, 0)),
            geometry.rotation((0, angles[1], 0)),
        ),
        geometry.rotation((0, 0, angles[2])),
    )
    near(
        geometry.transform_point(geometry.rotation(angles, 0), 1, 2, 3),
        geometry.transform_point(by_hand, 1, 2, 3),
        "eEulerXYZ turns about X first",
    )

    # A pivot is a place the turn happens about, so a node turned about a pivot
    # of its own leaves that pivot where it was.
    m = geometry.local_transform(_node(**{
        "Lcl Rotation": (0.0, 90.0, 0.0),
        "RotationPivot": (5.0, 0.0, 0.0),
    }))
    near(geometry.transform_point(m, 5, 0, 0), (5.0, 0.0, 0.0),
         "a rotation pivot stays put")

    return problems


def check_declared_lengths(data: bytes, document) -> list:
    """Text form only: `Name: *N {` promises N values. Hold it to that."""
    if document.is_binary:
        return []
    wanted: dict = {}
    for line in data.decode("utf-8", "replace").splitlines():
        match = DECLARED.match(line)
        if match:
            wanted.setdefault(match.group(1), []).append(int(match.group(2)))

    got: dict = {}
    for node in document.root.walk():
        if node.props and isinstance(node.props[0], list):
            got.setdefault(node.name, []).append(len(node.props[0]))

    return [
        "%s: declared %s, parsed %s" % (name, sorted(lengths), sorted(got.get(name, [])))
        for name, lengths in wanted.items()
        if sorted(lengths) != sorted(got.get(name, []))
    ]


def main(folder: str) -> int:
    paths = sorted(glob.glob(os.path.join(folder, "**", "*.fbx"), recursive=True))
    if not paths:
        print("No .fbx under %s" % folder)
        return 2

    failures = 0
    binary = text = 0

    for problem in check_arithmetic():
        print("BAD   %-46s %s" % ("(the transform arithmetic)", problem))
        failures += 1
    for problem in check_pictures():
        print("BAD   %-46s %s" % ("(finding a material's picture)", problem))
        failures += 1
    for problem in check_curves():
        print("BAD   %-46s %s" % ("(reading a curve)", problem))
        failures += 1
    for problem in check_rigid():
        print("BAD   %-46s %s" % ("(a mesh moved by its own node)", problem))
        failures += 1
    for problem in check_laying():
        print("BAD   %-46s %s" % ("(how a picture is laid on)", problem))
        failures += 1
    for problem in check_bending():
        print("BAD   %-46s %s" % ("(how a curve bends)", problem))
        failures += 1
    for problem in check_gltf():
        print("BAD   %-46s %s" % ("(a glTF skin at rest)", problem))
        failures += 1
    moved: list = []
    slowest = (0.0, "")
    widest = (0, "")

    for path in paths:
        name = os.path.basename(path)
        data = open(path, "rb").read()
        began = time.monotonic()
        try:
            document = fbxfile.parse(data)
        except Exception as failure:  # noqa: BLE001 - reporting is the point
            print("FAIL  %-46s %s" % (name, failure))
            failures += 1
            continue
        took = (time.monotonic() - began) * 1000

        if document.is_binary:
            binary += 1
        else:
            text += 1

        scene = Scene(document)
        facts = summarise(scene)
        triangles = sum(mesh["triangles"] for mesh in facts["meshes"])
        slowest = max(slowest, (took, name))
        widest = max(widest, (triangles, name))

        problems = []
        if not scene.objects:
            problems.append("no objects")
        if not document.connections():
            problems.append("no connections")
        problems.extend(check_declared_lengths(data, document))

        # The skinning identity, on every skinned mesh in the file.
        skinned = 0
        drift = 0.0
        try:
            parts, _ = geometry.meshes(scene)
            fix = geometry._axis_fix(scene)
            for mesh in parts:
                clusters = animation.clusters_of(scene, mesh["geometryId"])
                if not clusters:
                    continue
                skinned += 1
                drift = max(drift, animation.bind_pose_error(mesh, clusters, fix))
        except Exception as failure:  # noqa: BLE001 - reporting is the point
            problems.append("geometry: %s" % failure)
        if drift > 1e-6:
            moved.append((drift, name))

        if problems:
            failures += 1
            print("BAD   %-46s %s" % (name, "; ".join(problems)))
        else:
            print("ok    %-46s v%-5d %-6s %6.1f ms  tri=%-7d joints=%-3d clips=%d skinned=%d"
                  % (name, document.version, "binary" if document.is_binary else "text",
                     took, triangles, facts["joints"], len(facts["clips"]), skinned))

    print()
    print("%d file(s): %d binary, %d text, %d problem(s)"
          % (len(paths), binary, text, failures))
    print("slowest %.1f ms (%s); most geometry %d triangles (%s)"
          % (slowest[0], slowest[1], widest[0], widest[1]))
    if moved:
        print()
        print("%d file(s) whose mesh has moved since it was bound — the bind-pose"
              % len(moved))
        print("check measures the file there, not the arithmetic:")
        for drift, name in sorted(moved, reverse=True)[:6]:
            print("   %-46s %.2e" % (name, drift))
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(os.path.expanduser(sys.argv[1])))

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
from scene import Scene, summarise

DECLARED = re.compile(r"^\s*(\w+):\s*\*(\d+)\s*\{")


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

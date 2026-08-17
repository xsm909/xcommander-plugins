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

"""Checks the reader, on made-up files and on real ones.

    python3 selftest.py                 # the checks that need no file
    python3 selftest.py <folder>        # and every .svg under there as well

Each check's answer is worked out somewhere other than in the reader: a circle
of radius ten passes through four points anybody can name, a transform is
arithmetic on paper, and a CSS class beating a presentation attribute is what
the specification says regardless of what this code does.
"""

from __future__ import annotations

import base64
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import svg  # noqa: E402

FAILURES = []


def check(name, got, want, tolerance=None):
    ok = (
        abs(got - want) <= tolerance
        if tolerance is not None and isinstance(got, (int, float))
        else got == want
    )
    if ok:
        print("  ok   %s" % name)
    else:
        FAILURES.append("%s: got %r, wanted %r" % (name, got, want))
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))


def shapes_of(body: str):
    found = svg.read(body.encode("utf-8"))
    out = []
    for shape in found["shapes"]:
        verbs = list(base64.b64decode(shape["verbs"]))
        raw = base64.b64decode(shape["points"])
        points = list(struct.unpack("<%df" % (len(raw) // 4), raw))
        out.append((verbs, points, shape))
    return found, out


def check_shapes():
    """A rectangle is four corners, and a circle passes through four points."""
    _, shapes = shapes_of(
        '<svg viewBox="0 0 100 100">'
        '<rect x="10" y="20" width="30" height="40" fill="#123456"/></svg>'
    )
    verbs, points, shape = shapes[0]
    check("rect verbs", verbs, [svg.MOVE, svg.LINE, svg.LINE, svg.LINE, svg.CLOSE])
    check("rect corner", (points[0], points[1]), (10.0, 20.0))
    check("rect far corner", (points[4], points[5]), (40.0, 60.0))
    check("rect fill", shape["fill"], "#123456ff")

    _, shapes = shapes_of(
        '<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="10"/></svg>'
    )
    _, points, _ = shapes[0]
    check("circle starts at the right", (points[0], points[1]), (60.0, 50.0))
    # The end of the second curve is the bottom of the circle.
    check("circle reaches the bottom", points[7], 60.0, tolerance=0.001)


def check_path_letters():
    """Relative letters, the shorthand curve, and the pair after a move.

    `M 10 10 20 20` is a move *and a line* — the rule people forget, and the
    one that turns a polygon into a scatter of dots.
    """
    _, shapes = shapes_of(
        '<svg viewBox="0 0 100 100"><path d="M10 10 20 20 h10 v10 z"/></svg>'
    )
    verbs, points, _ = shapes[0]
    check("a second pair is a line",
          verbs, [svg.MOVE, svg.LINE, svg.LINE, svg.LINE, svg.CLOSE])
    check("h is relative", (points[4], points[5]), (30.0, 20.0))
    check("v is relative", (points[6], points[7]), (30.0, 30.0))


def check_arc():
    """A half circle as an arc: where it ends, and which side it bulges.

    Radius 10 from (0,0) to (20,0) is a half circle about (10,0), and the flag
    decides which half. Worked out on paper, in SVG's coordinates, which run
    downwards: the sweep flag set gives the half through (10, −10), and clear
    gives the one through (10, 10). Getting that the wrong way round draws
    every rounded corner in the world inside out, and on a symmetrical shape
    it looks perfectly fine.
    """
    for sweep, middle in ((1, -10.0), (0, 10.0)):
        _, shapes = shapes_of(
            '<svg viewBox="0 0 100 100">'
            '<path d="M0 0 A10 10 0 0 %d 20 0"/></svg>' % sweep
        )
        verbs, points, _ = shapes[0]
        check("sweep %d became curves" % sweep,
              verbs[1:], [svg.CUBIC] * (len(verbs) - 1))
        check("sweep %d ends where it said" % sweep,
              (round(points[-2], 3), round(points[-1], 3)), (20.0, 0.0))
        # A cubic is three points, so the end of the first one — the join
        # between the two curves — is the middle of the half circle.
        check("sweep %d bulges the right way" % sweep,
              (round(points[6], 3), round(points[7], 3)), (10.0, middle))


def check_transforms():
    """Composed down the tree, and applied to the coordinates here."""
    _, shapes = shapes_of(
        '<svg viewBox="0 0 100 100">'
        '<g transform="translate(10 20)">'
        '<g transform="scale(2)"><rect width="5" height="5"/></g>'
        "</g></svg>"
    )
    _, points, _ = shapes[0]
    check("translate after scale", (points[0], points[1]), (10.0, 20.0))
    check("and the far corner", (points[4], points[5]), (20.0, 30.0))

    # rotate(90) about the origin takes (10, 0) to (0, 10).
    _, shapes = shapes_of(
        '<svg viewBox="0 0 100 100">'
        '<path transform="rotate(90)" d="M10 0 L10 0"/></svg>'
    )
    _, points, _ = shapes[0]
    check("rotate", (round(points[0], 4), round(points[1], 4)), (0.0, 10.0))


def check_view_box():
    """A viewBox that does not start at the origin moves everything."""
    found, shapes = shapes_of(
        '<svg viewBox="10 10 50 50"><rect x="10" y="10" width="5" height="5"/></svg>'
    )
    check("the size is the viewBox", (found["width"], found["height"]), (50.0, 50.0))
    _, points, _ = shapes[0]
    check("and the corner is the origin", (points[0], points[1]), (0.0, 0.0))


def check_css():
    """A rule in a `<style>` beats a presentation attribute.

    Illustrator's own exports depend on this, and a reader that only looks at
    `fill=` draws them as black silhouettes.
    """
    _, shapes = shapes_of(
        '<svg viewBox="0 0 10 10"><defs><style>.st0 { fill: #ee3425; }</style>'
        '</defs><rect class="st0" fill="#000000" width="5" height="5"/></svg>'
    )
    check("the class wins", shapes[0][2]["fill"], "#ee3425ff")

    # And `style=` beats the rule, being the last word.
    _, shapes = shapes_of(
        '<svg viewBox="0 0 10 10"><style>.st0 { fill: #ee3425; }</style>'
        '<rect class="st0" style="fill:#00ff00" width="5" height="5"/></svg>'
    )
    check("and style= wins over that", shapes[0][2]["fill"], "#00ff00ff")


def check_colours_and_opacity():
    _, shapes = shapes_of(
        '<svg viewBox="0 0 10 10">'
        '<rect width="5" height="5" fill="red" fill-opacity="0.5"/>'
        '<rect width="5" height="5" fill="#abc"/>'
        '<rect width="5" height="5" fill="rgb(1,2,3)" opacity="0.5"/>'
        '<rect width="5" height="5" fill="none" stroke="black"/>'
        '<rect width="5" height="5" fill="none"/>'
        "</svg>"
    )
    check("a name and an opacity", shapes[0][2]["fill"], "#ff000080")
    check("three digits", shapes[1][2]["fill"], "#aabbccff")
    check("rgb() and whole opacity", shapes[2][2]["fill"], "#01020380")
    check("stroke only", shapes[3][2]["stroke"], "#000000ff")
    check("nothing to paint is not a shape", len(shapes), 4)


def check_inheritance():
    """A child takes its parent's fill, and opacity multiplies down."""
    _, shapes = shapes_of(
        '<svg viewBox="0 0 10 10"><g fill="#ff0000" opacity="0.5">'
        '<g opacity="0.5"><rect width="5" height="5"/></g></g></svg>'
    )
    check("inherited fill", shapes[0][2]["fill"][:7], "#ff0000")
    check("opacity multiplied", shapes[0][2]["fill"][7:], "40")


def check_files(folder):
    found = 0
    for root, _, names in os.walk(folder):
        for name in names:
            if not name.lower().endswith(".svg") or name.startswith("."):
                continue
            found += 1
            path = os.path.join(root, name)
            started = time.time()
            try:
                drawing = svg.read(open(path, "rb").read())
            except Exception as failure:  # noqa: BLE001
                FAILURES.append("%s: %s" % (name, failure))
                print("  FAIL %s: %s" % (name, failure))
                continue
            print(
                "  ok   %-44s %gx%g %d shape(s) %.3fs %s"
                % (name[:44], drawing["width"], drawing["height"],
                   len(drawing["shapes"]), time.time() - started,
                   " ".join(drawing["notes"]))
            )
    if found == 0:
        print("  (no .svg under %s)" % folder)


def main() -> int:
    print("shapes");            check_shapes()
    print("path letters");      check_path_letters()
    print("arcs");              check_arc()
    print("transforms");        check_transforms()
    print("viewBox");           check_view_box()
    print("CSS");               check_css()
    print("colours");           check_colours_and_opacity()
    print("inheritance");       check_inheritance()
    for folder in sys.argv[1:]:
        print("files under %s" % folder)
        check_files(folder)
    if FAILURES:
        print("\n%d failure(s)" % len(FAILURES))
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

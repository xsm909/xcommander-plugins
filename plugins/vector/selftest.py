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

import eps  # noqa: E402
import ps  # noqa: E402
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
        # A run of text is a shape with words instead of geometry.
        if "verbs" not in shape:
            out.append(([], [], shape))
            continue
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


def check_gradients():
    """A gradient reaches the host as coordinates, not as a fraction.

    Everything the format knows is undone here: `objectBoundingBox` units are
    fractions of the shape's own box, and the host has never heard of the box.
    """
    found, shapes = shapes_of(
        '<svg viewBox="0 0 100 100"><defs>'
        '<linearGradient id="g"><stop offset="0" stop-color="#ff0000"/>'
        '<stop offset="1" stop-color="#0000ff"/></linearGradient>'
        '<linearGradient id="down" href="#g" x1="0" y1="0" x2="0" y2="1"/>'
        '<radialGradient id="r"><stop offset="0" stop-color="#fff"/>'
        '<stop offset="1" stop-color="#000"/></radialGradient>'
        "</defs>"
        '<rect x="10" y="20" width="80" height="40" fill="url(#g)"/>'
        '<rect width="10" height="10" fill="url(#down)"/>'
        '<circle cx="50" cy="50" r="25" fill="url(#r)"/>'
        '<rect width="80" height="40" fill="url(#r)"/></svg>'
    )
    # The default gradient runs left to right across the shape's own box.
    across = shapes[0][2]["fillGradient"]
    check("a gradient is in real coordinates",
          (across["from"], across["to"]), ([10.0, 20.0], [90.0, 20.0]))
    check("and keeps its stops", [stop["colour"] for stop in across["stops"]],
          ["#ff0000ff", "#0000ffff"])

    # Stops are often not on the gradient that is used: it points at another.
    down = shapes[1][2]["fillGradient"]
    check("stops followed through a reference",
          [stop["colour"] for stop in down["stops"]], ["#ff0000ff", "#0000ffff"])
    check("and the direction is its own", (down["from"], down["to"]),
          ([0.0, 0.0], [0.0, 10.0]))

    round_one = shapes[2][2]["fillGradient"]
    check("a round gradient on a round shape",
          (round_one["centre"], round_one["radius"]), ([50.0, 50.0], 25.0))

    # And on a stretched shape it would be an ellipse, which the contract
    # cannot say — so it is one colour, and the drawing admits it.
    check("a stretched round gradient falls back",
          shapes[3][2].get("fillGradient") is None
          and shapes[3][2]["fill"] is not None, True)
    check("and the drawing says so",
          any("round gradient" in note for note in found["notes"]), True)


def check_use():
    """`<use>` draws the declaration again, in the style of where it is used.

    Icon sets are built out of this: one path in `<defs>` and a `<use>` for
    every place it appears. The instance must take its colour from the `<use>`,
    not from the declaration, and `x`/`y` must move it.
    """
    _, shapes = shapes_of(
        '<svg viewBox="0 0 100 100">'
        '<defs><rect id="box" width="10" height="10" fill="#ff0000"/></defs>'
        '<use href="#box" x="20" y="30" fill="#00ff00"/>'
        '<use href="#box"/></svg>'
    )
    check("two instances", len(shapes), 2)
    check("moved by x and y", (shapes[0][1][0], shapes[0][1][1]), (20.0, 30.0))
    check("the declaration is not drawn where it stands",
          (shapes[1][1][0], shapes[1][1][1]), (0.0, 0.0))
    # The fill on the `<use>` is *inherited* by the instance, so the
    # declaration's own fill wins where it has one — which is what the
    # specification says and what looks wrong until you read it.
    check("the declaration keeps its own colour", shapes[0][2]["fill"], "#ff0000ff")

    # A symbol is a box of children, and it is drawn through a use as well.
    _, shapes = shapes_of(
        '<svg viewBox="0 0 100 100">'
        '<symbol id="pair"><rect width="5" height="5"/>'
        '<rect x="5" width="5" height="5"/></symbol>'
        '<use href="#pair" x="10"/></svg>'
    )
    check("a symbol brings its children", len(shapes), 2)
    check("and they are where the use put them",
          (shapes[1][1][0], shapes[1][1][1]), (15.0, 0.0))

    # And a file that points at itself stops rather than falling over.
    found, shapes = shapes_of(
        '<svg viewBox="0 0 10 10"><g id="loop"><use href="#loop"/></g></svg>'
    )
    check("a loop is refused, not followed", len(shapes), 0)


def check_inheritance():
    """A child takes its parent's fill, and opacity multiplies down."""
    _, shapes = shapes_of(
        '<svg viewBox="0 0 10 10"><g fill="#ff0000" opacity="0.5">'
        '<g opacity="0.5"><rect width="5" height="5"/></g></g></svg>'
    )
    check("inherited fill", shapes[0][2]["fill"][:7], "#ff0000")
    check("opacity multiplied", shapes[0][2]["fill"][7:], "40")


def eps_of(body, box="0 0 100 100"):
    """A minimal EPS around a fragment of PostScript."""
    text = "%%!PS-Adobe-3.0 EPSF-3.0\n%%%%BoundingBox: %s\n%s\n" % (box, body)
    found = eps.read(text.encode("latin-1"))
    out = []
    for shape in found["shapes"]:
        # A run of text is a shape with words instead of geometry.
        if "verbs" not in shape:
            out.append(([], [], shape))
            continue
        verbs = list(base64.b64decode(shape["verbs"]))
        raw = base64.b64decode(shape["points"])
        points = list(struct.unpack("<%df" % (len(raw) // 4), raw))
        out.append((verbs, points, shape))
    return found, out


def check_postscript():
    """The machine, on fragments whose answers are arithmetic.

    **The one that matters is the second.** A PostScript file is a program, and
    nearly every one of them defines its own shorthand before drawing anything.
    A reader that matched on operator names would work on one exporter and on
    none of the others; this executes what the file wrote, so the file's own
    definitions work by construction.
    """
    found, shapes = eps_of("10 20 moveto 30 20 lineto 30 40 lineto fill")
    check("the size is the bounding box", (found["width"], found["height"]),
          (100.0, 100.0))
    # PostScript counts up from the bottom left; a screen counts down from the
    # top left, so y=20 in a box 100 tall arrives at 80.
    verbs, points, _ = shapes[0]
    check("turned the right way up", (points[0], points[1]), (10.0, 80.0))
    check("and the verbs are the path", verbs, [0, 1, 1])

    _, shapes = eps_of(
        "/m { moveto } def /l { lineto } def 5 5 m 25 5 l 25 25 l fill"
    )
    check("a file's own shorthand works", len(shapes), 1)
    check("and draws the same thing", shapes[0][0], [0, 1, 1])

    # Colour: red two ways, and the CMYK worked out on paper — no cyan, all
    # magenta and yellow, no black is pure red.
    _, shapes = eps_of("1 0 0 setrgbcolor 0 0 moveto 10 10 lineto fill")
    check("rgb", shapes[0][2]["fill"], "#ff0000ff")
    _, shapes = eps_of("0 1 1 0 setcmykcolor 0 0 moveto 10 10 lineto fill")
    check("cmyk", shapes[0][2]["fill"], "#ff0000ff")
    _, shapes = eps_of("0.5 setgray 0 0 moveto 10 10 lineto fill")
    check("grey", shapes[0][2]["fill"], "#808080ff")

    # `where` must find an operator this machine implements, or every file
    # installs its own emulation of it — which is what turned a red logo grey.
    machine = ps.Machine([])
    machine.run(ps.tokenise("/setcmykcolor where"))
    check("where finds an operator", machine.stack[-1], True)
    machine = ps.Machine([])
    machine.run(ps.tokenise("/nosuchoperator where"))
    check("and does not invent one", machine.stack[-1], False)

    # A transform applies to what is drawn under it, and `grestore` takes it
    # back off.
    _, shapes = eps_of(
        "gsave 10 10 translate 0 0 moveto 5 0 lineto fill grestore "
        "0 0 moveto 5 0 lineto fill"
    )
    check("translate moves it", (shapes[0][1][0], shapes[0][1][1]), (10.0, 90.0))
    check("and grestore puts it back",
          (shapes[1][1][0], shapes[1][1][1]), (0.0, 100.0))

    # A file that calls itself must stop, not take the application with it.
    try:
        eps_of("/f { f } def f 0 0 moveto 1 1 lineto fill")
        check("a file that calls itself is stopped", "no error", "an error")
    except (eps.EpsError, ps.PostScriptError):
        check("a file that calls itself is stopped", True, True)


def check_text():
    """Words, where they sit, and the size they inherit.

    A drawing may not bring a font, so what crosses is the words rather than
    their outlines. The things that have to be right are the ones a reader
    cannot see itself getting wrong: the point is the **baseline**, `<tspan>`
    places itself, and font settings inherit from the group.
    """
    found, shapes = shapes_of(
        '<svg viewBox="0 0 200 100">'
        '<g font-size="20" font-family="Courier">'
        '<text x="10" y="30" fill="#ff0000">Hello</text>'
        '<text x="100" y="60" text-anchor="middle">Mid'
        '<tspan x="100" y="80" fill="#0000ff">span</tspan></text>'
        "</g></svg>"
    )
    words = [shape[2] for shape in shapes]
    check("a run for each piece of text", len(words), 3)
    check("what it says", words[0]["text"], "Hello")
    check("where it sits", words[0]["at"], [10.0, 30.0])
    check("the size is inherited", words[0]["size"], 20.0)
    check("and the family is reduced to one we have", words[0]["family"], "mono")
    check("the anchor travels", words[1]["anchor"], "middle")
    check("a tspan places itself", words[2]["at"], [100.0, 80.0])
    check("and keeps its own colour", words[2]["fill"], "#0000ffff")
    check("and the drawing admits the substitution",
          any("faces" in note for note in found["notes"]), True)


def check_files(folder):
    found = 0
    for root, _, names in os.walk(folder):
        for name in names:
            kind = name.rsplit(".", 1)[-1].lower()
            if kind not in ("svg", "eps", "epsf", "ps") or name.startswith("."):
                continue
            found += 1
            path = os.path.join(root, name)
            started = time.time()
            try:
                reader = svg.read if kind == "svg" else eps.read
                drawing = reader(open(path, "rb").read())
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
        print("  (no drawings under %s)" % folder)


def main() -> int:
    print("shapes");            check_shapes()
    print("path letters");      check_path_letters()
    print("arcs");              check_arc()
    print("transforms");        check_transforms()
    print("viewBox");           check_view_box()
    print("CSS");               check_css()
    print("colours");           check_colours_and_opacity()
    print("inheritance");       check_inheritance()
    print("use");               check_use()
    print("gradients");         check_gradients()
    print("postscript");        check_postscript()
    print("text");              check_text()
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

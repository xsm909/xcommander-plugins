# Vector graphics

Drawings made of shapes, drawn as shapes.

**Why not rasterise.** A drawing is worth keeping as a drawing because it stays
sharp however far into it you go. Turn it into a picture in the plugin and that
is gone — a picture magnified is mush — so what crosses the pipe is the shapes
themselves, and the host draws them at whatever size the canvas is showing.

**Where the line is.** The plugin does everything a format knows about:
transforms composed down the tree and applied to the coordinates, styles
inherited and cascaded, arcs cut into cubic curves, rectangles and circles
turned into paths, colours resolved to `#rrggbbaa`. The host is handed a flat
list of paths with four verbs — move, line, cubic, close — and no notion of
what SVG is. The same division the 3D viewer keeps.

## EPS: run, not parsed

A PostScript file is a **program**. Nothing in it says "here is a rectangle";
it says "put two numbers on the stack and call `rectfill`". And nearly every EPS
begins with a *prolog* defining its own shorthand — Illustrator draws with `m`,
`l`, `c` and `f`, which exist only because the file defined them a hundred lines
earlier. So `ps.py` is a small stack machine — tokens, an operand stack, a
dictionary stack, procedures, and the drawing operators underneath — and a
file's own definitions then work by construction.

Three things are the reader's own rather than the language's: the
`%%BoundingBox` comment, which is the only statement of size a PostScript
program has; turning the whole thing over, because PostScript counts up from the
bottom left and a screen counts down from the top left; and stepping over the
binary header of a DOS EPS to reach the program inside it.

**Not done:** text and fonts, images, clipping, patterns and shading
dictionaries. Operators it does not know are skipped and **named** in the
caption — which of them a file wanted is the only useful account of what is
missing from the picture.

**Two traps that cost a morning**, both of them silent:

- `itransform` must really be the *inverse* of `transform`. Illustrator rounds
  points to the pixel grid with the pair, and aliasing one to the other applies
  the transform twice: the drawing comes out as a spray of lines from nowhere.
- `/setcmykcolor where` must answer **yes** for an operator the machine
  implements. Answering no makes the file install its own emulation and hand
  over colours through a conversion nobody wanted — the Tcl logo came out grey.

## SVG, and what is not read

Read: `<path>` with every letter of the path grammar, `rect` (rounded too),
`circle`, `ellipse`, `line`, `polyline`, `polygon`, `<g>`, transforms
(`matrix`, `translate`, `scale`, `rotate`, `skewX`, `skewY`), the `viewBox`,
presentation attributes, `style=`, CSS rules in a `<style>` block, inheritance,
`opacity` / `fill-opacity` / `stroke-opacity`, `fill-rule`, stroke width, caps
and joins, colours by name, `#rgb`, `#rrggbb`, `#rrggbbaa` and `rgb()`.

**`<use>`**, which icon sets are built out of — one path in `<defs>` and a
`<use>` for every place it appears. The instance takes the style of where it is
used, `x` and `y` move it, a `<symbol>` brings its children, and a file that
points at itself is stopped rather than followed.

**Gradients**, linear and round, with their stops followed through a reference
where the gradient that is used carries none of its own. Fractions of the
shape's own box become real coordinates here, and the gradient's transform is
composed with the shape's. The one case that cannot cross: a **round** gradient
on a shape that has been stretched unevenly is an ellipse, which the contract
has no way to say — that one is drawn in the average of its stops, and the
drawing says so.

**Text**, as words rather than as outlines — a drawing may not bring a font, so
what crosses is what it says, where its baseline sits, how big it is and which
of three faces it asked for, and the host sets it in one it has. The drawing
says that a substitution happened. `<tspan>` places itself, the anchor is
honoured, and a transform travels with the run because it cannot be baked into
letters.

**Not read, and said in the caption rather than drawn wrong:** filters,
clipping and masking.

**The trap worth knowing:** a CSS rule in a `<style>` block beats a
presentation attribute, and Illustrator's exports rely on it — every fill in
them is a class. A reader that only looks at `fill=` draws those files as black
silhouettes.

## Testing

    python3 selftest.py                       # the checks that need no file
    python3 selftest.py /Users/Shared/temp/vector

`/Users/Shared/temp/vector` holds the corpus: SVGs from an icon site and from
Illustrator, and the Tcl and Tk logos, which are Illustrator 5.5 EPS and the
reason the machine exists.

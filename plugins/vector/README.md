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

## SVG, and what is not read

Read: `<path>` with every letter of the path grammar, `rect` (rounded too),
`circle`, `ellipse`, `line`, `polyline`, `polygon`, `<g>`, transforms
(`matrix`, `translate`, `scale`, `rotate`, `skewX`, `skewY`), the `viewBox`,
presentation attributes, `style=`, CSS rules in a `<style>` block, inheritance,
`opacity` / `fill-opacity` / `stroke-opacity`, `fill-rule`, stroke width, caps
and joins, colours by name, `#rgb`, `#rrggbb`, `#rrggbbaa` and `rgb()`.

**Not read, and said in the caption rather than drawn wrong:** text, filters,
clipping and masking, and `<use>`. Gradients are drawn in the average of their
stops, which is a shape in roughly the right colour rather than a hole.

**The trap worth knowing:** a CSS rule in a `<style>` block beats a
presentation attribute, and Illustrator's exports rely on it — every fill in
them is a class. A reader that only looks at `fill=` draws those files as black
silhouettes.

## Testing

    python3 selftest.py                       # the checks that need no file
    python3 selftest.py /Users/Shared/temp/vector

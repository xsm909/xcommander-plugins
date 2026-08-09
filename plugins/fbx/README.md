# FBX

Looks inside Autodesk's interchange format.

**F3** on a `.fbx` draws the model — shaded, turnable, framed on itself. Drag to
turn it, scroll to zoom, two fingers to move it, double-click to put it back.
**Shift+F3** picks the other viewer instead: what the file holds, as a report —
meshes and how heavy they are, skeleton, clips and how long they run, what it
was written by, which way is up.

Textures and animation are not here yet. It is being built in steps.

## Files

| | |
| --- | --- |
| `fbxfile.py` | The container: the tree a file *is*. Binary and text, no meaning attached. |
| `scene.py` | What the tree means: the object graph, and the counting behind the report. |
| `geometry.py` | Placing, triangulating and shading meshes into world-space triangles. |
| `main.py` | The contributions. |
| `selftest.py` | Runs the reader over a folder of real files. |

## Reading FBX

An FBX file is a tree of named records. Almost nothing is nested inside the
thing it belongs to: a mesh, its material, the bone that deforms it and the
curve that animates it are all siblings in `Objects`, related by a separate
`Connections` list. `scene.py` builds that graph; everything else is questions
asked of it.

Two shapes exist and both matter. The binary one is length-prefixed, and from
version 7500 its offsets are 64-bit rather than 32. The text one is braces and
commas — about **one file in eight** in a real corpus — and it writes arrays as
`Vertices: *24 { a: … }`, so those numbers are lifted onto the node itself.
Nothing downstream can tell which shape a file came from, which is the point.

Polygons are a flat index list where **the last index of each polygon is
bitwise-negated**. That is the only marker of where one ends.

Time is counted in units of 1/46186158000 of a second.

## Drawing it

The plugin cannot draw — it returns *content*, and the host renders it. So the
model travels as `mesh3d`: world-space triangles with a normal each, packed as
float32 and base64'd, because a mesh of twenty thousand triangles written out as
decimal digits is megabytes of text to parse before anything appears.

Everything the format makes hard stays on this side: the nine-part transform
chain composed up the node hierarchy, n-gon triangulation, the four combinations
of normal mapping and reference mode — with face normals computed when a file
carries none — and the axis fix, so a Z-up file arrives Y-up like every other.
Identical corners are shared: on the heaviest file here that turned 52 074
polygon corners into 10 549 vertices.

## Checking it

```
python3 selftest.py ~/Games/UE_5.7/Engine/Content
```

It reports every file and fails on three things: a parse error, a tree that
came out empty — the failure that looks like success — and, in text files, an
array whose length disagrees with the `*N` the file declares, which is how a
subtly wrong mesh is caught before anything is drawn.

Measured against Unreal's own importer test suite plus the rest of its content,
**69 files, 63 binary and 6 text, versions 7300 / 7400 / 7500, 0 problems**,
0.6–29 ms each.

That suite is worth pointing it at deliberately: it exists because these cases
break importers — `CustomCurve_BrokenTangent`, `Negative_Keys_Anim`,
`Keys_*_resample`, LOD groups, shuffled skin weights.

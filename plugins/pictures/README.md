# Pictures

The raster formats the machine's own decoder will not open.

**Why it exists.** The host shows a picture by handing the file to the engine,
and what the engine reads is not the same on every machine — measured on
2026-08-17, macOS opens Photoshop, Targa, TIFF, HEIC and JPEG 2000 through the
system's own reader, where Skia on its own knows four formats. GIMP's `.xcf` is
read by nobody, anywhere, which is why it is the first one here.

## What it does today

F3 on an `.xcf` shows the picture: every **visible** layer composited from the
bottom up, with its offset, its opacity and its mask. The answer goes back as an
ordinary `image`, so it costs the host nothing — F3, the Ctrl+Q panel viewport
and a tool pointing at a file all draw it through the same picture canvas, with
1:1 and zoom for free.

Read: XCF versions with 32- and 64-bit pointers, RLE and zlib tiles and
uncompressed ones, RGB / greyscale / indexed layers with and without alpha,
layer masks, and the wider precisions by taking the top byte of each sample.

**Blend modes**, the ones that work a channel at a time: multiply, screen,
overlay, difference, addition, subtraction, darken, lighten, divide, dodge,
burn, hard and soft light, grain extract and merge, exclusion and linear burn —
in both GIMP's old numbering and its new one. Each is a table of every pair of
values built once, so the inner loop is an index rather than arithmetic.

**Not read, and said out loud rather than shown wrong:** the modes that mix
channels together — hue, saturation, colour, luminance. A layer using one is
laid on normally and the picture says how many that happened to. Layer *groups*
contribute nothing of their own — what is inside them is composited, but a
group's own opacity and mode are not applied.

## What this reader cannot read, it hands over

A TIFF compressed as fax or as JPEG, a Photoshop file saved without its
flattened copy — these are refused here and may well be read by the machine
itself. Measured 2026-08-17: macOS reads Photoshop, Targa and TIFF through the
system, and Windows reads TIFF (and HEIC, where the Store extension is
installed) through WIC. So the file goes over as it is and the engine has a go;
if it cannot either, the canvas says so in one sentence. GIMP's format is the
exception — nothing anywhere reads it, so there is nobody to hand it to.

## The two things worth knowing before touching it

1. **A tile is byte planes, not pixels.** Inside a 64×64 tile every plane is
   compressed on its own: all the reds, then all the greens. A reader that
   treats a tile as a run of pixels produces a picture in coloured stripes.
2. **The long run-length opcodes are escapes, not runs.** 127 and 128 do not
   mean "a run of that length" — they are followed by a sixteen-bit length.

## Testing

    python3 selftest.py                       # the checks that need no file
    python3 selftest.py /Users/Shared/temp/pictures

Making a `.xcf` to test with, GIMP being installed:

    gimp-console-3 -idf --batch-interpreter=plug-in-script-fu-eval \
        -b '(…)' -b '(gimp-quit 0)'

**`--batch-interpreter` is not optional** — without it the console waits for the
interactive script-fu prompt and never returns, which looks exactly like a hang.
The Python interpreter (`python-fu-eval`) hangs the same way even with it, so
script-fu is the route that works.

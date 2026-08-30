# Sound tags

What a recording says it is: the title, who played it, the album and the year,
the cover art inside the file — and what the container says about the sound.

`O` in the sound viewer, or the button in its title bar — `Ctrl+Shift+O` in a
panel, where a bare letter is the panel's own quick search. The same slide panel
a document's structure opens in, because a recording has no structure and the
two never want it at once.

## Why it is a plugin of its own, beside the viewer

**The sound viewer is declarative.** The machine's own engine plays the file and
draws its shape, so that plugin runs no Python at all — and the tags inside the
file have nobody to come from. A **describer** is a contribution of its own for
exactly this: claimed by extension, asked when somebody opens the panel, and it
answers for a file whoever happens to be playing it. See `docs/plugins.md` in
the application.

The same division as everywhere else here: what the *decoder* knows — that it
really is 48 kHz, that it really is that long — comes from the engine; what the
*file* writes down about itself comes from here. Where the header lies, this
reports what the header says, because that is the question it was asked.

## Four schemes, one table

Nothing about a recording's metadata is the same in two formats, and that is the
whole of the work. A photograph's is one TIFF directory reached five ways; this
is four unrelated schemes that happen to hold the same six facts:

| | |
| --- | --- |
| **ID3v2** in an MP3, and in an AIFF's `ID3 ` chunk | v2.2, v2.3 and v2.4 — three- and four-letter frames, four text encodings, the synchsafe length, `APIC` |
| **MP4 atoms** in an M4A | `moov` → `udta` → `meta` → `ilst`, with `©nam` and its family, `trkn` as a pair, `covr` |
| **Vorbis comments** in a FLAC | `NAME=value` lines, and the `PICTURE` block |
| **RIFF `LIST INFO`** in a WAV | four-character codes, `INAM` and its family |

So there are four readers with **one table of names** at the end of them, and
the table is what makes them one thing rather than four panels.

## Things that had to be got right

**The MPEG frame header is two tables read the wrong way round.** The version
bits count *down* — 3 is MPEG 1, 0 is MPEG 2.5 — and so do the layer bits, where
3 is Layer I. Get either backwards and a 48 kHz file reads as 12 kHz with a
straight face. The same bitrate index means 64 kbit/s on MPEG 1 and 40 on MPEG 2.

**A sync pattern is eleven bits, and eleven bits happen.** One frame that parses
is not a frame; a frame whose length lands exactly on another one is. Without
that check a false start inside the tag reads as a file at the wrong rate rather
than as an error.

**Unsynchronisation is undone over the whole tag, not per frame.** A writer that
set the flag has scattered a zero after every `0xFF`, and the frame lengths
describe the bytes with those taken back out.

**An MP4's `moov` may be at either end.** An encoder that knows the length before
it starts writes it first; anything recording as it goes cannot. The chain of
atom lengths says *where* it is even when its bytes have not been read, so
exactly that range is fetched rather than the whole file being pulled over the
pipe.

**An audio sample entry's rate sits four bytes further along than it looks** —
there is a `pre_defined` and a `reserved` between it and the sample size. And
the size field itself means nothing for AAC, which writes 16 into it and has no
bit depth at all, so it is only reported for a lossless file.

**A genre is often a number.** `(17)` is Rock, and half the world still writes it
that way.

## What is shown, and what is not read at all

**Everything the file names is shown.** The ones worth a reader's eye are picked
out and put at the top — title, who played it, the album, the year — and the
rest goes underneath in *Everything else*, in the file's own words. It used to be
counted instead, which told the reader something was being kept from them and
then would not say what. *«а что мешает их отображать»* — nothing did.

Genuinely not read, because nothing here parses them: APE tags, ID3v1 (a v2 tag
stands in front of every file that has one), and Ogg — which the viewer does not
play either.

## Checking it

    python3 selftest.py                 # files built byte by byte here
    python3 selftest.py <folder>        # and whatever is really on the disk

The made-up files are built in `selftest.py` because that is the only way to know
what should come out before it goes in. The folder adds the other half: that real
output from a real encoder reads, and reads as whatever the system says it is —
`afconvert` writes wav, aiff, m4a and flac from anything, and `afinfo` is the
second opinion.

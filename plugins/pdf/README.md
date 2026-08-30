# PDF as Markdown

A PDF read as a **document**, not drawn as a page.

**This is deliberately not a PDF viewer.** Nothing here renders anything: no
PDFium, no system engine, no raster mode, no page thumbnails, no OCR, and not
one third-party byte. F3 gives the document as text with its headings, its lists
and its tables; the page as it was laid out is one **Enter** away, in whatever
this machine already opens PDFs with. A browser draws a page better than we ever
would — and nothing else shows a document *without* its furniture, which is what
this is for.

So the thing it removes is as much the point as the thing it shows: no running
heads, no page numbers, no line breaks left over from a column measure nobody is
reading in, no "Figure 3" where a picture used to be.

## The three routes

Structure is looked for three ways, in this order of trust.

1. **The tags the file carries.** `/StructTreeRoot` says which text is a
   heading, which is a table cell and where the rows end. Where it exists it is
   simply right, and roughly two documents in five have it — Word, Pages and
   Apache FOP all write it.
2. **The grid the page draws.** With no tags, the rectangles and rules a table
   is drawn out of *are* the table: text is filed into whichever cell contains
   it, and a cell with three lines in it stays one cell. Both sources are used,
   because a word processor draws cells as rectangles and a typesetter draws
   them as rules.
3. **The baselines.** Last of all: lines from shared baselines, columns from the
   gaps, headings from the size. It is a guess, and a reading that rests on it
   says so in a line at the top.

## Files

| File | What it does |
| --- | --- |
| `pdfdoc.py` | The file layer: a real lexer, the cross-reference chain, object streams, stream filters, the page tree |
| `pdfcrypt.py` | RC4 and AES, written out here because the standard library has neither, and the standard security handler that uses them |
| `pdffont.py` | What the bytes mean: `/ToUnicode`, named encodings, `/Differences`, glyph names, and the widths |
| `pdfpage.py` | Runs one page to find out where the words sit and what boxes were drawn |
| `pdfmd.py` | Decides what is a heading, what is a table, and what is furniture |
| `main.py` | The door |
| `selftest.py` | `python3 selftest.py [folder]` |

## Things that had to be got right, and cost something

**Read with a lexer, not with a pattern.** The prototypes that proved this worth
building scanned for `N G obj` with a regular expression, and that is wrong
twice over: a literal string may contain the word `obj` — there is a check for
exactly that — and a PDF from 1.5 onwards keeps most of its dictionaries inside
compressed object streams, where no pattern can see them.

**Every `BDC` opens a level, not only the ones carrying an `/MCID`.** A
`/Span << /ActualText >>` around a single letter closes with its own `EMC`, and
treating that as the enclosing cell's silently truncated cells:
`PlayStation, Xbox, Steam` came out as `Pla`.

**`/ActualText` replaces the glyphs; it does not accompany them.** Emitting both
is how `action` came out as `aaction`. In one measured file the letter "a" is
the byte `!` in a subset font, and only this says so.

**`/MacRomanEncoding` is common and must really be decoded.** Python has the
`mac_roman` codec; without it a curly quote reads as `ÒJhon WickÓ`.

**Widths are not decoration.** Where a word ends is arithmetic on the font's own
`/Widths`, and guessing it at half an em per letter puts the boundary between
two columns in the wrong place.

**A word broken across a line has to be put back.** The tags say "this is one
paragraph" and say nothing about how it was set, so the pieces are separated by
where they sat on the page and the hyphens are settled from that —
`Ве-ликобритания` becomes `Великобритания`, while `Ново-Николаевск` keeps its
hyphen. The exception is a **drop capital**, which is one letter on its own
baseline and not a line of its own.

**A page in columns must be read down its columns.** Reading a curriculum vitae
with a sidebar across the columns produces the sidebar and the body interleaved
line by line, which is worse than useless because it still reads like a
document. The gutter is a strip nothing is written across; what tells it from
the space between two columns *of a table* is that a quarter of the page stands
alone on one side or the other, which cannot happen in a table.

**A scan says so and stops.** Half the pages in a real collection carry no text
at all. Two dozen pages without a word settles it, so a six-hundred-page scan is
answered in a second rather than in twenty.

## The table head

**A table may have no head, and most tables in a PDF have none.** Tagged, the
answer is in the file: `/THead`, or a first row that is all `/TH`. Untagged,
three things may say so and nothing else may — the first row is filled with a
tint the rest is not, every word of it is bold and the rest is not, or it alone
has no empty cell while the body has plenty. Where none holds, the table is
drawn with no head at all.

That last part needed the host: GFM cannot write a headless table, because the
divider row is what makes a table a table. So the plugin writes an empty head
row and the host's markdown reader draws no head bar for one. `<br>` inside a
cell became a line break in the same change — a row is one line of source, so
there is no other way to keep a cell of two lines.

## Encryption

Most encrypted PDFs are not secret: they carry an owner password forbidding
printing, and a *user* password that is empty, so every reader opens them
without asking anybody anything. Those open here. RC4 and AES-128/256 through
revision 6 are implemented; a file whose user password is really set is reported
as locked, and that is the end of it — nothing here attacks anything.

## Not done, and why

- **Pictures.** `DCTDecode`, `JPXDecode`, `CCITTFaxDecode` and `JBIG2Decode` are
  left as the bytes they arrived as. A document read as text has no use for
  them, and decoding them is the beginning of being a renderer.
- **A scan.** No OCR. It is said plainly and Enter opens the pages.
- **Type 3 fonts** drawn as glyph procedures resolve through `/ToUnicode` or
  `/Differences` like any other font, and not at all without one.
- **Predefined CJK CMaps** (`UniGB-UCS2-H` and its family) are treated as
  two-byte and read through `/ToUnicode`. A file with neither shows nothing for
  that font and says so.

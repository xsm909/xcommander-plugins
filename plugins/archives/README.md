# Archives

A ZIP or a tarball as a folder. Press **Enter** on one and the panel walks into
it; **F5** out of it unpacks, **F5** into it packs, **F3** on it shows a table of
what is inside without opening it at all.

**Both formats in one plugin**, because they differ by a codec and share
everything else — the address, the walk, the table, the refusal to write an
archive that is not on this machine. Two plugins would be two copies of that
machinery in two processes, since one plugin cannot import another. What they do
*not* share is in `zipbox.py` and `tarbox.py`, and it is more than a codec: see
below.

There is no pack command and no unpack command in here, and that is the design
rather than an omission. An archive is a **file system**, so everything the
application already knows how to do with one works on it — the copy with its
progress and its cancel, the collision dialog, the search, the disk map. The
right-click menu's *Extract here*, *Extract to a folder* and *Pack into
archive…* are the application's own copy pointed at an archive.

## What it does

| | |
| --- | --- |
| **Enter** | walks in, and walks back out to the folder holding the archive |
| **F3** | the members as a table — a ZIP's ratio, a tar's mode |
| **F5 / F6 out** | copies or moves files out, one or many, folders included |
| **F5 into** | adds files to the archive, creating it if it is not there |
| **Alt+F5** | packs what is marked into a new archive |
| **Alt+F9** | unpacks the archive under the cursor |
| **F7** | a folder inside the archive, stored so an empty one survives |
| **F2** | renames a member, or a folder and everything under it |
| **Del** | removes a member |

`.zip`, `.jar`, `.whl`, `.apk` and `.epub` are entered as folders, and so are
`.tar`, `.tgz`, `.tar.gz`, `.tbz2`, `.tar.bz2`, `.txz`, `.tar.xz` and their
spellings. F3 reads all of those and `.docx`, `.xlsx` and `.pptx` as well — those
are ZIPs too, and looking at the parts of one without renaming it is worth
having.

A bare `.gz`, `.bz2` or `.xz` is opened by asking the bytes rather than the name:
a tarball if that is what it is, and otherwise **one file**, shown in a folder of
one and named after the archive with the compression taken off. Nothing in gzip
says what the file inside was called, so that is the best there is — and it is
what every other tool does.

The name chooses the format when packing, so `holiday.tgz` is a gzipped tarball
and `holiday.zip` is a ZIP. A plain `.tar` is never compressed, whatever the
compression setting says.

## Reading works anywhere; writing works here

Bytes are read through the host, which resolves whatever the archive is sitting
on. **An archive on an FTP server opens exactly like one on the disk**, and this
plugin never learns the difference.

Writing has no such road: the host serves reads to plugins and not writes, so an
archive can only be *changed* where the platform can open it — on this machine.
Asked to pack into an archive that is somewhere else, it says so plainly instead
of half working.

## A tar is not a ZIP with different bytes

A ZIP has a directory at the end saying where everything is. **A tar has
nothing** — it is a stream of headers each followed by its bytes. Three things
follow, and they are the whole difference:

- **Listing costs a walk**, and for a compressed tarball a full decompression.
  It is done once and the archive is kept open, so it is paid per archive and not
  per keystroke. This is why a 2 GB `.tar.gz` takes a moment to open and a 2 GB
  `.zip` does not.
- **A member's size goes in front of its bytes.** There is nowhere to put it
  afterwards, so a file arriving in pieces is written beside the archive first
  and added once its real length is known. A cancelled copy therefore leaves the
  archive untouched, which is better than the ZIP side manages.
- **A compressed tarball cannot be appended to at all.** So members are staged
  in a plain `.tar` beside the archive, and the archive is written **once**, when
  the application says the copy has finished. Until that moment the archive on
  disk is exactly as it was, and then it is replaced in one rename.

  You may see a `.xcommander-part` beside an archive while a pack is running.
  If the application ever stops mid-pack it stays there: nothing that was in the
  archive is lost, and the part file is an ordinary tar that any tool can open.
  The next pack into that archive throws it away and starts from what the archive
  actually holds.

  A listing during a pack reads the staged tar, so the panel shows what has been
  packed rather than what the file on disk still says.

- **What tar keeps that ZIP does not**: the permissions, and a symbolic link as
  a link. F3 shows the mode beside each member.

## What it costs

**Reading is from the end.** The central directory is the last thing in a ZIP,
so that is what is read, and then each member by its own offset. Listing a 4 GB
archive reads a few hundred kilobytes. The first version of this plugin pulled
the whole file into memory and capped itself at 256 MB; that is gone.

**Deleting and renaming rewrite the archive.** The directory is at the end and
an entry cannot be cut out of the middle of the file, so there is no cheaper
way — every archiver does this. Members are streamed through, so the memory it
takes does not depend on the size of the archive, but the time does.

**An overwrite is a rewrite too.** Two members under one name is legal in the
format and no two readers agree about which one wins, so the old one is taken
out first.

**A ZIP is not held open.** It is closed when a file finishes being written, not
when the whole pack does. Holding it open would save re-reading the directory per
file — and would leave an archive with no directory at the end of it if the
application ever stopped mid-pack, which is to say no archive at all. A tarball
cannot be written that way and stages instead, which comes to the same promise by
the other road: the archive on disk is never half anything.

## Two things about old archives

**Names.** An archive made before UTF-8 does not say what code page its names
are in. A ZIP claims 437 and means whatever the machine was set to; a tar says
nothing at all. The archivers that shipped in Russia wrote 866, Windows tools
wrote 1251, and read the wrong way round a file cannot be found again.

By default the plugin works it out by counting evidence: 866 puts box-drawing
characters either side of the alphabet and 1251 puts currency signs and stray
punctuation there, so a name holding either is a name read the wrong way.
Settings → Plugins → Archives can name a code page instead, for the archive the
guess gets wrong.

**A name that is already right is never touched.** In a tar that is exact — the
bytes come back carrying surrogates when and only when they were not UTF-8. In a
ZIP the flag says so, and where the flag is missing UTF-8 is one of the readings
tried, because the `zip` on every Linux box writes UTF-8 names without setting
it.

**Encryption.** A member encrypted with AES cannot be read: there is no AES in
the Python standard library. The old ZipCrypto scheme needs a password, and
there is nowhere to ask for one yet. Either says so rather than showing an empty
file.

## A member that points outside the archive is left out

`../../etc/passwd` in an archive is not a file with an odd name, it is an attempt
to write over something outside wherever it is unpacked. Tarballs are where this
is actually seen. Those entries never appear
in the listing, so nothing further down has to remember to check, and the plugin
log says how many were dropped.

## Checking it

```
python3 selftest.py
```

Real archives in a temporary folder, both formats and every compression: the
date survives packing, a cancelled copy leaves no half a member sealed as if it
were whole, a delete keeps the other members' dates, a folder rename moves its
branch, a `.tgz` is not written until it is finished, a bare `.gz` reads as the
one file it holds, and listing a 4 MB archive does not read 4 MB. Nothing
installed, nothing on the path.

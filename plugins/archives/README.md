# Archives

A ZIP as a folder. Press **Enter** on one and the panel walks into it; **F5**
out of it unpacks, **F5** into it packs, **F3** on it shows a table of what is
inside without opening it at all.

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
| **F3** | the members as a table: name, size, packed, ratio, date |
| **F5 / F6 out** | copies or moves files out, one or many, folders included |
| **F5 into** | adds files to the archive, creating it if it is not there |
| **Alt+F5** | packs what is marked into a new archive |
| **Alt+F9** | unpacks the archive under the cursor |
| **F7** | a folder inside the archive, stored so an empty one survives |
| **F2** | renames a member, or a folder and everything under it |
| **Del** | removes a member |

`.zip`, `.jar`, `.whl`, `.apk` and `.epub` are entered as folders. F3 reads
those and `.docx`, `.xlsx` and `.pptx` as well — those are ZIPs too, and looking
at the parts of one without renaming it is worth having.

## Reading works anywhere; writing works here

Bytes are read through the host, which resolves whatever the archive is sitting
on. **An archive on an FTP server opens exactly like one on the disk**, and this
plugin never learns the difference.

Writing has no such road: the host serves reads to plugins and not writes, so an
archive can only be *changed* where the platform can open it — on this machine.
Asked to pack into an archive that is somewhere else, it says so plainly instead
of half working.

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

**Nothing is held open.** The archive is closed when a file finishes being
written, not when the whole pack does. Holding it open would save re-reading the
directory per file — and would leave an archive with no directory at the end of
it if the application ever stopped mid-pack, which is to say no archive at all.

## Two things about old archives

**Names.** A ZIP made before UTF-8 does not say what code page its names are in.
The spec says 437, the archivers that shipped in Russia wrote 866, and Windows
tools wrote 1251 — and read the wrong way round, a file cannot be found again.
By default the plugin works it out by counting evidence: 866 puts box-drawing
characters either side of the alphabet and 1251 puts currency signs there, so a
name holding either is a name read the wrong way. Settings → Plugins → Archives
can say which to use instead, for the archive the guess gets wrong. Names that
*do* declare UTF-8 are never touched.

**Encryption.** A member encrypted with AES cannot be read: there is no AES in
the Python standard library. The old ZipCrypto scheme needs a password, and
there is nowhere to ask for one yet. Either says so rather than showing an empty
file.

## A member that points outside the archive is left out

`../../etc/passwd` in a ZIP is not a file with an odd name, it is an attempt to
write over something outside wherever it is unpacked. Those entries never appear
in the listing, so nothing further down has to remember to check, and the plugin
log says how many were dropped.

## Checking it

```
python3 selftest.py
```

Real archives in a temporary folder: the date survives packing, a cancelled copy
leaves no half a member sealed as if it were whole, a delete keeps the other
members' dates, a folder rename moves its branch, and listing a 4 MB archive
does not read 4 MB. Nothing installed, nothing on the path.

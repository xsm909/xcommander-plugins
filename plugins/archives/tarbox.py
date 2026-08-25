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

"""Everything here knows what a tar is. It is a different animal from a ZIP.

A ZIP has a directory at the end saying where everything is. **A tar has
nothing.** It is a stream of headers each followed by its bytes, and the only
way to know what is in one is to read all of it — and if it is compressed, to
decompress all of it. Three things follow, and they shape the whole file:

* **Listing costs a walk.** :mod:`tarfile` does it once and remembers the
  offsets, so the archive is kept open and the walk is paid for once per
  archive rather than once per keystroke.
* **A member's size goes in front of its bytes.** There is nowhere to put it
  afterwards, so a member arriving in chunks is written to a file of its own
  first and added when its real size is known. Which also means a copy that is
  cancelled has touched nothing.
* **A compressed tarball cannot be appended to at all.** `tarfile` says so in
  as many words: mode ``"a"`` is uncompressed only. Adding a file means writing
  the archive again, so members are staged in a plain tar beside it and the
  archive is written once, when the host says the operation has finished.

The one thing tar does better: it stores what a file *was* — the mode, the
owner, and a symbolic link as a link rather than as a copy of what it points at.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import posixpath
import shutil
import tarfile
import time
from typing import Callable, Dict, List, Optional, Tuple

import legacynames

AUTO = legacynames.AUTO
OEM = legacynames.OEM
WINDOWS = legacynames.WINDOWS
LITERAL = legacynames.LITERAL

#: The plain tar that members are staged in while a pack is running, and the
#: file a rewrite is assembled in. Beside the archive rather than in the system
#: temporary folder: a rename within one directory is atomic, and a rename
#: across volumes is a copy that can run out of room half way through.
PART = ".xcommander-part"

#: Compression by the extension that names it. The value is what goes after the
#: colon in a `tarfile` mode, and an empty one is a plain tar.
BY_EXTENSION = {
    "tar": "",
    "tgz": "gz",
    "gz": "gz",
    "taz": "gz",
    "tbz": "bz2",
    "tbz2": "bz2",
    "bz2": "bz2",
    "txz": "xz",
    "xz": "xz",
    "tlz": "xz",
    "lzma": "xz",
}

#: How to open one of those on its own, for the single compressed file that is
#: not a tarball at all.
OPENERS = {
    "gz": gzip.open,
    "bz2": bz2.open,
    "xz": lzma.open,
}


def compression_for(name: str) -> str:
    """Which compression a file of this name should be written with.

    By the name, because that is the only thing there is to go on before the
    file exists — and it is the same rule that decides Enter opens it as a
    folder. Reading asks the bytes instead, which is more reliable and only
    possible once something is there.
    """
    lowered = name.lower()
    for suffix, how in (
        (".tar.gz", "gz"),
        (".tar.bz2", "bz2"),
        (".tar.xz", "xz"),
        (".tar.lzma", "xz"),
    ):
        if lowered.endswith(suffix):
            return how
    extension = lowered.rsplit(".", 1)[-1] if "." in lowered else ""
    return BY_EXTENSION.get(extension, "")


def inner_name_of(archive_path: str) -> str:
    """What the one file inside a bare `.gz` is called.

    There is nothing in the format that says — gzip has an optional name field
    and almost nothing writes it — so it is the archive's own name with the
    compression taken off, which is what every tool does and what the person who
    compressed it would expect.
    """
    name = posixpath.basename(archive_path.replace("\\", "/"))
    for suffix in (".gz", ".bz2", ".xz", ".lzma", ".z"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)] or name
    return name


# -- names -----------------------------------------------------------------


def decoded_name(name: str, choice: str = AUTO) -> str:
    """A member's name, read in whatever code page it was written in.

    ``tarfile`` decodes with UTF-8 and ``surrogateescape``, so a name that was
    never UTF-8 comes back carrying surrogates — the original bytes, kept
    intact. That is what makes this a repair rather than a guess.
    """
    if name.isascii() or choice == LITERAL:
        return name

    # **Only a name that is not already text.** `tarfile` decodes with
    # surrogateescape, so a name that *was* UTF-8 comes back as itself and a
    # name that was not comes back carrying surrogates — the original bytes,
    # kept intact. The surrogates are the exact signal, so nothing has to be
    # guessed about a name that is already right.
    if choice == AUTO and not any(0xD800 <= ord(c) <= 0xDFFF for c in name):
        return name

    raw = name.encode("utf-8", "surrogateescape")
    return legacynames.repaired(raw, choice, name)


def normalised(name: str) -> str:
    """A member name as a path: forward slashes, no leading one, no `./`."""
    name = name.replace("\\", "/").lstrip("/")
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/") if name not in ("", "/") else ""


def is_dangerous(name: str) -> bool:
    """Whether this member is trying to be somewhere it was not put."""
    if name.startswith("/") or name.startswith("\\"):
        return True
    if len(name) > 1 and name[1] == ":":
        return True
    return ".." in name.replace("\\", "/").split("/")


# -- reading ---------------------------------------------------------------


class Listing:
    """One entry as the panel needs it."""

    __slots__ = ("name", "is_dir", "size", "modified", "target")

    def __init__(self, name, is_dir, size, modified, target=None):
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.modified = modified
        self.target = target


class Archive:
    """An open tarball, or a single compressed file pretending to be one.

    The second case is not a trick: a `.gz` holds exactly one file and no name
    for it, so showing it as a folder with one entry is the only reading that
    lets the panel do anything with it at all.
    """

    def __init__(self, tar: Optional[tarfile.TarFile], solo: Optional[Listing]):
        self.tar = tar
        self.solo = solo

    @property
    def is_tar(self) -> bool:
        return self.tar is not None

    def close(self) -> None:
        if self.tar is not None:
            self.tar.close()


def opened(fileobj, name: str = "") -> Archive:
    """Reads whatever this is: a tarball, or one compressed file.

    The bytes decide, not the name. `.gz` is worn by both a tarball and a single
    compressed file, and a plugin that trusted the name would show one of them
    as broken.
    """
    fileobj.seek(0)
    try:
        return Archive(tarfile.open(fileobj=fileobj, mode="r:*"), None)
    except tarfile.ReadError:
        pass

    # Not a tar. If it decompresses at all it is one file, which is a folder of
    # one as far as the panel is concerned.
    for how, opener in OPENERS.items():
        fileobj.seek(0)
        try:
            with opener(fileobj) as stream:
                size = _drain(stream)
        except Exception:
            continue
        return Archive(None, Listing(inner_name_of(name), False, size, None))

    raise tarfile.ReadError("not a tar archive and not a compressed file")


def _drain(stream) -> int:
    """How long the decompressed thing is, without keeping any of it."""
    total = 0
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            return total
        total += len(chunk)


def _walk(archive: Archive, choice: str):
    """Every member, named the way it will be shown."""
    for info in archive.tar.getmembers():
        yield info, normalised(decoded_name(info.name, choice))


def children(
    archive: Archive,
    inner: str,
    choice: str = AUTO,
    on_refused: Optional[Callable[[str], None]] = None,
) -> List[Listing]:
    """What is directly under ``inner``, folders included."""
    if not archive.is_tar:
        return [archive.solo] if not inner else []

    prefix = inner + "/" if inner else ""
    folders: Dict[str, None] = {}
    files: List[Listing] = []

    for info, name in _walk(archive, choice):
        if is_dangerous(name):
            if on_refused is not None:
                on_refused(name)
            continue
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):].strip("/")
        if not rest:
            continue

        head, _, tail = rest.partition("/")
        if tail or info.isdir():
            folders.setdefault(head, None)
            continue
        files.append(
            Listing(
                head,
                False,
                info.size,
                float(info.mtime),
                info.linkname or None if info.issym() or info.islnk() else None,
            )
        )

    entries = [Listing(name, True, 0, None) for name in folders]
    entries.extend(files)
    return entries


def find(archive: Archive, inner: str, choice: str = AUTO):
    """The member at this path, or None."""
    if not archive.is_tar:
        return archive.solo if inner == archive.solo.name else None
    for info, name in _walk(archive, choice):
        if name == inner and not info.isdir():
            return info
    return None


def holds(archive: Archive, inner: str, choice: str = AUTO) -> bool:
    """Whether anything is stored under this path, making it a folder."""
    if not archive.is_tar:
        return False
    prefix = inner + "/"
    for _, name in _walk(archive, choice):
        if name.startswith(prefix):
            return True
    return False


def read_at(archive: Archive, member, offset: int, length: int, fileobj=None) -> bytes:
    """``length`` bytes of a member from ``offset``.

    A compressed tar has no random access, so an offset is reached by reading
    what comes before it and throwing it away — the same bill deflate charges
    inside a ZIP.
    """
    if not archive.is_tar:
        return _solo_bytes(fileobj, offset, length)

    stream = archive.tar.extractfile(member)
    if stream is None:
        # A directory, or a link with nothing of its own in the archive.
        return b""
    with stream:
        if offset:
            remaining = offset
            while remaining > 0:
                skipped = stream.read(min(remaining, 1 << 20))
                if not skipped:
                    return b""
                remaining -= len(skipped)
        return stream.read(length)


def _solo_bytes(fileobj, offset: int, length: int) -> bytes:
    for opener in OPENERS.values():
        fileobj.seek(0)
        try:
            with opener(fileobj) as stream:
                if offset:
                    stream.seek(offset)
                return stream.read(length)
        except Exception:
            continue
    return b""


def rows(archive: Archive, choice: str = AUTO):
    """The table F3 draws: one row per member, in the order they are stored."""
    if not archive.is_tar:
        solo = archive.solo
        yield [solo.name, solo.size, "", "", ""]
        return

    for info, name in _walk(archive, choice):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.mtime))
        kind = "dir" if info.isdir() else ("link" if info.issym() else "")
        yield [name, info.size, kind, _mode(info.mode), when]


def _mode(mode: int) -> str:
    """`rwxr-xr-x`, which is a thing tar keeps and ZIP mostly does not."""
    bits = ""
    for shift in (6, 3, 0):
        value = (mode >> shift) & 7
        bits += "r" if value & 4 else "-"
        bits += "w" if value & 2 else "-"
        bits += "x" if value & 1 else "-"
    return bits


def total(archive: Archive) -> Tuple[int, int]:
    """How many files, and how much they are altogether."""
    if not archive.is_tar:
        return 1, archive.solo.size
    count = size = 0
    for info in archive.tar.getmembers():
        if info.isdir():
            continue
        count += 1
        size += info.size
    return count, size


# -- writing ---------------------------------------------------------------


def staging_for(path: str) -> str:
    return path + PART


class Staging:
    """A plain tar being built beside the archive it will become.

    **Why not write the archive itself.** There is no appending to a compressed
    tarball — `tarfile` opens mode ``"a"`` uncompressed only — so every added
    file would mean writing the whole archive again, and fifty files would mean
    fifty rewrites of a growing thing. Members go into a plain tar here and the
    archive is written once, when the host says the operation has ended.

    **Why it is safe to leave lying about.** The archive itself is not touched
    until that moment, and then it is replaced in one rename. A crash half way
    through a pack leaves the archive as it was and a `.xcommander-part` beside
    it — nothing is lost that was there before, and what is in the part file can
    be looked at with any tar tool.
    """

    def __init__(self, archive_path: str, compression: str, level: int):
        self.archive_path = archive_path
        self.compression = compression
        self.level = level
        self.path = staging_for(archive_path)
        self.dirty = False
        #: Every name in the staging tar, kept in memory. The host asks whether
        #: a member is already there before writing each one, and answering that
        #: by walking the tar would make a pack of n files cost n walks.
        self.known: set = set()

    def prepare(self, log: Optional[Callable[[str], None]] = None) -> None:
        """Puts what the archive already holds into the staging tar.

        A part file left over from a run that stopped is thrown away rather than
        added to: it was seeded from the archive as it was *then*, and what is
        in the archive now is the truth.
        """
        if os.path.exists(self.path):
            if log is not None:
                log("discarding a part file left from an earlier run: %s" % self.path)
            os.unlink(self.path)

        self.known = set()
        if not os.path.exists(self.archive_path):
            return
        if not self.compression:
            shutil.copyfile(self.archive_path, self.path)
        else:
            opener = OPENERS[self.compression]
            with opener(self.archive_path, "rb") as source, open(
                self.path, "wb"
            ) as target:
                shutil.copyfileobj(source, target, 1 << 20)
        self.known = set(self.names())

    def add(self, inner: str, source_path: str, modified: Optional[float]) -> None:
        """Puts one file in, with the size it actually turned out to be."""
        mode = "a" if os.path.exists(self.path) else "w"
        with tarfile.open(self.path, mode, format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo(inner)
            info.size = os.path.getsize(source_path)
            info.mtime = int(modified if modified else time.time())
            info.mode = 0o644
            with open(source_path, "rb") as handle:
                tar.addfile(info, handle)
        self.known.add(normalised(inner))
        self.dirty = True

    def add_directory(self, inner: str) -> None:
        mode = "a" if os.path.exists(self.path) else "w"
        with tarfile.open(self.path, mode, format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo(inner.rstrip("/"))
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = int(time.time())
            tar.addfile(info)
        self.known.add(normalised(inner))
        self.dirty = True

    def names(self) -> List[str]:
        if not os.path.exists(self.path):
            return []
        with tarfile.open(self.path) as tar:
            return [normalised(name) for name in tar.getnames()]

    def drop(self, inner: str) -> int:
        """Takes a member and everything under it out of the staging tar."""
        kept = rewrite(self.path, dropping(inner), "", 0)
        self.known = set(self.names())
        self.dirty = True
        return kept

    def flush(self) -> None:
        """Writes the archive, once, and takes the staging tar away.

        The archive is assembled beside itself and renamed over the old one, so
        an interruption anywhere in here leaves the archive that was there.
        """
        if not os.path.exists(self.path):
            self.dirty = False
            return
        if not self.dirty and os.path.exists(self.archive_path):
            os.unlink(self.path)
            return

        if not self.compression:
            os.replace(self.path, self.archive_path)
            self.dirty = False
            return

        assembling = self.archive_path + PART + ".out"
        try:
            with open(self.path, "rb") as source, _writer(
                assembling, self.compression, self.level
            ) as target:
                shutil.copyfileobj(source, target, 1 << 20)
            os.replace(assembling, self.archive_path)
        finally:
            if os.path.exists(assembling):
                os.unlink(assembling)
        os.unlink(self.path)
        self.dirty = False


def _writer(path: str, compression: str, level: int):
    if compression == "gz":
        return gzip.open(path, "wb", compresslevel=max(1, min(9, level or 6)))
    if compression == "bz2":
        return bz2.open(path, "wb", compresslevel=max(1, min(9, level or 6)))
    return lzma.open(path, "wb", preset=max(0, min(9, level or 6)))


def rewrite(
    path: str,
    transform: Callable[[str], Optional[str]],
    compression: str,
    level: int,
) -> int:
    """Writes the tarball again with every name put through ``transform``.

    Returning None for a name drops that member. Deleting and renaming inside a
    tar both come here, and there is no cheaper way: a tar is a stream, and a
    member cannot be taken out of the middle of one without moving everything
    after it.
    """
    if not os.path.exists(path):
        return 0

    part = path + PART + ".out"
    kept = 0
    try:
        read_mode = "r:*"
        write_mode = "w:" + compression if compression else "w"
        with tarfile.open(path, read_mode) as source, tarfile.open(
            part, write_mode, format=tarfile.PAX_FORMAT
        ) as target:
            for info in source.getmembers():
                name = transform(normalised(info.name))
                if name is None:
                    continue
                carried = info.replace(name=name, deep=True)
                if info.isreg():
                    stream = source.extractfile(info)
                    target.addfile(carried, stream)
                else:
                    target.addfile(carried)
                kept += 1
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.unlink(part)
    return kept


def dropping(inner: str) -> Callable[[str], Optional[str]]:
    """A transform that removes ``inner`` and everything under it."""
    prefix = inner.rstrip("/") + "/"

    def transform(name: str) -> Optional[str]:
        if name.rstrip("/") == inner.rstrip("/"):
            return None
        if name.startswith(prefix):
            return None
        return name

    return transform


def renaming(source: str, target: str) -> Callable[[str], Optional[str]]:
    """A transform that moves ``source`` to ``target``, folder and all."""
    prefix = source.rstrip("/") + "/"

    def transform(name: str) -> Optional[str]:
        if name.rstrip("/") == source.rstrip("/"):
            return target
        if name.startswith(prefix):
            return target.rstrip("/") + "/" + name[len(prefix):]
        return name

    return transform


def basename(inner: str) -> str:
    return posixpath.basename(inner.rstrip("/"))

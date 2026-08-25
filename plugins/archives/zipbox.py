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

"""Everything here knows what a ZIP is. Nothing else in the plugin does.

The format itself is :mod:`zipfile`'s business — it has read ZIPs for thirty
years, it handles zip64, the data descriptor and the encryption, and a hand
written parser would only be a worse one. What is left is the part a library
cannot decide for you:

* **What a member is called.** The spec says names are in code page 437 unless
  a flag says UTF-8, and the archivers that shipped in Russia wrote code page
  866 while Windows tools wrote 1251. Getting this wrong is not a cosmetic
  matter: it decides whether a file can be found again.
* **What is a folder.** A ZIP has no directories, only names with slashes in
  them, and some archivers store an entry per folder while others do not.
* **What has to be rewritten.** Deleting from a ZIP means writing a new one,
  because the central directory is at the end and an entry cannot be taken out
  of the middle.
* **What must be refused.** A member called ``../../etc/passwd`` is not a file
  in the archive, it is an attempt to write outside wherever it is unpacked.
"""

from __future__ import annotations

import io
import os
import posixpath
import shutil
import time
import zipfile
from typing import Callable, Iterable, List, Optional, Tuple

#: How a name with no UTF-8 flag should be read.
AUTO = "auto"
OEM = "oem"
WINDOWS = "windows"
LITERAL = "literal"

#: The flag an archiver sets when it wrote the name in UTF-8.
UTF8_FLAG = 0x800

#: Extension of the file a rewrite is assembled in, beside the archive itself.
#: Beside it rather than in the system temporary folder on purpose: a rename
#: within one directory is atomic, and a rename across volumes is a copy that
#: can run out of room half way through the archive it is replacing.
PART = ".xcommander-part"


# -- names -----------------------------------------------------------------


def _plausibility(text: str) -> int:
    """How much this reads like a file name somebody typed.

    A wrongly decoded name is not usually gibberish in an obvious way — both
    code page 866 and code page 1251 turn the same bytes into Cyrillic, just
    into *different* Cyrillic. What separates them is the bytes either side of
    the alphabet: 866 puts box-drawing characters there and 1251 puts currency
    signs and stray letters, and a name holding either is a name read the wrong
    way round. So this counts evidence rather than guessing.
    """
    score = 0
    for character in text:
        code = ord(character)
        if character.isalnum() or character in " ._-+()[]{}#@!,;'&~":
            score += 2
        elif 0x2500 <= code <= 0x259F:  # box drawing and blocks
            score -= 4
        elif 0x00A0 <= code <= 0x00BF or code in (0x00A4, 0x00A6, 0x00A7):
            score -= 3  # currency, section marks: 1251 reading 866's letters
        elif code < 0x20:
            score -= 8  # a control character is never in a name
    return score


def decoded_name(info: zipfile.ZipInfo, choice: str = AUTO) -> str:
    """The member's name, read in whatever code page it was written in.

    ``zipfile`` has already decoded it: UTF-8 where the flag says so, code page
    437 otherwise. Code page 437 maps every byte, so that decoding is lossless
    and the original bytes can be had back — which is what makes this possible
    at all rather than a guess at a mangled string.
    """
    name = info.filename
    if info.flag_bits & UTF8_FLAG or name.isascii() or choice == LITERAL:
        return name

    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        # Not a 437 decoding after all — some other codec got there first, and
        # second-guessing it would only do damage.
        return name

    if choice == OEM:
        return _decode(raw, "cp866", name)
    if choice == WINDOWS:
        return _decode(raw, "cp1251", name)

    best, score = name, _plausibility(name)
    for codec in ("cp866", "cp1251"):
        candidate = _decode(raw, codec, None)
        if candidate is None:
            continue
        rating = _plausibility(candidate)
        if rating > score:
            best, score = candidate, rating
    return best


def _decode(raw: bytes, codec: str, fallback: Optional[str]) -> Optional[str]:
    try:
        return raw.decode(codec)
    except UnicodeDecodeError:
        return fallback


def stamp(info: zipfile.ZipInfo) -> Optional[float]:
    """A member's date as seconds since the epoch.

    ZIP keeps local time with no zone in it, so it is read as local time. That
    is not a rounding error to be fixed — it is what the format records, and
    pretending otherwise moves every file in every old archive by an hour.
    """
    try:
        return time.mktime(tuple(info.date_time) + (0, 0, -1))
    except (OverflowError, ValueError):
        return None


def is_dangerous(name: str) -> bool:
    """Whether this member is trying to be somewhere it was not put.

    An absolute path, or one climbing out with ``..``. Such an entry is not a
    file that happens to be oddly named: unpacked without thought it writes
    over something outside the folder it was unpacked into. It is refused here,
    at the listing, so that nothing further down has to remember to check.
    """
    if name.startswith("/") or name.startswith("\\"):
        return True
    if len(name) > 1 and name[1] == ":":
        return True
    return ".." in name.replace("\\", "/").split("/")


def normalised(name: str) -> str:
    """A member name as a path: forward slashes, no leading one."""
    return name.replace("\\", "/").lstrip("/")


# -- reading ---------------------------------------------------------------


class Listing:
    """One entry as the panel needs it, whatever the archive called it."""

    __slots__ = ("name", "is_dir", "size", "modified")

    def __init__(self, name: str, is_dir: bool, size: int, modified: Optional[float]):
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.modified = modified


def children(
    archive: zipfile.ZipFile,
    inner: str,
    choice: str = AUTO,
    on_refused: Optional[Callable[[str], None]] = None,
) -> List[Listing]:
    """What is directly under ``inner``, folders included.

    Folders are collected from the names whether or not the archive stored an
    entry for them — plenty of archivers do not, and a tree that only appears
    for the ones that did is not a tree.
    """
    prefix = inner + "/" if inner else ""
    folders = {}
    files = []

    for info in archive.infolist():
        name = normalised(decoded_name(info, choice))
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
        if tail or info.is_dir():
            folders.setdefault(head, None)
            continue
        files.append(Listing(head, False, info.file_size, stamp(info)))

    entries = [Listing(name, True, 0, None) for name in folders]
    entries.extend(files)
    return entries


def find(
    archive: zipfile.ZipFile, inner: str, choice: str = AUTO
) -> Optional[zipfile.ZipInfo]:
    """The member at this path, or None. Folders answer None: see [holds]."""
    for info in archive.infolist():
        if normalised(decoded_name(info, choice)) == inner and not info.is_dir():
            return info
    return None


def holds(archive: zipfile.ZipFile, inner: str, choice: str = AUTO) -> bool:
    """Whether anything is stored under this path, making it a folder."""
    prefix = inner + "/"
    for info in archive.infolist():
        name = normalised(decoded_name(info, choice))
        if name == prefix or name.startswith(prefix):
            return True
    return False


def read_at(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, offset: int, length: int
) -> bytes:
    """``length`` bytes of a member from ``offset``.

    Deflate has no random access, so an offset is reached by decompressing what
    comes before it and throwing it away. That is what the format costs; the
    reads the panel and the viewers do are sequential, so it is paid once.
    """
    with archive.open(info) as member:
        if offset:
            remaining = offset
            while remaining > 0:
                skipped = member.read(min(remaining, 1 << 20))
                if not skipped:
                    return b""
                remaining -= len(skipped)
        return member.read(length)


# -- writing ---------------------------------------------------------------


def _compression(level: int) -> Tuple[int, Optional[int]]:
    """Deflate at ``level``, or no compression at all when it is zero.

    Level zero is *stored*, not "deflate as fast as you can": a file already
    compressed — a JPEG, another archive — comes out bigger when deflated, and
    somebody who asked for no compression asked for none.
    """
    if level <= 0:
        return zipfile.ZIP_STORED, None
    return zipfile.ZIP_DEFLATED, min(9, level)


def _info_for(name: str, modified: Optional[float], level: int) -> zipfile.ZipInfo:
    when = time.localtime(modified if modified else time.time())
    info = zipfile.ZipInfo(name, date_time=when[:6])
    info.compress_type, compresslevel = _compression(level)
    if compresslevel is not None:
        # Private, and stable since 3.7: this is the only way to say what level
        # a member of our own making is written at.
        try:
            info._compresslevel = compresslevel
        except AttributeError:  # pragma: no cover - a Python that renamed it
            pass
    # rw-r--r--, and the top half is what tells a POSIX unzip it is a file.
    info.external_attr = (0o100644) << 16
    return info


class Member:
    """One member being written into an archive, a chunk at a time.

    The archive is opened when the first chunk arrives and closed when the file
    ends, and it is closed **every time** rather than held open across a whole
    pack. Holding it open would save re-reading the central directory per file,
    and it would also mean that an application which stopped — a crash, a power
    cut, somebody closing the window — left an archive with no directory at the
    end of it, which is to say no archive. Correct first; a measurement can
    argue for the other later.
    """

    def __init__(
        self,
        path: str,
        inner: str,
        modified: Optional[float] = None,
        size: Optional[int] = None,
        level: int = 6,
    ):
        self._path = path
        self._inner = inner
        self._level = level
        self._written = 0

        # An overwrite is a rewrite. Appending a second entry under the same
        # name is what the format allows and what no reader agrees about: some
        # show the first, some the last, and the panel showed one file where
        # two now are. The user was asked before this was reached.
        if os.path.exists(path) and inner in stored_names(path):
            rewrite(path, lambda name: None if name == inner else name, level)

        info = _info_for(inner, modified, level)
        if size is not None:
            # A hint, not a promise — it is what decides whether the member
            # needs the zip64 form, and the real size is written at the end.
            info.file_size = int(size)

        compression, compresslevel = _compression(level)
        self._archive = zipfile.ZipFile(
            path,
            "a",
            compression=compression,
            compresslevel=compresslevel,
            allowZip64=True,
        )
        try:
            self._handle = self._archive.open(
                info, "w", force_zip64=size is None
            )
        except Exception:
            self._archive.close()
            raise

    @property
    def inner(self) -> str:
        return self._inner

    def write(self, data: bytes) -> None:
        if data:
            self._handle.write(data)
            self._written += len(data)

    def close(self, complete: bool = True) -> None:
        """Seals the member, or takes it back out when the copy failed.

        A cancelled copy has already put bytes in the file, so "discard" cannot
        mean "do nothing": the member is closed so the archive is valid, and
        then the archive is rewritten without it. Half a file, sealed as if it
        were whole, is the one outcome worth going to this trouble to avoid.
        """
        try:
            self._handle.close()
        finally:
            self._archive.close()
        if not complete:
            rewrite(self._path, lambda name: None if name == self._inner else name,
                    self._level)


def stored_names(path: str) -> List[str]:
    """Every member name in the archive on disk, as this plugin reads them."""
    if not os.path.exists(path):
        return []
    with zipfile.ZipFile(path) as archive:
        return [normalised(info.filename) for info in archive.infolist()]


def add_directory(path: str, inner: str, level: int = 6) -> None:
    """Stores a folder entry, so an empty folder survives being packed."""
    name = inner.rstrip("/") + "/"
    if os.path.exists(path) and name in stored_names(path):
        return
    with zipfile.ZipFile(path, "a", allowZip64=True) as archive:
        info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = (0o040755 << 16) | 0x10  # a directory, both ways
        archive.writestr(info, b"")


def rewrite(path: str, transform: Callable[[str], Optional[str]], level: int = 6) -> int:
    """Writes the archive again, with every name put through ``transform``.

    Returning None for a name drops that member. This is how deleting and
    renaming inside an archive work, and there is no cheaper way: the central
    directory is at the end of the file and an entry in the middle cannot be
    cut out of it.

    Members are streamed through rather than read whole, so the memory this
    needs does not depend on how big the archive is. The time does — a delete
    from a large archive rewrites all of it — and that is the format's bill,
    not this function's.
    """
    if not os.path.exists(path):
        return 0

    part = path + PART
    kept = 0
    compression, compresslevel = _compression(level)
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(
            part, "w", compression=compression, compresslevel=compresslevel,
            allowZip64=True
        ) as target:
            for info in source.infolist():
                name = transform(normalised(info.filename))
                if name is None:
                    continue
                carried = zipfile.ZipInfo(name, date_time=info.date_time)
                carried.compress_type = info.compress_type
                carried.external_attr = info.external_attr
                carried.internal_attr = info.internal_attr
                carried.create_system = info.create_system
                carried.comment = info.comment
                carried.flag_bits = info.flag_bits & UTF8_FLAG
                if info.is_dir():
                    target.writestr(carried, b"")
                    kept += 1
                    continue
                carried.file_size = info.file_size
                with source.open(info) as reading, target.open(
                    carried, "w", force_zip64=info.file_size >= 1 << 31
                ) as writing:
                    shutil.copyfileobj(reading, writing, 1 << 20)
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
        if name == inner or name.rstrip("/") == inner.rstrip("/"):
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
            return target + "/" if name.endswith("/") else target
        if name.startswith(prefix):
            return target.rstrip("/") + "/" + name[len(prefix):]
        return name

    return transform


def rows(archive: zipfile.ZipFile, choice: str = AUTO) -> Iterable[list]:
    """The table F3 draws: one row per member, in the order they are stored."""
    for info in archive.infolist():
        stored = info.file_size
        packed = info.compress_size
        ratio = "" if not stored else "%d%%" % round(100 * packed / stored)
        when = "%04d-%02d-%02d %02d:%02d" % info.date_time[:5]
        name = normalised(decoded_name(info, choice))
        yield [name, stored, packed, ratio, when]


def total(archive: zipfile.ZipFile) -> Tuple[int, int, int]:
    """How many members, how big they are, and how big they became."""
    count = stored = packed = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        count += 1
        stored += info.file_size
        packed += info.compress_size
    return count, stored, packed


def opened(fileobj: io.IOBase) -> zipfile.ZipFile:
    """The archive behind a file-like object, read-only."""
    return zipfile.ZipFile(fileobj)


def basename(inner: str) -> str:
    return posixpath.basename(inner.rstrip("/"))

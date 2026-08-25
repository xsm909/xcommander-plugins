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

"""Archives as folders: walk into one, copy out of it, pack into it.

**ZIP and tar in one plugin**, because they differ by a codec and share
everything else — the URL, the walk, the F3 table, the refusal to write an
archive that is not on this machine. Two plugins would be two copies of that
machinery in two processes, since one plugin cannot import another. What each
format *does* differ by lives in :mod:`zipbox` and :mod:`tarbox`, and the two are
not alike: a ZIP has a directory at the end and a tar has nothing, so a tarball
is listed by walking it and a compressed one cannot be appended to at all.

**What this is not.** It is not a pack command, an unpack command, a progress
window or a queue. An archive here is a *file system*, and everything the panels
already know how to do with a file system therefore works on it: Enter walks in,
F5 copies out, F5 the other way packs, F3 views a file inside one, the search
finds things in it, and the disk map will measure it. The application's own copy
carries all of that, with its progress, its collision dialog and its cancel, and
none of it is written twice here.

**Reading goes through the host, writing does not.** Bytes are read with
``plugin.read_file``, which resolves whatever transport the archive is on — so an
archive sitting on an FTP server opens exactly like one on the disk, and the
plugin never learns the difference. Writing has no such road: the host serves
reads to plugins and not writes, so an archive can only be *changed* where the
platform can open it, which means on this machine. Anywhere else says so plainly
rather than half working.

**Nothing is held open.** Every write closes the archive when the file ends, and
every read closes it when the panel has moved on. An archive with no central
directory at the end of it is not an archive, and the way to get one is to hold
it open across an application that then stops.
"""

from __future__ import annotations

import os
import tarfile
import time
import zipfile
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import hostfile
import tarbox
import zipbox
from xcommander import (
    DIRECTORY,
    Entry,
    FILE,
    FileSystem,
    Plugin,
    RpcError,
    error,
    local_path,
    table,
)

plugin = Plugin("org.xcommander.archives", "Archives")

#: How long a read-only archive stays open with nothing asking for it.
IDLE = 120.0

#: How many archives can be open at once. Small on purpose: each one holds a
#: buffered reader, and a panel is looking at one archive at a time.
KEEP = 4


def _choice() -> str:
    """Which code page an old name is read in — see :mod:`zipbox`."""
    return str(plugin.setting("legacyNames", zipbox.AUTO))


def _level() -> int:
    try:
        return int(plugin.setting("level", 6))
    except (TypeError, ValueError):
        return 6


def _split(url: str) -> Tuple[str, str]:
    """Splits ``zip:///inner/path?from=file:///C:/box.zip`` into its two halves.

    The path is the path *within* the archive and the archive itself rides in
    the query. That is the host's convention, and it is what makes going up a
    level ordinary string work and lets the archive live on any transport.
    """
    parsed = urlparse(url)
    archive = (parse_qs(parsed.query).get("from") or [""])[0]
    if not archive:
        raise RpcError("No archive in %s" % url)
    return archive, zipbox.normalised(unquote(parsed.path or ""))


class _Open:
    """One archive open for reading, and what it was when it was opened."""

    __slots__ = ("archive", "handle", "size", "modified", "touched")

    def __init__(self, archive, handle, size, modified):
        self.archive = archive
        self.handle = handle
        self.size = size
        self.modified = modified
        self.touched = time.monotonic()

    def close(self) -> None:
        try:
            self.archive.close()
        finally:
            self.handle.close()


class _ArchiveFileSystem(FileSystem):
    """What the two formats share, which is everything but the format.

    Opening an archive over the host, keeping it open while it has not changed,
    and refusing to *write* one that is not on this machine. A subclass says how
    to read the bytes it is handed and nothing else about any of this.
    """

    def __init__(self):
        self._open: Dict[str, _Open] = {}

    def _load(self, handle, archive_url: str):
        """The format's own reader over an open, seekable file."""
        raise NotImplementedError

    def _reader(self, archive_url: str) -> _Open:
        """The archive, open for reading, reused while it has not changed.

        Reused because reading an archive's directory is not free — for a tar it
        means walking the whole thing — and a panel asks for a listing on every
        refresh. Checked because an archive is a file somebody else can change
        under us, and a listing of what it used to be is worse than a slow one.
        """
        size, modified = self._measure(archive_url)
        cached = self._open.get(archive_url)
        now = time.monotonic()
        if cached is not None:
            if (
                cached.size == size
                and cached.modified == modified
                and now - cached.touched < IDLE
            ):
                cached.touched = now
                return cached
            self._forget(archive_url)

        handle = hostfile.opened(plugin, archive_url, size)
        try:
            archive = self._load(handle, archive_url)
        except RpcError:
            handle.close()
            raise
        except Exception as failure:
            handle.close()
            raise RpcError("Cannot read %s: %s" % (archive_url, failure))

        while len(self._open) >= KEEP:
            oldest = min(self._open, key=lambda key: self._open[key].touched)
            self._forget(oldest)
        opened = _Open(archive, handle, size, modified)
        self._open[archive_url] = opened
        return opened

    def _measure(self, archive_url: str) -> Tuple[int, Optional[int]]:
        """The archive's size and date, as the host sees them right now."""
        stat = plugin.stat(archive_url)
        if not stat:
            raise RpcError("%s is not there" % archive_url)
        size = int(stat.get("size") or 0)
        if size <= 0:
            raise RpcError("%s is empty" % archive_url)
        return size, stat.get("modified")

    def _forget(self, archive_url: str) -> None:
        cached = self._open.pop(archive_url, None)
        if cached is not None:
            cached.close()

    def _forget_all(self) -> None:
        for archive_url in list(self._open):
            self._forget(archive_url)

    def _absent(self, archive_url: str) -> bool:
        return not plugin.stat(archive_url)

    def _writable_path(self, archive_url: str) -> str:
        """The archive's path on this machine, or a plain refusal.

        Reading goes through the host and works anywhere. Writing cannot: the
        host serves reads to plugins, not writes, so changing an archive means
        opening it here. Said as an error the moment it is asked for, rather
        than by half packing something.
        """
        path = local_path(archive_url)
        if not path:
            raise RpcError(
                "Only an archive on this machine can be written to. "
                "%s is somewhere else — copy it here first." % archive_url
            )
        return path


# -- the viewer ------------------------------------------------------------


@plugin.viewer(
    "zip.contents",
    "Archive contents",
    priority=30,
    extensions=["zip", "jar", "whl", "apk", "docx", "xlsx", "pptx", "epub"],
)
def list_contents(url):
    """F3: what is in there, as a table, without walking into it.

    Also the one thing that works on a `.docx` — those are ZIPs, and being able
    to look at the parts of one without renaming it is worth a row in the menu.
    """
    try:
        opened = _reader(url)
    except RpcError as failure:
        return error(str(failure))

    rows = list(zipbox.rows(opened.archive, _choice()))
    if not rows:
        return error("The archive is empty.")

    count, stored, packed = zipbox.total(opened.archive)
    plugin.log(
        "%s: %d file(s), %d bytes stored as %d" % (url, count, stored, packed)
    )
    return table(["Name", "Size", "Packed", "Ratio", "Modified"], rows)


# -- the file system -------------------------------------------------------


class ZipFileSystem(_ArchiveFileSystem):
    """A ZIP read and written as a directory.

    Writable, with one honest limit: the archive has to be on this machine, for
    the reason in the module docstring. `writable = True` is therefore a
    statement about the format and not about every archive — a ZIP on FTP lists
    and copies out, and says what it cannot do if asked to take a file.
    """

    scheme = "zip"
    writable = True
    icon = "archive"

    def __init__(self):
        super().__init__()
        self._member: Optional[zipbox.Member] = None
        self._member_url: Optional[str] = None
        self._incoming: Dict[str, Tuple[Optional[int], Optional[float]]] = {}

    def _load(self, handle, archive_url: str):
        try:
            return zipbox.opened(handle)
        except zipfile.BadZipFile as failure:
            raise RpcError("%s is not a readable ZIP: %s" % (archive_url, failure))

    # -- listing ----------------------------------------------------------

    def list(self, url: str) -> List[Entry]:
        archive_url, inner = _split(url)

        # An archive that is not there yet is an empty folder, not an error: a
        # pack creates the file with its first member, and the panel may well
        # be looking at where it is about to appear.
        if self._absent(archive_url):
            return []

        opened = self._reader(archive_url)
        refused = []
        listing = zipbox.children(
            opened.archive, inner, _choice(), on_refused=refused.append
        )
        if refused:
            plugin.log(
                "%s: %d entry(ies) point outside the archive and were left out: %s"
                % (archive_url, len(refused), ", ".join(sorted(set(refused))[:5])),
                level="warning",
            )
        return [
            Entry(
                name=item.name,
                kind=DIRECTORY if item.is_dir else FILE,
                size=item.size,
                modified=item.modified,
            )
            for item in listing
        ]

    def stat(self, url: str) -> Optional[Entry]:
        archive_url, inner = _split(url)

        if not inner:
            # The top of the archive is a folder named after the archive, and it
            # answers even before the file exists — the copy that is about to
            # create it asks.
            return Entry(name=zipbox.basename(archive_url), kind=DIRECTORY)
        if self._absent(archive_url):
            return None

        opened = self._reader(archive_url)
        info = zipbox.find(opened.archive, inner, _choice())
        if info is not None:
            return Entry(
                name=zipbox.basename(inner),
                kind=FILE,
                size=info.file_size,
                modified=zipbox.stamp(info),
            )
        if zipbox.holds(opened.archive, inner, _choice()):
            return Entry(name=zipbox.basename(inner), kind=DIRECTORY)
        return None

    def read(self, url: str, offset: int, length: int) -> bytes:
        archive_url, inner = _split(url)
        opened = self._reader(archive_url)
        info = zipbox.find(opened.archive, inner, _choice())
        if info is None:
            raise RpcError("%s is not in the archive" % inner)
        try:
            return zipbox.read_at(opened.archive, info, offset, length)
        except RuntimeError as failure:
            # What zipfile raises for an encrypted member with no password.
            raise RpcError("%s cannot be read: %s" % (inner, failure))

    # -- writing ----------------------------------------------------------

    def begin_write(
        self, url: str, size: Optional[int], modified: Optional[float]
    ) -> None:
        # Kept until the first chunk arrives, because a member's date has to be
        # in its header and this is the only time the host says what it was.
        self._incoming[url] = (size, modified)

    def write(self, url: str, data: bytes, mode: str) -> None:
        archive_url, inner = _split(url)
        if not inner:
            raise RpcError("An archive cannot be written to as if it were a file")
        path = self._writable_path(archive_url)

        if mode == "create":
            self._discard()
            # Nothing is read from the archive while it is being changed, and
            # the listing that was cached is about to be wrong anyway.
            self._forget(archive_url)
            size, modified = self._incoming.pop(url, (None, None))
            self._member = zipbox.Member(
                path, inner, modified=modified, size=size, level=_level()
            )
            self._member_url = url

        if self._member is None or self._member_url != url:
            raise RpcError("%s was appended to without being started" % inner)
        self._member.write(data)

    def close_write(self, url: str, complete: bool) -> None:
        if self._member is None or self._member_url != url:
            return
        member, self._member, self._member_url = self._member, None, None
        archive_url, _ = _split(url)
        self._forget(archive_url)
        member.close(complete=complete)
        self._incoming.pop(url, None)

    def _discard(self) -> None:
        """Throws away a member left half written by a copy that never ended."""
        if self._member is None:
            return
        member, self._member, self._member_url = self._member, None, None
        try:
            member.close(complete=False)
        except Exception as failure:  # pragma: no cover - best effort
            plugin.log("could not close %s: %s" % (member.inner, failure), "warning")

    # -- changing ---------------------------------------------------------

    def mkdir(self, url: str) -> None:
        archive_url, inner = _split(url)
        if not inner:
            raise RpcError("The archive itself already exists")
        path = self._writable_path(archive_url)
        self._forget(archive_url)
        zipbox.add_directory(path, inner, _level())

    def delete(self, url: str) -> None:
        archive_url, inner = _split(url)
        path = self._writable_path(archive_url)
        if not inner:
            # Deleting the archive is the panel above's business, not ours: from
            # in here there would be nowhere left to stand.
            raise RpcError("Leave the archive and delete it as a file")
        self._forget(archive_url)
        before = zipbox.stored_names(path)
        kept = zipbox.rewrite(path, zipbox.dropping(inner), _level())
        if kept == len(before):
            raise RpcError("%s is not in the archive" % inner)

    def rename(self, source: str, target: str) -> None:
        source_archive, source_inner = _split(source)
        target_archive, target_inner = _split(target)
        if source_archive != target_archive:
            raise RpcError("A file can only be renamed inside its own archive")
        if not source_inner or not target_inner:
            raise RpcError("The archive itself is renamed from the folder holding it")

        path = self._writable_path(source_archive)
        names = zipbox.stored_names(path)
        if target_inner in names:
            raise RpcError("%s is already in the archive" % target_inner)
        if source_inner not in names and not any(
            name.startswith(source_inner.rstrip("/") + "/") for name in names
        ):
            raise RpcError("%s is not in the archive" % source_inner)

        self._forget(source_archive)
        zipbox.rewrite(path, zipbox.renaming(source_inner, target_inner), _level())


# -- tar -------------------------------------------------------------------


@plugin.viewer(
    "tar.contents",
    "Tarball contents",
    priority=30,
    extensions=["tar", "tgz", "tbz", "tbz2", "txz", "gz", "bz2", "xz"],
)
def list_tar_contents(url):
    """F3 on a tarball: what is in it, with the mode tar bothers to keep."""
    try:
        opened = tars._reader(url)
    except RpcError as failure:
        return error(str(failure))

    rows = list(tarbox.rows(opened.archive, _choice()))
    if not rows:
        return error("The archive is empty.")
    count, size = tarbox.total(opened.archive)
    plugin.log("%s: %d file(s), %d bytes" % (url, count, size))
    return table(["Name", "Size", "Kind", "Mode", "Modified"], rows)


class TarFileSystem(_ArchiveFileSystem):
    """A tarball read and written as a directory.

    Written through a **staging tar** beside the archive, because a compressed
    tarball cannot be appended to: `tarfile` opens mode ``"a"`` uncompressed
    only. Members go into a plain tar, and the archive is written once — when
    the host says the operation has finished, or when the plugin is stopped.

    While that staging tar exists it *is* the archive as far as this plugin is
    concerned: a listing during a pack reads it, so what the panel sees is what
    has been packed rather than what the file on disk still says.
    """

    scheme = "tar"
    writable = True
    icon = "archive"

    def __init__(self):
        super().__init__()
        self._staging: Optional[tarbox.Staging] = None
        self._member_url: Optional[str] = None
        self._member_path: Optional[str] = None
        self._member_handle = None
        self._incoming: Dict[str, Tuple[Optional[int], Optional[float]]] = {}

    def _load(self, handle, archive_url: str):
        try:
            return tarbox.opened(handle, archive_url)
        except tarfile.ReadError as failure:
            raise RpcError("%s is not a readable tarball: %s" % (archive_url, failure))

    # -- listing ----------------------------------------------------------

    def _staged(self, archive_url: str) -> Optional[tarbox.Staging]:
        """The staging tar for this archive, if one is being built right now."""
        staging = self._staging
        if staging is None:
            return None
        path = local_path(archive_url)
        return staging if path and path == staging.archive_path else None

    def _reader(self, archive_url: str) -> _Open:
        # A pack in progress is the truth about the archive, and it is a plain
        # tar sitting on the disk: read that rather than the file it will
        # become, which has not been written yet.
        staging = self._staged(archive_url)
        if staging is not None and os.path.exists(staging.path):
            handle = open(staging.path, "rb")
            try:
                return _Open(tarbox.opened(handle, archive_url), handle, 0, None)
            except Exception as failure:
                handle.close()
                raise RpcError("Cannot read what has been packed: %s" % failure)
        return super()._reader(archive_url)

    def list(self, url: str) -> List[Entry]:
        archive_url, inner = _split(url)
        if self._absent(archive_url) and self._staged(archive_url) is None:
            return []

        opened = self._reader(archive_url)
        refused = []
        listing = tarbox.children(
            opened.archive, inner, _choice(), on_refused=refused.append
        )
        if refused:
            plugin.log(
                "%s: %d entry(ies) point outside the archive and were left out: %s"
                % (archive_url, len(refused), ", ".join(sorted(set(refused))[:5])),
                level="warning",
            )
        return [
            Entry(
                name=item.name,
                kind=DIRECTORY if item.is_dir else FILE,
                size=item.size,
                modified=item.modified,
                target=item.target,
            )
            for item in listing
        ]

    def stat(self, url: str) -> Optional[Entry]:
        archive_url, inner = _split(url)
        if not inner:
            return Entry(name=tarbox.basename(archive_url), kind=DIRECTORY)

        # During a pack the question is asked once per file — "is this one there
        # already" — so it is answered from the names held in memory rather than
        # by walking the staging tar n times for n files.
        staging = self._staged(archive_url)
        if staging is not None:
            if inner in staging.known:
                return Entry(name=tarbox.basename(inner), kind=FILE)
            if any(name.startswith(inner + "/") for name in staging.known):
                return Entry(name=tarbox.basename(inner), kind=DIRECTORY)
            return None

        if self._absent(archive_url):
            return None
        opened = self._reader(archive_url)
        member = tarbox.find(opened.archive, inner, _choice())
        if member is not None:
            return Entry(
                name=tarbox.basename(inner),
                kind=FILE,
                size=member.size,
                modified=float(member.mtime) if opened.archive.is_tar else None,
            )
        if tarbox.holds(opened.archive, inner, _choice()):
            return Entry(name=tarbox.basename(inner), kind=DIRECTORY)
        return None

    def read(self, url: str, offset: int, length: int) -> bytes:
        archive_url, inner = _split(url)
        opened = self._reader(archive_url)
        member = tarbox.find(opened.archive, inner, _choice())
        if member is None:
            raise RpcError("%s is not in the archive" % inner)
        return tarbox.read_at(
            opened.archive, member, offset, length, fileobj=opened.handle
        )

    # -- writing ----------------------------------------------------------

    def begin_write(
        self, url: str, size: Optional[int], modified: Optional[float]
    ) -> None:
        self._incoming[url] = (size, modified)

    def write(self, url: str, data: bytes, mode: str) -> None:
        archive_url, inner = _split(url)
        if not inner:
            raise RpcError("An archive cannot be written to as if it were a file")
        path = self._writable_path(archive_url)

        if mode == "create":
            self._discard()
            self._start(path, archive_url)
            # **The size goes in front of the bytes in a tar**, and there is
            # nowhere to put it afterwards, so the member is written to a file
            # of its own and added when its real length is known. Which is also
            # why a cancelled copy here has touched nothing at all.
            self._member_path = self._staging.path + ".member"
            self._member_handle = open(self._member_path, "wb")
            self._member_url = url

        if self._member_handle is None or self._member_url != url:
            raise RpcError("%s was appended to without being started" % inner)
        if data:
            self._member_handle.write(data)

    def _start(self, path: str, archive_url: str) -> None:
        """Makes sure the staging tar for this archive is the one we are on."""
        if self._staging is not None and self._staging.archive_path == path:
            return
        self._flush_all()
        self._forget(archive_url)
        self._staging = tarbox.Staging(
            path, tarbox.compression_for(path), _level()
        )
        self._staging.prepare(log=lambda message: plugin.log(message, "warning"))

    def close_write(self, url: str, complete: bool) -> None:
        if self._member_handle is None or self._member_url != url:
            return
        handle, self._member_handle = self._member_handle, None
        member_path, self._member_path = self._member_path, None
        self._member_url = None
        size, modified = self._incoming.pop(url, (None, None))
        handle.close()

        try:
            if complete and self._staging is not None:
                _, inner = _split(url)
                self._staging.add(inner, member_path, modified)
        finally:
            if member_path and os.path.exists(member_path):
                os.unlink(member_path)

    def finish_writes(self, url: str) -> None:
        """The operation is over: write the archive, once."""
        self._discard()
        self._flush_all()
        archive_url, _ = _split(url)
        self._forget(archive_url)

    def _flush_all(self) -> None:
        staging = self._staging
        if staging is None:
            return
        self._staging = None
        try:
            staging.flush()
        except Exception as failure:
            plugin.log(
                "could not finish %s: %s" % (staging.archive_path, failure), "error"
            )
            raise RpcError("Could not finish %s: %s" % (staging.archive_path, failure))

    def _discard(self) -> None:
        """Throws away a member the copy never finished sending."""
        if self._member_handle is None:
            return
        handle, self._member_handle = self._member_handle, None
        member_path, self._member_path = self._member_path, None
        self._member_url = None
        try:
            handle.close()
        finally:
            if member_path and os.path.exists(member_path):
                os.unlink(member_path)

    # -- changing ---------------------------------------------------------

    def mkdir(self, url: str) -> None:
        archive_url, inner = _split(url)
        if not inner:
            raise RpcError("The archive itself already exists")
        path = self._writable_path(archive_url)
        self._start(path, archive_url)
        self._staging.add_directory(inner)

    def delete(self, url: str) -> None:
        archive_url, inner = _split(url)
        path = self._writable_path(archive_url)
        if not inner:
            raise RpcError("Leave the archive and delete it as a file")

        # Through the staging tar, so deleting several members is one rewrite of
        # a plain tar and one compression at the end rather than one of each per
        # member.
        self._start(path, archive_url)
        if inner not in self._staging.known and not any(
            name.startswith(inner + "/") for name in self._staging.known
        ):
            raise RpcError("%s is not in the archive" % inner)
        self._staging.drop(inner)

    def rename(self, source: str, target: str) -> None:
        source_archive, source_inner = _split(source)
        target_archive, target_inner = _split(target)
        if source_archive != target_archive:
            raise RpcError("A file can only be renamed inside its own archive")
        if not source_inner or not target_inner:
            raise RpcError("The archive itself is renamed from the folder holding it")

        path = self._writable_path(source_archive)
        self._start(path, source_archive)
        if target_inner in self._staging.known:
            raise RpcError("%s is already in the archive" % target_inner)
        if source_inner not in self._staging.known and not any(
            name.startswith(source_inner + "/") for name in self._staging.known
        ):
            raise RpcError("%s is not in the archive" % source_inner)

        tarbox.rewrite(
            self._staging.path,
            tarbox.renaming(source_inner, target_inner),
            "",
            0,
        )
        self._staging.known = set(self._staging.names())
        self._staging.dirty = True


def _reader(url: str) -> _Open:
    """The zip viewer's way in, sharing the file system's open archives."""
    return zips._reader(url)


zips = ZipFileSystem()
tars = TarFileSystem()
plugin.add_filesystem(zips)
plugin.add_filesystem(tars)


@plugin.on_shutdown
def _cleanup():
    """Nothing half written is left sealed as whole, and nothing is left staged.

    The staged tar is written out here rather than dropped: the host says when an
    operation ends, but a window closing is not an operation ending, and files
    somebody packed a moment ago should be in the archive whichever way the
    application went away.
    """
    zips._discard()
    tars._discard()
    tars._flush_all()
    zips._forget_all()
    tars._forget_all()


plugin.run()

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

import time
import zipfile
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import hostfile
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


class ArchiveFileSystem(FileSystem):
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
        self._open: Dict[str, _Open] = {}
        self._member: Optional[zipbox.Member] = None
        self._member_url: Optional[str] = None
        self._incoming: Dict[str, Tuple[Optional[int], Optional[float]]] = {}

    # -- opening for reading ----------------------------------------------

    def _reader(self, archive_url: str) -> _Open:
        """The archive, open for reading, reused while it has not changed.

        Reused because reading the central directory of a large archive is not
        free and a panel asks for a listing on every refresh; checked because an
        archive is a file somebody else can change under us, and a listing of
        what it used to be is worse than a slow one.
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
            archive = zipbox.opened(handle)
        except zipfile.BadZipFile as failure:
            handle.close()
            raise RpcError("%s is not a readable ZIP: %s" % (archive_url, failure))
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

    def _absent(self, archive_url: str) -> bool:
        return not plugin.stat(archive_url)

    # -- writing ----------------------------------------------------------

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


def _reader(url: str) -> _Open:
    """The viewer's way in, sharing the file system's open archives."""
    return filesystem._reader(url)


filesystem = ArchiveFileSystem()
plugin.add_filesystem(filesystem)


@plugin.on_shutdown
def _cleanup():
    """Anything half written is thrown away rather than left sealed as whole."""
    filesystem._discard()
    for archive_url in list(filesystem._open):
        filesystem._forget(archive_url)


plugin.run()

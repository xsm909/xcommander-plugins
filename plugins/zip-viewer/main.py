"""Archives, both as a listing and as a folder you can walk into.

Two things live here, and the split is the point.

The **viewer** answers F3 with a table of what is inside. It exists to mark the
boundary between the two plugin runtimes: showing a text file needs no code,
parsing a ZIP central directory does.

The **file system** is what makes Enter on an archive behave the way it does in
Total Commander — the archive opens as a directory and the panels walk it like
any other. It registers the ``zip:`` scheme, and the manifest declares which
extensions are really folders, so the core never learns what a ZIP is.

Note what neither assumes: the bytes arrive through ``read_file``, which goes
back through the host, so both work on an archive sitting on an FTP server that
a completely different plugin provides.
"""

from __future__ import annotations

import io
import posixpath
import time
import zipfile
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from xcommander import DIRECTORY, Entry, FILE, FileSystem, Plugin, RpcError, error, table

plugin = Plugin("org.xcommander.zip-viewer", "Archives")

# The central directory lives at the end, so the whole archive has to come
# across. Cap it rather than dragging a DVD image into memory.
MAX_BYTES = 256 << 20


@plugin.viewer(
    "zip.contents",
    "Archive contents",
    priority=30,
    extensions=["zip", "jar", "whl", "apk", "docx", "xlsx", "pptx", "epub"],
)
def list_contents(url):
    data = plugin.read_file(url, max_bytes=MAX_BYTES)
    if len(data) >= MAX_BYTES:
        return error("Archive is larger than %d MB." % (MAX_BYTES >> 20))

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as failure:
        return error("Not a readable ZIP archive: %s" % failure)

    rows = []
    for info in archive.infolist():
        stored = info.file_size
        packed = info.compress_size
        ratio = "" if not stored else "%d%%" % round(100 * packed / stored)
        modified = "%04d-%02d-%02d %02d:%02d" % info.date_time[:5]
        rows.append([info.filename, stored, packed, ratio, modified])

    if not rows:
        return error("The archive is empty.")

    plugin.log("listed %d entries in %s" % (len(rows), url))
    return table(["Name", "Size", "Packed", "Ratio", "Modified"], rows)


def _split(url: str) -> Tuple[str, str]:
    """Splits a ``zip:`` URL into (archive url, path inside the archive).

    They look like ``zip:///inner/path?from=file:///C:/box.zip``: the path is
    the path within the archive, the archive itself rides in the query. That
    keeps "go up one level" ordinary string work for the host, and lets the
    archive live on any transport.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    archive = (query.get("from") or [""])[0]
    if not archive:
        raise RpcError("No archive in %s" % url)
    inner = unquote(parsed.path or "").strip("/")
    return archive, inner


class _Cached:
    """One archive held open, with what it was when we read it."""

    def __init__(self, zip_file: zipfile.ZipFile, data: bytes):
        self.zip = zip_file
        self.data = data
        self.touched = time.monotonic()


class ArchiveFileSystem(FileSystem):
    """Reads a ZIP as a directory tree.

    Read-only on purpose: writing into an archive means rewriting it, and a
    panel that offers to move a file into a ZIP and then cannot is worse than
    one that says no. Copying *out* works, because that is only reads.
    """

    scheme = "zip"

    # Archives are read whole, so keep few and drop the oldest.
    MAX_CACHED = 3
    IDLE_TIMEOUT = 300.0

    def __init__(self):
        self._cache: Dict[str, _Cached] = {}

    # -- archive access ----------------------------------------------------

    def _open(self, archive_url: str) -> zipfile.ZipFile:
        cached = self._cache.get(archive_url)
        now = time.monotonic()
        if cached is not None and now - cached.touched < self.IDLE_TIMEOUT:
            cached.touched = now
            return cached.zip

        data = plugin.read_file(archive_url, max_bytes=MAX_BYTES)
        if not data:
            raise RpcError("Could not read %s" % archive_url)
        if len(data) >= MAX_BYTES:
            raise RpcError(
                "Archive is larger than %d MB." % (MAX_BYTES >> 20)
            )
        try:
            opened = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as failure:
            raise RpcError("Not a readable ZIP archive: %s" % failure)

        while len(self._cache) >= self.MAX_CACHED:
            oldest = min(self._cache, key=lambda key: self._cache[key].touched)
            self._cache.pop(oldest, None)
        self._cache[archive_url] = _Cached(opened, data)
        return opened

    # -- listing -----------------------------------------------------------

    def list(self, url: str) -> List[Entry]:
        archive_url, inner = _split(url)
        archive = self._open(archive_url)

        prefix = inner + "/" if inner else ""
        directories: Dict[str, None] = {}
        files: List[Entry] = []

        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix):].strip("/")
            if not rest:
                continue

            head, _, tail = rest.partition("/")
            if tail or info.is_dir():
                # A folder, whether it is stored as an entry of its own or only
                # implied by the names of the files under it.
                directories.setdefault(head, None)
                continue

            files.append(
                Entry(
                    name=head,
                    kind=FILE,
                    size=info.file_size,
                    modified=_stamp(info),
                )
            )

        entries: List[Entry] = [
            Entry(name=name, kind=DIRECTORY) for name in directories
        ]
        entries.extend(files)
        return entries

    def stat(self, url: str) -> Optional[Entry]:
        archive_url, inner = _split(url)
        if not inner:
            return Entry(name=posixpath.basename(archive_url), kind=DIRECTORY)

        archive = self._open(archive_url)
        try:
            info = archive.getinfo(inner)
        except KeyError:
            # Directories often have no entry of their own; if anything is
            # stored under this name, it is one.
            prefix = inner + "/"
            for name in archive.namelist():
                if name.replace("\\", "/").startswith(prefix):
                    return Entry(name=posixpath.basename(inner), kind=DIRECTORY)
            return None

        if info.is_dir():
            return Entry(name=posixpath.basename(inner), kind=DIRECTORY)
        return Entry(
            name=posixpath.basename(inner),
            kind=FILE,
            size=info.file_size,
            modified=_stamp(info),
        )

    def read(self, url: str, offset: int, length: int) -> bytes:
        archive_url, inner = _split(url)
        archive = self._open(archive_url)
        try:
            with archive.open(inner) as member:
                if offset:
                    member.read(offset)
                return member.read(length)
        except KeyError:
            raise RpcError("%s is not in the archive" % inner)


def _stamp(info: zipfile.ZipInfo) -> Optional[float]:
    """ZIP timestamps are local time with no zone; treat them as such."""
    try:
        return time.mktime(tuple(info.date_time) + (0, 0, -1))
    except (OverflowError, ValueError):
        return None


plugin.add_filesystem(ArchiveFileSystem())
plugin.run()

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

"""A playlist, as a listing and as a folder you can walk into.

**A playlist is not a document, it is a folder written down.** Every line in it
names a file that exists somewhere else, which is exactly what a directory is —
so the useful thing to do with one in a file manager is to walk into it. Enter
opens it as a folder of its tracks, F3 says what is in it, and everything the
application already does to a file works on a track: play it, copy it out, look
at what it is.

Nothing here plays anything. The tracks are ordinary files with ordinary names,
so whichever viewer claims an `.mp3` gets it — which is the sound viewer, and it
knows nothing about playlists.

Three formats, because they are the three that music players have written:
`.m3u` (whatever the machine's own alphabet was), `.m3u8` (the same thing in
UTF-8, which is what everything writes now) and `.pls` (an INI file that
Winamp's shoutcast lists still arrive in).
"""

from __future__ import annotations

import os
import posixpath
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from parse import Track, clock, decode, leaf, read as read_tracks

from xcommander import DIRECTORY, Entry, FILE, FileSystem, Plugin, RpcError, error, table

plugin = Plugin("org.xcommander.playlist", "Playlists")

# A playlist is a text file; anything past this is not one, and reading it whole
# would be reading somebody's mp3 as though it were a list.
MAX_BYTES = 8 << 20


def _folder_of(url: str) -> str:
    """The directory the playlist itself is in, for the relative lines.

    Only a local playlist has one. A list served over some other transport
    still opens; its relative lines are the ones that cannot be found, and the
    listing says so rather than pretending.
    """
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return ""
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and ":" in path[:4]:
        path = path[1:]
    return os.path.dirname(path.replace("/", os.sep))


def _read(url: str) -> List[Track]:
    data = plugin.read_file(url, max_bytes=MAX_BYTES)
    if len(data) >= MAX_BYTES:
        raise RpcError("A playlist of %d MB is not a playlist." % (MAX_BYTES >> 20))
    name = leaf(url).lower()
    text = decode(data, utf8=name.endswith(".m3u8"))
    kind = name.rsplit(".", 1)[-1] if "." in name else "m3u"
    return read_tracks(text, _folder_of(url), kind)


@plugin.viewer(
    "playlist.tracks",
    "Tracks",
    priority=30,
    extensions=["m3u", "m3u8", "pls"],
)
def list_tracks(url):
    """F3 on the playlist itself: what it names, and whether it is still there.

    The last column is the one worth having. A playlist outlives the files it
    points at — a folder gets moved, a drive is not mounted — and a list that
    shows twelve tracks where four of them are gone is a list that has told you
    nothing.
    """
    tracks = _read(url)
    if not tracks:
        return error("This playlist names no tracks.")

    rows = []
    for track in tracks:
        rows.append([
            track.order,
            track.title or posixpath.splitext(leaf(track.target))[0],
            clock(track.seconds),
            track.target,
            "elsewhere" if track.remote else ("missing" if track.missing else ""),
        ])
    plugin.log("read %d tracks from %s" % (len(tracks), url))
    return table(["#", "Title", "Length", "Where it points", "State"], rows)


class PlaylistFileSystem(FileSystem):
    """A playlist read as the folder it describes.

    Read-only, and flat: a playlist has no folders in it, whatever the files it
    names are arranged into on the disk. Writing is not offered because there is
    nothing sensible to write — a file copied "into" a playlist would have to be
    copied somewhere real first, and the panel has no way to ask where.
    """

    scheme = "m3u"

    # The docstring above says read-only; this is where the host is told, so a
    # panel standing in a playlist offers nothing it would then refuse.
    writable = False

    def _tracks(self, url: str) -> Tuple[str, List[Track], str]:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        playlist = (query.get("from") or [""])[0]
        if not playlist:
            raise RpcError("No playlist in %s" % url)
        inner = unquote(parsed.path or "").strip("/")
        return playlist, _read(playlist), inner

    def list(self, url: str) -> List[Entry]:
        _, tracks, _ = self._tracks(url)
        return [
            Entry(
                name=track.name,
                kind=FILE,
                size=track.size,
                modified=track.modified,
            )
            for track in tracks
        ]

    def stat(self, url: str) -> Optional[Entry]:
        playlist, tracks, inner = self._tracks(url)
        if not inner:
            return Entry(name=leaf(playlist), kind=DIRECTORY)
        for track in tracks:
            if track.name == inner:
                return Entry(
                    name=track.name,
                    kind=FILE,
                    size=track.size,
                    modified=track.modified,
                )
        return None

    def read(self, url: str, offset: int, length: int) -> bytes:
        _, tracks, inner = self._tracks(url)
        for track in tracks:
            if track.name != inner:
                continue
            if track.remote:
                raise RpcError("%s is not on this machine." % track.target)
            try:
                # Straight off the disk rather than back through the host: the
                # target is an ordinary local file, and a track is megabytes —
                # there is nothing to be gained by sending them round twice.
                with open(track.target, "rb") as handle:
                    if offset:
                        handle.seek(offset)
                    return handle.read(length)
            except OSError as failure:
                raise RpcError("%s: %s" % (track.target, failure))
        raise RpcError("%s is not in this playlist" % inner)


plugin.add_filesystem(PlaylistFileSystem())
plugin.run()

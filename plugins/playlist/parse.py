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

"""Reading the three kinds of list, and nothing else.

Kept apart from the plugin so it can be imported and checked without a host on
the other end — the same split the vector and node readers use.
"""

from __future__ import annotations

import configparser
import os
import posixpath
from typing import List, Optional, Tuple
from urllib.parse import urlsplit


class Track:
    """One line of a playlist, already resolved to where it points."""

    def __init__(self, order: int, title: str, seconds: int, target: str,
                 remote: bool):
        self.order = order
        self.title = title
        self.seconds = seconds

        # An absolute local path, or a URL for the lists that point at radio.
        self.target = target
        self.remote = remote

    @property
    def name(self) -> str:
        """What the track is called *as a file*, in the folder the playlist is.

        **It keeps the real extension**, and that is the whole of what makes
        this work: the host resolves a viewer by what a name ends in, so a
        track called `01 Song.mp3` is claimed by whatever claims mp3 and a track
        called `01 Song` is claimed by nothing.
        """
        _, extension = posixpath.splitext(leaf(self.target))
        stem = self.title or posixpath.splitext(leaf(self.target))[0]
        return "%02d %s%s" % (self.order, plain(stem), extension)

    @property
    def size(self) -> int:
        if self.remote:
            return 0
        try:
            return os.path.getsize(self.target)
        except OSError:
            return 0

    @property
    def modified(self) -> Optional[float]:
        if self.remote:
            return None
        try:
            return os.path.getmtime(self.target)
        except OSError:
            return None

    @property
    def missing(self) -> bool:
        return not self.remote and not os.path.exists(self.target)


def leaf(target: str) -> str:
    """The last part of a path or a URL, whichever this is."""
    if "://" in target:
        return posixpath.basename(urlsplit(target).path) or target
    return os.path.basename(target.replace("\\", "/"))


def plain(text: str) -> str:
    """A title with the characters a file name cannot carry taken out.

    The tracks are shown as files, and a title with a slash in it would read as
    a folder that is not there.
    """
    for bad in '/\\:*?"<>|':
        text = text.replace(bad, "-")
    return " ".join(text.split()).strip() or "track"


def decode(data: bytes, utf8: bool) -> str:
    """The bytes as text, in whatever they turn out to be written in.

    `.m3u8` is UTF-8 by definition. Plain `.m3u` is *whatever the machine that
    wrote it used*, which is why the fallback is a code page rather than an
    error: a list written in Windows Russian is a list, not a broken file.
    """
    for encoding in (["utf-8"] if utf8 else ["utf-8", "cp1251", "latin-1"]):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def resolve(target: str, folder: str) -> Tuple[str, bool]:
    """Where a line points, made absolute, and whether it left the disk.

    Relative paths are the normal case — a playlist beside its music says
    `01.mp3` — and they are relative to the *playlist*, not to wherever the
    panel happens to be standing.
    """
    target = target.strip()
    if "://" in target:
        return target, True
    native = target.replace("\\", os.sep).replace("/", os.sep)
    if os.path.isabs(native):
        return os.path.normpath(native), False
    return os.path.normpath(os.path.join(folder, native)), False


def parse_m3u(text: str, folder: str) -> List[Track]:
    tracks: List[Track] = []
    title = ""
    seconds = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # Only `#EXTINF` says anything about the next track; `#EXTM3U`,
            # `#EXTGRP` and the rest are somebody else's conventions.
            if line.upper().startswith("#EXTINF:"):
                body = line.split(":", 1)[1]
                length, _, said = body.partition(",")
                title = said.strip()
                try:
                    seconds = max(0, int(float(length.split(",")[0])))
                except ValueError:
                    seconds = 0
            continue
        target, remote = resolve(line, folder)
        tracks.append(Track(len(tracks) + 1, title, seconds, target, remote))
        title = ""
        seconds = 0
    return tracks


def parse_pls(text: str, folder: str) -> List[Track]:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as failure:
        raise ValueError("Not a readable playlist: %s" % failure)

    section = "playlist" if parser.has_section("playlist") else None
    if section is None:
        for name in parser.sections():
            if name.lower() == "playlist":
                section = name
                break
    if section is None:
        return []

    # `File1`, `Title1`, `Length1`, and the numbers are not promised to run
    # without gaps — so they are collected rather than counted through.
    entries = {}
    for key, value in parser.items(section):
        lowered = key.lower()
        for prefix in ("file", "title", "length"):
            if lowered.startswith(prefix) and lowered[len(prefix):].isdigit():
                entries.setdefault(int(lowered[len(prefix):]), {})[prefix] = value

    tracks: List[Track] = []
    for number in sorted(entries):
        line = entries[number].get("file", "").strip()
        if not line:
            continue
        target, remote = resolve(line, folder)
        try:
            seconds = max(0, int(entries[number].get("length", "0")))
        except ValueError:
            seconds = 0
        tracks.append(
            Track(len(tracks) + 1, entries[number].get("title", "").strip(),
                  seconds, target, remote)
        )
    return tracks



def clock(seconds: int) -> str:
    """Minutes and seconds, or nothing where the list did not say."""
    if seconds <= 0:
        return ""
    return "%d:%02d" % (seconds // 60, seconds % 60)


def read(text: str, folder: str, kind: str) -> List[Track]:
    """The tracks a list names. `kind` is the extension, lower case, no dot."""
    if kind == "pls":
        return parse_pls(text, folder)
    return parse_m3u(text, folder)

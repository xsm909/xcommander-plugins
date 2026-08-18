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

"""Checks the reader against lists written the way players write them.

    python3 selftest.py

Every answer here is worked out somewhere other than in the reader: what a
`#EXTINF` line means is in the format, a relative path resolves the way the
operating system says, and a `.pls` is an INI file whose numbering is not
promised to run without gaps.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parse  # noqa: E402

FAILURES = []


def check(what: str, got, want):
    if got != want:
        FAILURES.append("%s: got %r, wanted %r" % (what, got, want))


def m3u_basics():
    text = (
        "#EXTM3U\n"
        "#EXTGRP:Anything\n"
        "\n"
        "#EXTINF:194,Portishead - Roads\n"
        "roads.mp3\n"
        "# a bare comment\n"
        "/music/other/second.flac\n"
        "sub/third.m4a\n"
    )
    tracks = parse.read(text, "/music/list", "m3u")
    check("three tracks", len(tracks), 3)
    check("title", tracks[0].title, "Portishead - Roads")
    check("length", tracks[0].seconds, 194)
    check("relative resolves against the playlist", tracks[0].target,
          os.path.normpath("/music/list/roads.mp3"))
    check("absolute is left alone", tracks[1].target,
          os.path.normpath("/music/other/second.flac"))
    check("a folder in the line", tracks[2].target,
          os.path.normpath("/music/list/sub/third.m4a"))
    # The title belongs to the line after it and to no other line.
    check("no title carried over", tracks[1].title, "")


def names_are_files():
    """A track is shown as a file, so its name has to work as one."""
    text = "#EXTINF:0,AC/DC - Back in Black\nrock.mp3\n#EXTINF:0,\nnameless.ogg\n"
    tracks = parse.read(text, "/music", "m3u")
    check("the slash cannot stay", tracks[0].name, "01 AC-DC - Back in Black.mp3")
    check("no title falls back to the file", tracks[1].name, "02 nameless.ogg")
    check("the extension survives", os.path.splitext(tracks[0].name)[1], ".mp3")


def windows_lines():
    text = "..\\Music\\track.mp3\nD:\\Sound\\one.wav\n"
    tracks = parse.read(text, os.path.join(os.sep, "music", "list"), "m3u")
    check("backslashes are separators", os.path.basename(tracks[0].target),
          "track.mp3")
    check("up one level", tracks[0].target,
          os.path.normpath(os.path.join(os.sep, "music", "Music", "track.mp3")))


def radio():
    text = "#EXTINF:-1,Some Station\nhttp://stream.example/live.mp3\n"
    tracks = parse.read(text, "/music", "m3u")
    check("a URL is not a path", tracks[0].remote, True)
    check("and it is not made absolute", tracks[0].target,
          "http://stream.example/live.mp3")
    check("a negative length is no length", tracks[0].seconds, 0)


def pls():
    text = (
        "[playlist]\n"
        "NumberOfEntries=2\n"
        "File1=one.mp3\n"
        "Title1=The First\n"
        "Length1=100\n"
        "File7=seven.mp3\n"
        "Title7=The Seventh\n"
        "Length7=-1\n"
        "Version=2\n"
    )
    tracks = parse.read(text, "/music", "pls")
    check("both entries", len(tracks), 2)
    check("numbering may have holes", tracks[1].title, "The Seventh")
    check("renumbered in order", [t.order for t in tracks], [1, 2])
    check("a negative length is no length", tracks[1].seconds, 0)


def alphabets():
    """A list written before UTF-8 was universal is still a list."""
    russian = "#EXTINF:10,Кино - Группа крови\nkino.mp3\n"
    check("utf-8", parse.decode(russian.encode("utf-8"), utf8=True), russian)
    check("a code page, when the name does not promise utf-8",
          parse.decode(russian.encode("cp1251"), utf8=False), russian)


def what_is_there():
    """The state column is the reason to look at a playlist at all."""
    with tempfile.TemporaryDirectory() as folder:
        real = os.path.join(folder, "here.mp3")
        with open(real, "wb") as handle:
            handle.write(b"\0" * 32)
        tracks = parse.read("here.mp3\ngone.mp3\n", folder, "m3u")
        check("the one that is there", tracks[0].missing, False)
        check("its size", tracks[0].size, 32)
        check("the one that is not", tracks[1].missing, True)
        check("and it has no size", tracks[1].size, 0)


def main() -> int:
    for test in (m3u_basics, names_are_files, windows_lines, radio, pls,
                 alphabets, what_is_there):
        test()
    if FAILURES:
        for failure in FAILURES:
            print("FAIL", failure)
        return 1
    print("playlist: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

"""The archive work, checked without the application.

Run it with `python3 selftest.py`. It needs nothing installed and nothing on the
path: the modules it exercises know about ZIPs and about reading through the
host, and neither imports the SDK. Real archives are built in a temporary folder
and read back, because the questions worth asking here — is the date right, is
the deleted member gone, did the cancelled copy leave half a file — cannot be
answered by a mock.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import zipfile

import hostfile
import zipbox

FAILURES = []


def check(name, got, expected):
    if got != expected:
        FAILURES.append("%s\n  expected %r\n  got      %r" % (name, expected, got))


def ok(name, condition):
    if not condition:
        FAILURES.append("%s\n  expected it to hold, and it did not" % name)


class FakeHost:
    """The host, as far as :mod:`hostfile` can tell: bytes and a count of reads.

    The count is the point of it. An archive is supposed to be read from its end
    — the directory at the tail, then each member by its offset — and the way to
    know that is happening is that listing a big archive does not read most of
    it.
    """

    def __init__(self, data: bytes):
        self.data = data
        self.read_bytes = 0

    def read_file(self, url, max_bytes=1 << 20, offset=0):
        chunk = self.data[offset : offset + max_bytes]
        self.read_bytes += len(chunk)
        return chunk


# -- names -----------------------------------------------------------------


def legacy(text: str, codec: str) -> zipfile.ZipInfo:
    """An entry as an old archiver left it: bytes in a code page, and no flag.

    ``zipfile`` decodes a name with no UTF-8 flag as code page 437, and 437 maps
    every byte, so this is exactly the string it would hand us.
    """
    info = zipfile.ZipInfo(text.encode(codec).decode("cp437"))
    info.flag_bits = 0
    return info


def names():
    check(
        "a DOS name comes back as what was typed",
        zipbox.decoded_name(legacy("Отчёт за август.txt", "cp866")),
        "Отчёт за август.txt",
    )
    check(
        "so does a Windows one, which is the same bytes read the other way",
        zipbox.decoded_name(legacy("Отчёт за август.txt", "cp1251")),
        "Отчёт за август.txt",
    )
    # Told which code page to use, it obeys even where it would have chosen
    # the other one: the setting exists for the archive the guess gets wrong.
    check(
        "asked for one code page, that is the one used",
        zipbox.decoded_name(legacy("Отчёт.txt", "cp866"), zipbox.WINDOWS),
        "Отчёт.txt".encode("cp866").decode("cp1251"),
    )
    check(
        "and 'leave them alone' leaves them alone",
        zipbox.decoded_name(legacy("Отчёт.txt", "cp866"), zipbox.LITERAL),
        "Отчёт.txt".encode("cp866").decode("cp437"),
    )

    ascii_only = zipfile.ZipInfo("notes.txt")
    ascii_only.flag_bits = 0
    check("an ASCII name is nobody's business", zipbox.decoded_name(ascii_only), "notes.txt")

    modern = zipfile.ZipInfo("Отчёт.txt")
    modern.flag_bits = zipbox.UTF8_FLAG
    check(
        "a name that says it is UTF-8 is believed",
        zipbox.decoded_name(modern),
        "Отчёт.txt",
    )


def dangerous():
    for name in ("../outside.txt", "a/../../b.txt", "/etc/passwd", "C:/Windows/x"):
        ok("%s is refused" % name, zipbox.is_dangerous(name))
    for name in ("a/b.txt", "..dotted/x", "a..b/c"):
        ok("%s is a file, not an escape" % name, not zipbox.is_dangerous(name))


# -- reading ---------------------------------------------------------------


def reading(folder: str):
    path = os.path.join(folder, "read.zip")
    # Incompressible on purpose, and bigger than the read buffer: a filler of
    # one repeated byte deflates to nothing, and an archive smaller than the
    # buffer is read whole by one buffered read whatever we do. Neither would
    # prove anything about reading from the end.
    filler = os.urandom(4 << 20)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("readme.txt", "hello")
        archive.writestr("docs/inner.txt", "deeper")
        archive.writestr("docs/pictures/", "")
        archive.writestr("big.bin", filler)

    blob = open(path, "rb").read()
    host = FakeHost(blob)
    handle = hostfile.opened(host, "file:///read.zip", len(blob))
    archive = zipbox.opened(handle)

    top = {item.name: item for item in zipbox.children(archive, "")}
    check("the top level", sorted(top), ["big.bin", "docs", "readme.txt"])
    ok("a folder is a folder", top["docs"].is_dir)
    ok("a file is not", not top["big.bin"].is_dir)
    check("and it knows how big it is", top["big.bin"].size, len(filler))

    inner = {item.name: item for item in zipbox.children(archive, "docs")}
    check("one level down", sorted(inner), ["inner.txt", "pictures"])
    ok("a folder stored as its own entry still reads as one", inner["pictures"].is_dir)

    info = zipbox.find(archive, "docs/inner.txt")
    ok("a member is found by its path", info is not None)
    check("and read", zipbox.read_at(archive, info, 0, 100), b"deeper")
    check("from an offset", zipbox.read_at(archive, info, 2, 3), b"epe")
    ok("a folder is not a member", zipbox.find(archive, "docs") is None)
    ok("but it holds things", zipbox.holds(archive, "docs"))
    ok("and something absent holds nothing", not zipbox.holds(archive, "elsewhere"))

    # The whole point of reading through a file-like object: listing a 4 MB
    # archive reads the directory at the end of it and not the middle.
    ok(
        "listing does not drag the whole archive across (%d of %d bytes)"
        % (host.read_bytes, len(blob)),
        host.read_bytes < len(blob) / 2,
    )
    archive.close()
    handle.close()


# -- writing ---------------------------------------------------------------


def writing(folder: str):
    path = os.path.join(folder, "new.zip")
    when = time.mktime((2020, 5, 17, 13, 45, 0, 0, 0, -1))

    member = zipbox.Member(path, "notes.txt", modified=when, size=11, level=6)
    member.write(b"hello ")
    member.write(b"world")
    member.close()

    ok("the archive was created by its first member", os.path.exists(path))
    with zipfile.ZipFile(path) as archive:
        check("what went in came out", archive.read("notes.txt"), b"hello world")
        info = archive.getinfo("notes.txt")
        check(
            "with the date the source had, not the date it was packed",
            info.date_time[:5],
            (2020, 5, 17, 13, 45),
        )
        ok("and deflated", info.compress_type == zipfile.ZIP_DEFLATED)

    # A second member joins it rather than replacing the archive.
    second = zipbox.Member(path, "docs/deeper.txt", modified=when, size=4, level=0)
    second.write(b"more")
    second.close()
    with zipfile.ZipFile(path) as archive:
        check("both are there", sorted(archive.namelist()), ["docs/deeper.txt", "notes.txt"])
        ok(
            "level zero stores rather than compresses",
            archive.getinfo("docs/deeper.txt").compress_type == zipfile.ZIP_STORED,
        )

    # An overwrite the user agreed to leaves one entry, not two.
    again = zipbox.Member(path, "notes.txt", modified=when, size=5, level=6)
    again.write(b"again")
    again.close()
    with zipfile.ZipFile(path) as archive:
        check("one entry per name", archive.namelist().count("notes.txt"), 1)
        check("and it is the new one", archive.read("notes.txt"), b"again")

    # A cancelled copy takes its half a file back out with it.
    aborted = zipbox.Member(path, "half.bin", modified=when, size=1000, level=6)
    aborted.write(b"only the start")
    aborted.close(complete=False)
    with zipfile.ZipFile(path) as archive:
        ok("nothing half written was left sealed", "half.bin" not in archive.namelist())
        check("and the rest survived", archive.read("notes.txt"), b"again")
        ok("the archive is still readable", archive.testzip() is None)

    # An empty file is a file.
    empty = zipbox.Member(path, "nothing.txt", modified=when, size=0, level=6)
    empty.close()
    with zipfile.ZipFile(path) as archive:
        check("an empty member is stored", archive.read("nothing.txt"), b"")

    zipbox.add_directory(path, "empty-folder", 6)
    with zipfile.ZipFile(path) as archive:
        ok("an empty folder survives", "empty-folder/" in archive.namelist())
        ok(
            "and reads as a folder",
            archive.getinfo("empty-folder/").is_dir(),
        )


def changing(folder: str):
    path = os.path.join(folder, "change.zip")
    when = (2019, 3, 2, 8, 30, 0)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("keep.txt", "docs/a.txt", "docs/b.txt", "docs/deep/c.txt"):
            info = zipfile.ZipInfo(name, date_time=when)
            archive.writestr(info, name.encode())

    zipbox.rewrite(path, zipbox.dropping("docs/a.txt"), 6)
    with zipfile.ZipFile(path) as archive:
        check(
            "one member gone, the others untouched",
            sorted(archive.namelist()),
            ["docs/b.txt", "docs/deep/c.txt", "keep.txt"],
        )
        check(
            "and the dates were carried over, not stamped anew",
            archive.getinfo("keep.txt").date_time[:5],
            when[:5],
        )
        check("with the bytes intact", archive.read("docs/deep/c.txt"), b"docs/deep/c.txt")

    zipbox.rewrite(path, zipbox.dropping("docs"), 6)
    with zipfile.ZipFile(path) as archive:
        check(
            "deleting a folder takes everything under it",
            archive.namelist(),
            ["keep.txt"],
        )

    zipbox.rewrite(path, zipbox.renaming("keep.txt", "kept.txt"), 6)
    with zipfile.ZipFile(path) as archive:
        check("a rename is the same rewrite", archive.namelist(), ["kept.txt"])
        check("carrying the bytes", archive.read("kept.txt"), b"keep.txt")

    # A folder rename moves everything below it.
    path = os.path.join(folder, "tree.zip")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("old/one.txt", "1")
        archive.writestr("old/sub/two.txt", "2")
        archive.writestr("other.txt", "3")
    zipbox.rewrite(path, zipbox.renaming("old", "new"), 6)
    with zipfile.ZipFile(path) as archive:
        check(
            "the whole branch moved",
            sorted(archive.namelist()),
            ["new/one.txt", "new/sub/two.txt", "other.txt"],
        )

    ok(
        "no part file was left beside the archive",
        not any(name.endswith(zipbox.PART) for name in os.listdir(folder)),
    )


def dangerous_listing(folder: str):
    """An archive that tries to write outside itself lists without those names."""
    path = os.path.join(folder, "evil.zip")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fine.txt", "ok")
        archive.writestr("../escape.txt", "not ok")

    blob = open(path, "rb").read()
    handle = hostfile.opened(FakeHost(blob), "file:///evil.zip", len(blob))
    archive = zipbox.opened(handle)
    refused = []
    listing = zipbox.children(archive, "", zipbox.AUTO, refused.append)
    check("only the honest one is listed", [item.name for item in listing], ["fine.txt"])
    check("and the other is reported", refused, ["../escape.txt"])
    archive.close()
    handle.close()


def main():
    folder = tempfile.mkdtemp(prefix="xcommander-archives-")
    try:
        names()
        dangerous()
        reading(folder)
        writing(folder)
        changing(folder)
        dangerous_listing(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    if FAILURES:
        print("\n\n".join(FAILURES))
        print("\n%d failed" % len(FAILURES))
        return 1
    print("archives: all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())

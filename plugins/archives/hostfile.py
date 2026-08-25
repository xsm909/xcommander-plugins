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

"""A file on the host, read the way ``open()`` would let you read it.

An archive is read from the end: the central directory is the last thing in it,
and every member is then found by an offset. That is the opposite of how the
first version of this plugin worked — it pulled the whole archive across and
capped itself at 256 MB, so a DVD image in a ZIP was simply out of reach and a
2 GB backup took a 2 GB copy in memory to list.

``host.read`` has always taken an offset. What was missing was something that
looks like a file, so that :mod:`zipfile` can do the seeking for us rather than
the format being parsed by hand. That is all this is: seek and read, answered by
the host, which means it works on an archive sitting on FTP or inside another
plugin's file system exactly as it does on the local disk.
"""

from __future__ import annotations

import io
from typing import Optional


class HostFile(io.RawIOBase):
    """A seekable, read-only file whose bytes come from the host.

    Wrap it in :class:`io.BufferedReader` before handing it to anything that
    reads in small pieces — that is what turns a stream of little reads into
    whole blocks, and one round trip instead of hundreds.
    """

    def __init__(self, plugin, url: str, size: int):
        self._plugin = plugin
        self._url = url
        self._size = max(0, int(size))
        self._position = 0

    # -- what kind of file this is -----------------------------------------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def size(self) -> int:
        return self._size

    # -- moving about ------------------------------------------------------

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._position + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError("Unknown whence: %r" % whence)

        # Past the end is allowed and reads nothing, which is what a real file
        # does; before the beginning is an error, which it also does.
        if target < 0:
            raise OSError("Cannot seek before the start of %s" % self._url)
        self._position = target
        return self._position

    def tell(self) -> int:
        return self._position

    # -- reading -----------------------------------------------------------

    def readinto(self, buffer) -> int:
        want = len(buffer)
        if want == 0 or self._position >= self._size:
            return 0
        want = min(want, self._size - self._position)

        data = self._plugin.read_file(self._url, max_bytes=want, offset=self._position)
        if not data:
            return 0
        buffer[: len(data)] = data
        self._position += len(data)
        return len(data)

    def read(self, size: Optional[int] = -1) -> bytes:
        if size is None or size < 0:
            size = max(0, self._size - self._position)
        buffer = bytearray(size)
        read = self.readinto(buffer)
        return bytes(buffer[:read])


def opened(plugin, url: str, size: int, buffer: int = 1 << 18) -> io.BufferedReader:
    """[HostFile] with buffering, which is how it should always be used."""
    return io.BufferedReader(HostFile(plugin, url, size), buffer_size=buffer)

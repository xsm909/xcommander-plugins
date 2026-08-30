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

"""What a recording says it is — the panel beside the sound viewer.

**A plugin of its own, beside the viewer rather than inside it**, and for the
reason the whole describer contribution exists: the sound viewer is
*declarative*, because the machine's own engine is what plays a file and draws
its shape. It runs no code of ours, so the tags inside the file have nobody to
come from. This is that somebody, and it is asked whoever is playing.

It reads the head of the file, and one exact range further in where an MP4
keeps its atoms at the end. Never a sample.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xcommander import Plugin, fact, fact_group, facts  # noqa: E402

import tags  # noqa: E402

plugin = Plugin("org.xcommander.soundtags", "Sound tags")

#: Every format the sound viewer offers to play, and a few it does not — a file
#: nobody can play still says what it is, and that is worth more than a refusal.
DESCRIBED = [
    "aac", "ac3", "adts", "aif", "aifc", "aiff", "amr", "au", "caf", "flac",
    "m4a", "m4b", "mp2", "mp3", "mp4", "oga", "ogg", "opus", "snd", "wav",
    "wave", "wma",
]

#: ID3 stands in front of an MP3, a FLAC's blocks come first, and a RIFF's
#: chunks are walked from the start — so the head is where nearly everything
#: is. The MP4 whose atoms sit at the end is the exception, and it is handled
#: by asking for exactly them; see [_describe].
HEAD_BYTES = 4 << 20

#: A `moov` atom worth fetching on its own. A long recording's is a table of
#: every sample in it, and past this it is not tags any more, it is an index.
MOOV_BYTES = 32 << 20


@plugin.describer("sound.about", "About this recording", extensions=DESCRIBED)
def about(url: str) -> dict:
    started = time.time()
    name = url.rsplit("/", 1)[-1]
    whole = (plugin.stat(url) or {}).get("size")

    try:
        found = _describe(url, whole)
    except Exception as failure:  # noqa: BLE001 - a damaged header is not a crash
        return facts(
            [_file_group(name, whole, tags.Sound())],
            note="This file could not be read: %s" % failure,
        )

    groups = [
        _tags_group(found),
        _file_group(name, whole, found),
        _sound_group(found),
    ]
    plugin.log(
        "%s: %s, %d tag(s)%s, %.2fs"
        % (name, found.kind or "unknown", len(found.tags),
           ", a cover" if found.picture else "", time.time() - started)
    )
    return facts(
        [g for g in groups if g],
        note=_note(found),
        picture=found.picture,
        mime_type=found.picture_type or "image/jpeg",
    )


def _describe(url: str, whole) -> tags.Sound:
    """The file read, with the one case that needs a second look taken.

    **An MP4's `moov` may be at either end.** An encoder that knows the length
    before it starts writes it first; anything recording as it goes cannot, and
    puts it after the sound — so on half the files there are, the tags sit past
    whatever head is read. The chain of atom lengths says *where* it is even
    when its bytes have not arrived, so exactly that range is asked for rather
    than the whole file being pulled over the pipe.
    """
    head = plugin.read_file(url, max_bytes=HEAD_BYTES)
    found = tags.read(head)
    if head[4:8] != b"ftyp" or found.tags or found.seconds:
        return found
    where = tags.moov_at(head)
    if where is None or where[0] + where[1] <= len(head):
        return found
    offset, length = where
    if length > MOOV_BYTES:
        found.notes.append("Its table of contents is too large to read.")
        return found
    atom = plugin.read_file(url, max_bytes=length, offset=offset)
    if len(atom) < 8:
        return found
    later = tags.read_mp4_moov(atom, whole if isinstance(whole, int) else 0)
    later.kind = found.kind or later.kind
    return later


def _tags_group(found) -> dict:
    rows = []
    for key in tags.ORDER:
        value = found.tags.get(key)
        if value:
            rows.append(fact(key.capitalize(), value, wide=len(value) > 40))
    return fact_group("Tags", rows)


def _file_group(name: str, whole, found) -> dict:
    rows = [fact("Name", name)]
    if isinstance(whole, int):
        rows.append(fact("Size", tags.size(whole)))
    if found.kind:
        rows.append(fact("Format", found.kind))
    if found.seconds:
        rows.append(fact("Length", tags.clock(found.seconds)))
    return fact_group("File", rows)


def _sound_group(found) -> dict:
    rows = []
    if found.rate:
        rows.append(fact("Sample rate", "%s kHz" % _trim(found.rate / 1000)))
    channels = tags.channels(found.channels)
    if channels:
        rows.append(fact("Channels", channels))
    if found.bits:
        rows.append(fact("Bit depth", "%d bits" % found.bits))
    if found.bitrate:
        rows.append(fact("Bitrate", "%d kbit/s" % round(found.bitrate / 1000)))
    return fact_group("Sound", rows)


def _trim(value: float) -> str:
    return ("%g" % round(value, 3))


def _note(found) -> str:
    remarks = list(found.notes)
    if not found.tags and not found.picture:
        remarks.append("It carries no tags — only what its header says.")
    if found.extra:
        # **Said rather than shown.** What is above is a choice out of what is
        # there, and a summary that does not admit to being one is a claim.
        remarks.append(
            "%d more field(s) the file names: %s."
            % (len(found.extra), ", ".join(sorted(found.extra)[:8]))
        )
    return " ".join(remarks) if remarks else ""


if __name__ == "__main__":
    plugin.run()

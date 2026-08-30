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

"""What a recording says it is: the tags, the cover, and what is in the file.

**Nothing here is the same in two formats, which is the whole of the work.** A
photograph's metadata is one TIFF directory reached five ways; a recording's is
four unrelated schemes that happen to hold the same six facts — ID3 frames with
a four-character name and a synchsafe length, an MP4 atom tree with `©nam` in
it, a FLAC block of `NAME=value` lines, and a RIFF `LIST INFO` chunk of
four-character codes. So this is four readers with one table of names at the
end of them, and the table is what makes them one thing.

**And the shape of the sound is not read here.** How long it is, what rate and
how many channels comes out of the *decoder* the viewer already uses — the
plugin only reads what the container writes down about itself, which is why a
file whose header lies about its length is reported as its header says. Where
the header carries nothing, nothing is claimed.

No decoding, ever: this reads a header and a tag block and stops.
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple


class Sound:
    """What one file turned out to be, and what it had to say."""

    def __init__(self, kind: str = ""):
        self.kind = kind
        #: The tags, under the names in `NAMES` — `title`, `artist` and so on.
        self.tags: Dict[str, str] = {}
        #: Anything named that this does not have a word for, in the file's
        #: own words. Counted rather than shown, so a summary admits to being
        #: one.
        self.extra: Dict[str, str] = {}
        self.picture: Optional[bytes] = None
        self.picture_type: str = ""
        self.seconds: float = 0.0
        self.rate: int = 0
        self.channels: int = 0
        self.bits: int = 0
        self.bitrate: int = 0
        self.notes: List[str] = []


#: What each scheme calls the same handful of things. The keys on the left are
#: this plugin's own, and are what the panel is built out of.
NAMES = {
    # ID3v2.3 and 2.4
    "TIT2": "title", "TPE1": "artist", "TALB": "album", "TPE2": "album artist",
    "TCON": "genre", "TRCK": "track", "TPOS": "disc", "TYER": "year",
    "TDRC": "year", "TDRL": "year", "TCOM": "composer", "TPUB": "publisher",
    "TENC": "encoded by", "TSSE": "encoder", "TBPM": "beats per minute",
    "TLEN": "length", "TCOP": "copyright", "COMM": "comment",
    # ID3v2.2, three characters
    "TT2": "title", "TP1": "artist", "TAL": "album", "TP2": "album artist",
    "TCO": "genre", "TRK": "track", "TYE": "year", "TCM": "composer",
    "COM": "comment",
    # MP4
    "\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
    "aART": "album artist", "\xa9gen": "genre", "\xa9day": "year",
    "\xa9wrt": "composer", "\xa9cmt": "comment", "\xa9too": "encoder",
    "trkn": "track", "disk": "disc", "tmpo": "beats per minute",
    "cprt": "copyright",
    # Vorbis comments, in FLAC
    "TITLE": "title", "ARTIST": "artist", "ALBUM": "album",
    "ALBUMARTIST": "album artist", "GENRE": "genre", "DATE": "year",
    "TRACKNUMBER": "track", "DISCNUMBER": "disc", "COMPOSER": "composer",
    "COMMENT": "comment", "DESCRIPTION": "comment", "COPYRIGHT": "copyright",
    "ENCODER": "encoder", "PUBLISHER": "publisher", "ORGANIZATION": "publisher",
    # RIFF LIST INFO
    "INAM": "title", "IART": "artist", "IPRD": "album", "IGNR": "genre",
    "ICRD": "year", "ITRK": "track", "ICMT": "comment", "ICOP": "copyright",
    "ISFT": "encoder", "IENG": "encoded by",
}

#: The order the panel reads them in. Anything not here is counted, not shown.
ORDER = ["title", "artist", "album artist", "album", "track", "disc", "year",
         "genre", "composer", "publisher", "beats per minute", "comment",
         "copyright", "encoder", "encoded by"]

#: ID3v1's numbered genres, which ID3v2 still writes as `(17)`.
GENRES = [
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge",
    "Hip-Hop", "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B",
    "Rap", "Reggae", "Rock", "Techno", "Industrial", "Alternative", "Ska",
    "Death Metal", "Pranks", "Soundtrack", "Euro-Techno", "Ambient",
    "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion", "Trance", "Classical",
    "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise",
    "Alternative Rock", "Bass", "Soul", "Punk", "Space", "Meditative",
    "Instrumental Pop", "Instrumental Rock", "Ethnic", "Gothic", "Darkwave",
    "Techno-Industrial", "Electronic", "Pop-Folk", "Eurodance", "Dream",
    "Southern Rock", "Comedy", "Cult", "Gangsta", "Top 40", "Christian Rap",
    "Pop/Funk", "Jungle", "Native US", "Cabaret", "New Wave", "Psychedelic",
    "Rave", "Showtunes", "Trailer", "Lo-Fi", "Tribal", "Acid Punk",
    "Acid Jazz", "Polka", "Retro", "Musical", "Rock & Roll", "Hard Rock",
]


def read(data: bytes) -> Sound:
    """Whatever this file will say, whichever of the containers it is."""
    if data[:4] == b"fLaC":
        return _flac(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return _wav(data)
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return _aiff(data)
    if data[4:8] == b"ftyp":
        return _mp4(data)
    if data[:3] == b"ID3" or _mpeg_frame_at(data, 0) is not None:
        return _mpeg(data)
    return Sound()


# -- ID3v2, which lives in front of an MP3 and inside an AIFF --------------


def _synchsafe(raw: bytes) -> int:
    """ID3's own integer: seven bits a byte, so it can never look like a frame."""
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


def read_id3(data: bytes, found: Sound) -> int:
    """Every frame of an ID3v2 tag at the front of [data]. Returns its length."""
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    major = data[3]
    flags = data[5]
    size = _synchsafe(data[6:10])
    end = min(10 + size, len(data))
    at = 10

    if flags & 0x40 and major >= 3:  # an extended header, of no interest
        if at + 4 <= end:
            extended = (_synchsafe(data[at:at + 4]) if major >= 4
                        else struct.unpack_from(">I", data, at)[0] + 4)
            at += max(0, extended)

    # **Unsynchronisation is undone over the whole tag, not per frame.** A
    # writer that set the flag has scattered a zero after every 0xFF, and a
    # reader that skips that step gets frame lengths that are wrong by however
    # many there were — which reads as a tag full of nonsense rather than as
    # an error.
    body = data[at:end]
    if flags & 0x80:
        body = body.replace(b"\xff\x00", b"\xff")
        at = 0
        end = len(body)
    else:
        body = data
    width = 3 if major == 2 else 4
    header = 6 if major == 2 else 10

    while at + header <= end:
        name = body[at:at + width]
        if not name.strip(b"\0"):
            break  # padding: the rest of the tag is nothing
        if major == 2:
            length = int.from_bytes(body[at + 3:at + 6], "big")
        elif major == 4:
            length = _synchsafe(body[at + 4:at + 8])
        else:
            length = struct.unpack_from(">I", body, at + 4)[0]
        if length <= 0 or at + header + length > end:
            break
        payload = body[at + header:at + header + length]
        _id3_frame(name.decode("latin-1"), payload, found)
        at += header + length
    return 10 + size


def _id3_frame(name: str, payload: bytes, found: Sound) -> None:
    if name in ("APIC", "PIC"):
        _id3_picture(name, payload, found)
        return
    if not payload:
        return
    text = _id3_text(payload, name)
    if not text:
        return
    key = NAMES.get(name)
    if key:
        found.tags.setdefault(key, _genre(text) if key == "genre" else text)
    else:
        found.extra.setdefault(name, text)


def _id3_text(payload: bytes, name: str) -> str:
    encoding = payload[0]
    body = payload[1:]
    if name in ("COMM", "COM", "USLT", "ULT"):
        # Language, then a short description, then the comment itself.
        body = body[3:]
        body = _after_terminator(body, encoding)
    return _decode(encoding, body).strip("\0 ").strip()


def _after_terminator(body: bytes, encoding: int) -> bytes:
    if encoding in (1, 2):
        for i in range(0, len(body) - 1, 2):
            if body[i:i + 2] == b"\0\0":
                return body[i + 2:]
        return b""
    cut = body.find(b"\0")
    return body[cut + 1:] if cut >= 0 else b""


def _decode(encoding: int, body: bytes) -> str:
    if encoding == 0:
        return body.decode("latin-1", "replace")
    if encoding == 1:
        return body.decode("utf-16", "replace").lstrip("﻿")
    if encoding == 2:
        return body.decode("utf-16-be", "replace")
    return body.decode("utf-8", "replace")


def _id3_picture(name: str, payload: bytes, found: Sound) -> None:
    if found.picture or len(payload) < 4:
        return
    encoding = payload[0]
    if name == "PIC":  # v2.2: three characters of format, not a MIME type
        kind = payload[1:4].decode("latin-1", "replace").lower()
        mime = {"jpg": "image/jpeg", "png": "image/png"}.get(kind, "image/jpeg")
        rest = payload[5:]
    else:
        cut = payload.find(b"\0", 1)
        if cut < 0:
            return
        mime = payload[1:cut].decode("latin-1", "replace") or "image/jpeg"
        rest = payload[cut + 2:]  # past the terminator and the picture type
    body = _after_terminator(rest, encoding)  # past the description
    if body:
        found.picture = body
        found.picture_type = mime if "/" in mime else "image/" + mime


def _genre(text: str) -> str:
    """`(17)` is Rock, and half the world still writes it that way."""
    value = text.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    if value.isdigit():
        number = int(value)
        return GENRES[number] if 0 <= number < len(GENRES) else text
    return text


# -- MP3, and what its own frames say --------------------------------------

#: Bitrates, in thousands, by (MPEG version 1?, layer). The layer bits count
#: *down* — 3 is Layer I — which is the one thing about this header that trips
#: everybody, along with the version bits, where 3 is MPEG 1 and 0 is MPEG 2.5.
_BITRATES = {
    (True, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (True, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (True, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (False, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (False, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (False, 3): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
}

#: Sample rates by version: 3 is MPEG 1, 2 is MPEG 2, 0 is MPEG 2.5, and 1 is
#: reserved and means this is not a frame at all.
_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000],
          0: [11025, 12000, 8000]}

#: Samples in one frame, by (MPEG version 1?, layer).
_SAMPLES = {(True, 1): 1152, (True, 2): 1152, (True, 3): 384,
            (False, 1): 576, (False, 2): 1152, (False, 3): 384}


def _mpeg_frame_at(data: bytes, at: int) -> Optional[Tuple[int, int, int, int, int]]:
    """`(rate, channels, bitrate, frame length, samples)` if a frame starts here."""
    if at + 4 > len(data) or data[at] != 0xFF or (data[at + 1] & 0xE0) != 0xE0:
        return None
    version = (data[at + 1] >> 3) & 3
    layer = (data[at + 1] >> 1) & 3
    if version == 1 or layer == 0:  # both values are reserved
        return None
    bitrate_index = (data[at + 2] >> 4) & 15
    rate_index = (data[at + 2] >> 2) & 3
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    first = version == 3
    bitrate = _BITRATES[(first, layer)][bitrate_index] * 1000
    rate = _RATES[version][rate_index]
    if not bitrate or not rate:
        return None
    channels = 1 if ((data[at + 3] >> 6) & 3) == 3 else 2
    padding = (data[at + 2] >> 1) & 1
    samples = _SAMPLES[(first, layer)]
    if layer == 3:  # Layer I counts its frame in slots of four bytes
        length = (12 * bitrate // rate + padding) * 4
    else:
        length = samples // 8 * bitrate // rate + padding
    return rate, channels, bitrate, max(length, 4), samples


def _mpeg(data: bytes) -> Sound:
    found = Sound("MP3")
    at = read_id3(data, found)
    # The first real frame, within a little slack for whatever sits between.
    for probe in range(at, min(at + 65536, len(data) - 4)):
        frame = _mpeg_frame_at(data, probe)
        if frame is None:
            continue
        # **A sync pattern is eleven bits, and eleven bits happen.** One frame
        # that parses is not a frame; a frame whose length lands exactly on
        # another one is. Checked before anything is believed, because a false
        # start reads as a file at the wrong rate rather than as an error.
        following = _mpeg_frame_at(data, probe + frame[3])
        if following is None and probe + frame[3] + 4 < len(data):
            continue
        found.rate, found.channels, found.bitrate, length, samples = frame
        _xing(data, probe, length, samples, found)
        break
    if not found.seconds and found.bitrate:
        # Constant bitrate: the length is arithmetic on what is left of the
        # file. Said as an estimate rather than as a fact, because a file with
        # a variable bitrate and no header to say so will be wrong.
        found.seconds = max(0.0, (len(data) - at) * 8 / found.bitrate)
        found.notes.append("Its length is worked out from the bitrate.")
    return found


def _xing(data: bytes, frame: int, length: int, samples: int,
          found: Sound) -> None:
    """The header a variable-bitrate encoder leaves in its first frame."""
    window = data[frame:frame + length + 4]
    for marker in (b"Xing", b"Info"):
        at = window.find(marker)
        if at < 0:
            continue
        flags = struct.unpack_from(">I", window, at + 4)[0]
        cursor = at + 8
        if flags & 1 and cursor + 4 <= len(window):
            frames = struct.unpack_from(">I", window, cursor)[0]
            if found.rate and frames:
                found.seconds = samples * frames / found.rate
        return


# -- FLAC ------------------------------------------------------------------


def _flac(data: bytes) -> Sound:
    found = Sound("FLAC")
    at = 4
    while at + 4 <= len(data):
        header = data[at]
        last = header & 0x80
        kind = header & 0x7F
        length = int.from_bytes(data[at + 1:at + 4], "big")
        body = data[at + 4:at + 4 + length]
        if kind == 0 and len(body) >= 18:  # STREAMINFO
            packed = int.from_bytes(body[10:18], "big")
            found.rate = packed >> 44
            found.channels = ((packed >> 41) & 7) + 1
            found.bits = ((packed >> 36) & 31) + 1
            samples = packed & ((1 << 36) - 1)
            if found.rate:
                found.seconds = samples / found.rate
        elif kind == 4:  # VORBIS_COMMENT
            _vorbis(body, found)
        elif kind == 6 and len(body) > 32:  # PICTURE
            _flac_picture(body, found)
        at += 4 + length
        if last:
            break
    if found.seconds and len(data):
        found.bitrate = int(len(data) * 8 / found.seconds)
    return found


def _vorbis(body: bytes, found: Sound) -> None:
    if len(body) < 4:
        return
    length = struct.unpack_from("<I", body, 0)[0]
    at = 4 + length
    if at + 4 > len(body):
        return
    count = struct.unpack_from("<I", body, at)[0]
    at += 4
    for _ in range(min(count, 512)):
        if at + 4 > len(body):
            return
        size = struct.unpack_from("<I", body, at)[0]
        at += 4
        if at + size > len(body):
            return
        line = body[at:at + size].decode("utf-8", "replace")
        at += size
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if not value:
            continue
        key = NAMES.get(name.upper())
        if key:
            found.tags.setdefault(key, value)
        else:
            found.extra.setdefault(name.upper(), value)


def _flac_picture(body: bytes, found: Sound) -> None:
    if found.picture:
        return
    at = 4  # the picture's kind, which the panel does not need
    length = struct.unpack_from(">I", body, at)[0]
    at += 4
    mime = body[at:at + length].decode("latin-1", "replace")
    at += length
    if at + 4 > len(body):
        return
    length = struct.unpack_from(">I", body, at)[0]
    at += 4 + length  # the description
    at += 16  # width, height, depth, colours
    if at + 4 > len(body):
        return
    length = struct.unpack_from(">I", body, at)[0]
    at += 4
    if length and at + length <= len(body):
        found.picture = body[at:at + length]
        found.picture_type = mime or "image/jpeg"


# -- MP4 and its atoms ------------------------------------------------------


def _mp4(data: bytes) -> Sound:
    found = Sound("MPEG-4")
    brand = data[8:12]
    if brand[:3] == b"M4A" or brand[:3] == b"M4B":
        found.kind = "M4A"
    moov = _atom(data, b"moov", 0, len(data))
    if moov is None:
        return found
    _from_moov(data, moov[0], moov[1], found)
    if found.seconds:
        found.bitrate = int(len(data) * 8 / found.seconds)
    return found


def read_mp4_moov(atom: bytes, whole: int = 0) -> Sound:
    """The `moov` atom on its own, for a file whose atom sits past the head.

    **Which happens on half the files there are.** An encoder that knows the
    length before it starts writes `moov` first; anything recording as it goes
    cannot, and puts it at the end. The chain of atom lengths says where it is
    even when its bytes have not been read, so the range is fetched exactly
    rather than the whole file being pulled over the pipe.
    """
    found = Sound("M4A")
    box = _atom(atom, b"moov", 0, len(atom))
    if box is None:
        return found
    _from_moov(atom, box[0], box[1], found)
    if found.seconds and whole:
        found.bitrate = int(whole * 8 / found.seconds)
    return found


def moov_at(data: bytes) -> Optional[Tuple[int, int]]:
    """Where the `moov` atom begins and how long it is, from the chain alone."""
    at = 0
    while at + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4:at + 8]
        if length == 1:
            if at + 16 > len(data):
                return None
            length = struct.unpack_from(">Q", data, at + 8)[0]
        if length < 8:
            return None
        if kind == b"moov":
            return at, length
        at += length
    return None


def _from_moov(data: bytes, start: int, end: int, found: Sound) -> None:
    header = _find_atom(data, b"mvhd", start, end)
    if header:
        at, _stop = header
        version = data[at]
        if version == 1 and at + 28 <= len(data):
            scale, duration = struct.unpack_from(">IQ", data, at + 20)
        elif at + 20 <= len(data):
            scale, duration = struct.unpack_from(">II", data, at + 12)
        else:
            scale = duration = 0
        if scale:
            found.seconds = duration / scale
    lossless = _find_atom(data, b"alac", start, end)
    sound_track = lossless or _find_atom(data, b"mp4a", start, end)
    if sound_track:
        at, _stop = sound_track
        # An audio sample entry: six reserved bytes, the data reference, eight
        # more reserved, then the channel count. The rate is 16.16 fixed point
        # and sits four bytes further along than a first reading suggests —
        # there is a `pre_defined` and a `reserved` between it and the size.
        if at + 28 <= len(data):
            channels, size = struct.unpack_from(">HH", data, at + 16)
            found.channels = channels
            found.rate = struct.unpack_from(">I", data, at + 24)[0] >> 16
            # **Only where it means something.** AAC writes 16 into the field
            # and has no bit depth at all; saying "16 bits" of a lossy file is
            # a fact about the box and not about the sound.
            if lossless:
                found.bits = size

    ilst = _find_atom(data, b"ilst", start, end)
    if ilst:
        _ilst(data, ilst[0], ilst[1], found)


def _ilst(data: bytes, start: int, end: int, found: Sound) -> None:
    at = start
    while at + 8 <= end:
        (length,) = struct.unpack_from(">I", data, at)
        if length < 8 or at + length > end:
            return
        name = data[at + 4:at + 8].decode("latin-1", "replace")
        payload = _atom(data, b"data", at + 8, at + length)
        if payload:
            body_at, body_end = payload
            kind = struct.unpack_from(">I", data, body_at)[0] & 0xFFFFFF
            body = data[body_at + 8:body_end]
            _ilst_value(name, kind, body, found)
        at += length


def _ilst_value(name: str, kind: int, body: bytes, found: Sound) -> None:
    if name == "covr":
        if not found.picture and body:
            found.picture = body
            found.picture_type = "image/png" if kind == 14 else "image/jpeg"
        return
    if not body:
        return
    if name in ("trkn", "disk") and len(body) >= 4:
        # A pair: which one, and how many there are.
        number, total = struct.unpack_from(">HH", body, 2)
        text = "%d of %d" % (number, total) if total else str(number)
    elif kind == 1:
        text = body.decode("utf-8", "replace").strip()
    elif kind in (21, 22) and len(body) <= 8:
        text = str(int.from_bytes(body, "big", signed=kind == 21))
    else:
        return
    if not text:
        return
    key = NAMES.get(name)
    if key:
        found.tags.setdefault(key, _genre(text) if key == "genre" else text)
    elif name != "----":
        # `----` is not a name: it is the box a freeform atom keeps its real
        # name in, one level further down, and counting it as a field would be
        # counting the envelope.
        found.extra.setdefault(name.strip("\xa9"), text)


def _atom(data: bytes, want: bytes, start: int, end: int
          ) -> Optional[Tuple[int, int]]:
    """The body of the first [want] among the children of this range."""
    at = start
    while at + 8 <= end:
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4:at + 8]
        body = at + 8
        if length == 1:
            if at + 16 > end:
                return None
            length = struct.unpack_from(">Q", data, at + 8)[0]
            body = at + 16
        if length == 0:
            length = end - at
        if length < 8 or at + length > end:
            return None
        if kind == want:
            return body, at + length
        at += length
    return None


#: Atoms whose children are worth walking into. `meta` is the awkward one: it
#: is a *full* box, so its children start four bytes late, and a walker that
#: forgets reads its version as a length and finds nothing.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta",
               b"stsd", b"mvex", b"edts"}


def _find_atom(data: bytes, want: bytes, start: int, end: int,
               depth: int = 0) -> Optional[Tuple[int, int]]:
    if depth > 8:
        return None
    at = start
    while at + 8 <= end:
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4:at + 8]
        body = at + 8
        if length == 1:
            if at + 16 > end:
                return None
            length = struct.unpack_from(">Q", data, at + 8)[0]
            body = at + 16
        if length == 0:
            length = end - at
        if length < 8 or at + length > end:
            return None
        if kind == want:
            return body, at + length
        if kind in _CONTAINERS:
            inner = _find_atom(data, want, body + (4 if kind == b"meta" else 0),
                               at + length, depth + 1)
            if inner:
                return inner
        # A sample description names its format four bytes in, past the count.
        if kind == b"stsd":
            inner = _find_atom(data, want, body + 8, at + length, depth + 1)
            if inner:
                return inner
        at += length
    return None


# -- RIFF and IFF -----------------------------------------------------------


def _wav(data: bytes) -> Sound:
    found = Sound("WAV")
    at = 12
    end = min(len(data), struct.unpack_from("<I", data, 4)[0] + 8)
    data_bytes = 0
    while at + 8 <= end:
        kind = data[at:at + 4]
        (length,) = struct.unpack_from("<I", data, at + 4)
        body = data[at + 8:at + 8 + length]
        if kind == b"fmt " and len(body) >= 16:
            _format, channels, rate, byte_rate, _align, bits = \
                struct.unpack_from("<HHIIHH", body, 0)
            found.channels, found.rate, found.bits = channels, rate, bits
            found.bitrate = byte_rate * 8
        elif kind == b"data":
            data_bytes = length
        elif kind == b"LIST" and body[:4] == b"INFO":
            _riff_info(body[4:], found)
        elif kind == b"id3 " or kind == b"ID3 ":
            read_id3(body, found)
        at += 8 + length + (length & 1)
    if data_bytes and found.bitrate:
        found.seconds = data_bytes * 8 / found.bitrate
    return found


def _riff_info(body: bytes, found: Sound) -> None:
    at = 0
    while at + 8 <= len(body):
        name = body[at:at + 4].decode("latin-1", "replace")
        (length,) = struct.unpack_from("<I", body, at + 4)
        if at + 8 + length > len(body):
            return
        text = body[at + 8:at + 8 + length].split(b"\0", 1)[0]
        value = text.decode("utf-8", "replace").strip()
        if value:
            key = NAMES.get(name)
            if key:
                found.tags.setdefault(key, value)
            else:
                found.extra.setdefault(name, value)
        at += 8 + length + (length & 1)


def _aiff(data: bytes) -> Sound:
    found = Sound("AIFF")
    at = 12
    end = min(len(data), struct.unpack_from(">I", data, 4)[0] + 8)
    while at + 8 <= end:
        kind = data[at:at + 4]
        (length,) = struct.unpack_from(">I", data, at + 4)
        body = data[at + 8:at + 8 + length]
        if kind == b"COMM" and len(body) >= 18:
            channels, frames, bits = struct.unpack_from(">HIH", body, 0)
            found.channels, found.bits = channels, bits
            found.rate = int(_extended(body[8:18]))
            if found.rate:
                found.seconds = frames / found.rate
                found.bitrate = found.rate * channels * bits
        elif kind in (b"ID3 ", b"id3 "):
            read_id3(body, found)
        elif kind == b"NAME":
            found.tags.setdefault("title", body.decode("latin-1", "replace").strip())
        elif kind == b"AUTH":
            found.tags.setdefault("artist", body.decode("latin-1", "replace").strip())
        at += 8 + length + (length & 1)
    return found


def _extended(raw: bytes) -> float:
    """AIFF writes its sample rate as an 80-bit float, and nothing else does."""
    if len(raw) < 10:
        return 0.0
    exponent = struct.unpack_from(">H", raw, 0)[0]
    mantissa = int.from_bytes(raw[2:10], "big")
    sign = -1 if exponent & 0x8000 else 1
    exponent &= 0x7FFF
    if exponent == 0 and mantissa == 0:
        return 0.0
    if exponent == 0x7FFF:
        return 0.0
    return sign * mantissa * 2.0 ** (exponent - 16383 - 63)


# -- saying it --------------------------------------------------------------


def clock(seconds: float) -> str:
    """`3:47`, and `1:02:15` where it is that long."""
    if seconds <= 0:
        return ""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def size(count: int) -> str:
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if count >= step:
            return "%.1f %s" % (count / step, unit)
    return "%d bytes" % count


def channels(count: int) -> str:
    return {0: "", 1: "mono", 2: "stereo"}.get(count, "%d channels" % count)


__all__ = ["NAMES", "ORDER", "Sound", "channels", "clock", "moov_at", "read",
           "read_id3", "read_mp4_moov", "size"]

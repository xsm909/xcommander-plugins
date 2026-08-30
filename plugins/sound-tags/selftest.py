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

"""Checks the tag readers, on files made up here and on real ones.

    python3 selftest.py                 # the checks that need no file
    python3 selftest.py <folder>        # and every sound file under there too

**The made-up files are built byte by byte in this file**, which is the only
way to know what should come out before it goes in — a fixture written by
somebody else's encoder proves that today's copy of it still reads and nothing
more. What the folder adds is the other half: that real output from a real
encoder is read at all, and read as whatever the system says it is.
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tags  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        FAILURES.append("%s: got %r, wanted %r" % (name, got, want))
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))


# -- building the tags by hand ---------------------------------------------


def synchsafe(value: int) -> bytes:
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F,
                  (value >> 7) & 0x7F, value & 0x7F])


def id3_frame(name: str, payload: bytes, major: int = 3) -> bytes:
    if major == 2:
        return name.encode("latin-1")[:3] + len(payload).to_bytes(3, "big") + payload
    length = synchsafe(len(payload)) if major == 4 else struct.pack(">I", len(payload))
    return name.encode("latin-1")[:4] + length + b"\0\0" + payload


def id3(frames: bytes, major: int = 3, flags: int = 0) -> bytes:
    return (b"ID3" + bytes([major, 0, flags]) + synchsafe(len(frames)) + frames)


def text(value: str, encoding: int = 0) -> bytes:
    if encoding == 0:
        return b"\0" + value.encode("latin-1")
    return b"\x03" + value.encode("utf-8")


def mpeg_frame(bitrate_index: int = 9, rate_index: int = 0) -> bytes:
    """One MPEG 1 Layer III frame header, and enough silence to fill it."""
    header = bytes([
        0xFF,
        0xFB,  # MPEG 1, Layer III, no CRC
        (bitrate_index << 4) | (rate_index << 2),
        0x00,  # stereo
    ])
    bitrate = tags._BITRATES[(True, 1)][bitrate_index] * 1000
    rate = tags._RATES[3][rate_index]
    length = 1152 // 8 * bitrate // rate
    return header + b"\0" * (length - 4)


def check_id3():
    frames = (
        id3_frame("TIT2", text("Sixteen Tons"))
        + id3_frame("TPE1", text("Tennessee Ernie Ford"))
        + id3_frame("TALB", text("Capitol Sings"))
        + id3_frame("TRCK", text("3/12"))
        + id3_frame("TCON", text("(17)"))
        + id3_frame("TYER", text("1955"))
        + id3_frame("WXXX", b"\0http://example.com")
        + id3_frame("COMM", b"\0eng" + b"\0" + b"a note")
    )
    # Two frames' worth of sound, so the frame check has a second one to land on.
    data = id3(frames) + mpeg_frame() * 2
    found = tags.read(data)

    check("the title", found.tags.get("title"), "Sixteen Tons")
    check("the artist", found.tags.get("artist"), "Tennessee Ernie Ford")
    check("the album", found.tags.get("album"), "Capitol Sings")
    check("the track", found.tags.get("track"), "3/12")
    check("a numbered genre in words", found.tags.get("genre"), "Rock")
    check("the year", found.tags.get("year"), "1955")
    check("a comment past its language", found.tags.get("comment"), "a note")
    check("and what has no name here is counted", "WXXX" in found.extra, True)
    check("the rate off the frame", found.rate, 44100)
    check("stereo", found.channels, 2)
    check("the bitrate", found.bitrate, 128000)


def check_id3_encodings():
    frames = id3_frame("TIT2", b"\x01\xff\xfe" + "Кино".encode("utf-16-le"))
    found = tags.read(id3(frames) + mpeg_frame() * 2)
    check("utf-16 with a mark", found.tags.get("title"), "Кино")

    frames = id3_frame("TIT2", text("Café", encoding=3), major=4)
    found = tags.read(id3(frames, major=4) + mpeg_frame() * 2)
    check("utf-8 in a v2.4 tag", found.tags.get("title"), "Café")

    frames = id3_frame("TT2", text("Older"), major=2)
    found = tags.read(id3(frames, major=2) + mpeg_frame() * 2)
    check("a three-letter frame in v2.2", found.tags.get("title"), "Older")


def check_unsynchronisation():
    """A writer that set the flag scattered a zero after every 0xFF.

    Built the way a writer builds one, which is the part that matters: the
    frame's declared length is what it will be **once the zeros are taken back
    out**, and the bytes in the file are the escaped ones. A reader that
    unescapes per frame instead of over the whole tag is off by however many
    there were, and reads the rest as nonsense rather than as an error.
    """
    frames = (id3_frame("TIT2", text("AB"))
              + id3_frame("TPE1", b"\0" + b"\xff" + b"tail"))
    escaped = frames.replace(b"\xff", b"\xff\x00")
    tag = b"ID3" + bytes([3, 0, 0x80]) + synchsafe(len(escaped)) + escaped
    found = tags.read(tag + mpeg_frame() * 2)
    check("the tag is unescaped as a whole", found.tags.get("title"), "AB")
    check("and the escaped byte comes back",
          found.tags.get("artist"), "ÿtail")


def check_id3_picture():
    cover = b"\xff\xd8\xff\xe0" + b"not really a jpeg"
    payload = (b"\0" + b"image/jpeg\0" + b"\x03" + b"front\0" + cover)
    frames = id3_frame("APIC", payload) + id3_frame("TIT2", text("With a sleeve"))
    found = tags.read(id3(frames) + mpeg_frame() * 2)
    check("the cover comes out whole", found.picture, cover)
    check("and says what it is", found.picture_type, "image/jpeg")
    check("and the tags beside it still read",
          found.tags.get("title"), "With a sleeve")


def check_mpeg_headers():
    """The two fields everybody gets backwards."""
    # MPEG 1 Layer III at 48 kHz and 256 kbit/s — the answer the system's own
    # `afinfo` gives for the file this was written against. Bitrate index 13,
    # rate index 1.
    frame = tags._mpeg_frame_at(bytes([0xFF, 0xFB, 0xD4, 0x00]) + b"\0" * 900, 0)
    check("MPEG 1 rate", frame[0], 48000)
    check("MPEG 1 bitrate", frame[2], 256000)

    # Version 1 of the version field is reserved, and is not a frame.
    # MPEG 2 at 24 kHz and 40 kbit/s — the same two indices as above mean
    # different numbers here, which is exactly the trap: bitrate index 5 is
    # 64 kbit/s on MPEG 1 and 40 on MPEG 2, and rate index 1 is 48 kHz there
    # and 24 kHz here.
    frame = tags._mpeg_frame_at(bytes([0xFF, 0xF3, 0x54, 0x00]) + b"\0" * 900, 0)
    check("MPEG 2 rate", frame[0], 24000)
    check("MPEG 2 bitrate", frame[2], 40000)
    check("a reserved version is not a frame",
          tags._mpeg_frame_at(bytes([0xFF, 0xEB, 0xD4, 0x00]) + b"\0" * 900, 0),
          None)
    check("a reserved layer is not a frame",
          tags._mpeg_frame_at(bytes([0xFF, 0xF9, 0xD4, 0x00]) + b"\0" * 900, 0),
          None)
    check("nor is a free bitrate",
          tags._mpeg_frame_at(bytes([0xFF, 0xFB, 0x04, 0x00]) + b"\0" * 900, 0),
          None)


# -- FLAC -------------------------------------------------------------------


def flac_block(kind: int, body: bytes, last: bool = False) -> bytes:
    return bytes([kind | (0x80 if last else 0)]) + len(body).to_bytes(3, "big") + body


def check_flac():
    # STREAMINFO: the rate, channels and depth are packed across a 64-bit word
    # with the sample count, which is the one place a bit shift decides whether
    # a file reads as itself or as noise.
    rate, channels, bits, samples = 48000, 2, 24, 48000 * 15
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | samples
    streaminfo = b"\0" * 10 + packed.to_bytes(8, "big") + b"\0" * 16

    vendor = b"a made-up encoder"
    comments = [b"TITLE=Sixteen Tons", b"ARTIST=Tennessee Ernie Ford",
                b"REPLAYGAIN_TRACK_GAIN=-3.2 dB"]
    body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    for comment in comments:
        body += struct.pack("<I", len(comment)) + comment

    cover = b"\x89PNG\r\n\x1a\n" + b"a sleeve"
    mime = b"image/png"
    picture = (struct.pack(">I", 3) + struct.pack(">I", len(mime)) + mime
               + struct.pack(">I", 0) + b"\0" * 16
               + struct.pack(">I", len(cover)) + cover)

    data = (b"fLaC" + flac_block(0, streaminfo) + flac_block(4, body)
            + flac_block(6, picture, last=True) + b"\0" * 64)
    found = tags.read(data)

    check("FLAC", found.kind, "FLAC")
    check("its rate", found.rate, rate)
    check("its channels", found.channels, channels)
    check("its depth", found.bits, bits)
    check("its length", tags.clock(found.seconds), "0:15")
    check("its title", found.tags.get("title"), "Sixteen Tons")
    check("its artist", found.tags.get("artist"), "Tennessee Ernie Ford")
    check("a comment nobody named is counted",
          "REPLAYGAIN_TRACK_GAIN" in found.extra, True)
    check("its cover", found.picture, cover)
    check("and what kind of cover", found.picture_type, "image/png")


# -- MP4 --------------------------------------------------------------------


def atom(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body) + 8) + kind + body


def ilst_item(name: bytes, body: bytes, kind: int = 1) -> bytes:
    return atom(name, atom(b"data", struct.pack(">II", kind, 0) + body))


def m4a(moov_first: bool = True) -> bytes:
    mvhd = atom(b"mvhd", b"\0\0\0\0" + b"\0" * 8
                + struct.pack(">II", 600, 600 * 15) + b"\0" * 80)
    sample = (b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 8
              + struct.pack(">HHHH", 2, 16, 0, 0)
              + struct.pack(">I", 44100 << 16))
    stsd = atom(b"stsd", struct.pack(">II", 0, 1) + atom(b"mp4a", sample))
    stbl = atom(b"stbl", stsd)
    minf = atom(b"minf", stbl)
    mdia = atom(b"mdia", minf)
    trak = atom(b"trak", mdia)
    cover = b"\xff\xd8\xff" + b"a sleeve"
    ilst = atom(b"ilst",
                ilst_item("©nam".encode("latin-1"), b"Sixteen Tons")
                + ilst_item("©ART".encode("latin-1"), b"Tennessee Ernie Ford")
                + ilst_item(b"trkn", struct.pack(">HHH", 0, 3, 12), kind=0)
                + ilst_item(b"covr", cover, kind=13)
                + ilst_item(b"desc", b"something else")
                + ilst_item(b"----", b"a freeform one"))
    meta = atom(b"meta", b"\0\0\0\0" + atom(b"hdlr", b"\0" * 24) + ilst)
    udta = atom(b"udta", meta)
    moov = atom(b"moov", mvhd + trak + udta)
    mdat = atom(b"mdat", b"\0" * 4096)
    head = atom(b"ftyp", b"M4A \0\0\0\0M4A mp42isom")
    return head + (moov + mdat if moov_first else mdat + moov), cover


def check_mp4():
    data, cover = m4a()
    found = tags.read(data)
    check("M4A", found.kind, "M4A")
    check("its length", tags.clock(found.seconds), "0:15")
    check("its rate", found.rate, 44100)
    check("its channels", found.channels, 2)
    check("a lossy file claims no bit depth", found.bits, 0)
    check("its title", found.tags.get("title"), "Sixteen Tons")
    check("its artist", found.tags.get("artist"), "Tennessee Ernie Ford")
    check("a track of a set", found.tags.get("track"), "3 of 12")
    check("its cover", found.picture, cover)
    check("an atom nobody has a word for is kept as it stands",
          found.extra.get("desc"), "something else")
    # `----` is not a name: it is the box a freeform atom keeps its real name
    # in, one level further down. Showing the envelope would be showing the
    # wrong thing rather than showing more.
    check("and the freeform envelope is not one of them",
          "----" in found.extra, False)

    # The same file with its atoms after the sound, which is what anything
    # recording as it goes has to write.
    late, _cover = m4a(moov_first=False)
    where = tags.moov_at(late)
    check("the chain says where the atoms are", where is not None, True)
    offset, length = where
    check("and reading only that range is enough",
          tags.read_mp4_moov(late[offset:offset + length]).tags.get("title"),
          "Sixteen Tons")


# -- RIFF and IFF -----------------------------------------------------------


def check_wav():
    fmt = struct.pack("<HHIIHH", 1, 2, 44100, 44100 * 4, 4, 16)
    info = b"INFO"
    for name, value in ((b"INAM", b"Sixteen Tons\0"), (b"IART", b"Ford\0"),
                        (b"IKEY", b"a word nobody named\0")):
        info += name + struct.pack("<I", len(value)) + value
        if len(value) & 1:
            info += b"\0"
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"LIST" + struct.pack("<I", len(info)) + info
            + b"data" + struct.pack("<I", 44100 * 4 * 15) + b"\0" * 32)
    data = b"RIFF" + struct.pack("<I", len(body)) + body
    found = tags.read(data)
    check("WAV", found.kind, "WAV")
    check("its rate", found.rate, 44100)
    check("its depth", found.bits, 16)
    check("its length from the data chunk", tags.clock(found.seconds), "0:15")
    check("its title", found.tags.get("title"), "Sixteen Tons")
    check("a code nobody named is counted", "IKEY" in found.extra, True)


def check_aiff():
    # 44100 as the 80-bit float AIFF and nothing else uses.
    rate = b"\x40\x0e\xac\x44" + b"\0" * 6
    comm = struct.pack(">HIH", 2, 44100 * 15, 16) + rate
    frames = id3_frame("TIT2", text("Sixteen Tons"))
    body = (b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm
            + b"ID3 " + struct.pack(">I", len(id3(frames))) + id3(frames))
    data = b"FORM" + struct.pack(">I", len(body)) + body
    found = tags.read(data)
    check("AIFF", found.kind, "AIFF")
    check("an 80-bit rate", found.rate, 44100)
    check("its channels", found.channels, 2)
    check("its length", tags.clock(found.seconds), "0:15")
    check("an ID3 chunk inside an AIFF", found.tags.get("title"), "Sixteen Tons")


def check_sayings():
    check("under an hour", tags.clock(227), "3:47")
    check("over one", tags.clock(3735), "1:02:15")
    check("no length at all", tags.clock(0), "")
    check("one channel", tags.channels(1), "mono")
    check("six", tags.channels(6), "6 channels")
    check("a size", tags.size(1536), "1.5 KB")


# -- and whatever is really on the disk --------------------------------------


def check_files(folder: str) -> None:
    kinds = (".mp3", ".m4a", ".m4b", ".flac", ".wav", ".aif", ".aiff", ".aac")
    paths = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if name.lower().endswith(kinds):
                paths.append(os.path.join(root, name))
    for path in sorted(paths):
        try:
            with open(path, "rb") as handle:
                found = tags.read(handle.read(4 << 20))
        except Exception as failure:  # noqa: BLE001
            FAILURES.append("%s: %s" % (path, failure))
            print("  FAIL %s: %s" % (os.path.basename(path), failure))
            continue
        print("  %-28s %-6s %-7s %6s Hz %-9s %s"
              % (os.path.basename(path)[:28], found.kind or "?",
                 tags.clock(found.seconds), found.rate or "?",
                 tags.channels(found.channels),
                 ", ".join("%s=%s" % (k, v) for k, v in
                           list(found.tags.items())[:2]) or "no tags"))
        if not found.kind:
            FAILURES.append("%s: nothing recognised it" % path)


def main() -> int:
    print("ID3");                 check_id3()
    print("its encodings");       check_id3_encodings()
    print("unsynchronisation");   check_unsynchronisation()
    print("a cover in an ID3");   check_id3_picture()
    print("MPEG frame headers");  check_mpeg_headers()
    print("FLAC");                check_flac()
    print("MP4");                 check_mp4()
    print("WAV");                 check_wav()
    print("AIFF");                check_aiff()
    print("saying it");           check_sayings()
    for folder in sys.argv[1:]:
        print("files under %s" % folder)
        check_files(folder)
    if FAILURES:
        print("\n%d failure(s):" % len(FAILURES))
        for failure in FAILURES:
            print("  " + failure)
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

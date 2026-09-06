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

"""A name written before anybody agreed which code page to use.

Both formats have the problem and neither records the answer. A ZIP from before
UTF-8 says its names are code page 437 and means whatever the machine that wrote
them was set to; a tar says nothing at all and holds bytes. The archivers that
shipped in Russia wrote code page 866, Windows tools wrote 1251, and read the
wrong way round a file cannot be found again — which is not a cosmetic matter.

What is shared is the *deciding*, so it lives here and each format brings its own
bytes. Nothing is guessed for a name that says what it is.

**And the machine gets a say.** The readings tried used to be a fixed three,
written for archives made in Russia, so a German or Polish Windows never had its
own page among them at all. `Auto` now tries this machine's page as well and
prefers it on a tie, and `System` is that page outright for when the guessing
gets it wrong — which it will, because an archive carries the code page of the
machine that *wrote* it and only a person looking at the names can settle that.
"""

from __future__ import annotations

import locale
import sys
from typing import Optional, Tuple

#: How a name with nothing declaring its encoding should be read.
AUTO = "auto"
OEM = "oem"
WINDOWS = "windows"
LITERAL = "literal"
SYSTEM = "system"

#: The readings worth trying wherever the machine's own is not one of them.
#:
#: UTF-8 is in the list and is first because plenty of archivers wrote UTF-8
#: names without setting the flag that says so — the `zip` on every Linux box
#: does — and because a run of code-page bytes almost never happens to be valid
#: UTF-8, so a strict decode that succeeds is strong evidence on its own.
CANDIDATES = ("utf-8", "cp866", "cp1251")


def system_codec() -> str:
    """What this machine means when nothing says otherwise.

    Asked for on 2026-09-06: *«нужен авто выбор для OS»*. The two lists above
    were written for archives made in Russia — 866 and 1251 — and on a German
    or Polish Windows the page that machine actually writes was never among the
    readings tried at all.

    Windows has an answer and it is the ANSI code page. macOS and Linux have
    one too and it is UTF-8: their file names are bytes, but every tool that
    writes an archive on them writes UTF-8, and `locale` on a machine with no
    locale set says ASCII, which would be a worse guess than the truth.
    """
    if sys.platform != "win32":
        return "utf-8"
    try:
        page = locale.getpreferredencoding(False)
    except Exception:
        return "cp1252"
    return page.lower() if page else "cp1252"


def candidates() -> Tuple[str, ...]:
    """The readings to try, this machine's own first after UTF-8.

    **First after UTF-8 rather than first outright**, because a strict UTF-8
    decode that succeeds is evidence and a code-page decode never fails: cp1251
    reads any bytes at all. Order settles ties, and on a tie the machine's own
    page is the likelier answer — which is the whole of what "auto for the OS"
    can honestly mean.
    """
    mine = system_codec()
    order = ["utf-8"]
    if mine not in order:
        order.append(mine)
    for codec in CANDIDATES:
        if codec not in order:
            order.append(codec)
    return tuple(order)


def plausibility(text: str) -> int:
    """How much this reads like a file name somebody typed.

    A wrongly decoded name is not gibberish in any obvious way — 866 and 1251
    turn the same bytes into Cyrillic, just into *different* Cyrillic. What
    separates them is the bytes either side of the alphabet: 866 puts
    box-drawing characters there and 1251 puts currency signs and stray
    letters, and a name holding either is a name read the wrong way round. So
    this counts evidence rather than guessing.
    """
    score = 0
    for character in text:
        code = ord(character)
        if character.isalnum() or character in " ._-+()[]{}#@!,;'&~/":
            score += 2
        elif 0x2500 <= code <= 0x259F:  # box drawing and blocks
            score -= 4
        elif 0x00A0 <= code <= 0x00BF or code in (0x00A4, 0x00A6, 0x00A7):
            score -= 3  # currency and section marks: 1251 reading 866's letters
        elif 0xD800 <= code <= 0xDFFF:
            score -= 8  # a surrogate: bytes that are not text in any reading
        elif code < 0x20:
            score -= 8  # a control character is never in a name
        else:
            # Anything else at all — a dagger, a per-mille sign, a smart quote.
            # **This has to cost, not merely fail to pay.** UTF-8 Cyrillic read
            # as 1251 comes out as letters with such marks sprinkled between
            # them, and while they were worth nothing the wrong reading won on
            # length alone: fourteen mostly-letters beat nine all-letters, so a
            # perfectly good `Отчёт.txt` was "repaired" into mojibake.
            score -= 3
    return score


def repaired(raw: bytes, choice: str, fallback: str) -> str:
    """The name those bytes were meant to be.

    ``fallback`` is what the format's own decoding produced, and is what comes
    back when it is the most plausible reading or when the choice is to leave
    names alone.
    """
    if choice == LITERAL:
        return fallback
    if choice == OEM:
        return _decode(raw, "cp866", fallback)
    if choice == WINDOWS:
        return _decode(raw, "cp1251", fallback)
    if choice == SYSTEM:
        return _decode(raw, system_codec(), fallback)

    best, score = fallback, plausibility(fallback)
    for codec in candidates():
        candidate = _decode(raw, codec, None)
        if candidate is None or candidate == fallback:
            continue
        rating = plausibility(candidate)
        if rating > score:
            best, score = candidate, rating
    return best


def _decode(raw: bytes, codec: str, fallback: Optional[str]) -> Optional[str]:
    try:
        return raw.decode(codec)
    except UnicodeDecodeError:
        return fallback

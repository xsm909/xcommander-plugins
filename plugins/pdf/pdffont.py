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

"""Bytes in a content stream, and what letters they stand for.

**No font program is ever loaded.** The glyphs are not needed — only the
characters they stand for — and every route to those is a table in the file:
`/ToUnicode`, a named encoding, `/Differences`, and the glyph names themselves.
That is what keeps this viewer free of a font engine.

Four traps, all of them paid for once already and all of them here:

- **`/MacRomanEncoding` is common and must really be decoded.** Python has the
  `mac_roman` codec, and without it a curly quote comes out as `ÒJhon WickÓ`.
- **A subset font can spell anything as anything.** In one measured file the
  letter "a" is the byte `!`, and only `/ToUnicode` or `/ActualText` says so.
- **A glyph name is a third road**: `/Differences` gives names, and `uni0416`,
  `afii10017` and `Agrave` all resolve without opening a font.
- **Widths matter.** They are what says where a word ends, and a viewer that
  guesses them at half an em puts the columns of a table in the wrong places.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import pdfdoc

# -- encodings -------------------------------------------------------------

#: The three named encodings, as code → character. Built from Python's own
#: codecs where one exists, so there is no 256-entry table to get wrong.
def _codec_table(codec: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for code in range(32, 256):
        try:
            char = bytes([code]).decode(codec)
        except UnicodeDecodeError:
            continue
        if char and char != "�":
            out[code] = char
    return out


WIN_ANSI = _codec_table("cp1252")
MAC_ROMAN = _codec_table("mac_roman")

#: `StandardEncoding` where it differs from Latin-1. Everything below 127 is
#: ASCII apart from the quotes, and the upper half is punctuation and accents.
STANDARD: Dict[int, str] = dict((c, chr(c)) for c in range(32, 127))
STANDARD.update({
    0x27: "’", 0x60: "‘",
    0xA1: "¡", 0xA2: "¢", 0xA3: "£", 0xA4: "⁄", 0xA5: "¥", 0xA6: "ƒ",
    0xA7: "§", 0xA8: "¤", 0xA9: "'", 0xAA: "“", 0xAB: "«", 0xAC: "‹",
    0xAD: "›", 0xAE: "ﬁ", 0xAF: "ﬂ", 0xB1: "–", 0xB2: "†", 0xB3: "‡",
    0xB4: "·", 0xB6: "¶", 0xB7: "•", 0xB8: "‚", 0xB9: "„", 0xBA: "”",
    0xBB: "»", 0xBC: "…", 0xBD: "‰", 0xBF: "¿", 0xC1: "`", 0xC2: "´",
    0xC3: "ˆ", 0xC4: "˜", 0xC5: "¯", 0xC6: "˘", 0xC7: "˙", 0xC8: "¨",
    0xCA: "˚", 0xCB: "¸", 0xCD: "˝", 0xCE: "˛", 0xCF: "ˇ", 0xD0: "—",
    0xE1: "Æ", 0xE3: "ª", 0xE8: "Ł", 0xE9: "Ø", 0xEA: "Œ", 0xEB: "º",
    0xF1: "æ", 0xF5: "ı", 0xF8: "ł", 0xF9: "ø", 0xFA: "œ", 0xFB: "ß",
})

#: `PDFDocEncoding` is only ever met in metadata, which this viewer reads
#: through :func:`pdfdoc.text_of`; the drawing encodings are the three above.

BASE_ENCODINGS = {
    "WinAnsiEncoding": WIN_ANSI,
    "MacRomanEncoding": MAC_ROMAN,
    "StandardEncoding": STANDARD,
    "MacExpertEncoding": STANDARD,  # an expert set has no unicode of its own
    "PDFDocEncoding": WIN_ANSI,
}

#: Glyph names worth naming, for the `/Differences` road. The Adobe glyph list
#: has four thousand entries and almost all of them are `uniXXXX` in disguise;
#: what is left, and what is here, are the names a real file actually uses.
_GLYPHS: Dict[str, str] = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]",
    "asciicircum": "^", "underscore": "_", "grave": "`",
    "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~",
    "quoteleft": "‘", "quoteright": "’",
    "quotedblleft": "“", "quotedblright": "”",
    "quotesinglbase": "‚", "quotedblbase": "„",
    "guilsinglleft": "‹", "guilsinglright": "›",
    "guillemotleft": "«", "guillemotright": "»",
    "endash": "–", "emdash": "—", "bullet": "•",
    "ellipsis": "…", "dagger": "†", "daggerdbl": "‡",
    "perthousand": "‰", "fraction": "⁄", "florin": "ƒ",
    "fi": "ﬁ", "fl": "ﬂ", "ff": "ﬀ", "ffi": "ﬃ",
    "ffl": "ﬄ", "germandbls": "ß", "dotlessi": "ı",
    "currency": "¤", "cent": "¢", "sterling": "£", "yen": "¥",
    "section": "§", "paragraph": "¶", "copyright": "©", "registered": "®",
    "trademark": "™", "degree": "°", "plusminus": "±", "multiply": "×",
    "divide": "÷", "minus": "−", "logicalnot": "¬", "mu": "µ",
    "onequarter": "¼", "onehalf": "½", "threequarters": "¾",
    "onesuperior": "¹", "twosuperior": "²", "threesuperior": "³",
    "ordfeminine": "ª", "ordmasculine": "º", "exclamdown": "¡",
    "questiondown": "¿", "brokenbar": "¦", "dieresis": "¨", "macron": "¯",
    "acute": "´", "cedilla": "¸", "circumflex": "ˆ", "caron": "ˇ",
    "breve": "˘", "dotaccent": "˙", "ring": "˚", "ogonek": "˛",
    "tilde": "˜", "hungarumlaut": "˝", "nbspace": " ",
    "AE": "Æ", "ae": "æ", "OE": "Œ", "oe": "œ", "Oslash": "Ø",
    "oslash": "ø", "Lslash": "Ł", "lslash": "ł", "Eth": "Ð", "eth": "ð",
    "Thorn": "Þ", "thorn": "þ", "Euro": "€", "euro": "€",
}
# The accented Latin letters, which are all `<letter><accent>` by name.
for _letter in "AEIOUYaeiouy":
    for _accent, _combining in (("acute", "́"), ("grave", "̀"),
                                ("circumflex", "̂"), ("dieresis", "̈"),
                                ("tilde", "̃"), ("ring", "̊"),
                                ("macron", "̄"), ("caron", "̌"),
                                ("breve", "̆"), ("cedilla", "̧"),
                                ("ogonek", "̨"), ("hungarumlaut", "̋"),
                                ("dotaccent", "̇")):
        _composed = unicodedata.normalize("NFC", _letter + _combining)
        if len(_composed) == 1:
            _GLYPHS.setdefault(_letter + _accent, _composed)
for _letter in "CcNnSsZzGgKkLlRrTtDdWw":
    for _accent, _combining in (("acute", "́"), ("caron", "̌"),
                                ("cedilla", "̧"), ("dotaccent", "̇"),
                                ("circumflex", "̂"), ("commaaccent", "̦")):
        _composed = unicodedata.normalize("NFC", _letter + _combining)
        if len(_composed) == 1:
            _GLYPHS.setdefault(_letter + _accent, _composed)

_UNI = re.compile(r"^uni([0-9A-Fa-f]{4,6})$")
_U = re.compile(r"^u([0-9A-Fa-f]{4,6})$")
_CID_NAME = re.compile(r"^(?:cid|g|glyph|index)(\d+)$")
#: `afii10017` and its kin: the Cyrillic and Greek names an older tool writes.
_AFII = {
    10017: "А", 10018: "Б", 10019: "В", 10020: "Г", 10021: "Д", 10022: "Е",
    10024: "Ж", 10025: "З", 10026: "И", 10027: "Й", 10028: "К", 10029: "Л",
    10030: "М", 10031: "Н", 10032: "О", 10033: "П", 10034: "Р", 10035: "С",
    10036: "Т", 10037: "У", 10038: "Ф", 10039: "Х", 10040: "Ц", 10041: "Ч",
    10042: "Ш", 10043: "Щ", 10044: "Ъ", 10045: "Ы", 10046: "Ь", 10047: "Э",
    10048: "Ю", 10049: "Я", 10065: "а", 10066: "б", 10067: "в", 10068: "г",
    10069: "д", 10070: "е", 10072: "ж", 10073: "з", 10074: "и", 10075: "й",
    10076: "к", 10077: "л", 10078: "м", 10079: "н", 10080: "о", 10081: "п",
    10082: "р", 10083: "с", 10084: "т", 10085: "у", 10086: "ф", 10087: "х",
    10088: "ц", 10089: "ч", 10090: "ш", 10091: "щ", 10092: "ъ", 10093: "ы",
    10094: "ь", 10095: "э", 10096: "ю", 10097: "я", 10023: "Ё", 10071: "ё",
}


def glyph_to_char(name: str) -> str:
    """A glyph name as the character it stands for, or the empty string."""
    if not name:
        return ""
    if name in _GLYPHS:
        return _GLYPHS[name]
    base = name.split(".")[0]  # `a.sc`, `one.oldstyle`
    if base != name and base in _GLYPHS:
        return _GLYPHS[base]
    m = _UNI.match(base) or _U.match(base)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return ""
    if base.startswith("afii"):
        try:
            return _AFII.get(int(base[4:]), "")
        except ValueError:
            return ""
    if len(base) == 1:
        return base
    return ""


# -- CMaps -----------------------------------------------------------------


def parse_tounicode(data: bytes) -> Dict[int, str]:
    """`/ToUnicode` as code → text. The one table that always tells the truth."""
    table: Dict[int, str] = {}
    for m in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        pairs = re.findall(rb"<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|/[^\s/<\[]+)", m.group(1))
        for src, dst in pairs:
            try:
                code = int(src, 16)
            except ValueError:
                continue
            table[code] = _cmap_value(dst)
    for m in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        body = m.group(1)
        for lo, hi, dst in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|/[^\s/<\[]+)", body
        ):
            try:
                first, last = int(lo, 16), int(hi, 16)
            except ValueError:
                continue
            text = _cmap_value(dst)
            if not text:
                continue
            # A range names its first character and counts up from there.
            start = ord(text[-1])
            prefix = text[:-1]
            for i in range(min(last - first + 1, 65536)):
                table[first + i] = prefix + chr(start + i)
        for lo, hi, items in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", body, re.S
        ):
            try:
                first = int(lo, 16)
            except ValueError:
                continue
            for i, dst in enumerate(re.findall(rb"<[0-9A-Fa-f]*>|/[^\s/<\]]+", items)):
                table[first + i] = _cmap_value(dst)
    return table


def _cmap_value(token: bytes) -> str:
    if token.startswith(b"/"):
        return glyph_to_char(token[1:].decode("latin-1"))
    digits = token.strip(b"<>")
    if len(digits) % 2:
        digits += b"0"
    try:
        raw = bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return ""
    return raw.decode("utf-16-be", "ignore")


def codespace_widths(data: bytes) -> List[int]:
    """How many bytes a code is, according to an embedded CMap."""
    widths = set()
    for m in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        for low, _high in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", m.group(1)):
            widths.add(max(1, len(low) // 2))
    return sorted(widths)


# -- one font --------------------------------------------------------------


class Font:
    """One `/Font` resource: what its codes mean and how wide they are."""

    def __init__(self, doc: pdfdoc.Document, d: Dict[str, Any]):
        self.doc = doc
        self.dict = d
        self.subtype = str(doc.resolve(d.get("Subtype")) or "")
        self.base_font = str(doc.resolve(d.get("BaseFont")) or "")
        self.two_byte = False
        self.to_unicode: Dict[int, str] = {}
        self.encoding: Dict[int, str] = {}
        self.widths: Dict[int, float] = {}
        self.default_width = 500.0
        self.type3_matrix: Optional[List[float]] = None

        self._read_tounicode()
        if self.subtype == "Type0":
            self._read_type0()
        else:
            self._read_simple()
        self._read_style()

    # -- what the bytes mean ----------------------------------------------

    def _read_tounicode(self) -> None:
        stream = self.doc.resolve(self.dict.get("ToUnicode"))
        if isinstance(stream, pdfdoc.Stream):
            try:
                self.to_unicode = parse_tounicode(stream.data)
            except Exception:  # noqa: BLE001 - a damaged table is no table
                self.to_unicode = {}

    def _read_simple(self) -> None:
        doc = self.doc
        encoding = doc.resolve(self.dict.get("Encoding"))
        base = dict(STANDARD)
        symbolic = self._symbolic()
        if symbolic and not isinstance(encoding, (str, dict)):
            base = {}
        if isinstance(encoding, pdfdoc.Name):
            base = dict(BASE_ENCODINGS.get(str(encoding), STANDARD))
        elif isinstance(encoding, dict):
            named = doc.resolve(encoding.get("BaseEncoding"))
            if isinstance(named, pdfdoc.Name):
                base = dict(BASE_ENCODINGS.get(str(named), STANDARD))
            elif symbolic:
                base = dict(STANDARD)
            differences = doc.resolve(encoding.get("Differences"))
            if isinstance(differences, list):
                code = 0
                for item in differences:
                    item = doc.resolve(item)
                    if isinstance(item, (int, float)) and not isinstance(item, bool):
                        code = int(item)
                    elif isinstance(item, pdfdoc.Name):
                        char = glyph_to_char(str(item))
                        if char:
                            base[code] = char
                        else:
                            m = _CID_NAME.match(str(item))
                            if m:
                                base.pop(code, None)
                        code += 1
        self.encoding = base

        first = doc.resolve(self.dict.get("FirstChar"))
        widths = doc.resolve(self.dict.get("Widths"))
        if isinstance(widths, list) and isinstance(first, (int, float)):
            for i, w in enumerate(widths):
                w = doc.resolve(w)
                if isinstance(w, (int, float)) and not isinstance(w, bool):
                    self.widths[int(first) + i] = float(w)
        descriptor = doc.dict_of(self.dict.get("FontDescriptor"))
        missing = doc.resolve(descriptor.get("MissingWidth"))
        if isinstance(missing, (int, float)):
            self.default_width = float(missing)
        elif self.widths:
            self.default_width = 0.0
        if self.subtype == "Type3":
            matrix = pdfdoc.numbers(doc, self.dict.get("FontMatrix"))
            if len(matrix) == 6:
                self.type3_matrix = matrix

    def _symbolic(self) -> bool:
        descriptor = self.doc.dict_of(self.dict.get("FontDescriptor"))
        flags = self.doc.resolve(descriptor.get("Flags"))
        return bool(isinstance(flags, (int, float)) and int(flags) & 4)

    def _read_type0(self) -> None:
        doc = self.doc
        encoding = doc.resolve(self.dict.get("Encoding"))
        self.two_byte = True
        if isinstance(encoding, pdfdoc.Stream):
            widths = codespace_widths(encoding.data)
            self.two_byte = not widths or 2 in widths or max(widths) > 1
        descendants = doc.resolve(self.dict.get("DescendantFonts"))
        child = doc.dict_of(descendants[0]) if isinstance(descendants, list) and descendants else {}
        default = doc.resolve(child.get("DW"))
        self.default_width = float(default) if isinstance(default, (int, float)) else 1000.0
        w = doc.resolve(child.get("W"))
        if isinstance(w, list):
            self._read_cid_widths([doc.resolve(x) for x in w])
        if not self.to_unicode:
            # Identity-H with no /ToUnicode: the codes are glyph indices and
            # nothing in the file says what they mean. Said out loud rather
            # than guessed at — see `Font.readable`.
            pass

    def _read_cid_widths(self, w: List[Any]) -> None:
        i = 0
        while i < len(w):
            first = w[i]
            if not isinstance(first, (int, float)):
                break
            if i + 1 < len(w) and isinstance(w[i + 1], list):
                for j, width in enumerate(self.doc.resolve(w[i + 1])):
                    width = self.doc.resolve(width)
                    if isinstance(width, (int, float)):
                        self.widths[int(first) + j] = float(width)
                i += 2
            elif i + 2 < len(w):
                last, width = w[i + 1], w[i + 2]
                if isinstance(last, (int, float)) and isinstance(width, (int, float)):
                    span = int(last) - int(first)
                    if 0 <= span < 65536:
                        for code in range(int(first), int(last) + 1):
                            self.widths[code] = float(width)
                i += 3
            else:
                break

    def _read_style(self) -> None:
        """Bold and italic, from the descriptor or from the name."""
        descriptor = self.doc.dict_of(self.dict.get("FontDescriptor"))
        flags = self.doc.resolve(descriptor.get("Flags"))
        flags = int(flags) if isinstance(flags, (int, float)) else 0
        weight = self.doc.resolve(descriptor.get("StemV"))
        name = self.base_font.lower()
        self.bold = bool(flags & (1 << 18)) or "bold" in name or "black" in name or "heavy" in name
        if not self.bold and isinstance(weight, (int, float)) and weight >= 120:
            self.bold = True
        self.italic = bool(flags & (1 << 6)) or "italic" in name or "oblique" in name
        self.serif = bool(flags & 2)
        self.fixed = bool(flags & 1)

    @property
    def readable(self) -> bool:
        """Whether this font's codes can be turned into characters at all.

        A subset Identity-H font with no `/ToUnicode` cannot: its codes are
        positions in a font program, and the only way back is the very font
        engine this viewer does not have. Saying so is better than printing
        the private-use characters those codes would otherwise become.
        """
        if self.subtype == "Type0":
            return bool(self.to_unicode)
        return bool(self.to_unicode or self.encoding)

    # -- using it ----------------------------------------------------------

    def codes(self, data: bytes) -> List[int]:
        if self.two_byte:
            return [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data) - 1, 2)]
        return list(data)

    def char(self, code: int) -> str:
        text = self.to_unicode.get(code)
        if text is not None:
            return text
        text = self.encoding.get(code)
        if text is not None:
            return text
        if self.two_byte:
            return ""
        if 32 <= code < 127:
            return chr(code)
        return ""

    def width(self, code: int) -> float:
        """The glyph's advance, in thousandths of the text size."""
        width = self.widths.get(code)
        if width is None:
            width = self.default_width
            if not width and not self.widths:
                width = 500.0
        return width


def fonts_of(doc: pdfdoc.Document, resources: Any,
             cache: Optional[Dict[int, Font]] = None) -> Dict[str, Font]:
    """Every `/Font` in one resource dictionary, by its name in the stream.

    Cached by object number where the caller keeps a cache: a document usually
    has half a dozen fonts and two hundred pages, and reading `/ToUnicode`
    afresh for every page is most of the cost of reading the document.
    """
    out: Dict[str, Font] = {}
    table = doc.resolve(doc.dict_of(resources).get("Font"))
    if not isinstance(table, dict):
        return out
    for name, value in table.items():
        key = value.num if isinstance(value, pdfdoc.Ref) else None
        if cache is not None and key is not None and key in cache:
            out[str(name)] = cache[key]
            continue
        d = doc.dict_of(value)
        if not d:
            continue
        try:
            font = Font(doc, d)
        except Exception:  # noqa: BLE001 - one unreadable font is not the page
            continue
        out[str(name)] = font
        if cache is not None and key is not None:
            cache[key] = font
    return out


__all__ = ["Font", "fonts_of", "glyph_to_char", "parse_tounicode"]

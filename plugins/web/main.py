"""Web resources — a page as a folder of the things it is made of.

Point a panel at a site and the page becomes a listing: its images, scripts,
stylesheets and fonts as files, and the pages it links to as folders you can
walk into. F3 views them and F5 copies them to the disk, because this is an
ordinary file system as far as the application is concerned.

**Read-only, and not by omission.** `write`, `mkdir`, `delete` and `rename`
are simply never implemented, so the host refuses them the way it refuses any
backend that cannot do a thing. A tool that browses somebody else's site has
no business having a delete key that works.
"""

from __future__ import annotations

import email.utils
import gzip
import io
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from xcommander import DIRECTORY, Entry, FILE, FileSystem, Plugin, Root, RpcError

plugin = Plugin("org.xcommander.web", "Web resources")

#: How much of a page is read before giving up on parsing it. A page larger
#: than this is not a page, it is a download that happens to be text/html.
MAX_HTML = 4 << 20

#: Bodies kept whole because the server would not serve a byte range. One at a
#: time: this is for reading a file the panel is streaming, not a cache.
MAX_WHOLE = 64 << 20

#: What counts as a file rather than a page, whatever it was referenced as.
ASSET_SUFFIXES = {
    "css", "js", "mjs", "json", "xml", "txt", "csv", "md", "map",
    "png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico", "bmp",
    "woff", "woff2", "ttf", "otf", "eot",
    "mp3", "mp4", "webm", "ogg", "wav", "m4a", "mov",
    "pdf", "zip", "gz", "tar", "rar", "7z", "wasm",
}

PAGE_SUFFIXES = {"html", "htm", "xhtml", "php", "asp", "aspx", "jsp", "cgi"}

SKIP_SCHEMES = ("mailto:", "javascript:", "data:", "tel:", "about:", "blob:")


def setting(key: str, default):
    value = plugin.setting(key, default)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).lower() == "true"
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return value or default


# -- fetching ------------------------------------------------------------------


class _Response:
    __slots__ = ("status", "headers", "body", "url")

    def __init__(self, status: int, headers, body: bytes, url: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url


class Fetcher:
    """Everything that talks to the network, in one place.

    One opener, one set of headers, one place that decides whether a request is
    allowed at all — which is what makes "respect robots.txt" a switch rather
    than something scattered over every call site.
    """

    def __init__(self):
        self._opener = urllib.request.build_opener()
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._lock = threading.Lock()

    def _headers(self, compress: bool = True) -> dict:
        return {
            "User-Agent": setting("userAgent", "xcommander-web/1.0"),
            "Accept": "*/*",
            # Compression is welcome for a page, which is read whole and
            # parsed. It is poison for a byte range: the offsets the host asks
            # for are offsets into the *file*, and a range of a gzip stream is
            # neither the file nor something that can be inflated on its own.
            "Accept-Encoding": "gzip, deflate" if compress else "identity",
        }

    def allowed(self, url: str) -> bool:
        if not setting("robots", True):
            return True
        parsed = urlparse(url)
        origin = "%s://%s" % (parsed.scheme, parsed.netloc)

        with self._lock:
            rules = self._robots.get(origin, False)
        if rules is False:
            rules = self._load_robots(origin)
            with self._lock:
                self._robots[origin] = rules
        # No robots.txt, or one that could not be read, allows everything —
        # that is what the standard says an absent file means.
        if rules is None:
            return True
        return rules.can_fetch(setting("userAgent", "xcommander-web/1.0"), url)

    def _load_robots(self, origin: str):
        rules = urllib.robotparser.RobotFileParser()
        rules.set_url(origin + "/robots.txt")
        try:
            request = urllib.request.Request(
                origin + "/robots.txt", headers=self._headers()
            )
            with self._opener.open(request, timeout=setting("timeout", 20)) as answer:
                rules.parse(answer.read(512 << 10).decode("utf-8", "replace").splitlines())
            return rules
        except Exception:  # noqa: BLE001 - an unreadable robots.txt is no robots.txt
            return None

    def open(self, url: str, method: str = "GET", byte_range: Optional[Tuple[int, int]] = None) -> _Response:
        if not self.allowed(url):
            raise RpcError(
                "robots.txt on this site asks readers not to fetch %s. "
                "Turn the switch off in the plugin's settings if it is yours." % url
            )

        headers = self._headers(compress=byte_range is None)
        if byte_range is not None:
            headers["Range"] = "bytes=%d-%d" % byte_range

        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=setting("timeout", 20)) as answer:
                body = b"" if method == "HEAD" else _decoded(answer)
                return _Response(answer.status, answer.headers, body, answer.url)
        except urllib.error.HTTPError as failure:
            raise RpcError("%s %s for %s" % (failure.code, failure.reason, url))
        except urllib.error.URLError as failure:
            raise RpcError("Could not reach %s: %s" % (url, failure.reason))
        except Exception as failure:  # noqa: BLE001 - a socket can fail in many ways
            raise RpcError("Could not read %s: %s" % (url, failure))


def _decoded(answer) -> bytes:
    """Undoes Content-Encoding, which urllib does not do for us."""
    raw = answer.read(MAX_WHOLE)
    encoding = (answer.headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in encoding:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 - a body that will not inflate is served as it came
        return raw
    return raw


fetcher = Fetcher()


# -- reading a page ------------------------------------------------------------

#: Where a reference can hide. Kept as data because the list only ever grows.
REFERENCES = {
    "a": ("href",),
    "area": ("href",),
    "img": ("src", "data-src"),
    "script": ("src",),
    "link": ("href",),
    "source": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "track": ("src",),
}

SETS = {"srcset", "imagesrcset", "data-srcset"}


class _Links(HTMLParser):
    """Collects every URL a page points at, in the order it points at them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base: Optional[str] = None
        #: (reference, tag) pairs, still relative and still unfiltered.
        self.found: List[Tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): (value or "") for key, value in attrs}

        if tag == "base" and values.get("href") and self.base is None:
            self.base = values["href"]
            return

        for attribute in REFERENCES.get(tag, ()):
            reference = values.get(attribute)
            if reference:
                self.found.append((reference.strip(), tag))

        for attribute in SETS & values.keys():
            # `a.png 1x, b.png 2x` — the descriptor is not part of the URL.
            for candidate in values[attribute].split(","):
                reference = candidate.strip().split(" ")[0]
                if reference:
                    self.found.append((reference, tag))

    # A style sheet is a page's biggest source of images, and they are only
    # reachable through url(...) — no attribute carries them.
    def handle_data(self, data):
        if self.lasttag == "style" and "url(" in data:
            for reference in re.findall(r"url\(\s*['\"]?([^'\")]+)", data):
                self.found.append((reference.strip(), "style"))


def _is_page(url: str, tag: str) -> bool:
    """Whether something is a page to walk into or a file to read.

    The suffix decides when there is one, because a link to a PDF is a file
    however it was written. Without one, what it was referenced *as* decides:
    a link is somewhere to go, everything else is something the page is made
    of.
    """
    suffix = _suffix(url)
    if suffix in ASSET_SUFFIXES:
        return False
    if suffix in PAGE_SUFFIXES or not suffix:
        return tag in ("a", "area", "iframe")
    return False


def _suffix(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _clean(url: str) -> str:
    """Drops the fragment: `#section` names a place in a page, not a resource."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def _name_for(url: str, same_host: bool) -> str:
    """What a resource is called in the listing.

    Its own file name, which is what the user is looking for. A resource from
    somewhere else keeps its host in front, because `logo.png` from three
    different CDNs is three different files and one name.
    """
    parsed = urlparse(url)
    name = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not name:
        name = parsed.netloc or "index"
    elif parsed.query:
        # Two URLs differing only by query are two resources.
        name = "%s?%s" % (name, parsed.query[:40])
    if not same_host and parsed.netloc:
        name = "%s · %s" % (parsed.netloc, name)
    return name.replace("/", "_")


# -- the file system -----------------------------------------------------------


class WebFs(FileSystem):
    """One scheme's worth of the web, as a read-only file system.

    The application addresses an entry as *parent URL* + *name*, which for a
    page's own resources is exactly where they live — but a page also points at
    things on other paths and other hosts, and those cannot be addressed that
    way. So every listing remembers what each name really stood for, and every
    later call looks it up. Without that, a font from a CDN would be fetched
    from the page's own directory and 404.
    """

    def __init__(self, scheme: str):
        self.scheme = scheme
        #: Addressed URL -> the URL it really means.
        self._real: Dict[str, str] = {}
        #: Addressed URL -> whether it is a page.
        self._pages: Dict[str, bool] = {}
        self._lock = threading.Lock()
        self._whole: Optional[Tuple[str, bytes]] = None

    # -- what the host asks for -------------------------------------------

    def roots(self) -> List[Root]:
        """None. A scheme is not a place.

        The drives list is somewhere to *go*, and there is no such place as
        "the web" — a row that lands on a stand-in address is a row that does
        nothing anyone wanted. The way in is a saved connection, or a URL typed
        into Go to; both are already offered where connections are set up.
        """
        return []

    def default_location(self) -> str:
        # Somewhere that exists and is tiny, rather than an error or a guess at
        # what the user meant. example.com is the address reserved for exactly
        # this by IANA.
        return "%s://example.com/" % self.scheme

    def list(self, url: str) -> List[Entry]:
        page = self._resolve(url)
        answer = fetcher.open(page)
        kind = (answer.headers.get("Content-Type") or "").lower()

        if "html" not in kind and "xml" not in kind:
            raise RpcError(
                "%s is %s, not a page. Open it with F3, or copy it with F5."
                % (page, kind.split(";")[0] or "not text")
            )

        entries = self._parse(answer.url or page, answer.body[:MAX_HTML], url)
        if setting("sizes", False):
            self._measure(entries, url)
        return entries

    def stat(self, url: str) -> Optional[Entry]:
        real = self._resolve(url)
        with self._lock:
            known = self._pages.get(url)

        name = _name_for(real, True)
        if known:
            return Entry(name, kind=DIRECTORY)

        try:
            answer = fetcher.open(real, method="HEAD")
        except RpcError:
            # Plenty of servers refuse HEAD. Not knowing is not an error: the
            # panel can still open it, and that is a GET.
            return Entry(name, kind=FILE)

        return Entry(
            name,
            kind=FILE,
            size=int(answer.headers.get("Content-Length") or 0),
            modified=_modified(answer.headers.get("Last-Modified")),
        )

    def read(self, url: str, offset: int, length: int) -> bytes:
        """Bytes of the file, which is not the same as bytes of a range.

        A byte range is answered against whatever representation the server has
        stored, and that need not be the one it would have sent: ask iana.org
        for the first 64 bytes of a page and it answers 206, no
        Content-Encoding, and 64 bytes of a compressed copy it keeps — the
        range is of *that*, and the total it quotes is that file's length.
        There is no header that says so.

        So the whole thing is fetched and decoded once, kept while the panel
        reads through it, and sliced here. One request per file either way, and
        the bytes are the file's. Ranges are left for the rare resource too big
        to hold, where a server that stores compressed copies is not the sort
        of server that is serving it.
        """
        real = self._resolve(url)

        with self._lock:
            whole = self._whole
        if whole is not None and whole[0] == real:
            return whole[1][offset : offset + length]

        answer = fetcher.open(real)
        if len(answer.body) < MAX_WHOLE:
            with self._lock:
                self._whole = (real, answer.body)
            return answer.body[offset : offset + length]

        ranged = fetcher.open(real, byte_range=(offset, offset + length - 1))
        if ranged.status == 206:
            return ranged.body[:length]
        return ranged.body[offset : offset + length]

    # -- internals --------------------------------------------------------

    def _resolve(self, url: str) -> str:
        with self._lock:
            return self._real.get(url, url)

    def _remember(self, addressed: str, real: str, is_page: bool) -> None:
        with self._lock:
            if len(self._real) > 20000:
                self._real.clear()
                self._pages.clear()
            self._real[addressed] = real
            self._pages[addressed] = is_page

    def _parse(self, base: str, body: bytes, addressed_parent: str) -> List[Entry]:
        text = body.decode(_charset(body), "replace")
        links = _Links()
        try:
            links.feed(text)
        except Exception:  # noqa: BLE001 - malformed markup is the normal case
            pass

        root = urljoin(base, links.base) if links.base else base
        host = urlparse(root).netloc

        want_links = setting("links", True)
        want_offsite = setting("offsite", True)
        want_outbound = setting("outbound", False)

        entries: List[Entry] = []
        taken: Dict[str, int] = {}
        seen: set = set()

        for reference, tag in links.found:
            if not reference or reference.startswith(SKIP_SCHEMES):
                continue
            target = _clean(urljoin(root, reference))
            parsed = urlparse(target)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            if target in seen or target == root:
                continue

            same_host = parsed.netloc == host
            is_page = _is_page(target, tag)

            if is_page and not want_links:
                continue
            if not same_host and not want_offsite:
                continue
            # A link off the site is still worth listing — it is part of what
            # the page is — but walking into it is a different decision.
            if is_page and not same_host and not want_outbound:
                is_page = False

            seen.add(target)
            name = _name_for(target, same_host)
            count = taken.get(name, 0)
            taken[name] = count + 1
            if count:
                name = "%s (%d)" % (name, count + 1)

            entries.append(Entry(name, kind=DIRECTORY if is_page else FILE))
            self._remember(
                _child(addressed_parent, name), target, is_page
            )

        return entries

    def _measure(self, entries: List[Entry], addressed_parent: str) -> None:
        """Fills in sizes with a HEAD each, several at a time.

        Off by default: it is one extra request per resource, and a listing is
        useful without it. On, it is what makes a whole site measurable — the
        disk map walks this file system like any other.
        """
        targets = [e for e in entries if e.kind == FILE]
        if not targets:
            return

        def measure(entry: Entry) -> None:
            try:
                answer = fetcher.open(
                    self._resolve(_child(addressed_parent, entry.name)), method="HEAD"
                )
                entry.size = int(answer.headers.get("Content-Length") or 0)
                entry.modified = _modified(answer.headers.get("Last-Modified"))
            except Exception:  # noqa: BLE001 - an unmeasurable resource stays at zero
                pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(measure, targets))


def _child(parent: str, name: str) -> str:
    """How the application will address this entry when it asks for it.

    It builds a child URL out of the directory it listed and the entry's name,
    percent-encoding the name — so the same arithmetic has to happen here for
    the lookup to find anything.
    """
    from urllib.parse import quote

    return parent.rstrip("/") + "/" + quote(name, safe="")


def _charset(body: bytes) -> str:
    match = re.search(rb'charset=["\']?([\w-]+)', body[:4096], re.I)
    return match.group(1).decode("ascii", "replace") if match else "utf-8"


def _modified(header: Optional[str]) -> Optional[float]:
    if not header:
        return None
    try:
        return time.mktime(email.utils.parsedate(header))
    except Exception:  # noqa: BLE001 - a malformed date is no date
        return None


for _scheme in ("https", "http"):
    plugin.add_filesystem(WebFs(_scheme))

plugin.run()

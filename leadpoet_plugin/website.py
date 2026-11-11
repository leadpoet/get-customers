"""Read a company's public website, and keep only facts that are really on it.

Everything fetched here is untrusted. It is passed to the model as data, and any
fact the model returns must quote the page it claims to come from, word for word.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

MAX_BYTES = 3_000_000  # Big sites put most of a megabyte of code before the first visible word.
MAX_PAGE_CHARS = 9_000
MAX_EXTRA_PAGES = 4
TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 3
FETCH_SECONDS = 15  # In total. A page that drips one byte at a time never trips a socket timeout.
SCAN_SECONDS = 30
# Reasoning models spend this budget on thinking first. Too small and the answer is cut off mid-word.
MODEL_MAX_TOKENS = 8000
_URL_SAFE = "%/:=&?~#+!$,;'@()*[]"
USER_AGENT = "leadpoet-hermes-plugin/0.1 (+https://leadpoet.com)"

_USEFUL_PATH = re.compile(
    r"/(about|company|pricing|customers?|case-stud|stories|product|platform|solutions?|features?|"
    r"use-cases?|industries|why)", re.IGNORECASE)
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
_BLOCK_TAGS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "section", "article", "tr", "header", "footer"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*(?:0+(?:\.0*)?|\.0+)(?![.\d])", re.IGNORECASE)
_CLASS_SELECTOR = re.compile(r"\s*[\w-]*\.([\w-]+)\s*")
_NUMBER = re.compile(r"\d[\d,.]*")


class SiteError(Exception):
    """A problem the user can understand and fix."""


@dataclass
class Page:
    url: str
    title: str
    text: str


@dataclass
class Scan:
    domain: str
    pages: list[Page] = field(default_factory=list)


def _style_sheets(html: str) -> list[str]:
    """Find style blocks with plain string search, so a hostile page can't make this slow."""
    lower, sheets, position = html.lower(), [], 0
    while True:
        start = lower.find("<style", position)
        opened = lower.find(">", start) if start >= 0 else -1
        end = lower.find("</style", opened) if opened >= 0 else -1
        if end < 0:
            return sheets
        sheets.append(html[opened + 1:end])
        position = end + 1


def _plain_rules(sheet: str) -> list[tuple[str, str]]:
    """Rules outside any @ block. Rules for one screen size don't hide text from everyone."""
    rules, depth, start, selectors = [], 0, 0, ""
    for brace in re.finditer(r"[{}]", sheet):
        if brace.group() == "{":
            if depth == 0:
                selectors = sheet[start:brace.start()]
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0 and not selectors.lstrip().startswith("@"):
                rules.append((selectors, sheet[start:brace.start()]))
        start = brace.end()
    return rules


def _hidden_classes(html: str, while_loading: bool = True) -> set[str]:
    """Classes the page's own style block always hides. Some sites hide everything with visibility until a script runs."""
    classes: set[str] = set()
    for sheet in _style_sheets(html):
        for selectors, rules in _plain_rules(sheet):
            if _HIDDEN_STYLE.search(rules if while_loading else re.sub(r"visibility\s*:\s*hidden", "", rules, flags=re.I)):
                classes |= {match.group(1) for match in map(_CLASS_SELECTOR.fullmatch, selectors.split(",")) if match}
    return classes


def _is_hidden(attrs: dict, classes: set[str]) -> bool:
    return ("hidden" in attrs or str(attrs.get("aria-hidden") or "").lower() == "true"
            or bool(_HIDDEN_STYLE.search(str(attrs.get("style") or "")))
            or bool(classes & set(str(attrs.get("class") or "").split())))


class _Extractor(HTMLParser):
    def __init__(self, hidden_classes: set[str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_classes = hidden_classes or set()
        self.parts: list[str] = []
        self.size = 0
        self.links: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._hidden_tag = ""
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._hidden_tag:
            self._hidden_depth += tag == self._hidden_tag
            return
        if _is_hidden(dict(attrs), self._hidden_classes) and tag not in _VOID_TAGS:  # Words a reader can't see can't prove anything.
            self._hidden_tag, self._hidden_depth = tag, 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._hidden_tag:
            self._hidden_depth -= tag == self._hidden_tag
            if not self._hidden_depth:
                self._hidden_tag = ""
            return
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_tag:
            return
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)
            self.size += len(data)

    def text(self) -> str:
        joined = " ".join(self.parts)
        lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in joined.split("\n"))
        return "\n".join(line for line in lines if line)


def normalize_site(raw: str) -> str:
    """Turn what the user typed into an https URL, or explain why it can't be one."""
    value = (raw or "").strip().strip("<>")
    if not value or re.search(r"\s", value):
        raise SiteError("Send one website, like /customers yoursite.com")
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        raise SiteError("That doesn't look like a website. Try /customers yoursite.com") from None
    if parsed.scheme not in ("http", "https") or "." not in host or parsed.username or parsed.password:
        raise SiteError("That doesn't look like a website. Try /customers yoursite.com")
    return f"{parsed.scheme}://{host}{port}{parsed.path or '/'}"


def is_public_host(host: str) -> bool:
    """Only public internet addresses, so a link can't reach this machine or its network."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def _resolves(host: str) -> bool:
    try:
        return bool(socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP))
    except (socket.gaierror, UnicodeError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _public_socket(host: str, port: int, timeout: Any) -> socket.socket:
    """Check the address at the moment of connecting, so DNS can't change its answer after the check."""
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not found or not all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in found):
        raise OSError("not a public address")
    family, kind, protocol, _, address = found[0]
    connection = socket.socket(family, kind, protocol)
    try:
        connection.settimeout(timeout)
        connection.connect(address)
    except OSError:
        connection.close()
        raise
    return connection


class _PublicHttp(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _public_socket(self.host, self.port, self.timeout)


class _PublicHttps(http.client.HTTPSConnection):
    def connect(self) -> None:
        self.sock = self._context.wrap_socket(_public_socket(self.host, self.port, self.timeout), server_hostname=self.host)


class _PublicHttpHandler(urllib.request.HTTPHandler):
    def http_open(self, request: Any) -> Any:
        return self.do_open(_PublicHttp, request)


class _PublicHttpsHandler(urllib.request.HTTPSHandler):
    def https_open(self, request: Any) -> Any:
        return self.do_open(_PublicHttps, request, context=ssl.create_default_context())


_OPENER = urllib.request.build_opener(_NoRedirect, _PublicHttpHandler, _PublicHttpsHandler)


def _read_within(response: Any, deadline: float) -> bytes:
    read = getattr(response, "read1", response.read)
    data = bytearray()
    while len(data) < MAX_BYTES:
        chunk = read(min(65536, MAX_BYTES - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError
    return bytes(data)


def fetch(url: str, *, opener: Any = _OPENER, public: Any = is_public_host) -> tuple[str, str]:
    """Return (final_url, html). Every redirect hop is checked before it is followed."""
    current = url
    deadline = time.monotonic() + FETCH_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        try:
            parsed = urllib.parse.urlsplit(current)
            host = parsed.hostname
        except ValueError:
            raise SiteError("That address isn't valid.") from None
        if parsed.scheme not in ("http", "https") or not host:
            raise SiteError("That address isn't valid.")
        if not public(host):
            if not _resolves(host):
                raise SiteError(f"Couldn't find {host}. Check the spelling.")
            raise SiteError("That website isn't on the public internet.")
        current = urllib.parse.quote(current, safe=_URL_SAFE)  # Sites do link to paths with spaces in them.
        request = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                kind = response.headers.get("Content-Type", "")
                if "html" not in kind.lower():
                    raise SiteError("That address isn't a web page.")
                return current, _read_within(response, deadline).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                current = urllib.parse.urljoin(current, error.headers["Location"])
                continue
            if error.code in (401, 403):
                raise SiteError("That site doesn't let automated readers in, so I can't read it.") from None
            if error.code == 429:
                raise SiteError("That site asked me to slow down, so I stopped.") from None
            home = f" Try the home page: /customers {host}" if error.code == 404 and parsed.path not in ("", "/") else ""
            raise SiteError(f"That site answered with an error ({error.code}).{home}") from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError):
            raise SiteError("Couldn't reach that website.") from None
    raise SiteError("That website redirects too many times.")


def _parse(html: str, styled: bool = True) -> _Extractor:
    extractor = _Extractor(_hidden_classes(html, while_loading=styled))
    try:
        for start in range(0, len(html), 65536):  # Stop once there's more text than is ever used.
            extractor.feed(html[start:start + 65536])
            if extractor.size > 3 * MAX_PAGE_CHARS:
                break
        else:
            extractor.close()  # Newer Pythons hold a long script back until told the page has ended.
    except Exception:  # Malformed markup keeps whatever was read so far.
        pass
    if styled and len(extractor.text()) < 200:  # Text hidden only until a script runs is still the page.
        return _parse(html, styled=False)
    return extractor


def _pick_links(base: str, links: list[str], host: str) -> list[str]:
    seen: set[str] = {base, base.rstrip("/")}
    picked: list[str] = []
    for link in links:
        try:
            absolute = urllib.parse.urldefrag(urllib.parse.urljoin(base, link))[0]
            parts = urllib.parse.urlsplit(absolute)
            same = (parts.hostname or "").removeprefix("www.") == host.removeprefix("www.")
        except ValueError:  # One broken link on a page shouldn't stop the scan.
            continue
        if same and absolute not in seen and _USEFUL_PATH.search(parts.path):
            seen.add(absolute)
            picked.append(absolute)
    return picked[:MAX_EXTRA_PAGES]


def scan_site(url: str, *, fetcher: Any = fetch) -> Scan:
    started = time.monotonic()
    final, html = fetcher(url)
    home = _parse(html)
    host = urllib.parse.urlsplit(final).hostname or ""
    scan = Scan(domain=host.removeprefix("www."))
    scan.pages.append(Page(final, home.title.strip(), home.text()[:MAX_PAGE_CHARS]))
    for link in _pick_links(final, home.links, host):
        if time.monotonic() - started > SCAN_SECONDS:
            break
        try:
            page_url, page_html = fetcher(link)
        except SiteError:
            continue
        page = _parse(page_html)
        scan.pages.append(Page(page_url, page.title.strip(), page.text()[:MAX_PAGE_CHARS]))
    if sum(len(page.text) for page in scan.pages) < 200:
        raise SiteError("That site has almost no readable text. It may be built to load only in a browser.")
    return scan


def numbers_in(text: str) -> set[str]:
    """Whole numbers only, so 5 is never proved by 150."""
    return {match.rstrip(".,").replace(",", "") for match in _NUMBER.findall(text or "")}


def without_tag(text: str, name: str) -> str:
    """Remove a tag the page could use to break out of the block that marks it as data."""
    pattern = re.compile(rf"</?\s*{name}\b[^>]*>", re.IGNORECASE)
    while pattern.search(text):  # Taking one out can join the pieces around it into another.
        text = pattern.sub("", text)
    return text


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_is_on_page(scan: Scan, url: str, quote: str) -> bool:
    """True only when these exact words appear on the page the model named."""
    needle = _squash(quote or "")
    if len(needle) < 12:
        return False
    return any(page.url == url and needle in _squash(page.text) for page in scan.pages)


def parse_json_object(text: str | None) -> dict | None:
    """Read a JSON object out of a model reply, tolerating a code fence around it."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None

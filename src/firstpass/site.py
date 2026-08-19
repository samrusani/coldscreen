"""Site text extraction (stage 6 input): homepage plus up to 2 about pages.

Plain httpx with the existing evidence discipline: every response that is
received is persisted (url, status, retrieved_at, extracted text), and the
extracted text, not the raw HTML, is what the casefile carries forward.

Scope, per the work order:
- The given URL's page, plus up to 2 discovered same-host pages whose path
  suggests about, company, or team content. First degree only: links are
  discovered on the entry page, never on the discovered pages.
- http and https only; anything else is rejected with a clean error.
- No authentication, ever. No cookies, no credentials, a plain GET with an
  honest User-Agent.
- robots.txt is checked first with this tool's own agent token. Disallowed
  paths are skipped and the skip is recorded (the stage findings state it).
  Robots handling mirrors urllib.robotparser semantics with a conservative
  reading for failures: 401 and 403 mean fetch nothing, any other 4xx means
  no robots file exists (allow), and a 5xx or an unreachable host is
  treated as disallow-all because permission could not be established.
- Responses are read off the wire streamed and capped at
  max_site_response_bytes; a truncated read is flagged on the page.

HTML becomes text through a stdlib html.parser subclass that drops script,
style, head, nav, header, footer, and aside content. Link discovery scans
the WHOLE document including nav and footer, because that is exactly where
about links live; only the text extraction strips those regions.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_random_exponential

from . import __version__
from .ch_client import FetchRecord
from .stages.registry import NamedRecord

USER_AGENT_TOKEN = "firstpass-screen"
USER_AGENT = f"{USER_AGENT_TOKEN}/{__version__}"
MAX_ATTEMPTS = 3
ABOUT_PAGE_LIMIT = 2
ABOUT_PATH_TOKENS = ("about", "company", "team")

# Tags whose content is chrome or code, not page prose. head covers titles
# and meta; svg covers inline icon text.
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "head", "nav", "header", "footer", "aside", "svg"}
)
# Tags that end a block of prose: a newline is inserted so words from
# different blocks can never run together.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "ul",
        "ol",
        "table",
        "tr",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
    }
)


class SiteError(Exception):
    """The site URL cannot be used at all. Message is user-facing."""


class _RetryableSiteError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"retryable status {status}")
        self.status = status


class _SiteTransportError(Exception):
    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause) or type(cause).__name__)


class _TextAndLinkParser(HTMLParser):
    """One pass: prose text (chrome stripped) and every anchor href."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.hrefs.append(value)
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS and self._skip_depth == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS and self._skip_depth == 0:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    @property
    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """Visible prose from an HTML document, chrome regions stripped."""
    parser = _TextAndLinkParser()
    parser.feed(html)
    return parser.text


@dataclass(frozen=True)
class SitePage:
    """One fetched site page: where it came from and what it said."""

    url: str
    path: str
    status: int
    text: str
    truncated: bool = False

    @property
    def source_label(self) -> str:
        return f"site {self.path}"


@dataclass
class SiteFetchResult:
    """Everything the site fetch produced, pages and audit trail alike."""

    site_url: str
    pages: list[SitePage] = field(default_factory=list)
    robots_skipped: list[str] = field(default_factory=list)  # paths never fetched
    failures: list[str] = field(default_factory=list)  # human-readable notes
    records: list[NamedRecord] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return any(page.text for page in self.pages)


def validate_site_url(site_url: str) -> str:
    """Clean scheme and host validation; returns the stripped URL."""
    text = site_url.strip()
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise SiteError(
            f"site URL must use http or https, got {parts.scheme or 'no scheme'!r}."
            " Authentication-gated and non-web sources are out of scope."
        )
    if not parts.netloc:
        raise SiteError(f"site URL carries no host: {site_url!r}")
    return text


def _display_path(url: str) -> str:
    return urlsplit(url).path or "/"


@dataclass
class _CappedResponse:
    status: int
    body: bytes
    truncated: bool
    final_url: str = ""


class _SiteFetcher:
    """httpx wrapper: capped streaming reads with modest retries."""

    def __init__(
        self,
        timeout_seconds: float,
        max_bytes: int,
        now: Callable[[], datetime],
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._now = now
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._http = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"},
        )

    def close(self) -> None:
        self._http.close()

    def _get_once(self, url: str) -> _CappedResponse:
        try:
            with self._http.stream("GET", url) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableSiteError(response.status_code)
                received = bytearray()
                truncated = False
                for chunk in response.iter_bytes(chunk_size=65536):
                    remaining = self._max_bytes - len(received)
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        received.extend(chunk[:remaining])
                        truncated = True
                        break
                    received.extend(chunk)
                return _CappedResponse(
                    status=response.status_code,
                    body=bytes(received),
                    truncated=truncated,
                    final_url=str(response.url),
                )
        except httpx.TransportError as error:
            raise _SiteTransportError(error) from error

    def get(self, url: str) -> _CappedResponse:
        """GET with retries on transport failures, 429, and 5xx.

        The last retryable status is returned rather than raised once the
        attempts are exhausted, so the audit pack records the real response.
        """
        retrying = Retrying(
            retry=retry_if_exception_type((_RetryableSiteError, _SiteTransportError)),
            wait=wait_random_exponential(multiplier=0.5, max=10),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    return self._get_once(url)
        except _RetryableSiteError as error:
            return _CappedResponse(status=error.status, body=b"", truncated=False)
        raise AssertionError("unreachable: Retrying either returns or raises")

    def record(self, url: str, response: _CappedResponse, text: str, kind: str) -> FetchRecord:
        body = {
            "kind": kind,
            "url": url,
            "status": response.status,
            "truncated": response.truncated,
            "text": text,
        }
        if response.final_url and response.final_url != url:
            # Redirects are followed (https upgrades and www variants are
            # routine); the landing URL is recorded so the audit pack shows
            # where the bytes actually came from.
            body["final_url"] = response.final_url
        return FetchRecord(
            url=url,
            params={},
            status=response.status,
            body=body,
            retrieved_at=self._now(),
        )

    def error_record(self, url: str, message: str, kind: str) -> FetchRecord:
        """Audit record for a fetch that produced no response at all."""
        return FetchRecord(
            url=url,
            params={},
            status=0,
            body={"kind": kind, "url": url, "error": message},
            retrieved_at=self._now(),
        )


def _decode(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _load_robots(
    fetcher: _SiteFetcher, base: str, result: SiteFetchResult
) -> RobotFileParser | None:
    """Fetch and parse robots.txt; None means fetch nothing at all.

    Semantics mirror urllib.robotparser with a conservative failure
    posture; see the module docstring.
    """
    robots_url = f"{base}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        response = fetcher.get(robots_url)
    except _SiteTransportError as error:
        result.failures.append(f"robots.txt was unreachable ({error}); nothing was fetched")
        result.records.append(
            NamedRecord("site_robots", fetcher.error_record(robots_url, str(error), "site_robots"))
        )
        return None
    text = _decode(response.body)
    result.records.append(
        NamedRecord("site_robots", fetcher.record(robots_url, response, text, "site_robots"))
    )
    if 200 <= response.status < 300:
        parser.parse(text.splitlines())
        return parser
    if response.status in (401, 403):
        result.failures.append(
            f"robots.txt returned HTTP {response.status}; treated as disallow-all"
        )
        return None
    if 400 <= response.status < 500:
        parser.parse([])  # no robots file: everything is allowed
        return parser
    result.failures.append(f"robots.txt returned HTTP {response.status}; treated as disallow-all")
    return None


def _discover_about_urls(entry_url: str, hrefs: list[str]) -> list[str]:
    """Same-host links whose path suggests about, company, or team content."""
    entry_parts = urlsplit(entry_url)
    seen_paths = {entry_parts.path or "/"}
    discovered: list[str] = []
    for href in hrefs:
        absolute = urljoin(entry_url, href.strip())
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"}:
            continue
        if parts.netloc.lower() != entry_parts.netloc.lower():
            continue
        path = parts.path or "/"
        if path in seen_paths:
            continue
        lowered = path.lower()
        if not any(token in lowered for token in ABOUT_PATH_TOKENS):
            continue
        seen_paths.add(path)
        discovered.append(f"{parts.scheme}://{parts.netloc}{path}")
    return discovered


def fetch_site(
    site_url: str,
    timeout_seconds: float,
    max_response_bytes: int,
    now: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> SiteFetchResult:
    """Fetch the entry page plus up to 2 discovered about pages, politely."""
    clean_url = validate_site_url(site_url)
    clock = now or (lambda: datetime.now(UTC))
    result = SiteFetchResult(site_url=clean_url)
    parts = urlsplit(clean_url)
    base = f"{parts.scheme}://{parts.netloc}"

    fetcher = _SiteFetcher(timeout_seconds, max_response_bytes, clock, sleeper)
    try:
        robots = _load_robots(fetcher, base, result)
        if robots is None:
            # Everything below the entry URL is off limits; record the paths
            # that were wanted and never fetched.
            result.robots_skipped.append(_display_path(clean_url))
            return result

        page_number = 0

        def fetch_page(url: str) -> _TextAndLinkParser | None:
            nonlocal page_number
            path = _display_path(url)
            if not robots.can_fetch(USER_AGENT_TOKEN, url):
                result.robots_skipped.append(path)
                return None
            try:
                response = fetcher.get(url)
            except _SiteTransportError as error:
                result.failures.append(f"{path}: unreachable ({error})")
                page_number += 1
                result.records.append(
                    NamedRecord(
                        f"site_{page_number:03d}",
                        fetcher.error_record(url, str(error), "site_text"),
                    )
                )
                return None
            parser = _TextAndLinkParser()
            text = ""
            if response.status == 200:
                parser.feed(_decode(response.body))
                text = parser.text
            else:
                result.failures.append(f"{path}: HTTP {response.status}")
            page_number += 1
            result.records.append(
                NamedRecord(
                    f"site_{page_number:03d}",
                    fetcher.record(url, response, text, "site_text"),
                )
            )
            if response.status != 200:
                return None
            result.pages.append(
                SitePage(
                    url=url,
                    path=path,
                    status=response.status,
                    text=text,
                    truncated=response.truncated,
                )
            )
            return parser

        entry_parser = fetch_page(clean_url)
        if entry_parser is not None:
            fetched_about = 0
            for url in _discover_about_urls(clean_url, entry_parser.hrefs):
                if fetched_about >= ABOUT_PAGE_LIMIT:
                    break
                before = len(result.pages)
                fetch_page(url)
                if len(result.pages) > before:
                    fetched_about += 1
        return result
    finally:
        fetcher.close()

"""Site text extraction (stage 6 input): homepage plus up to 2 about pages.

Plain httpx with the existing evidence discipline: every response that is
received is persisted (url, status, retrieved_at, extracted text), and the
extracted text, not the raw HTML, is what the casefile carries forward.

Scope, per the work order:
- The given URL's page, plus up to 2 discovered same-host pages whose path
  suggests about, company, or team content. First degree only: links are
  discovered on the entry page, never on the discovered pages.
- http and https only, and no userinfo in the URL; anything else is
  rejected with a clean error.
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

Trust boundary (the hardening-1 work order, plus the review's F1 fix):
- Every hostname is resolved EXACTLY ONCE, through the injected resolver,
  by the host pinner. Every returned address is classified, and the
  connection is then PINNED to the validated address: a custom httpcore
  network backend receives the hostname at connect time, asks the pinner
  for the one validated address, and connects to that address while
  httpcore keeps the original hostname for the Host header and TLS SNI.
  Check and connect share a single resolution, so a DNS-rebinding record
  that answers public-then-private cannot swap in an internal address
  between the check and the connect. The per-host cache lives inside the
  pinner, so the cached address is by construction the address actually
  connected to. httpx's own resolution is never used on the site path
  (trust_env is off for this client so no proxy mount can bypass the
  pinned transport either).
- An address is rejected when it is loopback, RFC1918 private, link-local
  (including 169.254.169.254), CGNAT (100.64.0.0/10), unique-local or
  site-local IPv6, unspecified, reserved, or multicast, or when the
  hostname is a literal cloud metadata name. Blocked targets are never
  fetched and never persisted as body. One blocked address in a mixed
  resolver answer blocks the host.
- Redirects are never followed automatically. At most 3 hops are followed
  manually, and a hop is allowed only to the same host, with exactly two
  exceptions: an http to https upgrade on the same host, and adding or
  removing a leading "www.". Anything else is recorded as a blocked
  redirect: the evidence records the chain and the refused target, never
  the landing body, and the claims stage renders an explicit finding.
  Every hop, robots.txt included, goes through the same pinned transport.

HTML becomes text through a stdlib html.parser subclass that drops script,
style, head, nav, header, footer, and aside content. Link discovery scans
the WHOLE document including nav and footer, because that is exactly where
about links live; only the text extraction strips those regions.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpcore
import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_random_exponential

from . import __version__
from .ch_client import FetchRecord
from .stages.registry import NamedRecord

USER_AGENT_TOKEN = "coldscreen"
USER_AGENT = f"{USER_AGENT_TOKEN}/{__version__}"
MAX_ATTEMPTS = 3
MAX_REDIRECT_HOPS = 3
ABOUT_PAGE_LIMIT = 2
ABOUT_PATH_TOKENS = ("about", "company", "team")

# Literal cloud metadata hostnames, rejected without resolving. The metadata
# IP itself (169.254.169.254) falls inside the link-local block.
BLOCKED_METADATA_HOSTNAMES = frozenset({"metadata.google.internal", "metadata", "instance-data"})

_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")

# Resolves a hostname to its addresses. Injectable so tests never touch
# real DNS; the default uses the system resolver.
Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    """All addresses the system resolver returns for host, deduplicated."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _address_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when connecting to this address must be refused."""
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _address_blocked(mapped)
        if address in _SITE_LOCAL_V6:
            return True
    if isinstance(address, ipaddress.IPv4Address) and address in _CGNAT_V4:
        return True
    return (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_multicast
    )


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


class _BlockedTargetError(Exception):
    """The requested host itself is blocked; nothing was connected."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    # One entry per refused redirect: "path: reason". The chain itself lives
    # in the evidence record; the landing body is never fetched.
    blocked_redirects: list[str] = field(default_factory=list)
    records: list[NamedRecord] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return any(page.text for page in self.pages)


def validate_site_url(site_url: str) -> str:
    """Clean scheme, host, and userinfo validation; returns the stripped URL."""
    text = site_url.strip()
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise SiteError(
            f"site URL must use http or https, got {parts.scheme or 'no scheme'!r}."
            " Authentication-gated and non-web sources are out of scope."
        )
    if not parts.netloc:
        raise SiteError(f"site URL carries no host: {site_url!r}")
    if parts.username is not None or parts.password is not None:
        raise SiteError(
            "site URL must not carry userinfo (user:password@host). Credentials"
            " are never used and a URL shaped like that is a trust-boundary risk."
        )
    return text


def _display_path(url: str) -> str:
    return urlsplit(url).path or "/"


@dataclass
class _CappedResponse:
    status: int
    body: bytes
    truncated: bool
    location: str | None = None


@dataclass(frozen=True)
class _BlockedRedirect:
    """A redirect hop that was refused. The target was never connected."""

    target: str
    reason: str


@dataclass
class _FetchOutcome:
    """One manual-redirect fetch: the terminal response or the refusal."""

    chain: list[str]  # every URL actually requested, in order
    response: _CappedResponse | None = None
    blocked: _BlockedRedirect | None = None


def _hop_allowed(current: str, target: str) -> tuple[bool, str]:
    """Whether one redirect hop is allowed, and the refusal reason if not.

    Allowed: same host (and port). Exactly two exceptions: an http to https
    upgrade on the same host, and adding or removing a leading "www.".
    """
    c, t = urlsplit(current), urlsplit(target)
    if t.scheme not in {"http", "https"}:
        return False, "the redirect target scheme is not http or https"
    if t.username is not None or t.password is not None:
        return False, "the redirect target carries userinfo"
    if not (c.scheme == t.scheme or (c.scheme == "http" and t.scheme == "https")):
        return False, "the redirect would change the scheme other than an https upgrade"
    chost = (c.hostname or "").lower()
    thost = (t.hostname or "").lower()
    if not thost:
        return False, "the redirect target carries no host"
    if c.port != t.port:
        return False, "the redirect changes the port"
    if thost == chost or thost == f"www.{chost}" or chost == f"www.{thost}":
        return True, ""
    return False, "the redirect leaves the validated host"


class _HostPinner:
    """One resolution per host, shared by the check and the connection.

    pin() resolves a hostname through the injected resolver exactly once,
    classifies EVERY returned address, and caches the single validated
    address the connection must use. The pinned network backend below calls
    pin() at connect time and connects to exactly what it returns, so the
    address that was validated is the address that is dialed: a rebinding
    record cannot swap in an internal address between check and connect.

    classifier is injectable ONLY so the hermetic pinning test can prove
    the plumbing against a loopback server; production code never passes
    it and always classifies with _address_blocked.
    """

    def __init__(
        self,
        resolver: Resolver,
        classifier: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] | None = None,
    ) -> None:
        self._resolver = resolver
        self._classifier = classifier if classifier is not None else _address_blocked
        self._pinned: dict[str, str] = {}
        self._blocked: dict[str, str] = {}

    @staticmethod
    def _normalize(host: str) -> str:
        return host.strip().lower().rstrip(".").strip("[]")

    def quick_block_reason(self, host: str) -> str | None:
        """DNS-free refusals: metadata hostnames and blocked IP literals."""
        normalized = self._normalize(host)
        if not normalized:
            return "an empty host"
        if normalized in BLOCKED_METADATA_HOSTNAMES:
            return "a cloud metadata hostname"
        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            return None
        if self._classifier(literal):
            return "a blocked address range"
        return None

    def pin(self, host: str) -> str:
        """The one validated address for host; connections must use it.

        Raises _BlockedTargetError when the host is refused and
        _SiteTransportError when it cannot be resolved at all.
        """
        normalized = self._normalize(host)
        quick = self.quick_block_reason(host)
        if quick is not None:
            raise _BlockedTargetError(quick)
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            return normalized  # a permitted IP literal pins to itself
        if normalized in self._blocked:
            raise _BlockedTargetError(self._blocked[normalized])
        if normalized in self._pinned:
            return self._pinned[normalized]
        try:
            resolved = self._resolver(normalized)
        except OSError as error:
            raise _SiteTransportError(error) from error
        addresses = []
        for raw in resolved:
            try:
                addresses.append(ipaddress.ip_address(raw.split("%", 1)[0]))
            except ValueError:
                continue
        if not addresses:
            raise _SiteTransportError(OSError(f"{normalized} resolved to no usable address"))
        if any(self._classifier(address) for address in addresses):
            # One blocked A record blocks the host: a resolver that mixes
            # public and internal addresses must not be connectable at all.
            self._blocked[normalized] = "a blocked address range"
            raise _BlockedTargetError(self._blocked[normalized])
        pinned = str(addresses[0])
        self._pinned[normalized] = pinned
        return pinned

    def block_reason(self, host: str) -> str | None:
        """pin() as a query: the refusal reason, or None when connectable.

        Raises _SiteTransportError when the host cannot be resolved.
        """
        try:
            self.pin(host)
        except _BlockedTargetError as blocked:
            return blocked.reason
        return None


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    """httpcore backend that dials only pinner-validated addresses.

    httpcore hands connect_tcp the ORIGIN HOSTNAME and separately uses that
    hostname for the Host header and TLS SNI (server_hostname), so
    substituting the pinned address here changes only where the TCP
    connection goes: name-based virtual hosts and certificate validation
    keep working against the original name.
    """

    def __init__(self, pinner: _HostPinner) -> None:
        self._pinner = pinner
        self._inner = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        address = self._pinner.pin(host)
        return self._inner.connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:  # pragma: no cover - never used by the site stage
        raise _BlockedTargetError("a unix socket target")

    def sleep(self, seconds: float) -> None:  # pragma: no cover - retry plumbing
        self._inner.sleep(seconds)


class _PinnedTransport(httpx.HTTPTransport):
    """httpx transport whose connections go through _PinnedNetworkBackend.

    httpx does not expose the network backend, so this reaches into the
    transport's connection pool to install it. The isinstance guard fails
    closed if httpx internals ever change shape: the site stage must not
    run without connection pinning, and the hermetic pinning tests exercise
    this wiring against a real socket. Extra keyword arguments are
    forwarded to HTTPTransport so a test can pass verify=; production
    construction keeps the default verify-on against system CAs.
    """

    def __init__(self, pinner: _HostPinner, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        pool = getattr(self, "_pool", None)
        if not isinstance(pool, httpcore.ConnectionPool):
            raise RuntimeError(
                "httpx internals changed: the pinned network backend cannot be"
                " installed, so the site stage refuses to run without its"
                " anti-rebinding guard"
            )
        pool._network_backend = _PinnedNetworkBackend(pinner)


class _SiteFetcher:
    """httpx wrapper: capped streaming reads, modest retries, manual
    redirects, and pinned host resolution on every connection."""

    def __init__(
        self,
        timeout_seconds: float,
        max_bytes: int,
        now: Callable[[], datetime],
        sleeper: Callable[[float], None] | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._now = now
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._pinner = _HostPinner(resolver if resolver is not None else system_resolver)
        # trust_env=False: environment proxy mounts would route requests
        # around the pinned transport, and the site trust boundary requires
        # direct, pinned connections.
        self._http = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=_PinnedTransport(self._pinner),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"},
        )

    def close(self) -> None:
        self._http.close()

    def host_block_reason(self, url: str) -> str | None:
        """Blocked-host reason for this URL's host, or None when connectable.

        Delegates to the pinner, so the address this validates is the
        address the transport will actually dial. Raises _SiteTransportError
        when the host cannot be resolved at all.
        """
        return self._pinner.block_reason(urlsplit(url).hostname or "")

    def _get_once(self, url: str) -> _CappedResponse:
        try:
            with self._http.stream("GET", url) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableSiteError(response.status_code)
                if 300 <= response.status_code < 400:
                    # Redirects are never followed here; the manual loop in
                    # fetch() decides. The redirect body is never read.
                    return _CappedResponse(
                        status=response.status_code,
                        body=b"",
                        truncated=False,
                        location=response.headers.get("location"),
                    )
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
                )
        except httpx.TransportError as error:
            raise _SiteTransportError(error) from error

    def get(self, url: str) -> _CappedResponse:
        """One GET with retries on transport failures, 429, and 5xx.

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

    def fetch(self, url: str) -> _FetchOutcome:
        """GET with manual redirect handling, at most MAX_REDIRECT_HOPS hops.

        Raises _BlockedTargetError when the STARTING host is blocked (it is
        never connected). A refused redirect comes back as a blocked
        outcome: the chain records what was requested, and the refused
        target is named but never fetched. The pre-checks here are the
        polite refusals; the pinned transport re-enforces the same pinner
        verdict at connect time on every hop, so nothing depends on this
        method being called first.
        """
        reason = self.host_block_reason(url)
        if reason is not None:
            raise _BlockedTargetError(reason)
        chain = [url]
        current = url
        for hop in range(MAX_REDIRECT_HOPS + 1):
            try:
                response = self.get(current)
            except _BlockedTargetError as blocked:
                # Connect-time enforcement fired (the pre-check shares the
                # pinner cache, so this is a belt, not the usual path).
                if hop == 0:
                    raise
                return _FetchOutcome(
                    chain=chain,
                    blocked=_BlockedRedirect(
                        target=current,
                        reason=f"the redirect target resolves to {blocked.reason}",
                    ),
                )
            location = (response.location or "").strip()
            if not (300 <= response.status < 400 and location):
                return _FetchOutcome(chain=chain, response=response)
            target = urljoin(current, location)
            if hop == MAX_REDIRECT_HOPS:
                return _FetchOutcome(
                    chain=chain,
                    blocked=_BlockedRedirect(
                        target=target,
                        reason=f"the redirect chain exceeded {MAX_REDIRECT_HOPS} hops",
                    ),
                )
            allowed, refusal = _hop_allowed(current, target)
            if not allowed:
                return _FetchOutcome(
                    chain=chain, blocked=_BlockedRedirect(target=target, reason=refusal)
                )
            block = self.host_block_reason(target)
            if block is not None:
                return _FetchOutcome(
                    chain=chain,
                    blocked=_BlockedRedirect(
                        target=target, reason=f"the redirect target resolves to {block}"
                    ),
                )
            chain.append(target)
            current = target
        raise AssertionError("unreachable: the hop loop always returns")

    def record(
        self,
        url: str,
        response: _CappedResponse,
        text: str,
        kind: str,
        chain: list[str] | None = None,
    ) -> FetchRecord:
        body = {
            "kind": kind,
            "url": url,
            "status": response.status,
            "truncated": response.truncated,
            "text": text,
        }
        if chain and len(chain) > 1:
            # Same-host redirects (https upgrades and www variants are
            # routine) are followed manually; the chain shows where the
            # bytes actually came from.
            body["redirect_chain"] = list(chain)
            body["final_url"] = chain[-1]
        return FetchRecord(
            url=url,
            params={},
            status=response.status,
            body=body,
            retrieved_at=self._now(),
        )

    def blocked_redirect_record(self, url: str, outcome: _FetchOutcome, kind: str) -> FetchRecord:
        """Audit record for a refused redirect: the chain, never the body."""
        assert outcome.blocked is not None
        return FetchRecord(
            url=url,
            params={},
            status=0,
            body={
                "kind": kind,
                "url": url,
                "redirect_chain": list(outcome.chain),
                "blocked_target": outcome.blocked.target,
                "reason": outcome.blocked.reason,
            },
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
    posture; see the module docstring. A blocked site host and a robots
    redirect off the host both mean permission could not be established:
    fetch nothing.
    """
    robots_url = f"{base}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        outcome = fetcher.fetch(robots_url)
    except _BlockedTargetError as blocked:
        result.failures.append(
            f"the site host is {blocked.reason}; nothing was fetched from this site"
        )
        result.records.append(
            NamedRecord(
                "site_robots", fetcher.error_record(robots_url, blocked.reason, "site_blocked")
            )
        )
        return None
    except _SiteTransportError as error:
        result.failures.append(f"robots.txt was unreachable ({error}); nothing was fetched")
        result.records.append(
            NamedRecord("site_robots", fetcher.error_record(robots_url, str(error), "site_robots"))
        )
        return None
    if outcome.blocked is not None:
        result.failures.append(
            f"robots.txt redirected and the redirect was refused ({outcome.blocked.reason});"
            " treated as disallow-all"
        )
        result.blocked_redirects.append(f"/robots.txt: {outcome.blocked.reason}")
        result.records.append(
            NamedRecord(
                "site_robots",
                fetcher.blocked_redirect_record(robots_url, outcome, "site_blocked_redirect"),
            )
        )
        return None
    response = outcome.response
    assert response is not None
    text = _decode(response.body)
    result.records.append(
        NamedRecord(
            "site_robots",
            fetcher.record(robots_url, response, text, "site_robots", chain=outcome.chain),
        )
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
    resolver: Resolver | None = None,
) -> SiteFetchResult:
    """Fetch the entry page plus up to 2 discovered about pages, politely."""
    clean_url = validate_site_url(site_url)
    clock = now or (lambda: datetime.now(UTC))
    result = SiteFetchResult(site_url=clean_url)
    parts = urlsplit(clean_url)
    base = f"{parts.scheme}://{parts.netloc}"

    fetcher = _SiteFetcher(timeout_seconds, max_response_bytes, clock, sleeper, resolver)
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
                outcome = fetcher.fetch(url)
            except _BlockedTargetError as blocked:
                result.failures.append(f"{path}: the host is {blocked.reason}; not fetched")
                page_number += 1
                result.records.append(
                    NamedRecord(
                        f"site_{page_number:03d}",
                        fetcher.error_record(url, blocked.reason, "site_blocked"),
                    )
                )
                return None
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
            if outcome.blocked is not None:
                result.failures.append(f"{path}: blocked redirect ({outcome.blocked.reason})")
                result.blocked_redirects.append(f"{path}: {outcome.blocked.reason}")
                page_number += 1
                result.records.append(
                    NamedRecord(
                        f"site_{page_number:03d}",
                        fetcher.blocked_redirect_record(url, outcome, "site_blocked_redirect"),
                    )
                )
                return None
            response = outcome.response
            assert response is not None
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
                    fetcher.record(url, response, text, "site_text", chain=outcome.chain),
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

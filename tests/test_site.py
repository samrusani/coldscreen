"""Site fetching: robots discipline, discovery, size cap, html-to-text, and
the trust boundary (userinfo, blocked address ranges, manual redirects,
connection pinning against DNS rebinding).

Most tests run against respx; an unmocked request fails the test, which is
itself part of the property under test (for example: a robots-disallowed
path, or a cross-host redirect target, must never be fetched at all).
Resolution is always a fake resolver: no real DNS in tests, enforced by the
conftest autouse guard. The pinning tests at the bottom run against a real
loopback HTTP server with sockets re-enabled for 127.0.0.1 only, because
the property under test lives below the mock layer: the TCP connection
must go to the address the pinner validated, never to a second resolution.
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import httpx
import pytest
import respx

from coldscreen.site import (
    Resolver,
    SiteError,
    SiteFetchResult,
    _BlockedTargetError,
    _HostPinner,
    _PinnedTransport,
    fetch_site,
    html_to_text,
    validate_site_url,
)

from .conftest import PUBLIC_TEST_ADDRESS

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
BASE = "https://widgets.example"

HOME_HTML = """<!DOCTYPE html>
<html>
<head><title>Widgets</title><style>.x { color: red }</style>
<script>var tracker = "never text";</script></head>
<body>
<header>Header chrome</header>
<nav><a href="/">Home</a> <a href="/about-us">About us</a> <a href="/team">Team</a>
<a href="/company/history">Company history</a> <a href="/pricing">Pricing</a>
<a href="https://elsewhere.example/about">External about</a></nav>
<main>
<h1>Fabricated Widgets</h1>
<p>We make entirely fictional widgets &amp; parts.</p>
</main>
<aside>Sidebar noise</aside>
<footer>Footer chrome</footer>
</body>
</html>
"""

ABOUT_HTML = "<html><body><main><h1>About us</h1><p>Founded long ago.</p></main></body></html>"
TEAM_HTML = "<html><body><main><p>A team page.</p></main></body></html>"


def public_resolver(host: str) -> list[str]:
    return [PUBLIC_TEST_ADDRESS]


def fetch(
    url: str = BASE,
    max_bytes: int = 2_000_000,
    resolver: Resolver | None = None,
) -> SiteFetchResult:
    return fetch_site(
        url,
        timeout_seconds=5.0,
        max_response_bytes=max_bytes,
        now=lambda: NOW,
        sleeper=lambda _s: None,
        resolver=resolver or public_resolver,
    )


# -- URL validation ---------------------------------------------------------------


def test_http_and_https_pass_validation() -> None:
    assert validate_site_url("https://widgets.example") == "https://widgets.example"
    assert validate_site_url(" http://widgets.example/page ") == "http://widgets.example/page"


@pytest.mark.parametrize(
    "bad", ["ftp://widgets.example", "file:///etc/hosts", "javascript:alert(1)", "widgets.example"]
)
def test_other_schemes_are_rejected(bad: str) -> None:
    with pytest.raises(SiteError, match="http or https"):
        validate_site_url(bad)


def test_url_without_host_is_rejected() -> None:
    with pytest.raises(SiteError, match="no host"):
        validate_site_url("https://")


@pytest.mark.parametrize(
    "bad",
    [
        "https://user:secret@widgets.example/",
        "https://user@widgets.example/",
        "http://:password@widgets.example/",
    ],
)
def test_urls_with_userinfo_are_rejected_at_validation(bad: str) -> None:
    with pytest.raises(SiteError, match="userinfo"):
        validate_site_url(bad)


# -- html to text -------------------------------------------------------------------


def test_html_to_text_strips_chrome_and_keeps_prose() -> None:
    text = html_to_text(HOME_HTML)
    assert "Fabricated Widgets" in text
    assert "entirely fictional widgets & parts" in text  # entity decoded
    for chrome in ("Header chrome", "Footer chrome", "Sidebar noise", "never text", "color: red"):
        assert chrome not in text
    assert "About us" not in text  # nav text is stripped too


def test_html_to_text_separates_blocks() -> None:
    text = html_to_text("<p>one</p><p>two</p>")
    assert text.splitlines() == ["one", "two"]


# -- fetching and discovery -----------------------------------------------------------


def test_fetches_homepage_and_two_about_pages(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(200, text="User-agent: *\nAllow: /\n")
    respx_mock.get(f"{BASE}/").respond(200, text=HOME_HTML)
    about = respx_mock.get(f"{BASE}/about-us").respond(200, text=ABOUT_HTML)
    team = respx_mock.get(f"{BASE}/team").respond(200, text=TEAM_HTML)
    # A third candidate exists (/company/history) but the limit is 2.
    result = fetch()
    assert [p.path for p in result.pages] == ["/", "/about-us", "/team"]
    assert [p.source_label for p in result.pages] == ["site /", "site /about-us", "site /team"]
    assert about.call_count == 1
    assert team.call_count == 1
    assert result.robots_skipped == []
    assert result.failures == []
    assert result.blocked_redirects == []
    # Evidence: robots plus three pages, in fetch order.
    assert [n.name for n in result.records] == ["site_robots", "site_001", "site_002", "site_003"]
    page_record = result.records[1].record
    assert page_record.body["kind"] == "site_text"
    assert "Fabricated Widgets" in page_record.body["text"]


def test_discovery_ignores_other_hosts_and_non_about_paths(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(
        200,
        text=(
            '<a href="https://elsewhere.example/about">x</a>'
            '<a href="/pricing">y</a><a href="/about">z</a>'
        ),
    )
    respx_mock.get(f"{BASE}/about").respond(200, text=ABOUT_HTML)
    result = fetch()
    assert [p.path for p in result.pages] == ["/", "/about"]


def test_robots_disallowed_paths_are_skipped_and_recorded(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /team\nDisallow: /company\n"
    )
    respx_mock.get(f"{BASE}/").respond(200, text=HOME_HTML)
    about = respx_mock.get(f"{BASE}/about-us").respond(200, text=ABOUT_HTML)
    # No routes for /team or /company/history: fetching them would fail the
    # test. That absence IS the assertion that robots is respected.
    result = fetch()
    assert [p.path for p in result.pages] == ["/", "/about-us"]
    assert result.robots_skipped == ["/team", "/company/history"]
    assert about.call_count == 1


def test_robots_404_means_everything_is_allowed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(200, text="<p>hello</p>")
    result = fetch()
    assert [p.path for p in result.pages] == ["/"]


def test_robots_403_means_nothing_is_fetched(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(403)
    result = fetch()
    assert result.pages == []
    assert result.robots_skipped == ["/"]
    assert any("403" in note for note in result.failures)
    assert [n.name for n in result.records] == ["site_robots"]


def test_robots_5xx_after_retries_means_nothing_is_fetched(
    respx_mock: respx.MockRouter,
) -> None:
    robots = respx_mock.get(f"{BASE}/robots.txt").respond(503)
    result = fetch()
    assert robots.call_count == 3  # retried, then given up
    assert result.pages == []
    assert result.robots_skipped == ["/"]


def test_unreachable_robots_means_nothing_is_fetched(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").mock(side_effect=httpx.ConnectError("refused"))
    result = fetch()
    assert result.pages == []
    assert any("unreachable" in note for note in result.failures)
    assert result.records[0].record.status == 0
    assert "error" in result.records[0].record.body


def test_homepage_non_200_is_recorded_not_raised(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(404, text="gone")
    result = fetch()
    assert result.pages == []
    assert any("HTTP 404" in note for note in result.failures)
    # The 404 response itself is persisted for the audit pack.
    assert result.records[1].record.status == 404


def test_homepage_transport_error_is_recorded_not_raised(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("refused"))
    result = fetch()
    assert result.pages == []
    assert any("unreachable" in note for note in result.failures)
    assert result.records[1].record.status == 0


def test_response_size_cap_truncates_the_read(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    body = "<p>" + ("widget " * 4000) + "</p>"
    respx_mock.get(f"{BASE}/").respond(200, text=body)
    result = fetch(max_bytes=1000)
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.truncated is True
    assert len(page.text) <= 1000


def test_entry_url_with_a_path_is_respected(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/en/home").respond(200, text='<a href="/en/about">About</a><p>x</p>')
    respx_mock.get(f"{BASE}/en/about").respond(200, text=ABOUT_HTML)
    result = fetch(f"{BASE}/en/home")
    assert [p.path for p in result.pages] == ["/en/home", "/en/about"]
    assert result.pages[0].source_label == "site /en/home"


# -- blocked hosts: resolution happens before every connection -----------------------


@pytest.mark.parametrize(
    ("label", "address"),
    [
        ("loopback", "127.0.0.1"),
        ("rfc1918 10/8", "10.1.2.3"),
        ("rfc1918 172.16/12", "172.16.9.9"),
        ("rfc1918 192.168/16", "192.168.1.10"),
        ("link-local", "169.254.10.10"),
        ("metadata ip", "169.254.169.254"),
        ("cgnat", "100.64.0.7"),
        ("unspecified", "0.0.0.0"),
        ("v6 loopback", "::1"),
        ("v6 unique-local", "fc00::1"),
        ("v6 link-local", "fe80::1"),
        ("v6 site-local", "fec0::1"),
        ("v6 unspecified", "::"),
        ("v4-mapped v6 loopback", "::ffff:127.0.0.1"),
        ("v4-mapped v6 rfc1918", "::ffff:10.0.0.5"),
    ],
)
def test_hosts_resolving_to_blocked_ranges_are_never_fetched(
    respx_mock: respx.MockRouter, label: str, address: str
) -> None:
    """No route is registered: any HTTP request at all fails the test."""
    result = fetch(resolver=lambda host: [address])
    assert result.pages == []
    assert result.robots_skipped == ["/"]
    assert any("blocked address range" in note for note in result.failures), label
    assert [n.name for n in result.records] == ["site_robots"]
    assert result.records[0].record.body["kind"] == "site_blocked"


def test_mixed_resolution_with_one_blocked_address_blocks_the_host(
    respx_mock: respx.MockRouter,
) -> None:
    """A resolver answer mixing public and internal addresses blocks the
    host outright: DNS games must not open an internal path."""
    result = fetch(resolver=lambda host: [PUBLIC_TEST_ADDRESS, "10.0.0.8"])
    assert result.pages == []
    assert any("blocked address range" in note for note in result.failures)


def test_public_resolution_is_fetched(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(200, text="<p>hello</p>")
    result = fetch(resolver=lambda host: [PUBLIC_TEST_ADDRESS])
    assert [p.path for p in result.pages] == ["/"]


def test_ip_literal_urls_are_checked_without_resolution(respx_mock: respx.MockRouter) -> None:
    def no_resolution(host: str) -> list[str]:
        raise AssertionError("an IP literal must never be resolved")

    result = fetch("http://192.168.7.7/", resolver=no_resolution)
    assert result.pages == []
    assert any("blocked address range" in note for note in result.failures)


@pytest.mark.parametrize("host", ["metadata.google.internal", "metadata", "instance-data"])
def test_literal_metadata_hostnames_are_rejected(respx_mock: respx.MockRouter, host: str) -> None:
    def no_resolution(hostname: str) -> list[str]:
        raise AssertionError("a metadata hostname must never be resolved")

    result = fetch(f"http://{host}/latest/meta-data/", resolver=no_resolution)
    assert result.pages == []
    assert any("metadata hostname" in note for note in result.failures)


def test_unresolvable_host_is_a_failure_not_a_fetch(respx_mock: respx.MockRouter) -> None:
    def failing(host: str) -> list[str]:
        raise OSError("no such host")

    result = fetch(resolver=failing)
    assert result.pages == []
    assert any("unreachable" in note for note in result.failures)


# -- manual redirects: at most 3 hops, same host only ---------------------------------


def test_same_host_redirect_is_followed_and_the_chain_recorded(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": f"{BASE}/landed"})
    respx_mock.get(f"{BASE}/landed").respond(200, text="<p>landed</p>")
    result = fetch()
    assert [p.path for p in result.pages] == ["/"]
    assert result.pages[0].text == "landed"
    record = result.records[1].record
    assert record.body["redirect_chain"] == [BASE, f"{BASE}/landed"]
    assert record.body["final_url"] == f"{BASE}/landed"


def test_http_to_https_upgrade_redirect_is_followed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("http://widgets.example/robots.txt").respond(404)
    respx_mock.get("http://widgets.example/").respond(
        301, headers={"location": "https://widgets.example/"}
    )
    respx_mock.get("https://widgets.example/").respond(200, text="<p>secure</p>")
    result = fetch("http://widgets.example/")
    assert [p.path for p in result.pages] == ["/"]
    assert result.pages[0].text == "secure"


def test_www_toggle_redirect_is_followed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": "https://www.widgets.example/"})
    respx_mock.get("https://www.widgets.example/").respond(200, text="<p>www</p>")
    result = fetch()
    assert [p.path for p in result.pages] == ["/"]
    assert result.pages[0].text == "www"


def test_cross_host_redirect_is_blocked_and_never_fetched(
    respx_mock: respx.MockRouter,
) -> None:
    """No route for the landing host: fetching it would fail the test. The
    evidence records the chain and the refused target, never a body."""
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(302, headers={"location": "https://elsewhere.example/"})
    result = fetch()
    assert result.pages == []
    assert result.blocked_redirects == ["/: the redirect leaves the validated host"]
    record = result.records[1].record
    assert record.body["kind"] == "site_blocked_redirect"
    assert record.body["redirect_chain"] == [BASE]
    assert record.body["blocked_target"] == "https://elsewhere.example/"
    assert "text" not in record.body


def test_https_to_http_downgrade_redirect_is_blocked(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": "http://widgets.example/"})
    result = fetch()
    assert result.pages == []
    assert any("scheme" in entry for entry in result.blocked_redirects)


def test_redirect_changing_the_port_is_blocked(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": f"{BASE}:8443/"})
    result = fetch()
    assert result.pages == []
    assert any("port" in entry for entry in result.blocked_redirects)


def test_redirect_with_userinfo_is_blocked(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(
        301, headers={"location": "https://user:pw@widgets.example/"}
    )
    result = fetch()
    assert result.pages == []
    assert any("userinfo" in entry for entry in result.blocked_redirects)


def test_redirect_to_a_blocked_address_is_refused_before_connecting(
    respx_mock: respx.MockRouter,
) -> None:
    """Same-host redirect, but the second resolution flips to an internal
    address (a rebinding shape). Resolution is cached per fetcher, so the
    flip is simulated with an IP-literal target instead; the internal IP
    has no route, so any fetch attempt fails the test."""
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": "https://169.254.169.254/"})
    result = fetch()
    assert result.pages == []
    # Cross-host anyway, so the hop rule fires first; the point under test
    # is that nothing was fetched and the refusal is recorded.
    assert result.blocked_redirects


def test_redirect_chain_beyond_three_hops_is_refused(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": f"{BASE}/a"})
    respx_mock.get(f"{BASE}/a").respond(301, headers={"location": f"{BASE}/b"})
    respx_mock.get(f"{BASE}/b").respond(301, headers={"location": f"{BASE}/c"})
    hop4 = respx_mock.get(f"{BASE}/c").respond(301, headers={"location": f"{BASE}/d"})
    result = fetch()
    assert hop4.call_count == 1
    assert result.pages == []
    assert result.blocked_redirects == ["/: the redirect chain exceeded 3 hops"]
    record = result.records[1].record
    assert record.body["redirect_chain"] == [BASE, f"{BASE}/a", f"{BASE}/b", f"{BASE}/c"]


def test_three_hop_chain_to_a_terminal_page_succeeds(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": f"{BASE}/a"})
    respx_mock.get(f"{BASE}/a").respond(301, headers={"location": f"{BASE}/b"})
    respx_mock.get(f"{BASE}/b").respond(301, headers={"location": f"{BASE}/c"})
    respx_mock.get(f"{BASE}/c").respond(200, text="<p>done</p>")
    result = fetch()
    assert [p.path for p in result.pages] == ["/"]
    assert result.pages[0].text == "done"
    assert result.records[1].record.body["redirect_chain"] == [
        BASE,
        f"{BASE}/a",
        f"{BASE}/b",
        f"{BASE}/c",
    ]


def test_robots_redirecting_off_host_means_nothing_is_fetched(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(
        301, headers={"location": "https://elsewhere.example/robots.txt"}
    )
    result = fetch()
    assert result.pages == []
    assert result.robots_skipped == ["/"]
    assert any("disallow-all" in note for note in result.failures)
    assert result.records[0].record.body["kind"] == "site_blocked_redirect"


# -- connection pinning: check and connect share one resolution -----------------------
#
# These tests are hermetic but real: a loopback HTTP server, sockets
# re-enabled for 127.0.0.1 only (any other connect is refused by
# pytest-socket before a packet leaves), and injected resolvers. They prove
# the F1 property below the respx mock layer, where the TOCTOU lived.


class _RecordingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        server = cast("_LoopbackServer", self.server)
        server.hits.append((self.headers.get("Host"), self.path))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(server.body)))
        self.end_headers()
        self.wfile.write(server.body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class _LoopbackServer(ThreadingHTTPServer):
    def __init__(self, body: bytes) -> None:
        super().__init__(("127.0.0.1", 0), _RecordingHandler)
        self.body = body
        self.hits: list[tuple[str | None, str]] = []


def test_connect_time_enforcement_blocks_a_host_the_precheck_never_saw() -> None:
    """The pinner verdict is enforced at connect, not only at the polite
    pre-check: a request straight through the transport is refused before
    any socket exists (this test runs with sockets disabled)."""
    pinner = _HostPinner(resolver=lambda host: ["127.0.0.1"])
    client = httpx.Client(transport=_PinnedTransport(pinner), timeout=2.0)
    try:
        with pytest.raises(_BlockedTargetError, match="blocked address range"):
            client.get("http://sneaky.example/")
    finally:
        client.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_pinned_transport_dials_the_validated_address_and_keeps_the_host_header() -> None:
    """Positive pinning proof: the resolver's answer is the address the
    socket dials, the URL hostname is never system-resolved, and the Host
    header still carries the original name for virtual hosting. The
    permissive classifier is the module-private test seam that lets the
    validated address be loopback; policy classification is covered by the
    blocked-class tests above."""
    server = _LoopbackServer(b"PINNED-BODY-OK")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    resolver_calls: list[str] = []

    def resolver(host: str) -> list[str]:
        resolver_calls.append(host)
        return ["127.0.0.1"]

    getaddrinfo_hosts: list[object] = []
    real_getaddrinfo = socket.getaddrinfo

    def spying_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        getaddrinfo_hosts.append(host)
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    pinner = _HostPinner(resolver=resolver, classifier=lambda address: False)
    client = httpx.Client(transport=_PinnedTransport(pinner), timeout=5.0)
    try:
        socket.getaddrinfo = spying_getaddrinfo  # type: ignore[assignment]
        response = client.get(f"http://pinned-host.example:{port}/hello")
        # Two requests over one pinned connection: resolution stays single.
        second = client.get(f"http://pinned-host.example:{port}/again")
    finally:
        socket.getaddrinfo = real_getaddrinfo
        client.close()
        server.shutdown()
        server.server_close()
    assert response.status_code == 200
    assert response.text == "PINNED-BODY-OK"
    assert second.status_code == 200
    # The socket went to the resolver's answer; the name went in the Host
    # header, exactly as a name-based virtual host needs.
    assert server.hits == [
        (f"pinned-host.example:{port}", "/hello"),
        (f"pinned-host.example:{port}", "/again"),
    ]
    # One resolution, shared by check and connect; never a system lookup.
    assert resolver_calls == ["pinned-host.example"]
    assert "pinned-host.example" not in getaddrinfo_hosts


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dns_rebinding_cannot_deliver_an_internal_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's TOCTOU attack, replayed against the fix. The injected
    resolver answers PUBLIC first and loopback on any later call; the
    SYSTEM resolver is rigged to answer loopback for the same host, which
    is exactly what the vulnerable code consumed at connect time. With
    check and connect sharing one pinned resolution, every dial must go to
    the validated public address and the internal server must never be
    touched: zero hits, no page, no internal byte in any persisted record.
    A create_connection guard refuses non-loopback dials deterministically
    (no packet ever leaves the machine), so the run completes gracefully
    with the failure recorded, mirroring a dead public host."""
    secret = b"INTERNAL-SECRET-BODY"
    server = _LoopbackServer(secret)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    attack_host = "rebind-attack.example"
    resolver_calls: list[str] = []

    def flipping_resolver(host: str) -> list[str]:
        resolver_calls.append(host)
        if len(resolver_calls) == 1:
            return [PUBLIC_TEST_ADDRESS]  # passes classification
        return ["127.0.0.1"]  # the rebound answer: the internal target

    getaddrinfo_hosts: list[object] = []
    real_getaddrinfo = socket.getaddrinfo

    def rebinding_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        getaddrinfo_hosts.append(host)
        if host == attack_host:
            # What the vulnerable code would have consumed at connect time.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    dialed: list[object] = []
    real_create_connection = socket.create_connection

    def guarded_create_connection(
        address: tuple[object, object], *args: object, **kwargs: object
    ) -> socket.socket:
        dialed.append(address[0])
        if address[0] != "127.0.0.1":
            raise ConnectionRefusedError(f"test guard: refusing non-loopback dial to {address[0]}")
        return real_create_connection(address, *args, **kwargs)  # type: ignore[arg-type,unused-ignore]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    try:
        result = fetch_site(
            f"http://{attack_host}:{port}/",
            timeout_seconds=2.0,
            max_response_bytes=100_000,
            now=lambda: NOW,
            sleeper=lambda _s: None,
            resolver=flipping_resolver,
        )
    finally:
        server.shutdown()
        server.server_close()
    # Every dial went to the validated pinned address; loopback was never
    # dialed, so the transport connected to what was checked, not to a
    # re-resolution.
    assert dialed, "the transport never attempted a connection"
    assert set(dialed) == {PUBLIC_TEST_ADDRESS}
    # One resolution total: the rebound second answer was never asked for,
    # and the hostname was never handed to the system resolver.
    assert resolver_calls == [attack_host]
    assert attack_host not in getaddrinfo_hosts
    # The internal server was never touched and its body reached nothing.
    assert server.hits == []
    assert result.pages == []
    assert any("unreachable" in note for note in result.failures)
    persisted = json.dumps([named.record.body for named in result.records]).encode("utf-8")
    assert secret not in persisted
    assert b"127.0.0.1" not in persisted

"""Site fetching: robots discipline, discovery, size cap, html-to-text.

Every test runs against respx; an unmocked request fails the test, which is
itself part of the property under test (for example: a robots-disallowed
path must never be fetched at all).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from coldscreen.site import (
    SiteError,
    SiteFetchResult,
    fetch_site,
    html_to_text,
    validate_site_url,
)

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


def fetch(url: str = BASE, max_bytes: int = 2_000_000) -> SiteFetchResult:
    return fetch_site(
        url,
        timeout_seconds=5.0,
        max_response_bytes=max_bytes,
        now=lambda: NOW,
        sleeper=lambda _s: None,
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


def test_redirect_landing_url_is_recorded(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/robots.txt").respond(404)
    respx_mock.get(f"{BASE}/").respond(301, headers={"location": f"{BASE}/landed"})
    respx_mock.get(f"{BASE}/landed").respond(200, text="<p>landed</p>")
    result = fetch()
    assert [p.path for p in result.pages] == ["/"]
    record = result.records[1].record
    assert record.body.get("final_url") == f"{BASE}/landed"

"""HTTP cache behavior: keying, TTL, and what never gets stored."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from firstpass.http_cache import HttpCache, cache_key

RETRIEVED = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
URL = "https://api.company-information.service.gov.uk/company/99999999"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_cache(tmp_path: Path, ttl_seconds: float = 604800.0) -> tuple[HttpCache, FakeClock]:
    clock = FakeClock()
    cache = HttpCache(tmp_path / "cache.sqlite3", ttl_seconds=ttl_seconds, clock=clock)
    return cache, clock


def test_put_get_roundtrip(tmp_path: Path) -> None:
    cache, _clock = make_cache(tmp_path)
    cache.put(URL, {"items_per_page": "100"}, 200, '{"a": 1}', RETRIEVED)
    hit = cache.get(URL, {"items_per_page": "100"})
    assert hit is not None
    assert hit.body == '{"a": 1}'
    assert hit.status == 200
    assert hit.retrieved_at == RETRIEVED


def test_expired_entries_are_misses(tmp_path: Path) -> None:
    cache, clock = make_cache(tmp_path, ttl_seconds=100.0)
    cache.put(URL, None, 200, "{}", RETRIEVED)
    clock.now += 99.0
    assert cache.get(URL) is not None
    clock.now += 2.0
    assert cache.get(URL) is None


def test_params_are_part_of_the_key(tmp_path: Path) -> None:
    cache, _clock = make_cache(tmp_path)
    cache.put(URL, {"start_index": "0"}, 200, "page0", RETRIEVED)
    cache.put(URL, {"start_index": "100"}, 200, "page1", RETRIEVED)
    first = cache.get(URL, {"start_index": "0"})
    second = cache.get(URL, {"start_index": "100"})
    assert first is not None and first.body == "page0"
    assert second is not None and second.body == "page1"
    assert cache.get(URL) is None


def test_param_order_does_not_change_the_key() -> None:
    assert cache_key(URL, {"a": "1", "b": "2"}) == cache_key(URL, {"b": "2", "a": "1"})


def test_non_200_responses_are_not_cached(tmp_path: Path) -> None:
    cache, _clock = make_cache(tmp_path)
    cache.put(URL, None, 404, "not found", RETRIEVED)
    cache.put(URL, None, 500, "server error", RETRIEVED)
    assert cache.get(URL) is None


def test_cache_persists_across_instances(tmp_path: Path) -> None:
    cache = HttpCache(tmp_path / "cache.sqlite3")
    cache.put(URL, None, 200, "persisted", RETRIEVED)
    cache.close()
    reopened = HttpCache(tmp_path / "cache.sqlite3")
    hit = reopened.get(URL)
    assert hit is not None and hit.body == "persisted"
    reopened.close()


def test_corrupt_cache_file_is_recreated_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt cache warns, recreates itself, and keeps working."""
    path = tmp_path / "cache.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all")
    cache = HttpCache(path)
    warning = capsys.readouterr().err
    assert "recreating" in warning
    assert str(path) in warning
    assert cache.get(URL) is None
    cache.put(URL, None, 200, "fresh", RETRIEVED)
    hit = cache.get(URL)
    assert hit is not None and hit.body == "fresh"
    cache.close()

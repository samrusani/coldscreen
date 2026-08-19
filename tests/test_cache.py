"""HTTP cache behavior: keying, TTL, and what never gets stored."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coldscreen.http_cache import (
    CacheClearRefused,
    HttpCache,
    cache_key,
    clear_http_cache,
    http_cache_stats,
)

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


def test_clear_drops_entries_and_a_following_get_is_a_miss(tmp_path: Path) -> None:
    cache, _clock = make_cache(tmp_path)
    cache.put(URL, None, 200, '{"a": 1}', RETRIEVED)
    assert cache.get(URL) is not None
    assert cache.clear() == 1
    assert cache.get(URL) is None
    cache.close()


def test_clear_missing_file_is_not_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "http_cache.sqlite3"
    result = clear_http_cache(missing)
    assert result.missing is True
    assert result.entries_removed == 0
    assert not missing.exists()
    assert not missing.parent.exists()


def test_clear_refuses_symlink_and_does_not_wipe_the_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim.sqlite3"
    cache = HttpCache(victim)
    cache.put(URL, None, 200, '{"keep": true}', RETRIEVED)
    cache.close()
    original = victim.read_bytes()

    link = tmp_path / "http_cache.sqlite3"
    try:
        link.symlink_to(victim)
    except OSError as error:
        pytest.skip(f"symlinks not available: {error}")

    with pytest.raises(CacheClearRefused) as excinfo:
        clear_http_cache(link)
    assert str(link) in str(excinfo.value)
    assert victim.read_bytes() == original
    assert link.is_symlink()
    reopened = HttpCache(victim)
    hit = reopened.get(URL)
    assert hit is not None and hit.body == '{"keep": true}'
    reopened.close()


def test_stats_reports_counts_without_payloads(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = HttpCache(path)
    cache.put(URL, {"q": "99999999"}, 200, '{"secret_body": true}', RETRIEVED)
    cache.close()
    stats = http_cache_stats(path, ttl_days=7.0)
    assert stats.exists is True
    assert stats.entry_count == 1
    assert stats.size_bytes > 0
    assert stats.ttl_days == 7.0
    assert stats.unreadable is False
    assert not hasattr(stats, "url")
    assert not hasattr(stats, "body")


def test_stats_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    stats = http_cache_stats(path, ttl_days=3.5)
    assert stats.exists is False
    assert stats.entry_count == 0
    assert stats.size_bytes == 0
    assert stats.ttl_days == 3.5
    assert stats.unreadable is False


def test_stats_corrupt_file_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all")
    stats = http_cache_stats(path, ttl_days=7.0)
    assert stats.exists is True
    assert stats.unreadable is True
    assert stats.entry_count is None
    assert stats.size_bytes == path.stat().st_size
    # stats must not recreate or delete the corrupt file
    assert path.read_bytes() == b"this is not a sqlite database at all"

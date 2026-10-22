"""Tests of the caches (domain/utils/cache_utils.py)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from domain.utils.cache_utils import DiskCache, SingleFlightCache


def test_computes_once_and_serves_from_cache():
    cache, calls = SingleFlightCache(4), []

    first = cache.get_or_compute("k", lambda: calls.append(1) or "value")
    second = cache.get_or_compute("k", lambda: calls.append(1) or "other")

    assert first == second == "value" and len(calls) == 1


def test_concurrent_requests_share_one_computation():
    cache, calls = SingleFlightCache(4), []

    def compute():
        calls.append(1)
        time.sleep(0.2)
        return object()

    with ThreadPoolExecutor(8) as executor:
        results = list(executor.map(lambda _: cache.get_or_compute("k", compute), range(8)))

    assert len(calls) == 1 and len({id(result) for result in results}) == 1


def test_failure_is_shared_but_not_cached():
    cache, calls = SingleFlightCache(4), []

    def broken():
        calls.append(1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", broken)
    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", broken)

    assert len(calls) == 2 and cache.peek("k") is None


def test_lru_eviction_and_recency():
    cache = SingleFlightCache(2)
    cache.get_or_compute("a", lambda: 1)
    cache.get_or_compute("b", lambda: 2)
    cache.peek("a")
    cache.get_or_compute("c", lambda: 3)

    assert cache.keys() == ["a", "c"]


def test_ttl_expires_entries(monkeypatch):
    cache = SingleFlightCache(4, ttl=10)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    cache.get_or_compute("k", lambda: "old")

    now[0] = 105.0
    assert cache.peek("k") == "old"
    now[0] = 200.0
    assert cache.peek("k") is None
    assert cache.get_or_compute("k", lambda: "new") == "new"


def test_load_hit_skips_compute_and_store():
    cache, stored = SingleFlightCache(4), []

    value = cache.get_or_compute("k", lambda: pytest.fail("computed"), load=lambda: "disk", store=stored.append)

    assert value == "disk" and stored == []


def test_store_failure_does_not_break_the_request():
    def store(_):
        raise OSError("disk full")

    assert SingleFlightCache(4).get_or_compute("k", lambda: 7, store=store) == 7


def test_disk_cache_roundtrip_and_disabled(tmp_path):
    cache = DiskCache(str(tmp_path / "nested"))
    cache.store(("a", 1), {"x": 1})

    assert cache.load(("a", 1)) == {"x": 1}
    assert cache.load(("a", 2)) is None
    disabled = DiskCache(None)
    disabled.store("k", {"x": 1})
    assert not disabled.enabled and disabled.load("k") is None


def test_disk_cache_ignores_corrupted_files(tmp_path):
    cache = DiskCache(str(tmp_path))
    cache.store("k", {"x": 1})
    next(tmp_path.glob("*.json")).write_text("garbage")

    assert cache.load("k") is None


def test_failed_computation_leaves_no_stale_future_for_a_new_call():
    cache = SingleFlightCache(4)
    started, release = threading.Event(), threading.Event()

    def slow_failure():
        started.set()
        release.wait(2)
        raise RuntimeError("boom")

    outcomes = []

    def call(compute):
        try:
            outcomes.append(cache.get_or_compute("k", compute))
        except RuntimeError as error:
            outcomes.append(error)

    owner = threading.Thread(target=call, args=(slow_failure,))
    owner.start()
    started.wait(2)
    waiter = threading.Thread(target=call, args=(lambda: "never",))
    waiter.start()
    time.sleep(0.1)
    release.set()
    owner.join(2)
    waiter.join(2)

    assert all(isinstance(outcome, RuntimeError) for outcome in outcomes) and len(outcomes) == 2
    assert not cache._in_flight
    assert cache.get_or_compute("k", lambda: "retry") == "retry"


def test_disk_cache_swallows_unserializable_payload_and_leaves_no_temp_file(tmp_path):
    cache = DiskCache(str(tmp_path))

    cache.store("k", {"x": object()})

    assert cache.load("k") is None
    assert list(tmp_path.iterdir()) == []

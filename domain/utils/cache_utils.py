"""Thread-safe caches: in-memory with single-flight computation, and optional on disk.

Used by the pasture service to keep expensive Earth Engine results (measured
areas, trained classifiers, the latest embedding year) for the lifetime of the
process, so that repeated or concurrent requests for the same key compute the
value only once.
"""

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Hashable, Optional


class SingleFlightCache:
    """Bounded LRU cache where concurrent requests for one key share one computation.

    The first caller of a missing key computes the value; callers that arrive
    meanwhile wait for that result (or its exception) instead of computing it
    again. Failures are not cached, so the next call retries.

    Attributes:
        max_size (int): Maximum number of entries; the least recently used is evicted.
        ttl (float, optional): Seconds an entry stays valid; None keeps it until evicted.
    """

    def __init__(self, max_size: int, ttl: Optional[float] = None):
        """Creates an empty cache.

        Args:
            max_size (int): Maximum number of entries (at least 1).
            ttl (float, optional): Entry lifetime in seconds; None for no expiry.
        """
        self.max_size = max(1, max_size)
        self.ttl = ttl
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._in_flight: dict[Hashable, Future] = {}

    def peek(self, key: Hashable) -> Any:
        """Returns the cached value of a key without computing it.

        Args:
            key (Hashable): Cache key.

        Returns:
            The value, or None when missing or expired. Refreshes its recency.
        """
        with self._lock:
            return self._get_locked(key)

    def get_or_compute(
        self,
        key: Hashable,
        compute: Callable[[], Any],
        load: Optional[Callable[[], Any]] = None,
        store: Optional[Callable[[Any], None]] = None,
    ) -> Any:
        """Returns the value of a key, computing it at most once across threads.

        Args:
            key (Hashable): Cache key.
            compute (Callable): Produces the value on a miss.
            load (Callable, optional): Secondary lookup (e.g. a disk cache)
                tried before ``compute``; returns None on a miss.
            store (Callable, optional): Called with a freshly computed value
                (e.g. to persist it); its failures are ignored.

        Returns:
            The cached, loaded or computed value.

        Raises:
            Exception: Whatever ``compute`` raised, for the owner and every waiter.

        Notes:
            ``compute`` (and ``load``) must not call ``get_or_compute`` with the
            same key: the call would wait for itself forever. Waiters have no
            timeout. A ``None`` result from ``compute`` is not distinguishable
            from a miss and is treated as one by ``peek`` and later calls.

        Side effects:
            Inserts the value, evicting the least recently used entries when full.
        """
        with self._lock:
            cached = self._get_locked(key)
            if cached is not None:
                return cached
            future = self._in_flight.get(key)
            owner = future is None
            if owner:
                future = self._in_flight[key] = Future()
        if not owner:
            return future.result()
        try:
            value = load() if load is not None else None
            fresh = value is None
            if fresh:
                value = compute()
            with self._lock:
                self._put_locked(key, value)
                self._in_flight.pop(key, None)
            if fresh and store is not None:
                try:
                    store(value)
                except Exception:
                    pass
            future.set_result(value)
            return value
        except BaseException as error:
            with self._lock:
                self._in_flight.pop(key, None)
            future.set_exception(error)
            raise

    def keys(self) -> list:
        """Returns the cached keys, oldest first (expired entries included)."""
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        """Removes every cached entry (in-flight computations are not affected)."""
        with self._lock:
            self._entries.clear()

    def _get_locked(self, key: Hashable) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self.ttl is not None and time.monotonic() - stored_at > self.ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def _put_locked(self, key: Hashable, value: Any) -> None:
        self._entries[key] = (time.monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)


class DiskCache:
    """Optional JSON cache on disk, so results survive process restarts.

    Disabled (every lookup misses, nothing is written) when no directory is
    configured. Keys are hashed into file names; writes are atomic (temporary
    file plus rename), and unreadable or corrupted files count as misses, so
    the cache can never break a request. Keys are hashed from their ``repr``,
    so they must have a stable ``repr`` across runs (tuples of strings,
    numbers and enums do; objects with memory addresses do not).
    """

    def __init__(self, directory: Optional[str], ttl: Optional[float] = None):
        """Creates the cache.

        Args:
            directory (str, optional): Cache directory (created on first write);
                None or empty disables the cache.
            ttl (float, optional): Seconds a file stays valid, judged by its modification time
                when it is read; None keeps files forever.
        """
        self.directory = Path(directory).expanduser() if directory else None
        self.ttl = ttl

    @property
    def enabled(self) -> bool:
        """True when a directory is configured."""
        return self.directory is not None

    def _path(self, key: Hashable) -> Path:
        digest = hashlib.sha256(repr(key).encode()).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def load(self, key: Hashable) -> Optional[dict]:
        """Reads the payload stored under a key.

        Args:
            key (Hashable): Cache key (any value with a stable ``repr``).

        Returns:
            dict | None: The stored payload, or None on a miss, a disabled
            cache, an expired entry (older than ``ttl``) or an unreadable file.
        """
        if not self.enabled:
            return None
        try:
            path = self._path(key)
            if self.ttl is not None and time.time() - path.stat().st_mtime > self.ttl:
                return None
            return json.loads(path.read_text(encoding="utf-8"))["payload"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def store(self, key: Hashable, payload: dict) -> None:
        """Writes a JSON-serializable payload under a key (no-op when disabled).

        Args:
            key (Hashable): Cache key.
            payload (dict): JSON-serializable value.

        Side effects:
            Creates the directory and the file; OS errors and payloads that are not
            JSON-serializable are swallowed (leftover temporary files are removed).
        """
        if not self.enabled:
            return
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self._path(key)
            temporary = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_text(json.dumps({"key": repr(key), "payload": payload}), encoding="utf-8")
            os.replace(temporary, target)
        except (OSError, TypeError, ValueError):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

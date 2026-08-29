"""A small in-process TTL cache.

Deliberately in-memory and deliberately short-lived. This service returns
other people's personal data, so the cache is a request-deduplication
device, not a datastore: nothing is written to disk, nothing survives a
restart, and entries expire in minutes.

That is also why there is no Redis here. Persisting scraped personal data
across restarts turns a demo into a database of third parties, which is a
different thing with different obligations. If this ever needed a shared
cache, the entries would want per-record provenance and a deletion path --
see README, "Provenance".
"""

from __future__ import annotations

import asyncio
import time
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int, max_entries: int = 512) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: dict[str, tuple[float, T]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> T | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    async def set(self, key: str, value: T) -> None:
        async with self._lock:
            if len(self._store) >= self._max:
                self._evict_expired_locked()
            if len(self._store) >= self._max:
                # Still full: drop the entry closest to expiry.
                oldest = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest]
            self._store[key] = (time.monotonic() + self._ttl, value)

    async def purge(self, key: str) -> bool:
        """Remove one entry.

        Present because a cache holding personal data needs a deletion path,
        not only an expiry.
        """
        async with self._lock:
            return self._store.pop(key, None) is not None

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    def _evict_expired_locked(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._store.items() if exp < now]:
            del self._store[key]

    @property
    def size(self) -> int:
        return len(self._store)

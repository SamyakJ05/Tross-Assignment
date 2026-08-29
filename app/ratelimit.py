"""Per-API-key request rate limiting.

The Pacer in app.linkedin.client throttles every outbound request this
process makes to LinkedIn as a whole -- it protects LinkedIn's endpoints,
not one caller from another. A single valid API key can still request an
unbounded number of distinct profiles and consume the entire shared
LinkedIn-facing budget by itself. This enforces a simple per-key request
budget so one caller cannot starve every other caller of it.

In-memory and per-process, matching the cache in app/cache.py: this is a
politeness control on a single deployment, not a distributed rate limiter.
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import Header, HTTPException, status

from app.config import get_settings


class KeyRateLimiter:
    """A sliding-window request count per key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str, limit: int, window_seconds: float) -> None:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = max(1, int(window_seconds - (now - hits[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for this API key: {limit} requests per "
                    f"{int(window_seconds)}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)

    def reset(self) -> None:
        """Clear all counters. A test hook; never called in production."""
        self._hits.clear()


_limiter = KeyRateLimiter()


async def enforce_rate_limit(x_api_key: str | None = Header(default=None)) -> None:
    """Count this request against its API key's budget.

    Reads the header directly rather than a value validated by
    `require_api_key`, so an invalid key never consumes a real key's
    budget -- and so this dependency works standing alone in tests.
    """
    if not x_api_key:
        return
    settings = get_settings()
    _limiter.check(x_api_key, settings.rate_limit_per_minute, settings.rate_limit_window_seconds)


def reset_rate_limiter() -> None:
    """Test hook: clear counters between test cases that reuse the same key."""
    _limiter.reset()

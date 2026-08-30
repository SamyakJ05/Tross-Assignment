"""Per-client request rate limiting for the public profile endpoint.

The Pacer in app.linkedin.client throttles every outbound request this
process makes to LinkedIn as a whole -- it protects LinkedIn's endpoints,
not one caller from another. A single client can still request an unbounded
number of distinct profiles and consume the shared LinkedIn-facing budget.
This enforces a simple per-client budget.

In-memory and per-process, matching the cache in app/cache.py: this is a
politeness control on a single deployment, not a distributed rate limiter.
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException, Request, status

from app.config import get_settings


class ClientRateLimiter:
    """A sliding-window request count per caller identity."""

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
                    f"Rate limit exceeded for this client: {limit} requests per "
                    f"{int(window_seconds)}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)

    def reset(self) -> None:
        """Clear all counters. A test hook; never called in production."""
        self._hits.clear()


_limiter = ClientRateLimiter()


async def enforce_rate_limit(request: Request) -> None:
    """Count a public request without requiring a user-supplied credential."""
    settings = get_settings()
    # Render/Cloudflare supplies these values. Prefer the edge-authenticated
    # address and fall back to the socket peer for local runs and tests.
    client_id = request.headers.get("cf-connecting-ip")
    if not client_id:
        forwarded = request.headers.get("x-forwarded-for", "")
        client_id = forwarded.split(",", 1)[0].strip() if forwarded else ""
    if not client_id:
        client_id = request.client.host if request.client else "unknown"
    _limiter.check(client_id, settings.rate_limit_per_minute, settings.rate_limit_window_seconds)


def reset_rate_limiter() -> None:
    """Test hook: clear counters between test cases that reuse the same key."""
    _limiter.reset()

"""API key authentication.

The brief asks for a publicly deployed endpoint. Deployed publicly is not
the same as open: this endpoint returns third parties' personal data on
demand, and leaving it unauthenticated would make it a free profile-lookup
service for anyone who finds the URL.

So it takes a key. Keys are compared in constant time and never logged.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import Settings, get_settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings: Settings = get_settings()
    configured = settings.api_key_set

    if not configured:
        # Failing closed rather than open. An unconfigured deployment that
        # silently served everyone would be the worst outcome here.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No API keys are configured, so the service is refusing requests. Set API_KEYS "
                "in the environment."
            ),
        )

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
        )

    # compare_digest against every configured key, without short-circuiting
    # on the first mismatch.
    if not any(secrets.compare_digest(x_api_key, key) for key in configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

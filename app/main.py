"""The HTTP surface."""

from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.cache import TTLCache
from app.config import get_settings
from app.linkedin.client import Pacer
from app.linkedin.errors import (
    ChallengeRequired,
    LinkedInError,
    NotFound,
    RateLimited,
    RequestDenied,
    SessionExpired,
)
from app.linkedin.queries import registry_status
from app.linkedin.resolver import InvalidProfileURL, extract_slug
from app.models.envelope import ProfileResponse
from app.ratelimit import enforce_rate_limit
from app.service import get_profile

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")

app = FastAPI(
    title="LinkedIn Profile API",
    version="0.1.0",
    description=(
        "A reverse-engineered LinkedIn profile API built directly on Dash REST and RSC endpoints. "
        "No browser, no automation driver: raw HTTP with a manually injected session.\n\n"
        "Every response reports which tier answered and how complete the result is, because "
        "LinkedIn's dominant failure mode is a 200 carrying degraded data rather than an error."
    ),
)

_cache: TTLCache[ProfileResponse] = TTLCache(
    settings.cache_ttl_seconds,
    stale_ttl_seconds=settings.cache_stale_ttl_seconds,
)
_profile_locks: dict[str, asyncio.Lock] = {}
_profile_locks_guard = asyncio.Lock()
# One Pacer for the whole process, shared by every request. LinkedIn's
# reputation signal is per outbound IP, not per session, so pacing must be
# global here rather than reset for each incoming request. See Pacer's
# docstring in app/linkedin/client.py.
_pacer = Pacer(settings.min_request_interval_seconds)


def _response_quality(response: ProfileResponse) -> int:
    """Rank responses so an upstream degradation cannot poison the cache."""
    return {"needs_review": 1, "partial": 2, "complete": 3}[response.completeness.value]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Each upstream failure maps to the status that best describes it to *our*
# caller, which is not always the status LinkedIn returned. A 999 is not a
# thing an HTTP client should have to know about, but "we are blocked
# upstream" is.
_STATUS_MAP: dict[type[LinkedInError], int] = {
    NotFound: status.HTTP_404_NOT_FOUND,
    RateLimited: status.HTTP_429_TOO_MANY_REQUESTS,
    RequestDenied: status.HTTP_502_BAD_GATEWAY,
    ChallengeRequired: status.HTTP_503_SERVICE_UNAVAILABLE,
    SessionExpired: status.HTTP_503_SERVICE_UNAVAILABLE,
}


@app.exception_handler(LinkedInError)
async def _linkedin_error_handler(_, exc: LinkedInError) -> JSONResponse:
    code = _STATUS_MAP.get(type(exc), status.HTTP_502_BAD_GATEWAY)
    log.warning("%s: %s", exc.code, exc.message)
    return JSONResponse(
        status_code=code,
        content={
            "error": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "upstream_status": exc.status,
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _lock_for(slug: str) -> asyncio.Lock:
    async with _profile_locks_guard:
        return _profile_locks.setdefault(slug, asyncio.Lock())


async def _profile_response(url: str, refresh: bool) -> ProfileResponse:
    try:
        slug = extract_slug(url)
    except InvalidProfileURL as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    cached = await _cache.get(slug)
    if cached is not None and not refresh:
        return cached.model_copy(update={"cached": True})

    # Coalesce simultaneous misses for the same profile. Without this, one
    # Swagger double-click can launch two identical LinkedIn request chains.
    lock = await _lock_for(slug)
    async with lock:
        cached = await _cache.get(slug)
        if cached is not None and not refresh:
            return cached.model_copy(update={"cached": True})

        stale = await _cache.get_stale(slug)
        try:
            result = await get_profile(slug, settings, pacer=_pacer)
        except LinkedInError:
            if stale is not None:
                log.warning("Serving stale cached profile for %s after upstream failure", slug)
                return stale.model_copy(update={"cached": True})
            raise

        if stale is not None and _response_quality(result) < _response_quality(stale):
            log.warning("Serving stale cached profile for %s instead of degraded refresh", slug)
            return stale.model_copy(update={"cached": True})

        await _cache.set(slug, result)
        return result


@app.get(
    "/profile",
    response_model=ProfileResponse,
    dependencies=[Depends(enforce_rate_limit)],
    summary="Fetch a LinkedIn profile as structured JSON",
)
async def profile(
    url: str = Query(
        ...,
        description=(
            "A LinkedIn profile URL or bare vanity slug. Accepts locale subdomains, trailing "
            "paths and query strings, e.g. https://www.linkedin.com/in/samyakj05/"
        ),
        examples=["https://www.linkedin.com/in/samyakj05/"],
    ),
    refresh: bool = Query(
        False,
        description="Bypass the cache and fetch from LinkedIn.",
    ),
) -> ProfileResponse:
    return await _profile_response(url, refresh)


@app.get(
    "/v1/profile",
    response_model=ProfileResponse,
    dependencies=[Depends(enforce_rate_limit)],
    include_in_schema=False,
)
async def profile_v1_alias(url: str = Query(...), refresh: bool = Query(False)) -> ProfileResponse:
    """Backward-compatible alias for clients using the original route."""
    return await _profile_response(url, refresh)


@app.get("/v1/health", summary="Service and upstream health")
async def health() -> dict[str, object]:
    """Operational state, including the thing most likely to break.

    The queryId ages are the point of this endpoint. A hash that has not
    been verified in weeks is the leading indicator of silent extraction
    failure, and surfacing it here means staleness is visible before a
    consumer notices bad data.

    Unauthenticated by design so an uptime check can hit it. It exposes no
    profile data and no secrets -- only whether a session is configured.
    """
    registry = registry_status(
        profile_components_query_id=settings.linkedin_profile_components_query_id,
        profile_components_verified_on=settings.linkedin_profile_components_verified_on,
    )

    warnings: list[str] = []
    for q in registry:
        if not q["configured"]:
            warnings.append(f"{q['key']}: no queryId configured")
        elif q["stale"]:
            warnings.append(f"{q['key']}: last verified {q['age_days']} days ago")

    return {
        "status": "ok",
        "version": app.version,
        "session_configured": settings.has_session,
        "public_fallback_enabled": settings.enable_public_fallback,
        "cache_entries": _cache.size,
        "cache_ttl_seconds": settings.cache_ttl_seconds,
        "query_registry": registry,
        "query_registry_warnings": warnings,
    }


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "linkedin-profile-api",
        "docs": "/docs",
        "profile": "/profile?url=https://www.linkedin.com/in/example/",
        "health": "/v1/health",
    }

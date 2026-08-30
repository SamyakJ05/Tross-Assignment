"""The HTTP surface."""

from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
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
from app.models.envelope import ErrorResponse, ProfileResponse
from app.models.requests import SessionProfileRequest
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
        "No browser, no automation driver: raw HTTP with a backend or request-scoped session.\n\n"
        "Every response reports which tier answered and how complete the result is, because "
        "LinkedIn's dominant failure mode is a 200 carrying degraded data rather than an error."
    ),
    openapi_tags=[
        {"name": "Profiles", "description": "Structured LinkedIn profile extraction."},
        {"name": "Operations", "description": "Safe service health and maintenance signals."},
    ],
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


@app.exception_handler(HTTPException)
async def _http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Keep application-generated input and rate-limit failures consistent."""
    error = (
        "rate_limited"
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        else "invalid_url"
    )
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content=ErrorResponse(
            error=error,
            message=str(exc.detail),
            retryable=exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS,
        ).model_dump(mode="json", exclude_none=True),
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {
            "location": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=ErrorResponse(
            error="validation_error",
            message="One or more request parameters are invalid.",
            details=details,
        ).model_dump(mode="json", exclude_none=True),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_PROFILE_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "description": "A profile plus completeness, provenance, warnings, and cache metadata.",
        "content": {
            "application/json": {
                "examples": {
                    "complete": {
                        "summary": "Authenticated sections resolved",
                        "value": {
                            "profile": {
                                "public_identifier": "samyakj05",
                                "full_name": "Samyak Jain",
                                "headline": "Software Engineer",
                                "location": {"display": "India"},
                                "position_groups": [
                                    {
                                        "company_name": "Northwind",
                                        "positions": [
                                            {
                                                "title": "Software Engineer",
                                                "dates": {
                                                    "start_year": 2024,
                                                    "is_current": True,
                                                },
                                            }
                                        ],
                                    }
                                ],
                                "educations": [],
                                "skills": [{"name": "Python", "endorsement_count": None}],
                                "certifications": [],
                                "languages": [
                                    {
                                        "name": "English",
                                        "proficiency": "Full professional proficiency",
                                    }
                                ],
                            },
                            "completeness": "complete",
                            "sources": [
                                {"section": "top_card", "tier": "voyager_dash_rest"},
                                {"section": "experience", "tier": "linkedin_rsc"},
                                {"section": "languages", "tier": "linkedin_rsc"},
                            ],
                            "warnings": [],
                            "cached": False,
                            "elapsed_ms": 842,
                        },
                    },
                    "partial": {
                        "summary": "Useful fallback data with explicit quality metadata",
                        "value": {
                            "profile": {"public_identifier": "example"},
                            "completeness": "needs_review",
                            "sources": [],
                            "warnings": [
                                {
                                    "code": "missing_core_field",
                                    "message": "'full_name' is absent.",
                                    "section": "full_name",
                                }
                            ],
                            "cached": False,
                        },
                    },
                }
            }
        },
    },
    404: {
        "model": ErrorResponse,
        "description": "The LinkedIn profile could not be found.",
        "content": {
            "application/json": {
                "example": {
                    "error": "not_found",
                    "message": "The requested profile was not found.",
                    "retryable": False,
                    "upstream_status": 404,
                }
            }
        },
    },
    422: {
        "model": ErrorResponse,
        "description": "The URL or query parameters are invalid.",
        "content": {
            "application/json": {
                "example": {
                    "error": "invalid_url",
                    "message": "Expected a linkedin.com profile URL.",
                    "retryable": False,
                }
            }
        },
    },
    429: {
        "model": ErrorResponse,
        "description": "The caller or LinkedIn upstream is rate-limited.",
        "headers": {
            "Retry-After": {
                "description": "Seconds before the caller budget resets, when available.",
                "schema": {"type": "integer"},
            }
        },
        "content": {
            "application/json": {
                "example": {
                    "error": "rate_limited",
                    "message": "Rate limit exceeded for this client.",
                    "retryable": True,
                }
            }
        },
    },
    502: {
        "model": ErrorResponse,
        "description": "LinkedIn denied the request or returned an unexpected contract.",
    },
    503: {
        "model": ErrorResponse,
        "description": "The backend LinkedIn session needs attention before retrying.",
    },
}


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
    description=(
        "Fetch one profile through direct LinkedIn HTTP contracts. The response always includes "
        "quality and provenance metadata; an HTTP 200 does not imply every optional field "
        "was visible."
    ),
    operation_id="getLinkedInProfile",
    tags=["Profiles"],
    responses=_PROFILE_RESPONSES,
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


@app.post(
    "/profile/with-session",
    response_model=ProfileResponse,
    dependencies=[Depends(enforce_rate_limit)],
    summary="Fetch a profile with a caller-supplied LinkedIn session",
    description=(
        "Use ephemeral li_at and JSESSIONID cookie values for one direct fetch. Credentials are "
        "accepted only in the HTTPS request body, are not logged or returned, and are discarded "
        "when the request ends. This route bypasses the shared cache so one session's visible "
        "profile data cannot be served to another caller."
    ),
    operation_id="getLinkedInProfileWithSession",
    tags=["Profiles"],
    responses=_PROFILE_RESPONSES,
)
async def profile_with_session(body: SessionProfileRequest) -> ProfileResponse:
    try:
        slug = extract_slug(body.url)
    except InvalidProfileURL as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Re-validate a complete Settings object so JSESSIONID receives the same
    # quote normalization as an environment-provided secret. This object and
    # its LinkedInClient live only for this request and are never cached.
    values = settings.model_dump()
    values.update(
        linkedin_li_at=body.li_at.get_secret_value(),
        linkedin_jsessionid=body.jsessionid.get_secret_value(),
    )
    request_settings = type(settings).model_validate(values)
    return await get_profile(slug, request_settings, pacer=_pacer)


@app.get(
    "/v1/profile",
    response_model=ProfileResponse,
    dependencies=[Depends(enforce_rate_limit)],
    include_in_schema=False,
)
async def profile_v1_alias(url: str = Query(...), refresh: bool = Query(False)) -> ProfileResponse:
    """Backward-compatible alias for clients using the original route."""
    return await _profile_response(url, refresh)


@app.get("/v1/health", summary="Service and upstream health", tags=["Operations"])
async def health() -> dict[str, object]:
    """Operational state, including optional compatibility-route freshness.

    Modern extraction uses Dash REST and RSC and does not require a GraphQL
    queryId. The registry remains visible because a configured compatibility
    query becoming stale is useful maintenance information, but an
    intentionally unconfigured query is not a health warning.

    Unauthenticated by design so an uptime check can hit it. It exposes no
    profile data and no secrets -- only whether a session is configured.
    """
    registry = registry_status(
        profile_components_query_id=settings.linkedin_profile_components_query_id,
        profile_components_verified_on=settings.linkedin_profile_components_verified_on,
    )

    warnings: list[str] = []
    for q in registry:
        if q["configured"] and q["stale"]:
            warnings.append(f"{q['key']}: last verified {q['age_days']} days ago")

    return {
        "status": "ok",
        "version": app.version,
        "session_configured": settings.has_session,
        "public_fallback_enabled": settings.enable_public_fallback,
        "cache_entries": _cache.size,
        "cache_ttl_seconds": settings.cache_ttl_seconds,
        "primary_extraction": "dash_rest_and_rsc",
        "query_registry": registry,
        "query_registry_warnings": warnings,
        "query_registry_note": (
            "Optional GraphQL compatibility paths; unconfigured entries do not affect health."
        ),
    }


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "linkedin-profile-api",
        "docs": "/docs",
        "profile": "/profile?url=https://www.linkedin.com/in/example/",
        "profile_with_session": "/profile/with-session",
        "health": "/v1/health",
    }

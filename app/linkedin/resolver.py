"""URL parsing, and the slug-to-URN hop.

Two steps that look trivial and are not.

A LinkedIn profile URL carries a *vanity slug* -- the `samyakj05` in
`/in/samyakj05`. Modern Voyager endpoints do not accept slugs. They want an
`fsd_profile` URN, an opaque identifier that is stable across slug changes.
So every fetch begins with a resolution hop, and the dash REST endpoint is
the most reliable way to make it.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from app.linkedin.client import LinkedInClient
from app.linkedin.errors import NotFound, UnexpectedPayload
from app.linkedin.session import VOYAGER_BASE

# LinkedIn slugs allow unicode (many members use non-Latin names) and the
# trailing disambiguation hash LinkedIn appends, e.g. `jane-doe-1a2b3c4d`.
# Restricting to [a-z0-9-] silently 404s a large fraction of real profiles.
_SLUG_RE = re.compile(r"^[\w\-\.%]+$", re.UNICODE)

_PROFILE_PATH_RE = re.compile(r"/in/(?P<slug>[^/?#]+)", re.UNICODE)


class InvalidProfileURL(ValueError):
    pass


def extract_slug(url_or_slug: str) -> str:
    """Pull the vanity slug out of anything a user might paste.

    Accepts full URLs with or without scheme, locale subdomains
    (`in.linkedin.com`, `www.`), trailing paths like `/details/experience`,
    query strings, and a bare slug.
    """
    raw = url_or_slug.strip()
    if not raw:
        raise InvalidProfileURL("Empty input.")

    # A bare slug, no URL structure.
    if "/" not in raw and "." not in raw:
        if not _SLUG_RE.match(raw):
            raise InvalidProfileURL(f"{raw!r} is not a valid profile slug.")
        return unquote(raw)

    candidate = raw if "//" in raw else f"https://{raw}"
    parsed = urlparse(candidate)

    host = parsed.netloc.lower()
    if "linkedin.com" not in host:
        raise InvalidProfileURL(f"Expected a linkedin.com URL, got {parsed.netloc!r}.")

    match = _PROFILE_PATH_RE.search(parsed.path)
    if not match:
        raise InvalidProfileURL(
            "URL does not point at a member profile. Expected a path like /in/<slug>; "
            "company pages (/company/...) and posts are not supported."
        )

    slug = unquote(match.group("slug"))
    if not _SLUG_RE.match(slug):
        raise InvalidProfileURL(f"Extracted slug {slug!r} contains unexpected characters.")
    return slug


def normalise_urn(value: str) -> str:
    """Accept either a bare id or a full URN, return a full fsd_profile URN."""
    if value.startswith("urn:li:"):
        return value
    return f"urn:li:fsd_profile:{value}"


async def resolve_urn(client: LinkedInClient, slug: str, *, page_instance: str) -> tuple[str, dict]:
    """Resolve a vanity slug to an fsd_profile URN.

    Returns the URN and the raw payload, because that payload already
    contains the top-card fields -- name, headline, location -- and
    re-fetching them through GraphQL would be a wasted request against a
    rate-limited upstream.
    """
    url = (
        f"{VOYAGER_BASE}/identity/dash/profiles"
        f"?q=memberIdentity&memberIdentity={slug}"
        f"&decorationId=com.linkedin.voyager.dash.deco.identity.profile.WebTopCardCore-6"
    )
    headers = client.session.headers(page_instance=page_instance)
    payload = await client.get_json(url, headers=headers)

    urn = _find_profile_urn(payload)
    if not urn:
        raise NotFound(f"No profile found for slug {slug!r}.")
    return urn, payload


def _find_profile_urn(payload: dict) -> str | None:
    """Locate the fsd_profile URN in a dash profiles response.

    Checks `included` first because the normalized format puts entities
    there, then falls back to the elements array for the non-normalized
    shape. Written defensively: this is the one hop everything else depends
    on, and it is worth surviving a response shape we have not seen.
    """
    included = payload.get("included") or []
    for entity in included:
        urn = entity.get("entityUrn", "")
        if urn.startswith("urn:li:fsd_profile:"):
            return urn

    data = payload.get("data") or {}
    elements = data.get("elements") or data.get("*elements") or []
    for element in elements:
        if isinstance(element, str) and element.startswith("urn:li:fsd_profile:"):
            return element
        if isinstance(element, dict):
            urn = element.get("entityUrn", "")
            if urn.startswith("urn:li:fsd_profile:"):
                return urn

    return None


def public_profile_url(slug: str) -> str:
    return f"https://www.linkedin.com/in/{slug}"


def assert_resolvable(payload: dict) -> None:
    """Guard against a structurally valid response with no usable content."""
    if not payload.get("included") and not payload.get("data"):
        raise UnexpectedPayload(
            "Response carried neither 'data' nor 'included'. The endpoint shape has probably "
            "changed, or the accept header was not honoured."
        )

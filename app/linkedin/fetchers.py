"""The tiered fetch strategy.

Three ways to obtain a profile, tried in order of how much they return:

  1. VOYAGER_GRAPHQL     authenticated, per-section, richest
  2. VOYAGER_DASH_REST   authenticated, fewer fields, different failure surface
  3. PUBLIC_JSONLD       unauthenticated public page, thin but works anywhere

Tier 1 is the primary path and the one that satisfies the brief. The others
exist because the failure modes are genuinely independent: a stale queryId
kills tier 1 while tier 2 keeps working, and a datacentre IP block or an
expired cookie kills both while tier 3 keeps working. A service that only
implements tier 1 is offline whenever any of those happens, which -- given a
cookie harvested at one IP and used from another -- is a realistic Sunday
evening.

Falling back is not free and is never silent. Every section records the tier
that produced it, and the envelope downgrades completeness accordingly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from selectolax.parser import HTMLParser

from app.linkedin.client import LinkedInClient
from app.linkedin.errors import (
    LinkedInError,
    NotFound,
    SessionExpired,
    StaleQueryId,
    UnexpectedPayload,
)
from app.linkedin.queries import (
    SECTION_KEYS,
    Query,
    configured_profile_components_query,
    encode_variables,
)
from app.linkedin.resolver import public_profile_url, resolve_urn
from app.linkedin.rsc import (
    PROFILE_CARDS_ABOVE_ACTIVITY,
    PROFILE_CARDS_BELOW_ACTIVITY,
    PROFILE_CARDS_EXPERIENCE,
    fetch_profile_component,
    fetch_skills,
    skill_names,
    visible_strings,
)
from app.linkedin.session import VOYAGER_BASE
from app.models.envelope import SectionSource, Tier, Warning

log = logging.getLogger(__name__)


class FetchResult:
    """Raw payloads plus the provenance of each, before parsing."""

    def __init__(self) -> None:
        self.top_card: dict[str, Any] | None = None
        self.sections: dict[str, dict[str, Any]] = {}
        self.rsc_sections: dict[str, list[str]] = {}
        self.public_html: str | None = None
        self.sources: list[SectionSource] = []
        self.warnings: list[Warning] = []
        self.urn: str | None = None

    def record(
        self,
        section: str,
        tier: Tier,
        query: object | None = None,
        count: int | None = None,
    ) -> None:
        self.sources.append(
            SectionSource(
                section=section,
                tier=tier,
                query_id=getattr(query, "full_id", None),
                query_id_verified_on=(
                    query.last_verified.isoformat()
                    if query is not None and hasattr(query, "last_verified")
                    else None
                ),
                item_count=count,
            )
        )

    def warn(self, code: str, message: str, section: str | None = None) -> None:
        self.warnings.append(Warning(code=code, message=message, section=section))


async def fetch_profile(client: LinkedInClient, slug: str) -> FetchResult:
    """Fetch everything obtainable for a slug, degrading rather than failing."""
    result = FetchResult()
    page_instance = client.session.new_page_instance()
    authenticated_error: LinkedInError | None = None

    if client.session.is_authenticated:
        try:
            await _fetch_authenticated(client, slug, page_instance, result)
        except NotFound:
            raise
        except LinkedInError as exc:
            authenticated_error = exc
            log.warning("Authenticated tiers unavailable for %s: %s", slug, exc)
            result.warn(
                exc.code,
                "Authenticated fetch failed and the public fallback was used instead. "
                f"{exc.message}",
            )
    else:
        result.warn(
            "no_session",
            "No LinkedIn session is configured, so only the public fallback tier is available. "
            "Set LINKEDIN_LI_AT and LINKEDIN_JSESSIONID to enable the authenticated path.",
        )

    # Do not add a public request after a successful authenticated response.
    # It has no fields that can enrich the RSC card and can turn an otherwise
    # usable result into a session challenge on a reputation-sensitive IP.
    if client.settings.enable_public_fallback and not result.top_card and not result.rsc_sections:
        try:
            result.public_html = await client.get_text(public_profile_url(slug))
            result.record("public_page", Tier.PUBLIC_JSONLD)
        except LinkedInError as exc:
            log.warning("Public fallback failed for %s: %s", slug, exc)
            result.warn(exc.code, f"Public fallback tier also failed. {exc.message}")
    elif not result.top_card and not result.rsc_sections:
        result.warn("public_fallback_disabled", "Public fallback is disabled by configuration.")

    if not result.top_card and not result.public_html and not result.rsc_sections:
        if authenticated_error is not None:
            raise authenticated_error
        raise NotFound(
            f"Could not retrieve profile {slug!r} through any tier. See warnings for what each "
            f"tier reported."
        )

    return result


async def _fetch_authenticated(
    client: LinkedInClient,
    slug: str,
    page_instance: str,
    result: FetchResult,
) -> None:
    # --- resolution hop, which also yields the top card ------------------
    # Run this before the RSC card requests. A fresh Dash request has proven
    # more reliable before the three-card sequence, but an RSC response is
    # still retained if this independent hop redirects.
    urn: str | None = None
    try:
        urn, top_payload = await resolve_urn(client, slug, page_instance=page_instance)
    except NotFound:
        result.warn("resolver_not_found", "Dash resolver found no profile; trying RSC cards.")
    except SessionExpired:
        # Do not continue into endpoints that sometimes return public card
        # fragments even after LinkedIn has explicitly invalidated li_at.
        raise
    except LinkedInError as exc:
        result.warn(exc.code, f"Dash resolver failed: {exc.message}", "top_card")
    else:
        result.urn = urn
        result.top_card = top_payload
        result.record("top_card", Tier.VOYAGER_DASH_REST)

    # --- current RSC profile cards -------------------------------------
    # The modern profile UI no longer uses the old GraphQL component query.
    # These cards need only the vanity slug, so they remain available when
    # Dash fails for an otherwise valid session.
    components = (
        ("experience", PROFILE_CARDS_EXPERIENCE),
        ("above_activity", PROFILE_CARDS_ABOVE_ACTIVITY),
        ("below_activity", PROFILE_CARDS_BELOW_ACTIVITY),
    )
    for section, component in components:
        try:
            frames = await fetch_profile_component(client, slug, component)
            values = visible_strings(frames)
        except LinkedInError as exc:
            result.warn(exc.code, f"RSC {section} fetch failed: {exc.message}", section)
            if exc.tier_fatal:
                return
            continue
        if values:
            result.rsc_sections[section] = values
            result.record(section, Tier.LINKEDIN_RSC, count=len(values))

    # --- skills, via the details/skills pager ---------------------------
    # A different action shape than the cards above (a pager, not a
    # component fetch) and it needs the member's numeric profileId, so it
    # only runs once the resolution hop above has produced a urn.
    if urn is not None:
        profile_id = urn.rsplit(":", 1)[-1]
        try:
            frames = await fetch_skills(client, slug, profile_id)
            names = skill_names(frames)
        except LinkedInError as exc:
            result.warn(exc.code, f"RSC skills fetch failed: {exc.message}", "skills")
        else:
            if names:
                result.rsc_sections["skills"] = names
                result.record("skills", Tier.LINKEDIN_RSC, count=len(names))

    # --- per-section GraphQL --------------------------------------------
    if urn is None:
        return
    query = configured_profile_components_query(
        client.settings.linkedin_profile_components_query_id,
        client.settings.linkedin_profile_components_verified_on,
    )
    if not query.query_id:
        result.warn(
            "query_id_unconfigured",
            "No queryId is configured for profile components, so detailed sections were not "
            "fetched. Capture a current hash from DevTools and set it in app/linkedin/queries.py.",
        )
        return

    for section, section_key in SECTION_KEYS.items():
        try:
            payload = await _fetch_section(client, urn, section_key, page_instance, query)
        except StaleQueryId as exc:
            # One stale hash disables the whole GraphQL tier: every section
            # shares it, so continuing would issue a request per section
            # against an upstream that has already rejected the query.
            result.warn(
                "stale_query_id",
                f"queryId {exc.query_id!r} was rejected, so no detailed sections could be "
                f"fetched. Re-capture it from DevTools and update the registry.",
                section=section,
            )
            return
        except NotFound:
            continue
        except LinkedInError as exc:
            result.warn(
                exc.code,
                f"Section {section!r} could not be fetched: {exc.message}",
                section=section,
            )
            if exc.tier_fatal:
                # A terminal response (redirect, session failure, block or
                # changed payload) applies to every remaining section too.
                # Stop here instead of turning one bad request into a burst.
                return
            continue

        result.sections[section] = payload
        result.record(section, Tier.VOYAGER_GRAPHQL, query)


async def _fetch_section(
    client: LinkedInClient,
    urn: str,
    section_key: str,
    page_instance: str,
    query: Query,
) -> dict[str, Any]:
    """One expanded profile section via GraphQL.

    Note the Rest.li variable encoding -- parentheses and percent-encoded
    URN colons, not JSON. Passing JSON here returns an opaque 400.
    """
    variables = encode_variables(
        profileUrn=urn,
        sectionType=section_key,
    )
    url = f"{VOYAGER_BASE}/graphql?queryId={query.full_id}&variables={variables}"
    headers = client.session.headers(page_instance=page_instance)
    return await client.get_json(url, headers=headers, query_id=query.full_id)


# ---------------------------------------------------------------------------
# Public fallback tier
# ---------------------------------------------------------------------------


def extract_jsonld(html: str) -> dict[str, Any] | None:
    """Pull the schema.org block out of a public profile page.

    LinkedIn embeds a JSON-LD graph in logged-out profile pages for search
    engines. It is far thinner than Voyager -- typically name, headline,
    image, and coarse employment and education entries with no dates or
    descriptions -- but it needs no session and survives IP reputation
    blocks, which is exactly when it is wanted.

    Still a direct endpoint hit with no browser involved.
    """
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        graph = parsed.get("@graph") if isinstance(parsed, dict) else None
        candidates = graph if isinstance(graph, list) else [parsed]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "Person":
                return item

    return None


def assert_public_usable(html: str) -> None:
    """Distinguish a real public profile from an auth wall served with a 200."""
    lowered = html[:5000].lower()
    if "authwall" in lowered or "join linkedin" in lowered and "profile" not in lowered:
        raise UnexpectedPayload(
            "The public profile page returned an auth wall rather than profile content. "
            "LinkedIn shows this to unauthenticated clients it does not trust, which on a "
            "datacentre IP is common."
        )

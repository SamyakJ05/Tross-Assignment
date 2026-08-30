"""Orchestration: fetch, parse, merge, assess.

The single place where the tiers are stitched into one answer.
"""

from __future__ import annotations

import logging
import time

from app.config import Settings
from app.linkedin.client import LinkedInClient, Pacer
from app.linkedin.fetchers import extract_jsonld, fetch_profile
from app.models.domain import Profile
from app.models.envelope import ProfileResponse, assess
from app.parsing.mappers import (
    apply_rsc_below_activity,
    apply_rsc_experience,
    apply_rsc_languages,
    apply_rsc_skills,
    apply_sections,
    map_jsonld,
    map_top_card,
    merge,
)

log = logging.getLogger(__name__)


async def get_profile(
    slug: str, settings: Settings, *, pacer: Pacer | None = None
) -> ProfileResponse:
    """Fetch and assemble one profile.

    `pacer` is optional only for callers that genuinely make one isolated
    fetch, like the CLI. A long-running server must pass the same `Pacer`
    into every call so the request budget is shared across concurrent
    callers rather than reset per request; see `app/main.py`.
    """
    started = time.perf_counter()

    async with LinkedInClient(settings, pacer=pacer) as client:
        raw = await fetch_profile(client, slug)

    profile: Profile | None = None

    # Authenticated tiers first, so their values win any conflict.
    if raw.top_card:
        try:
            profile = map_top_card(raw.top_card, slug)
        except Exception as exc:  # noqa: BLE001
            log.warning("Top card parse failed for %s: %s", slug, exc)
            raw.warn("parse_failed", f"Top card could not be parsed: {exc}", "top_card")

    if profile and raw.sections:
        profile = apply_sections(profile, raw.sections)

    # RSC cards are independently addressable by vanity slug. If Dash is
    # blocked but RSC answered, retain the partial profile rather than
    # discarding it solely because identity fields are unavailable.
    if profile is None and raw.rsc_sections:
        profile = Profile(public_identifier=slug)

    if profile and raw.rsc_sections.get("experience"):
        profile = apply_rsc_experience(profile, raw.rsc_sections["experience"])
    if profile and raw.rsc_sections.get("below_activity"):
        profile = apply_rsc_below_activity(profile, raw.rsc_sections["below_activity"])
    if profile and raw.rsc_sections.get("skills"):
        profile = apply_rsc_skills(profile, raw.rsc_sections["skills"])
    if profile and raw.rsc_sections.get("languages"):
        profile = apply_rsc_languages(profile, raw.rsc_sections["languages"])

    # Public tier fills gaps, or stands alone if nothing else answered.
    if raw.public_html:
        person = extract_jsonld(raw.public_html)
        if person:
            public_profile = map_jsonld(person, slug)
            profile = merge(profile, public_profile) if profile else public_profile
        elif not profile:
            raw.warn(
                "no_jsonld",
                "The public page carried no JSON-LD block, which usually means an auth wall "
                "was served instead of profile content.",
            )

    if profile is None:
        # Every tier failed to yield anything parseable. Return the shell so
        # the caller still receives the warnings explaining why.
        profile = Profile(public_identifier=slug)

    if raw.urn and not profile.urn:
        profile.urn = raw.urn

    completeness, warnings = assess(profile, raw.sources, raw.warnings)

    return ProfileResponse(
        profile=profile,
        completeness=completeness,
        sources=raw.sources,
        warnings=warnings,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )

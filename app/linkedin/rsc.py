"""Direct client for LinkedIn's current server-component profile endpoints.

The current LinkedIn profile UI uses an RSC/SDUI stream rather than the old
Voyager profile-components GraphQL query. This module reproduces that request
as raw HTTP. It does not launch, connect to, or depend on a browser.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from typing import Any

from app.linkedin.client import LinkedInClient
from app.linkedin.errors import UnexpectedPayload

RSC_COMPONENT_URL = "https://www.linkedin.com/flagship-web/rsc-action/actions/component"
RSC_PAGINATION_URL = "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination"
PROFILE_CARDS_ABOVE_ACTIVITY = (
    "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsAboveActivity"
)
PROFILE_CARDS_EXPERIENCE = "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly"
PROFILE_CARDS_BELOW_ACTIVITY = (
    "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsBelowActivityPart1WithoutExp"
)
SKILLS_PAGER_ID = "com.linkedin.sdui.pagers.profile.details.skills"
LANGUAGES_PAGER_ID = "com.linkedin.sdui.pagers.profile.details.languages"


def _base64_token(byte_count: int = 16) -> str:
    return base64.b64encode(secrets.token_bytes(byte_count)).decode("ascii")


def _binding(key: str) -> dict[str, Any]:
    return {
        "type": "com.linkedin.sdui.components.core.BindingImpl",
        "value": {"key": key, "namespace": "MemoryNamespace"},
    }


def profile_component_payload(slug: str) -> dict[str, Any]:
    """Build the stable SDUI profile-state envelope for one vanity slug."""
    state_prefix = f"ProfileComponentState{slug}"
    state_keys = {
        "shouldRefreshScreenOnReappear": "ShouldRefreshScreen",
        "shouldFetchFromCache": "FetchFromCache",
        "shouldDisplayTabAnchors": "ShouldDisplayTabAnchors",
        "shouldReloadTopCardOnReappear": "ShouldReloadTopCardOnReappear",
        "deferredTopCardReloadProfileId": "DeferredTopCardReloadProfileId",
        "shouldDisplayStickyHeader": "ShouldDisplayStickyHeader",
        "shouldRefreshLanguageDetailScreen": "ShouldRefreshLanguageDetails",
        "lastPerformedActionRef": "LastPerformedActionRef",
        "shouldFocusOnReappear": "ShouldFocusOnReappear",
        "shouldFocusFeaturedOnReappear": "ShouldFocusFeaturedOnReappear",
        "lastFeaturedActionRef": "LastFeaturedActionRef",
        "shouldHideProfileCards": "ProfileHideCards",
    }
    profile_state = {
        "profileId": slug,
        **{name: _binding(f"{state_prefix}{suffix}") for name, suffix in state_keys.items()},
    }
    return {
        "clientArguments": {
            "payload": {
                "isSelfView": False,
                "vanityName": slug,
                "replaceableSectionArgs": {
                    "vanityName": slug,
                    "locale": "und",
                    "hideCardsForGoldenGate": False,
                    "shouldSetupReplaceableComponent": True,
                    "isSelfView": False,
                    "isSelfViewResolved": False,
                },
                "profileComponentState": profile_state,
            },
            "states": [],
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "screenId": "com.linkedin.sdui.flagshipnav.profile.Profile",
            "knownTemplateIds": [],
        }
    }


def rsc_headers(client: LinkedInClient, slug: str) -> dict[str, str]:
    """Build a fresh, internally consistent header set for one RSC call."""
    settings = client.settings
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    page_tracking_id = _base64_token()
    version = settings.linkedin_rsc_application_version
    track = {
        "clientVersion": version,
        "mpVersion": version,
        "osName": "web",
        "timezoneOffset": 5.5,
        "timezone": "Asia/Calcutta",
        "deviceFormFactor": "DESKTOP",
        "mpName": "web",
        "displayDensity": 2,
        "displayWidth": 3024,
        "displayHeight": 1964,
    }
    return {
        "cookie": client.session.cookie_header(),
        "csrf-token": client.session.csrf_token,
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://www.linkedin.com",
        "referer": f"https://www.linkedin.com/in/{slug}/",
        "user-agent": settings.user_agent,
        "accept-language": "en-US,en;q=0.9",
        "x-li-rsc-stream": "true",
        "x-li-application-version": version,
        "x-li-page-instance-tracking-id": page_tracking_id,
        "x-li-page-instance": (f"urn:li:page:d_flagship3_profile_view_base;{page_tracking_id}"),
        "x-li-anchor-page-key": "d_flagship3_profile_view_base",
        "x-li-pageforestid": trace_id,
        "x-li-application-instance": _base64_token(),
        "x-li-track": json.dumps(track, separators=(",", ":")),
        "x-li-traceparent": f"00-{trace_id}-{span_id}-00",
        "x-li-tracestate": f"LinkedIn={span_id}",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


async def fetch_profile_component(
    client: LinkedInClient,
    slug: str,
    component_id: str,
) -> list[Any]:
    """Fetch and decode one RSC component stream into its JSON frames."""
    raw = await client.post_stream(
        RSC_COMPONENT_URL,
        headers=rsc_headers(client, slug),
        payload=profile_component_payload(slug),
        params={
            "componentId": component_id,
            "sduiid": component_id,
            "parentSpanId": _base64_token(8),
        },
    )
    return decode_flight_frames(raw)


# ---------------------------------------------------------------------------
# Detail sections: paginated lists, not cards
#
# The profile-components GraphQL query that used to carry skills no longer
# fires at all (see app/linkedin/queries.py); LinkedIn's current /details
# sub-pages fetch their own paginated RSC data instead. This is a different
# action shape than the cards above -- a pagination request keyed by the
# member's numeric profileId (the tail of the fsd_profile URN already
# resolved elsewhere), not the vanity slug alone.
# ---------------------------------------------------------------------------


def _detail_pager_payload(
    slug: str,
    profile_id: str,
    *,
    pager_id: str,
    screen_id: str,
    start: int,
    count: int,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "vanityName": slug,
        "profileId": profile_id,
        "start": start,
        "count": count,
        **(extra_payload or {}),
    }
    request_metadata = {"$type": "proto.sdui.common.RequestMetadata"}
    return {
        "pagerId": pager_id,
        "clientArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": payload,
            "requestMetadata": request_metadata,
            "states": [],
            "screenId": screen_id,
            "knownTemplateIds": [],
        },
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": pager_id,
            "trigger": {
                "$case": "itemDistanceTrigger",
                "itemDistanceTrigger": {
                    "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                    "preloadDistance": 3,
                    "preloadLength": 250,
                },
            },
            "retryCount": 2,
            "requestedArguments": {
                "$type": "proto.sdui.actions.requests.RequestedArguments",
                "requestedStateKeys": [],
                "payload": payload,
                "requestMetadata": request_metadata,
            },
        },
    }


def _detail_pagination_headers(
    client: LinkedInClient,
    slug: str,
    section: str,
) -> dict[str, str]:
    """Like rsc_headers, but scoped to one /details section sub-page.

    The anchor page key changes to match ("..._skills_details"), and the
    referer points at the detail sub-page rather than the profile root --
    both are conspicuous if wrong, same as everything else in rsc_headers.
    """
    settings = client.settings
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    page_tracking_id = _base64_token()
    version = settings.linkedin_rsc_application_version
    anchor_page_key = f"d_flagship3_profile_view_base_{section}_details"
    track = {
        "clientVersion": version,
        "mpVersion": version,
        "osName": "web",
        "timezoneOffset": 5.5,
        "timezone": "Asia/Calcutta",
        "deviceFormFactor": "DESKTOP",
        "mpName": "web",
        "displayDensity": 2,
        "displayWidth": 3024,
        "displayHeight": 1964,
    }
    return {
        "cookie": client.session.cookie_header(),
        "csrf-token": client.session.csrf_token,
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://www.linkedin.com",
        "referer": f"https://www.linkedin.com/in/{slug}/details/{section}/",
        "user-agent": settings.user_agent,
        "accept-language": "en-US,en;q=0.9",
        "x-li-rsc-stream": "true",
        "x-li-application-version": version,
        "x-li-page-instance-tracking-id": page_tracking_id,
        "x-li-page-instance": f"urn:li:page:{anchor_page_key};{page_tracking_id}",
        "x-li-anchor-page-key": anchor_page_key,
        "x-li-pageforestid": trace_id,
        "x-li-application-instance": _base64_token(),
        "x-li-track": json.dumps(track, separators=(",", ":")),
        "x-li-traceparent": f"00-{trace_id}-{span_id}-00",
        "x-li-tracestate": f"LinkedIn={span_id}",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


async def fetch_skills(
    client: LinkedInClient,
    slug: str,
    profile_id: str,
    *,
    count: int = 100,
) -> list[Any]:
    """Fetch and decode the skills detail page's first page of results.

    Up to `count` skills are fetched in one bounded request. The default is
    high enough for ordinary profiles without turning the lookup into an
    unbounded crawl.
    """
    raw = await client.post_stream(
        RSC_PAGINATION_URL,
        headers=_detail_pagination_headers(client, slug, "skills"),
        payload=_detail_pager_payload(
            slug,
            profile_id,
            pager_id=SKILLS_PAGER_ID,
            screen_id="com.linkedin.sdui.flagshipnav.profile.ProfileSkillDetails",
            start=0,
            count=count,
            extra_payload={"filter": "ProfileSkillCategory_ALL"},
        ),
        params={
            "sduiid": SKILLS_PAGER_ID,
            "parentSpanId": _base64_token(8),
        },
    )
    return decode_flight_frames(raw)


async def fetch_languages(
    client: LinkedInClient,
    slug: str,
    profile_id: str,
    *,
    count: int = 100,
) -> list[Any]:
    """Fetch the languages detail page's first bounded result page."""
    raw = await client.post_stream(
        RSC_PAGINATION_URL,
        headers=_detail_pagination_headers(client, slug, "languages"),
        payload=_detail_pager_payload(
            slug,
            profile_id,
            pager_id=LANGUAGES_PAGER_ID,
            screen_id="com.linkedin.sdui.flagshipnav.profile.ProfileLanguageDetails",
            start=0,
            count=count,
        ),
        params={
            "sduiid": LANGUAGES_PAGER_ID,
            "parentSpanId": _base64_token(8),
        },
    )
    return decode_flight_frames(raw)


_ENDORSE_ARIA_RE = re.compile(r"^Endorse\s+(.+)$")


def skill_names(frames: list[Any]) -> list[str]:
    """Skill names from the pagination stream.

    Each skill entity carries an endorse-button `aria-label` of the form
    "Endorse <skill name>", present regardless of viewer relationship or
    endorsement visibility. That is a more reliable anchor than the plain
    display text, which lives in a separately-referenced frame this walk
    does not need to resolve.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            aria = value.get("aria-label")
            if isinstance(aria, str):
                match = _ENDORSE_ARIA_RE.match(aria)
                if match:
                    name = match.group(1).strip()
                    if name and name not in seen:
                        seen.add(name)
                        found.append(name)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for frame in frames:
        walk(frame)
    if found:
        return found

    # A second currently observed stream shape renders each skill as plain
    # visible text and omits the endorse button entirely. This is the shape
    # returned for some viewer/session combinations.
    return [
        value
        for value in visible_strings(frames)
        if not value.startswith(("{", "[")) and value.lower() not in {"skills", "show all skills"}
    ]


_LANGUAGE_UI_LABELS = {
    "languages",
    "show all languages",
    "show fewer languages",
    "expanded",
    "collapsed",
}
_LANGUAGE_PROFICIENCIES = {
    "elementary proficiency",
    "limited working proficiency",
    "professional working proficiency",
    "full professional proficiency",
    "native or bilingual proficiency",
}


def language_values(frames: list[Any]) -> list[str]:
    """Return ordered language names and proficiency labels from an RSC stream.

    The language detail pager renders each row as a name followed by an
    optional LinkedIn proficiency label. Keeping that order lets the domain
    mapper preserve languages whose proficiency is not visible rather than
    inventing a value.
    """
    found: list[str] = []
    seen_names: set[str] = set()
    for value in visible_strings(frames):
        cleaned = value.strip()
        lowered = cleaned.casefold()
        if lowered in _LANGUAGE_UI_LABELS:
            continue
        if lowered in _LANGUAGE_PROFICIENCIES:
            if found and found[-1].casefold() not in _LANGUAGE_PROFICIENCIES:
                found.append(cleaned)
            continue
        if cleaned not in seen_names:
            seen_names.add(cleaned)
            found.append(cleaned)
    return found


def decode_flight_frames(raw: bytes) -> list[Any]:
    """Decode the JSON-bearing frames from LinkedIn's text RSC stream."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnexpectedPayload("RSC response was not UTF-8 text.") from exc

    frames: list[Any] = []
    for line in text.splitlines():
        _, separator, payload = line.partition(":")
        if not separator:
            continue
        try:
            frames.append(json.loads(payload))
        except json.JSONDecodeError:
            continue

    if not frames:
        raise UnexpectedPayload("RSC response contained no JSON frames.")
    return frames


def text_fragments(frames: list[Any]) -> list[str]:
    """Collect user-visible text values from decoded SDUI component trees."""
    found: list[str] = []

    def walk(value: Any, parent_key: str | None = None) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, key)
        elif isinstance(value, list):
            for child in value:
                walk(child, parent_key)
        elif isinstance(value, str) and parent_key in {"text", "stringValue", "content"}:
            stripped = value.strip()
            if stripped and stripped not in found:
                found.append(stripped)

    for frame in frames:
        walk(frame)
    return found


def visible_strings(frames: list[Any]) -> list[str]:
    """Collect likely human-facing strings while excluding SDUI internals."""
    found: list[str] = []

    def is_visible(value: str) -> bool:
        if len(value) < 2 or len(value) > 1_500 or not re.search(r"[A-Za-z]", value):
            return False
        lowered = value.lower()
        if lowered in {
            "stringvalue",
            "intvalue",
            "booleanvalue",
            "floatvalue",
            "floatexpression",
            "profilecardsservedevent",
            "oncomponentappear",
            "bindableboolean",
            "collectionnamespace",
            "lazycolumn",
            "fillavailable",
            "fullpage",
            "ghostcompact",
        }:
            return False
        if value.startswith("urn:") or value.lower().endswith(" logo"):
            return False
        if re.fullmatch(r"[A-Za-z0-9+/]{20,}={0,2}", value):
            return False
        if value.startswith("$") or ":props:" in value:
            return False
        if any(
            marker in lowered
            for marker in (
                "com.linkedin.",
                "proto.sdui.",
                "profilecomponentstate",
                "entity-collection",
                "auto-component",
                "presentationstyle_",
                "modalsize_",
                "colorscheme_",
                "binding",
                "profile_",
                "profile-",
                "experience-",
                "var(--",
                "http://",
                "https://",
                "/in/",
            )
        ):
            return False
        if "_" in value or value.startswith("--"):
            return False
        if " " not in value and not re.search(r"[A-Z]", value):
            return False
        return not re.fullmatch(r"[a-f0-9_\- ]{16,}", lowered)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            stripped = value.strip()
            if is_visible(stripped) and stripped not in found:
                found.append(stripped)

    for frame in frames:
        walk(frame)
    return found

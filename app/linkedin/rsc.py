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
PROFILE_CARDS_ABOVE_ACTIVITY = (
    "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsAboveActivity"
)
PROFILE_CARDS_EXPERIENCE = "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly"
PROFILE_CARDS_BELOW_ACTIVITY = (
    "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsBelowActivityPart1WithoutExp"
)


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

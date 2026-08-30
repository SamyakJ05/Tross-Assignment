"""The HTTP client: pacing, and turning ambiguous responses into typed errors.

The classification logic in `_classify` is the substance of this module.
LinkedIn signals failure through at least five channels that are not the
status code, so a client that trusts `raise_for_status()` will parse an
interstitial HTML page as a profile and report success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from app.config import Settings
from app.linkedin.errors import (
    ChallengeRequired,
    NotFound,
    RateLimited,
    RequestDenied,
    SessionExpired,
    StaleQueryId,
    TransportError,
    UnexpectedPayload,
    UnexpectedRedirect,
)
from app.linkedin.session import Session

log = logging.getLogger(__name__)

# Substrings that identify LinkedIn's HTML rate-limit interstitial, which is
# served with a 200 and would otherwise parse as a successful empty response.
_INTERSTITIAL_MARKERS = (
    "visiting a very high number of pages",
    "unusual activity",
    "/authwall",
)

_CHALLENGE_MARKERS = ("/checkpoint/challenge", "/checkpoint/lg/login-submit", "captcha")


class Pacer:
    """Serialises outbound requests with a minimum interval.

    An explicit caveat, because it would be easy to mistake this for a
    defence: pacing does not defeat LinkedIn's detection. Their published
    patents describe modelling the *sequence* of request paths, and their
    engineering blog describes an automation score built specifically to
    catch low-frequency automation. Random delays perturb timing while
    leaving the sequence shape -- the thing actually modelled -- unchanged.

    What this does buy is a low absolute request volume and no bursts, which
    is appropriate for a service whose legitimate load is a handful of
    lookups. It is a politeness control, not an evasion.

    The interval is only enforced across calls that share one `Pacer`
    instance. A caller that builds a fresh `LinkedInClient` (and therefore a
    fresh `Pacer`) per request gets pacing within that single fetch but not
    across concurrent fetches -- the app entrypoint must construct one
    `Pacer` and pass it into every `LinkedInClient` it creates for that to
    hold across the whole process.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


class LinkedInClient:
    def __init__(
        self,
        settings: Settings,
        session: Session | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        self._settings = settings
        self.session = session or Session(settings)
        # A caller that shares one Pacer across every LinkedInClient it
        # creates gets a process-wide request budget; one created here
        # per instance only paces requests within this single client's
        # lifetime. See Pacer's docstring.
        self._pacer = pacer or Pacer(settings.min_request_interval_seconds)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LinkedInClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,  # a redirect is signal; see _classify
            http2=True,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("LinkedInClient must be used as an async context manager")
        return self._client

    @property
    def settings(self) -> Settings:
        """Read-only settings needed by request strategy selection."""
        return self._settings

    # -- requests ---------------------------------------------------------

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        query_id: str | None = None,
    ) -> dict[str, Any]:
        """Authenticated Voyager GET returning parsed JSON, or a typed error."""
        await self._pacer.wait()
        try:
            resp = await self.http.get(url, headers=headers or self.session.headers())
        except httpx.HTTPError as exc:
            raise TransportError(f"Transport failure contacting LinkedIn: {exc}") from exc

        self._classify(resp, query_id=query_id)

        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            preview = resp.text[:200].replace("\n", " ")
            raise UnexpectedPayload(
                f"Expected JSON, got {resp.headers.get('content-type', 'unknown')}: {preview!r}"
            ) from exc

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        """Unauthenticated GET for the public fallback tier."""
        await self._pacer.wait()
        try:
            resp = await self.http.get(
                url,
                headers=headers or self.session.public_headers(),
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"Transport failure contacting LinkedIn: {exc}") from exc

        self._classify(resp, public=True)
        return resp.text

    async def post_stream(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        params: dict[str, str],
    ) -> bytes:
        """Authenticated POST for an RSC stream, whose payload is not JSON."""
        await self._pacer.wait()
        try:
            resp = await self.http.post(url, headers=headers, json=payload, params=params)
        except httpx.HTTPError as exc:
            raise TransportError(f"Transport failure contacting LinkedIn: {exc}") from exc

        self._classify(resp, expected_json=False)
        return resp.content

    # -- classification ---------------------------------------------------

    def _classify(
        self,
        resp: httpx.Response,
        *,
        query_id: str | None = None,
        public: bool = False,
        expected_json: bool = True,
    ) -> None:
        """Raise the specific error this response represents, or return.

        Ordered by how early each signal is decided upstream, so the most
        fundamental block is reported rather than a downstream symptom.
        """
        status = resp.status_code

        # 999: LinkedIn's own non-standard code, decided before application
        # logic. UA or IP reputation. Waiting does not help.
        if status == 999:
            raise RequestDenied(
                "HTTP 999 Request Denied. LinkedIn rejected this request before it reached "
                "application logic, which points at user-agent or IP reputation rather than "
                "anything about the request itself. Datacentre ranges are commonly flagged.",
                status=status,
            )

        # A redirect towards a checkpoint means the session needs a human.
        location = resp.headers.get("location", "")
        if status in (301, 302, 303, 307, 308) and any(
            m in location.lower() for m in _CHALLENGE_MARKERS
        ):
            raise ChallengeRequired(
                f"Redirected to a checkpoint challenge ({location}). This session needs a human "
                f"to complete verification in a browser; the cookie must then be re-harvested. "
                f"A cookie harvested at one IP and used from another is a known trigger.",
                status=status,
            )

        if status in (301, 302, 303, 307, 308):
            # LinkedIn invalidates an expired li_at cookie by redirecting the
            # request to itself while sending a deletion Set-Cookie. Treating
            # this as an endpoint-contract redirect lets thin RSC fragments
            # masquerade as an authenticated success.
            deleting_li_at = any(
                value.lower().startswith("li_at=")
                and ("max-age=0" in value.lower() or "01-jan-1970" in value.lower())
                for value in resp.headers.get_list("set-cookie")
            )
            if deleting_li_at:
                raise SessionExpired(
                    "LinkedIn invalidated the backend li_at session. Replace LINKEDIN_LI_AT "
                    "with a fresh cookie from an authorized browser session.",
                    status=status,
                )
            raise UnexpectedRedirect(
                f"LinkedIn redirected a data request to {location!r}. The endpoint contract, "
                f"session context, or persisted-query route is no longer valid for this "
                f"request.",
                status=status,
                location=location or None,
            )

        if status in (401, 403):
            raise SessionExpired(
                "Session rejected (401/403). The li_at cookie has expired or been invalidated. "
                "Voyager sessions commonly last only a few days in practice regardless of the "
                "cookie's nominal expiry.",
                status=status,
            )

        if status == 429:
            retry_after = resp.headers.get("retry-after")
            raise RateLimited(
                "Rate limited (429).",
                status=status,
                retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
            )

        if status == 404:
            raise NotFound("Profile not found.", status=status)

        # A 400 on a GraphQL call is nearly always a queryId the server no
        # longer recognises. Worth its own error because the remedy is
        # specific: re-capture the hash and update the registry.
        if status == 400 and query_id:
            raise StaleQueryId(
                f"LinkedIn rejected queryId {query_id!r}. These hashes rotate whenever the "
                f"relevant frontend deploys. Re-capture a current one from DevTools and update "
                f"app/linkedin/queries.py.",
                query_id=query_id,
                status=status,
            )

        if status >= 500:
            raise TransportError(f"LinkedIn server error ({status}).", status=status)

        if status >= 400:
            raise UnexpectedPayload(f"Unexpected status {status}.", status=status)

        # --- the dangerous case: a 200 that is not a success --------------
        body_head = resp.text[:4000].lower()
        if any(m in body_head for m in _INTERSTITIAL_MARKERS):
            raise RateLimited(
                "Received LinkedIn's HTML rate-limit interstitial with a 200 status. This is a "
                "throttle, not a successful response; a client that trusts the status code "
                "records it as success and silently stores empty data.",
                status=status,
            )

        if any(m in body_head for m in _CHALLENGE_MARKERS):
            raise ChallengeRequired(
                "Received a checkpoint challenge page with a 200 status.", status=status
            )

        # An authenticated Voyager endpoint returning HTML has not answered
        # the question we asked, whatever the status says.
        if not public and expected_json:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                raise UnexpectedPayload(
                    f"Voyager returned {ctype!r} rather than JSON with a {status} status.",
                    status=status,
                )

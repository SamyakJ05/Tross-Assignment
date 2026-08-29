"""Session reconstruction: cookies, CSRF derivation, and header realism.

This module is the actual reverse-engineering surface. Everything here was
derived by observing what LinkedIn's own web client sends, and each header
below is present for a reason recorded in the docstrings -- several of them
are load-bearing in non-obvious ways.

There is deliberately no login flow. LinkedIn's authentication gates on
CAPTCHA, 2FA and checkpoint challenges that cannot be cleared from a raw
HTTP client, and attempting it programmatically is both fragile and the
fastest route to an account restriction. The session is harvested once from
a browser and injected through the environment, which is what production
systems in this space actually do.
"""

from __future__ import annotations

import json
import re
import uuid

from app.config import Settings

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"
WWW_BASE = "https://www.linkedin.com"


class Session:
    """Holds the harvested cookies and builds request headers from them."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cookie_header = settings.linkedin_cookie_header.strip()
        self._li_at = self._cookie_value("li_at") or settings.linkedin_li_at
        self._jsessionid = self._cookie_value("JSESSIONID") or settings.linkedin_jsessionid

    def _cookie_value(self, name: str) -> str:
        """Read one cookie from an injected Cookie header without logging it."""
        match = re.search(rf"(?:^|;\s*){re.escape(name)}=(\"[^\"]*\"|[^;]*)", self._cookie_header)
        return match.group(1).strip() if match else ""

    # -- identity ---------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return bool(self._li_at and self._jsessionid)

    @property
    def csrf_token(self) -> str:
        """The csrf-token header value: JSESSIONID with its quotes stripped.

        LinkedIn uses a double-submit cookie scheme. The server checks only
        that this header equals the JSESSIONID cookie -- it keeps no
        server-side token state.

        That is less naive than it first looks. A cross-origin attacker can
        cause a browser to *send* your cookies but the same-origin policy
        stops them *reading* those cookies, so they cannot populate a
        matching header. The scheme is stateless and sound in a browser.

        It offers no protection here for the simple reason that we hold the
        cookie directly rather than acting through a browser, so we can read
        it and echo it. Understanding that distinction is the difference
        between copying this line and knowing why it works.
        """
        return self._jsessionid.strip('"')

    def cookie_header(self) -> str:
        """Cookie header, byte-identical to a browser's.

        JSESSIONID keeps its literal double quotes here; only the derived
        csrf-token drops them. Sending an unquoted JSESSIONID cookie is a
        subtle mismatch against what a real client sends.
        """
        if self._cookie_header:
            return self._cookie_header
        return f"li_at={self._li_at}; JSESSIONID={self._jsessionid}"

    # -- headers ----------------------------------------------------------

    def _li_track(self) -> str:
        """The x-li-track telemetry blob.

        LinkedIn's client sends this on every request describing itself.
        Omitting it is a trivially detectable difference from a real client,
        so we send a coherent one. `clientVersion` lives in config because it
        drifts with LinkedIn's real deploys and a stale value is itself a
        signal.
        """
        return json.dumps(
            {
                "clientVersion": self._settings.li_client_version,
                "mpVersion": self._settings.li_client_version,
                "osName": "web",
                "timezoneOffset": 5.5,
                "timezone": "Asia/Calcutta",
                "deviceFormFactor": "DESKTOP",
                "mpName": "voyager-web",
                "displayDensity": 2,
                "displayWidth": 2560,
                "displayHeight": 1440,
            },
            separators=(",", ":"),
        )

    def _page_instance(self, page_key: str) -> str:
        """x-li-page-instance: which page is making the call, plus a per-page UUID.

        A real client generates a fresh UUID per page load and reuses it
        across every request that page fires. We mirror that: one instance
        per logical fetch, shared by its sub-requests. A new UUID on every
        single request would be a distinctive pattern in its own right.
        """
        return f"urn:li:page:{page_key};{uuid.uuid4()}"

    def headers(
        self,
        *,
        page_key: str = "d_flagship3_profile_view_base",
        page_instance: str | None = None,
        referer: str | None = None,
    ) -> dict[str, str]:
        """Full header set for an authenticated Voyager request."""
        h = {
            "cookie": self.cookie_header(),
            "csrf-token": self.csrf_token,
            # Selects the normalized response format: {data, included[]} with
            # a flat, URN-keyed entity array. Without it the same endpoint
            # returns a deeply nested structure that is far worse to parse.
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            # Changes the response encoding entirely. Its absence produces
            # confusing shapes rather than clean errors, which makes it one
            # of the harder omissions to debug.
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
            "x-li-track": self._li_track(),
            "x-li-page-instance": page_instance or self._page_instance(page_key),
            "user-agent": self._settings.user_agent,
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
            # A cross-origin fetch from the SPA, which is what this is
            # pretending to be. Browsers set these automatically and their
            # absence is conspicuous.
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer or f"{WWW_BASE}/feed/",
        }
        return h

    def public_headers(self) -> dict[str, str]:
        """Headers for the unauthenticated fallback tier.

        No cookies and no Voyager-specific headers -- this tier fetches the
        ordinary public profile page. The user-agent still has to look like a
        browser: a bare client-library UA draws an immediate 999.
        """
        return {
            "user-agent": self._settings.user_agent,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        }

    def new_page_instance(self, page_key: str = "d_flagship3_profile_view_base") -> str:
        """Mint one page instance to share across a logical fetch's requests."""
        return self._page_instance(page_key)

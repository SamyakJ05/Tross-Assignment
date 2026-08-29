"""Typed errors, one per rung of LinkedIn's actual failure ladder.

LinkedIn does not fail with clean HTTP semantics. It returns HTML pages
where JSON is expected, a non-standard 999 status, redirects to checkpoint
flows, and -- most dangerously -- 200 responses carrying degraded data.
Collapsing all of that into `raise_for_status()` throws away the only
information that tells you what to do next.

Each class below carries `retryable` and `tier_fatal`:

  retryable    worth trying again on this tier, later
  tier_fatal   this tier is unusable for now; fall through to the next

They differ. A 429 is retryable but tier-fatal right now. A checkpoint is
neither -- no amount of retrying or falling through fixes it, because it
needs a human in a browser.
"""

from __future__ import annotations


class LinkedInError(Exception):
    """Base for every upstream failure."""

    retryable: bool = False
    tier_fatal: bool = True
    code: str = "linkedin_error"

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class RequestDenied(LinkedInError):
    """HTTP 999.

    LinkedIn's own non-standard status, returned before any application
    logic runs. Fires on a non-browser user-agent or an IP whose reputation
    is poor -- frequently on the very first request from a datacentre range.

    It is not a rate limit and waiting does not clear it. On a cloud host
    this is the single most likely reason the authenticated tier never works
    at all, which is precisely why the public fallback tier exists.
    """

    retryable = False
    code = "request_denied"


class RateLimited(LinkedInError):
    """429, or the HTML interstitial that means the same thing.

    Note that Voyager has no documented Retry-After contract, so any backoff
    here is a guess. Treated as retryable but not immediately.
    """

    retryable = True
    code = "rate_limited"

    def __init__(self, message: str, *, status: int | None = None, retry_after: int | None = None):
        super().__init__(message, status=status)
        self.retry_after = retry_after


class ChallengeRequired(LinkedInError):
    """A checkpoint or verification redirect.

    Triggered by 2FA, by rate limits, and -- the case that matters for a
    cloud deployment -- by an IP or location change mid-session. A session
    cookie harvested at one location and used from another is exactly this
    pattern.

    Unrecoverable from a raw HTTP client by design. Resolution requires a
    human completing the challenge in a real browser, after which the cookie
    must be re-harvested.
    """

    retryable = False
    code = "challenge_required"


class UnexpectedRedirect(LinkedInError):
    """A redirect from an endpoint that is expected to return data.

    Unlike a checkpoint redirect, this does not identify a recoverable human
    action. It usually means the endpoint contract, session context, or
    persisted-query route has changed. Treating it as success would otherwise
    lead to a JSON decoding error that hides the useful upstream signal.
    """

    retryable = False
    code = "unexpected_redirect"

    def __init__(self, message: str, *, status: int | None = None, location: str | None = None):
        super().__init__(message, status=status)
        self.location = location


class SessionExpired(LinkedInError):
    """401 or 403. The cookie is no longer valid.

    Practitioner reports put effective Voyager session life at roughly 3-7
    days regardless of the cookie's nominal expiry, so this is an expected
    event on a long-running deployment rather than an exceptional one.
    """

    retryable = False
    code = "session_expired"


class NotFound(LinkedInError):
    """404, or a resolvable slug that yields no profile.

    Distinct from a block: the request was accepted and answered.
    """

    retryable = False
    tier_fatal = False
    code = "not_found"


class UnexpectedPayload(LinkedInError):
    """A 200 whose body is not what the endpoint is supposed to return.

    Most often an HTML error or interstitial page served with a 200, or a
    JSON body missing the structure the parser needs. Tier-fatal because
    retrying an endpoint that has changed shape will not change the shape.
    """

    retryable = False
    code = "unexpected_payload"


class StaleQueryId(LinkedInError):
    """A GraphQL queryId the server no longer recognises.

    Distinguished from a generic 400 because it has a specific remedy --
    re-capture the hash from DevTools and update the registry -- and because
    it is the failure this whole system is designed to survive.
    """

    retryable = False
    code = "stale_query_id"

    def __init__(self, message: str, *, query_id: str | None = None, status: int | None = None):
        super().__init__(message, status=status)
        self.query_id = query_id


class TransportError(LinkedInError):
    """Connection reset, timeout, DNS failure. Nothing to do with LinkedIn's
    application layer, and genuinely worth retrying."""

    retryable = True
    code = "transport_error"

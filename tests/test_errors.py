"""Response classification: every rung of the failure ladder.

This is the file that justifies the whole error module. LinkedIn signals
failure through at least five channels that are not the status code, and a
client that trusts raise_for_status() parses an interstitial as a profile
and reports success.

The 200-that-is-not-a-success tests are the important ones.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.linkedin.client import LinkedInClient
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


def _response(
    status: int,
    *,
    text: str = "{}",
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
) -> httpx.Response:
    merged = {"content-type": content_type}
    merged.update(headers or {})
    return httpx.Response(status_code=status, text=text, headers=merged)


@pytest.fixture
def client(settings: Settings) -> LinkedInClient:
    return LinkedInClient(settings)


# ---------------------------------------------------------------------------
# Status-code rungs
# ---------------------------------------------------------------------------


def test_999_is_request_denied(client: LinkedInClient) -> None:
    """LinkedIn's non-standard code, decided before application logic."""
    with pytest.raises(RequestDenied) as exc:
        client._classify(_response(999, content_type="text/html"))
    assert "999" in str(exc.value)
    assert exc.value.retryable is False


def test_999_message_names_the_likely_cause(client: LinkedInClient) -> None:
    """The error should point at UA or IP, not just report a number."""
    with pytest.raises(RequestDenied, match="IP reputation"):
        client._classify(_response(999, content_type="text/html"))


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_session_expired(client: LinkedInClient, status: int) -> None:
    with pytest.raises(SessionExpired):
        client._classify(_response(status))


def test_429_is_rate_limited_and_retryable(client: LinkedInClient) -> None:
    with pytest.raises(RateLimited) as exc:
        client._classify(_response(429, headers={"retry-after": "120"}))
    assert exc.value.retryable is True
    assert exc.value.retry_after == 120


def test_429_without_retry_after(client: LinkedInClient) -> None:
    """Voyager has no documented Retry-After contract, so absence is normal."""
    with pytest.raises(RateLimited) as exc:
        client._classify(_response(429))
    assert exc.value.retry_after is None


def test_404_is_not_found_and_not_tier_fatal(client: LinkedInClient) -> None:
    """A 404 was answered, so it should not disable the tier."""
    with pytest.raises(NotFound) as exc:
        client._classify(_response(404))
    assert exc.value.tier_fatal is False


def test_500_is_transport_error_and_retryable(client: LinkedInClient) -> None:
    with pytest.raises(TransportError) as exc:
        client._classify(_response(503))
    assert exc.value.retryable is True


# ---------------------------------------------------------------------------
# Challenge detection
# ---------------------------------------------------------------------------


def test_checkpoint_redirect_is_challenge(client: LinkedInClient) -> None:
    with pytest.raises(ChallengeRequired):
        client._classify(
            _response(302, headers={"location": "https://www.linkedin.com/checkpoint/challenge/"})
        )


def test_challenge_message_mentions_ip_change(client: LinkedInClient) -> None:
    """The cloud-deployment failure mode should be named in the error."""
    with pytest.raises(ChallengeRequired, match="harvested at one IP"):
        client._classify(
            _response(302, headers={"location": "https://www.linkedin.com/checkpoint/challenge/"})
        )


def test_ordinary_redirect_is_reported_explicitly(client: LinkedInClient) -> None:
    with pytest.raises(UnexpectedRedirect) as exc:
        client._classify(_response(302, headers={"location": "https://www.linkedin.com/feed/"}))
    assert exc.value.status == 302


# ---------------------------------------------------------------------------
# The dangerous rung: 200 responses that are not successes
# ---------------------------------------------------------------------------


def test_interstitial_with_200_is_rate_limited(
    client: LinkedInClient, interstitial_html: str
) -> None:
    """The failure a naive client records as success.

    LinkedIn serves its throttle page with a 200. Trusting the status code
    here means silently storing empty data.
    """
    with pytest.raises(RateLimited):
        client._classify(
            _response(200, text=interstitial_html, content_type="text/html", headers={})
        )


def test_authwall_with_200_is_rate_limited(client: LinkedInClient) -> None:
    body = '<html><body><a href="/authwall?trk=x">Sign in</a></body></html>'
    with pytest.raises(RateLimited):
        client._classify(_response(200, text=body, content_type="text/html"))


def test_challenge_page_with_200_is_detected(client: LinkedInClient) -> None:
    body = '<html><form action="/checkpoint/challenge/verify">captcha</form></html>'
    with pytest.raises(ChallengeRequired):
        client._classify(_response(200, text=body, content_type="text/html"))


def test_html_from_voyager_is_unexpected_payload(client: LinkedInClient) -> None:
    """An endpoint that should return JSON returning HTML has not answered."""
    with pytest.raises(UnexpectedPayload):
        client._classify(_response(200, text="<html>hello</html>", content_type="text/html"))


def test_html_is_allowed_on_the_public_tier(client: LinkedInClient) -> None:
    """The public tier fetches a page, so HTML is correct there."""
    client._classify(
        _response(200, text="<html>profile</html>", content_type="text/html"), public=True
    )


def test_clean_json_200_passes(client: LinkedInClient) -> None:
    client._classify(_response(200, text='{"data": {}}'))


# ---------------------------------------------------------------------------
# Stale queryId
# ---------------------------------------------------------------------------


def test_400_with_query_id_is_stale_query_id(client: LinkedInClient) -> None:
    """Distinguished from a generic 400 because the remedy is specific."""
    with pytest.raises(StaleQueryId) as exc:
        client._classify(_response(400), query_id="voyagerIdentityDashProfileComponents.abc123")
    assert exc.value.query_id == "voyagerIdentityDashProfileComponents.abc123"


def test_stale_query_id_message_names_the_fix(client: LinkedInClient) -> None:
    with pytest.raises(StaleQueryId, match="DevTools"):
        client._classify(_response(400), query_id="x.y")


def test_400_without_query_id_is_generic(client: LinkedInClient) -> None:
    with pytest.raises(UnexpectedPayload):
        client._classify(_response(400))

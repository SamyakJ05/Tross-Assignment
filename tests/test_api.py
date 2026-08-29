"""The HTTP contract.

The service layer is stubbed here: these tests are about auth, validation,
caching and error mapping, not about parsing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.linkedin.errors import ChallengeRequired, NotFound, RequestDenied, UnexpectedRedirect
from app.models.domain import Profile
from app.models.envelope import Completeness, ProfileResponse, SectionSource, Tier

HEADERS = {"X-API-Key": "test-key-one"}


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, settings: Settings):
    get_settings.cache_clear()
    monkeypatch.setenv("API_KEYS", "test-key-one,test-key-two")
    monkeypatch.setenv("LINKEDIN_LI_AT", "AQEDTEST")
    monkeypatch.setenv("LINKEDIN_JSESSIONID", '"ajax:1111111111111111111"')

    import app.main as main

    monkeypatch.setattr(main, "settings", get_settings())

    async def fake_get_profile(slug: str, _settings: Settings) -> ProfileResponse:
        return ProfileResponse(
            profile=Profile(
                public_identifier=slug,
                full_name="Asha Raman",
                headline="Engineer",
            ),
            completeness=Completeness.COMPLETE,
            sources=[SectionSource(section="top_card", tier=Tier.VOYAGER_GRAPHQL)],
        )

    monkeypatch.setattr(main, "get_profile", fake_get_profile)

    with TestClient(main.app) as client:
        yield client

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_requires_api_key(app_client: TestClient) -> None:
    resp = app_client.get("/v1/profile", params={"url": "https://linkedin.com/in/x"})
    assert resp.status_code == 401


def test_rejects_wrong_api_key(app_client: TestClient) -> None:
    resp = app_client.get(
        "/v1/profile",
        params={"url": "https://linkedin.com/in/x"},
        headers={"X-API-Key": "nope"},
    )
    assert resp.status_code == 401


def test_accepts_any_configured_key(app_client: TestClient) -> None:
    for key in ("test-key-one", "test-key-two"):
        resp = app_client.get(
            "/v1/profile",
            params={"url": "https://linkedin.com/in/x"},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 200


def test_health_is_open(app_client: TestClient) -> None:
    """An uptime check must not need a credential."""
    assert app_client.get("/v1/health").status_code == 200


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_returns_envelope_not_bare_profile(app_client: TestClient) -> None:
    """Provenance and completeness travel with the data, always."""
    resp = app_client.get(
        "/v1/profile",
        params={"url": "https://www.linkedin.com/in/samyakj05/"},
        headers=HEADERS,
    )
    body = resp.json()

    assert resp.status_code == 200
    assert body["profile"]["full_name"] == "Asha Raman"
    assert body["completeness"] == "complete"
    assert body["sources"][0]["tier"] == "voyager_graphql"
    assert "warnings" in body


def test_post_uses_request_scoped_credentials_without_caching(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    captured: list[Settings] = []

    async def capture_settings(slug: str, request_settings: Settings) -> ProfileResponse:
        captured.append(request_settings)
        return ProfileResponse(
            profile=Profile(public_identifier=slug),
            completeness=Completeness.PARTIAL,
        )

    monkeypatch.setattr(main, "get_profile", capture_settings)
    payload = {
        "url": "https://www.linkedin.com/in/request-session/",
        "credentials": {
            "LINKEDIN_LI_AT": "request-li-at",
            "LINKEDIN_JSESSIONID": "ajax:request",
            "LINKEDIN_COOKIE_HEADER": "li_at=request-li-at; JSESSIONID=\"ajax:request\"",
        },
    }

    response = app_client.post("/v1/profile", json=payload, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["cached"] is False
    assert captured[0].linkedin_li_at == "request-li-at"
    assert captured[0].linkedin_jsessionid == '"ajax:request"'
    assert captured[0].linkedin_cookie_header.startswith("li_at=")
    assert "request-li-at" not in response.text


def test_post_rejects_incomplete_request_scoped_credentials(app_client: TestClient) -> None:
    response = app_client.post(
        "/v1/profile",
        json={
            "url": "https://www.linkedin.com/in/request-session/",
            "credentials": {"LINKEDIN_LI_AT": "only-one-cookie"},
        },
        headers=HEADERS,
    )

    assert response.status_code == 422


def test_slug_extracted_from_messy_url(app_client: TestClient) -> None:
    resp = app_client.get(
        "/v1/profile",
        params={"url": "https://in.linkedin.com/in/samyakj05/details/experience/?trk=x"},
        headers=HEADERS,
    )
    assert resp.json()["profile"]["public_identifier"] == "samyakj05"


def test_invalid_url_is_422_with_reason(app_client: TestClient) -> None:
    resp = app_client.get(
        "/v1/profile",
        params={"url": "https://example.com/in/someone"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert "linkedin.com" in resp.json()["detail"]


def test_company_url_rejected(app_client: TestClient) -> None:
    resp = app_client.get(
        "/v1/profile",
        params={"url": "https://www.linkedin.com/company/northwind"},
        headers=HEADERS,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_second_request_is_served_from_cache(app_client: TestClient) -> None:
    params = {"url": "https://www.linkedin.com/in/cache-test/"}
    first = app_client.get("/v1/profile", params=params, headers=HEADERS).json()
    second = app_client.get("/v1/profile", params=params, headers=HEADERS).json()

    assert first["cached"] is False
    assert second["cached"] is True


def test_refresh_bypasses_cache(app_client: TestClient) -> None:
    params = {"url": "https://www.linkedin.com/in/refresh-test/"}
    app_client.get("/v1/profile", params=params, headers=HEADERS)
    refreshed = app_client.get(
        "/v1/profile", params={**params, "refresh": "true"}, headers=HEADERS
    ).json()
    assert refreshed["cached"] is False


def test_degraded_refresh_does_not_replace_a_better_cached_response(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient upstream redirect must not poison an otherwise good cache entry."""
    import app.main as main

    complete = ProfileResponse(
        profile=Profile(public_identifier="quality-test", full_name="Asha", headline="Engineer"),
        completeness=Completeness.COMPLETE,
    )
    degraded = ProfileResponse(
        profile=Profile(public_identifier="quality-test"),
        completeness=Completeness.NEEDS_REVIEW,
    )
    responses = iter((complete, degraded))

    async def sequential_get_profile(*_: object) -> ProfileResponse:
        return next(responses)

    monkeypatch.setattr(main, "get_profile", sequential_get_profile)
    params = {"url": "https://www.linkedin.com/in/quality-test/"}

    first = app_client.get("/v1/profile", params=params, headers=HEADERS).json()
    refreshed = app_client.get(
        "/v1/profile", params={**params, "refresh": "true"}, headers=HEADERS
    ).json()
    cached = app_client.get("/v1/profile", params=params, headers=HEADERS).json()

    assert first["completeness"] == "complete"
    assert refreshed["completeness"] == "needs_review"
    assert cached["completeness"] == "complete"
    assert cached["cached"] is True


def test_delete_purges_cache(app_client: TestClient) -> None:
    """A cache of personal data needs a deletion path, not just an expiry."""
    params = {"url": "https://www.linkedin.com/in/purge-test/"}
    app_client.get("/v1/profile", params=params, headers=HEADERS)

    deleted = app_client.request("DELETE", "/v1/profile", params=params, headers=HEADERS)
    assert deleted.json()["purged"] is True

    after = app_client.get("/v1/profile", params=params, headers=HEADERS).json()
    assert after["cached"] is False


# ---------------------------------------------------------------------------
# Upstream error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected_status,expected_code",
    [
        (NotFound("gone"), 404, "not_found"),
        (RequestDenied("blocked"), 502, "request_denied"),
        (ChallengeRequired("checkpoint"), 503, "challenge_required"),
        (UnexpectedRedirect("moved", status=302), 502, "unexpected_redirect"),
    ],
)
def test_upstream_errors_map_to_sensible_statuses(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """LinkedIn's status is not always the right status for our caller.

    A 999 means nothing to a client; 502 'we are blocked upstream' does.
    """
    import app.main as main

    async def failing(slug: str, _settings) -> ProfileResponse:
        raise error

    monkeypatch.setattr(main, "get_profile", failing)

    resp = app_client.get(
        "/v1/profile",
        params={"url": "https://www.linkedin.com/in/err/", "refresh": "true"},
        headers=HEADERS,
    )
    assert resp.status_code == expected_status
    assert resp.json()["error"] == expected_code


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_reports_query_registry_ages(app_client: TestClient) -> None:
    """The leading indicator of silent extraction failure."""
    body = app_client.get("/v1/health").json()

    assert body["status"] == "ok"
    assert body["session_configured"] is True
    assert isinstance(body["query_registry"], list)
    assert all("age_days" in q for q in body["query_registry"])


def test_health_warns_about_unconfigured_queries(app_client: TestClient) -> None:
    body = app_client.get("/v1/health").json()
    assert any("no queryId configured" in w for w in body["query_registry_warnings"])


def test_health_leaks_no_secrets(app_client: TestClient) -> None:
    """It reports whether a session exists, never what it is."""
    raw = app_client.get("/v1/health").text
    assert "AQEDTEST" not in raw
    assert "ajax:" not in raw

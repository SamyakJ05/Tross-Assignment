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
from app.ratelimit import reset_rate_limiter

HEADERS: dict[str, str] = {}


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, settings: Settings):
    get_settings.cache_clear()
    monkeypatch.setenv("LINKEDIN_LI_AT", "AQEDTEST")
    monkeypatch.setenv("LINKEDIN_JSESSIONID", '"ajax:1111111111111111111"')

    import app.main as main

    monkeypatch.setattr(main, "settings", get_settings())
    # The rate limiter's counters are process-wide, not per-app-instance, so
    # every test that reuses HEADERS's key must start from a clean budget.
    reset_rate_limiter()

    async def fake_get_profile(
        slug: str, _settings: Settings, **_kwargs: object
    ) -> ProfileResponse:
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
    reset_rate_limiter()


# ---------------------------------------------------------------------------
# Public URL-only contract
# ---------------------------------------------------------------------------


def test_profile_requires_only_the_url(app_client: TestClient) -> None:
    resp = app_client.get("/v1/profile", params={"url": "https://linkedin.com/in/x"})
    assert resp.status_code == 200


def test_openapi_has_one_public_profile_operation(app_client: TestClient) -> None:
    schema = app_client.get("/openapi.json").json()
    operation = schema["paths"]["/profile"]["get"]
    assert "security" not in operation
    assert {parameter["name"] for parameter in operation["parameters"]} == {"url", "refresh"}
    assert "/v1/profile" not in schema["paths"]


def test_openapi_documents_success_and_typed_failures(app_client: TestClient) -> None:
    operation = app_client.get("/openapi.json").json()["paths"]["/profile"]["get"]

    assert operation["operationId"] == "getLinkedInProfile"
    assert operation["tags"] == ["Profiles"]
    assert set(operation["responses"]) == {"200", "404", "422", "429", "502", "503"}
    assert "complete" in operation["responses"]["200"]["content"]["application/json"][
        "examples"
    ]
    assert operation["responses"]["404"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ErrorResponse")


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


def test_canonical_profile_route(app_client: TestClient) -> None:
    response = app_client.get(
        "/profile", params={"url": "https://www.linkedin.com/in/canonical/"}
    )
    assert response.status_code == 200
    assert response.json()["profile"]["public_identifier"] == "canonical"


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
    assert resp.json()["error"] == "invalid_url"
    assert "linkedin.com" in resp.json()["message"]


def test_missing_url_uses_the_same_error_envelope(app_client: TestClient) -> None:
    resp = app_client.get("/profile")

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"
    assert resp.json()["details"][0]["location"] == "query.url"


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

    async def sequential_get_profile(*_args: object, **_kwargs: object) -> ProfileResponse:
        return next(responses)

    monkeypatch.setattr(main, "get_profile", sequential_get_profile)
    params = {"url": "https://www.linkedin.com/in/quality-test/"}

    first = app_client.get("/v1/profile", params=params, headers=HEADERS).json()
    refreshed = app_client.get(
        "/v1/profile", params={**params, "refresh": "true"}, headers=HEADERS
    ).json()
    cached = app_client.get("/v1/profile", params=params, headers=HEADERS).json()

    assert first["completeness"] == "complete"
    assert refreshed["completeness"] == "complete"
    assert refreshed["cached"] is True
    assert cached["completeness"] == "complete"
    assert cached["cached"] is True


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

    async def failing(slug: str, _settings, **_kwargs: object) -> ProfileResponse:
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


def test_health_labels_unconfigured_queries_as_optional_compatibility(
    app_client: TestClient,
) -> None:
    body = app_client.get("/v1/health").json()

    assert body["primary_extraction"] == "dash_rest_and_rsc"
    assert body["query_registry_warnings"] == []
    assert "Optional GraphQL compatibility" in body["query_registry_note"]


def test_health_leaks_no_secrets(app_client: TestClient) -> None:
    """It reports whether a session exists, never what it is."""
    raw = app_client.get("/v1/health").text
    assert "AQEDTEST" not in raw
    assert "ajax:" not in raw


# ---------------------------------------------------------------------------
# Per-client rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_returns_429_after_the_configured_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One key cannot consume more than its own share of the LinkedIn-facing budget."""
    get_settings.cache_clear()
    monkeypatch.setenv("LINKEDIN_LI_AT", "AQEDTEST")
    monkeypatch.setenv("LINKEDIN_JSESSIONID", '"ajax:1111111111111111111"')
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")

    import app.main as main

    monkeypatch.setattr(main, "settings", get_settings())
    reset_rate_limiter()

    async def fake_get_profile(
        slug: str, _settings: Settings, **_kwargs: object
    ) -> ProfileResponse:
        return ProfileResponse(
            profile=Profile(public_identifier=slug), completeness=Completeness.PARTIAL
        )

    monkeypatch.setattr(main, "get_profile", fake_get_profile)

    params = {"url": "https://www.linkedin.com/in/rate-limit-test/"}

    with TestClient(main.app) as client:
        first = client.get("/v1/profile", params=params)
        second = client.get("/v1/profile", params=params)
        third = client.get("/v1/profile", params=params)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["error"] == "rate_limited"
    assert third.json()["retryable"] is True
    assert "Retry-After" in third.headers

    get_settings.cache_clear()
    reset_rate_limiter()


def test_rate_limit_is_scoped_per_client_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second caller must not be charged against the first caller's budget."""
    get_settings.cache_clear()
    monkeypatch.setenv("LINKEDIN_LI_AT", "AQEDTEST")
    monkeypatch.setenv("LINKEDIN_JSESSIONID", '"ajax:1111111111111111111"')
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")

    import app.main as main

    monkeypatch.setattr(main, "settings", get_settings())
    reset_rate_limiter()

    async def fake_get_profile(
        slug: str, _settings: Settings, **_kwargs: object
    ) -> ProfileResponse:
        return ProfileResponse(
            profile=Profile(public_identifier=slug), completeness=Completeness.PARTIAL
        )

    monkeypatch.setattr(main, "get_profile", fake_get_profile)
    params = {"url": "https://www.linkedin.com/in/rate-limit-scope-test/"}

    with TestClient(main.app) as client:
        a_first = client.get(
            "/v1/profile", params=params, headers={"CF-Connecting-IP": "192.0.2.1"}
        )
        a_second = client.get(
            "/v1/profile", params=params, headers={"CF-Connecting-IP": "192.0.2.1"}
        )
        b_first = client.get(
            "/v1/profile", params=params, headers={"CF-Connecting-IP": "192.0.2.2"}
        )

    assert a_first.status_code == 200
    assert a_second.status_code == 429
    assert b_first.status_code == 200

    get_settings.cache_clear()
    reset_rate_limiter()

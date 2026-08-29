"""Session reconstruction.

The CSRF derivation is the one line most people copy without understanding,
so it gets an explicit test rather than being covered incidentally.
"""

from __future__ import annotations

import json

from app.config import Settings
from app.linkedin.session import Session


def test_csrf_token_is_jsessionid_without_quotes(settings: Settings) -> None:
    session = Session(settings)
    assert session.csrf_token == "ajax:1111111111111111111"
    assert '"' not in session.csrf_token


def test_cookie_header_keeps_jsessionid_quotes(settings: Settings) -> None:
    """The cookie keeps its quotes; only the derived header drops them.

    Sending an unquoted JSESSIONID cookie is a subtle mismatch against a
    real browser and worth guarding against regression.
    """
    session = Session(settings)
    cookie = session.cookie_header()
    assert 'JSESSIONID="ajax:1111111111111111111"' in cookie
    assert "li_at=AQEDTESTTESTTESTTESTTESTTESTTEST" in cookie


def test_quotes_restored_when_environment_strips_them() -> None:
    """Secret managers routinely strip quotes; config restores them."""
    s = Settings(
        linkedin_li_at="",
        linkedin_jsessionid="ajax:2222222222222222222",
        linkedin_cookie_header="",
    )
    assert s.linkedin_jsessionid == '"ajax:2222222222222222222"'
    assert Session(s).csrf_token == "ajax:2222222222222222222"


def test_complete_cookie_header_supplies_the_authenticated_session() -> None:
    settings = Settings(
        linkedin_cookie_header=(
            'li_at=AQEDTEST; other=value; JSESSIONID="ajax:2222222222222222222"'
        )
    )
    session = Session(settings)

    assert settings.has_session is True
    assert session.is_authenticated is True
    assert session.csrf_token == "ajax:2222222222222222222"
    assert session.cookie_header() == settings.linkedin_cookie_header


def test_required_headers_present(settings: Settings) -> None:
    headers = Session(settings).headers()

    # Selects the normalized {data, included[]} format.
    assert headers["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    # Changes the response encoding entirely; absence produces confusing
    # shapes rather than clean errors.
    assert headers["x-restli-protocol-version"] == "2.0.0"
    assert headers["csrf-token"] == "ajax:1111111111111111111"
    assert "li_at" in headers["cookie"]


def test_li_track_is_valid_json_with_client_version(settings: Settings) -> None:
    track = json.loads(Session(settings).headers()["x-li-track"])
    assert track["clientVersion"] == settings.li_client_version
    assert track["osName"] == "web"
    assert track["deviceFormFactor"] == "DESKTOP"


def test_user_agent_is_not_a_client_library(settings: Settings) -> None:
    """A non-browser UA draws an immediate 999."""
    ua = Session(settings).headers()["user-agent"]
    assert "Mozilla" in ua
    for banned in ("python", "httpx", "curl", "requests"):
        assert banned not in ua.lower()


def test_page_instance_is_unique_per_call(settings: Settings) -> None:
    """A real client mints one UUID per page load, not per request."""
    session = Session(settings)
    a = session.new_page_instance()
    b = session.new_page_instance()
    assert a != b
    assert a.startswith("urn:li:page:")


def test_shared_page_instance_is_reused(settings: Settings) -> None:
    """Sub-requests of one logical fetch share the parent's instance."""
    session = Session(settings)
    instance = session.new_page_instance()
    first = session.headers(page_instance=instance)
    second = session.headers(page_instance=instance)
    assert first["x-li-page-instance"] == second["x-li-page-instance"] == instance


def test_public_headers_carry_no_credentials(settings: Settings) -> None:
    """The fallback tier must not leak the session into an anonymous fetch."""
    headers = Session(settings).public_headers()
    assert "cookie" not in headers
    assert "csrf-token" not in headers
    assert "Mozilla" in headers["user-agent"]


def test_is_authenticated_reflects_configuration(
    settings: Settings, anon_settings: Settings
) -> None:
    assert Session(settings).is_authenticated is True
    assert Session(anon_settings).is_authenticated is False

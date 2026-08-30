"""Fetch orchestration safety checks."""

from __future__ import annotations

from datetime import date

import pytest

import app.linkedin.fetchers as fetchers
from app.config import Settings
from app.linkedin.errors import SessionExpired, UnexpectedRedirect
from app.linkedin.queries import Query
from app.models.envelope import Tier


@pytest.mark.asyncio
async def test_terminal_section_error_stops_remaining_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One blocked section must not cause a request burst across every section."""
    result = fetchers.FetchResult()
    attempts: list[str] = []

    async def fake_resolve(*_: object, **__: object) -> tuple[str, dict]:
        return "urn:li:fsd_profile:TEST", {}

    async def fake_section(*_: object) -> dict:
        attempts.append("section")
        raise UnexpectedRedirect("redirected", status=302)

    monkeypatch.setattr(fetchers, "resolve_urn", fake_resolve)
    monkeypatch.setattr(fetchers, "_fetch_section", fake_section)
    async def fake_rsc(*_: object) -> list[object]:
        return []

    async def fake_fetch_skills(*_: object, **__: object) -> list[object]:
        return []

    monkeypatch.setattr(fetchers, "fetch_profile_component", fake_rsc)
    monkeypatch.setattr(fetchers, "fetch_skills", fake_fetch_skills)
    monkeypatch.setattr(
        fetchers,
        "configured_profile_components_query",
        lambda *_: Query(
            name="test",
            query_id="current",
            last_verified=date.today(),
            description="test query",
        ),
    )

    settings = Settings(
        linkedin_li_at="fixture",
        linkedin_jsessionid="ajax:123",
        linkedin_profile_components_query_id="current",
    )
    await fetchers._fetch_authenticated(
        type("Client", (), {"settings": settings})(),
        "fixture-person",
        "page",
        result,
    )

    assert attempts == ["section"]
    assert [warning.code for warning in result.warnings] == ["unexpected_redirect"]


@pytest.mark.asyncio
async def test_rsc_data_survives_a_dash_resolver_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RSC card has no URN dependency and must remain usable on its own."""
    result = fetchers.FetchResult()
    attempts: list[str] = []

    async def fake_rsc(*args: object) -> list[object]:
        attempts.append(f"rsc:{args[2]}")
        return [{"component": args[2]}]

    async def fake_resolve(*_: object, **__: object) -> tuple[str, dict]:
        attempts.append("dash")
        raise UnexpectedRedirect("redirected", status=302)

    monkeypatch.setattr(fetchers, "fetch_profile_component", fake_rsc)
    monkeypatch.setattr(
        fetchers,
        "visible_strings",
        lambda frames: ["RSC role"]
        if frames[0]["component"] == fetchers.PROFILE_CARDS_EXPERIENCE
        else [],
    )
    monkeypatch.setattr(fetchers, "resolve_urn", fake_resolve)

    settings = Settings(
        linkedin_li_at="example",
        linkedin_jsessionid="ajax:123",
        linkedin_profile_components_query_id="",
    )
    await fetchers._fetch_authenticated(
        type("Client", (), {"settings": settings})(),
        "fixture-person",
        "page",
        result,
    )

    assert result.rsc_sections == {"experience": ["RSC role"]}
    assert [warning.code for warning in result.warnings] == ["unexpected_redirect"]
    assert attempts[0] == "dash"


@pytest.mark.asyncio
async def test_expired_session_stops_before_rsc_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = fetchers.FetchResult()
    rsc_called = False

    async def expired(*_: object, **__: object) -> tuple[str, dict]:
        raise SessionExpired("expired")

    async def fake_rsc(*_: object) -> list[object]:
        nonlocal rsc_called
        rsc_called = True
        return []

    monkeypatch.setattr(fetchers, "resolve_urn", expired)
    monkeypatch.setattr(fetchers, "fetch_profile_component", fake_rsc)

    settings = Settings(linkedin_li_at="expired", linkedin_jsessionid="ajax:123")
    with pytest.raises(SessionExpired):
        await fetchers._fetch_authenticated(
            type("Client", (), {"settings": settings})(),
            "fixture-person",
            "page",
            result,
        )

    assert rsc_called is False


@pytest.mark.asyncio
async def test_skills_pager_uses_the_resolved_profile_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skills pager needs the numeric profileId, not the vanity slug."""
    result = fetchers.FetchResult()
    captured: dict[str, object] = {}

    async def fake_resolve(*_: object, **__: object) -> tuple[str, dict]:
        return "urn:li:fsd_profile:ACoAATEST", {}

    async def fake_rsc(*_: object) -> list[object]:
        return []

    async def fake_fetch_skills(_client: object, slug: str, profile_id: str, **_kw: object):
        captured["slug"] = slug
        captured["profile_id"] = profile_id
        return ["frame"]

    monkeypatch.setattr(fetchers, "resolve_urn", fake_resolve)
    monkeypatch.setattr(fetchers, "fetch_profile_component", fake_rsc)
    monkeypatch.setattr(fetchers, "fetch_skills", fake_fetch_skills)
    monkeypatch.setattr(fetchers, "skill_names", lambda _frames: ["Node.js", "Scrum"])
    monkeypatch.setattr(
        fetchers,
        "configured_profile_components_query",
        lambda *_: Query(name="test", query_id="", last_verified=date.today(), description="x"),
    )

    settings = Settings(linkedin_li_at="fixture", linkedin_jsessionid="ajax:123")
    await fetchers._fetch_authenticated(
        type("Client", (), {"settings": settings})(),
        "fixture-person",
        "page",
        result,
    )

    assert captured == {"slug": "fixture-person", "profile_id": "ACoAATEST"}
    assert result.rsc_sections["skills"] == ["Node.js", "Scrum"]
    assert any(s.section == "skills" and s.tier == Tier.LINKEDIN_RSC for s in result.sources)

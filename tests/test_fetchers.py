"""Fetch orchestration safety checks."""

from __future__ import annotations

from datetime import date

import pytest

import app.linkedin.fetchers as fetchers
from app.config import Settings
from app.linkedin.errors import UnexpectedRedirect
from app.linkedin.queries import Query


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

    settings = Settings(linkedin_profile_components_query_id="current", linkedin_cookie_header="")
    await fetchers._fetch_authenticated(
        type("Client", (), {"settings": settings})(),
        "fixture-person",
        "page",
        result,
    )

    assert attempts == ["section"]
    assert [warning.code for warning in result.warnings] == [
        "rsc_cookie_context_required",
        "unexpected_redirect",
    ]

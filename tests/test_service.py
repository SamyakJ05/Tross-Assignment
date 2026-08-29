"""The full raw-payload-to-response pipeline, without network access."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.linkedin.fetchers import FetchResult
from app.models.envelope import Completeness, SectionSource, Tier
from app.service import get_profile


@pytest.mark.asyncio
async def test_service_assembles_authenticated_sections(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    top_card: dict,
    experience: dict,
    education: dict,
    skills: dict,
) -> None:
    """Exercise the submitted response shape from representative raw payloads."""
    raw = FetchResult()
    raw.urn = "urn:li:fsd_profile:TEST"
    raw.top_card = top_card
    raw.sections = {
        "experience": experience,
        "education": education,
        "skills": skills,
    }
    raw.sources = [
        SectionSource(section="top_card", tier=Tier.VOYAGER_DASH_REST),
        SectionSource(section="experience", tier=Tier.VOYAGER_GRAPHQL),
        SectionSource(section="education", tier=Tier.VOYAGER_GRAPHQL),
        SectionSource(section="skills", tier=Tier.VOYAGER_GRAPHQL),
    ]

    async def fake_fetch_profile(*_: object) -> FetchResult:
        return raw

    monkeypatch.setattr("app.service.fetch_profile", fake_fetch_profile)

    response = await get_profile("fixture-person", settings)

    assert response.profile.full_name
    assert response.profile.headline
    assert response.profile.position_groups
    assert response.profile.educations
    assert response.profile.skills
    assert response.completeness is Completeness.PARTIAL
    assert [source.section for source in response.sources] == [
        "top_card",
        "experience",
        "education",
        "skills",
    ]


@pytest.mark.asyncio
async def test_service_returns_rsc_experience_when_dash_identity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    raw = FetchResult()
    raw.rsc_sections["experience"] = [
        "Northwind",
        "Full-time · 2 yrs",
        "Pune, India · On-site",
        "Software Engineer",
        "Aug 2024 - Present · 2 yrs",
    ]
    raw.sources = [SectionSource(section="experience", tier=Tier.LINKEDIN_RSC)]

    async def fake_fetch_profile(*_: object) -> FetchResult:
        return raw

    monkeypatch.setattr("app.service.fetch_profile", fake_fetch_profile)

    response = await get_profile("fixture-person", settings)

    assert response.profile.positions[0].title == "Software Engineer"
    assert response.completeness is Completeness.NEEDS_REVIEW

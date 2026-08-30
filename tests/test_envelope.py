"""Completeness assessment.

The `needs_review` state is what this module exists for: a fetch that
succeeded, parsed, and still produced something that looks wrong.
"""

from __future__ import annotations

from app.models.domain import Education, PositionGroup, Profile
from app.models.envelope import Completeness, SectionSource, Tier, assess


def _sources(*tiers: Tier) -> list[SectionSource]:
    return [SectionSource(section=f"s{i}", tier=t) for i, t in enumerate(tiers)]


def _full_profile() -> Profile:
    return Profile(
        public_identifier="x",
        full_name="Asha Raman",
        headline="Engineer",
        position_groups=[PositionGroup(company_name="Northwind")],
    )


def test_complete_when_primary_tier_and_core_fields_present() -> None:
    completeness, warnings = assess(_full_profile(), _sources(Tier.VOYAGER_GRAPHQL), [])
    assert completeness is Completeness.COMPLETE
    assert warnings == []


def test_complete_when_modern_dash_and_rsc_tiers_supply_the_profile() -> None:
    completeness, warnings = assess(
        _full_profile(),
        _sources(Tier.VOYAGER_DASH_REST, Tier.LINKEDIN_RSC),
        [],
    )

    assert completeness is Completeness.COMPLETE
    assert warnings == []


def test_partial_when_any_section_used_a_fallback() -> None:
    completeness, warnings = assess(
        _full_profile(),
        _sources(Tier.VOYAGER_GRAPHQL, Tier.PUBLIC_JSONLD),
        [],
    )
    assert completeness is Completeness.PARTIAL
    assert any(w.code == "degraded_tier" for w in warnings)


def test_missing_core_field_forces_needs_review() -> None:
    """A profile with no name is a parse failure, not a sparse profile."""
    profile = Profile(public_identifier="x", headline="Engineer")
    completeness, warnings = assess(profile, _sources(Tier.VOYAGER_GRAPHQL), [])

    assert completeness is Completeness.NEEDS_REVIEW
    assert any(w.code == "missing_core_field" for w in warnings)


def test_identity_without_history_is_the_stale_query_signature() -> None:
    """Name resolved, sections did not: the classic stale-queryId shape.

    The top card comes from a different endpoint than the sections, so this
    combination means the section fetches silently returned nothing.
    """
    profile = Profile(public_identifier="x", full_name="Asha Raman", headline="Engineer")
    completeness, warnings = assess(profile, _sources(Tier.VOYAGER_GRAPHQL), [])

    assert completeness is Completeness.NEEDS_REVIEW
    assert any(w.code == "identity_without_history" for w in warnings)


def test_education_alone_counts_as_history() -> None:
    """A student profile with no jobs is legitimate and must not be flagged."""
    profile = Profile(
        public_identifier="x",
        full_name="Asha Raman",
        headline="Student",
        educations=[Education(school_name="IIT Bombay")],
    )
    completeness, _ = assess(profile, _sources(Tier.VOYAGER_GRAPHQL), [])
    assert completeness is Completeness.COMPLETE


def test_severity_ordering_core_beats_degradation() -> None:
    """A missing name outranks any amount of tier degradation."""
    profile = Profile(public_identifier="x", headline="Engineer")
    completeness, _ = assess(profile, _sources(Tier.PUBLIC_JSONLD), [])
    assert completeness is Completeness.NEEDS_REVIEW


def test_no_sources_is_partial_not_complete() -> None:
    completeness, _ = assess(_full_profile(), [], [])
    assert completeness is Completeness.PARTIAL


def test_incoming_warnings_are_preserved() -> None:
    from app.models.envelope import Warning

    prior = [Warning(code="upstream", message="something happened")]
    _, warnings = assess(_full_profile(), _sources(Tier.VOYAGER_GRAPHQL), prior)
    assert any(w.code == "upstream" for w in warnings)


def test_degraded_sections_property() -> None:
    from app.models.envelope import ProfileResponse

    response = ProfileResponse(
        profile=_full_profile(),
        completeness=Completeness.PARTIAL,
        sources=[
            SectionSource(section="top_card", tier=Tier.VOYAGER_DASH_REST),
            SectionSource(section="experience", tier=Tier.LINKEDIN_RSC),
            SectionSource(section="skills", tier=Tier.PUBLIC_JSONLD),
        ],
    )
    assert response.degraded_sections == ["skills"]

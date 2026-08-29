"""Mapping payloads onto the domain model.

The promotion-history tests are the centre of this file. Flattening
LinkedIn's position groups is the single most common defect in profile
parsers, and it fails silently: you get plausible-looking output that has
quietly lost or duplicated a person's career history.
"""

from __future__ import annotations

from typing import Any

from app.linkedin.fetchers import extract_jsonld
from app.models.domain import Profile
from app.parsing.mappers import (
    apply_rsc_experience,
    map_education,
    map_experience,
    map_jsonld,
    map_skills,
    map_top_card,
    merge,
)


def test_maps_rsc_experience_without_inventing_dates() -> None:
    profile = Profile(public_identifier="x")
    values = [
        "Northwind",
        "2 yrs",
        "Pune, India · On-site",
        "Software Engineer",
        "Full-time",
        "Aug 2024 - Present · 2 yrs 1 mo",
    ]

    mapped = apply_rsc_experience(profile, values)

    assert mapped.position_groups[0].company_name == "Northwind"
    assert mapped.position_groups[0].positions[0].title == "Software Engineer"
    assert mapped.position_groups[0].positions[0].dates.is_current is True


def test_maps_current_rsc_experience_layout_and_groups_promotions() -> None:
    profile = Profile(public_identifier="x")
    values = [
        "Northwind",
        "Full-time · 2 yrs 8 mos",
        "Pune, India · On-site",
        "Software Engineer",
        "Aug 2024 - Present · 2 yrs 1 mo",
        "Software Engineer Intern",
        "Jan 2024 - Jul 2024 · 7 mos",
    ]

    mapped = apply_rsc_experience(profile, values)

    assert len(mapped.position_groups) == 1
    assert [item.title for item in mapped.position_groups[0].positions] == [
        "Software Engineer",
        "Software Engineer Intern",
    ]
    assert mapped.position_groups[0].positions[0].employment_type == "Full-time"

# ---------------------------------------------------------------------------
# Top card
# ---------------------------------------------------------------------------


def test_maps_identity_fields(top_card: dict[str, Any]) -> None:
    profile = map_top_card(top_card, "test-profile")
    assert profile.first_name == "Asha"
    assert profile.last_name == "Raman"
    assert profile.full_name == "Asha Raman"
    assert profile.headline == "Backend Engineer at Northwind Systems"
    assert profile.location.display == "Bengaluru, Karnataka, India"
    assert profile.follower_count == 1840


def test_connection_count_cap_is_recorded(top_card: dict[str, Any]) -> None:
    """500 is LinkedIn's display cap, not a real count.

    Reporting it as an exact figure would silently understate every large
    network.
    """
    profile = map_top_card(top_card, "test-profile")
    assert profile.connection_count == 500
    assert profile.connection_count_capped is True


def test_falls_back_to_requested_slug(top_card: dict[str, Any]) -> None:
    stripped = {"data": {}, "included": [{"entityUrn": "urn:li:fsd_profile:X", "firstName": "A"}]}
    assert map_top_card(stripped, "requested-slug").public_identifier == "requested-slug"


# ---------------------------------------------------------------------------
# Experience: the promotion-history case
# ---------------------------------------------------------------------------


def test_multi_role_employer_becomes_one_group(experience: dict[str, Any]) -> None:
    groups = map_experience(experience)
    northwind = next(g for g in groups if g.company_name == "Northwind Systems")

    assert northwind.is_multi_role
    assert len(northwind.positions) == 3


def test_promotion_history_survives_in_order(experience: dict[str, Any]) -> None:
    """The whole point: three promotions must remain three ordered roles."""
    groups = map_experience(experience)
    northwind = next(g for g in groups if g.company_name == "Northwind Systems")

    assert [p.title for p in northwind.positions] == [
        "Staff Engineer",
        "Senior Engineer",
        "Engineer",
    ]


def test_multi_role_positions_all_carry_the_employer(experience: dict[str, Any]) -> None:
    """Nested roles inherit the company from their parent group.

    Without this the roles are orphaned, which is the other half of the
    flattening bug.
    """
    groups = map_experience(experience)
    northwind = next(g for g in groups if g.company_name == "Northwind Systems")
    assert all(p.company_name == "Northwind Systems" for p in northwind.positions)


def test_single_role_employer_becomes_group_of_one(experience: dict[str, Any]) -> None:
    """One code path for consumers, whether or not there were promotions."""
    groups = map_experience(experience)
    fabrikam = next(g for g in groups if g.company_name == "Fabrikam Data")

    assert not fabrikam.is_multi_role
    assert len(fabrikam.positions) == 1
    assert fabrikam.positions[0].title == "Backend Engineer"


def test_employment_type_split_from_company(experience: dict[str, Any]) -> None:
    """'Fabrikam Data · Full-time' is two fields, not one company name."""
    groups = map_experience(experience)
    fabrikam = next(g for g in groups if g.company_name == "Fabrikam Data")
    assert fabrikam.positions[0].employment_type == "Full-time"


def test_flat_positions_view_matches_groups(experience: dict[str, Any]) -> None:
    profile = Profile(public_identifier="x", position_groups=map_experience(experience))
    assert len(profile.positions) == 4  # 3 at Northwind + 1 at Fabrikam


def test_current_position_detected_from_present(experience: dict[str, Any]) -> None:
    profile = Profile(public_identifier="x", position_groups=map_experience(experience))
    current = profile.current_positions
    assert len(current) == 1
    assert current[0].title == "Staff Engineer"


def test_dates_parsed_from_rendered_caption(experience: dict[str, Any]) -> None:
    """Captions are display strings; the duration suffix must be discarded."""
    groups = map_experience(experience)
    staff = next(g for g in groups if g.company_name == "Northwind Systems").positions[0]

    assert staff.dates.start_year == 2024
    assert staff.dates.start_month == 7
    assert staff.dates.is_current is True
    assert staff.dates.end_year is None


def test_closed_date_range_parsed(experience: dict[str, Any]) -> None:
    groups = map_experience(experience)
    engineer = next(g for g in groups if g.company_name == "Northwind Systems").positions[2]

    assert engineer.dates.start_year == 2022
    assert engineer.dates.end_year == 2023
    assert engineer.dates.end_month == 3
    assert engineer.dates.is_current is False


def test_description_extracted_from_subcomponents(experience: dict[str, Any]) -> None:
    groups = map_experience(experience)
    staff = next(g for g in groups if g.company_name == "Northwind Systems").positions[0]
    assert staff.description is not None
    assert "streaming architecture" in staff.description


# ---------------------------------------------------------------------------
# Education and skills
# ---------------------------------------------------------------------------


def test_education_splits_degree_and_field(education: dict[str, Any]) -> None:
    entries = map_education(education)
    first = entries[0]

    assert first.school_name == "Indian Institute of Technology, Bombay"
    assert first.degree_name == "Bachelor of Technology - BTech"
    assert first.field_of_study == "Computer Science"
    assert first.dates.start_year == 2016
    assert first.dates.end_year == 2020


def test_hidden_endorsements_are_none_not_zero(skills: dict[str, Any]) -> None:
    """Null means 'not visible to this viewer', which is not the same as zero.

    Defaulting to 0 asserts something false about most profiles, since
    endorsement counts are hidden from non-connections.
    """
    parsed = {s.name: s.endorsement_count for s in map_skills(skills)}

    assert parsed["Distributed Systems"] == 17
    assert parsed["PostgreSQL"] == 9
    assert parsed["Python"] is None


def test_skills_deduplicated(skills: dict[str, Any]) -> None:
    names = [s.name for s in map_skills(skills)]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Public fallback tier
# ---------------------------------------------------------------------------


def test_extracts_jsonld_person(public_html: str) -> None:
    person = extract_jsonld(public_html)
    assert person is not None
    assert person["name"] == "Asha Raman"


def test_maps_jsonld_to_profile(public_html: str) -> None:
    person = extract_jsonld(public_html)
    profile = map_jsonld(person, "test-profile")

    assert profile.full_name == "Asha Raman"
    assert profile.headline == "Staff Engineer"  # jobTitle arrives as a list
    assert profile.location.display == "Bengaluru, Karnataka, IN"
    assert profile.position_groups[0].company_name == "Northwind Systems"
    assert profile.educations[0].school_name == "Indian Institute of Technology, Bombay"


def test_jsonld_absent_returns_none() -> None:
    assert extract_jsonld("<html><body>nothing here</body></html>") is None


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_merge_prefers_richer_tier() -> None:
    rich = Profile(public_identifier="x", headline="From Voyager")
    thin = Profile(public_identifier="x", headline="From public page", summary="Only here")

    merged = merge(rich, thin)
    assert merged.headline == "From Voyager"
    assert merged.summary == "Only here"


def test_merge_fills_empty_lists(education: dict[str, Any], public_html: str) -> None:
    """A section that failed on tier 1 can still be filled from tier 3.

    An empty list counts as a gap, not as an answer -- otherwise a GraphQL
    section that returned nothing would block the fallback from contributing.
    """
    rich = Profile(public_identifier="x", headline="From Voyager", educations=[])
    thin = map_jsonld(extract_jsonld(public_html), "x")

    merged = merge(rich, thin)
    assert merged.headline == "From Voyager"
    assert len(merged.educations) == 1


def test_merge_does_not_overwrite_populated_lists(
    education: dict[str, Any],
    public_html: str,
) -> None:
    rich = Profile(public_identifier="x", educations=map_education(education))
    thin = map_jsonld(extract_jsonld(public_html), "x")

    merged = merge(rich, thin)
    assert len(merged.educations) == 2  # the richer tier's two entries, not the fallback's one

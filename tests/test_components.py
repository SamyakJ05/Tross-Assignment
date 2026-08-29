"""Component tree walking.

The pruning tests here guard a bug that already happened once during
development: an unpruned walk found nested role components a second time at
the top level, so a person with three promotions at one employer produced
the correct group *plus* three phantom standalone jobs. The output looked
entirely plausible. Without an explicit count assertion it would have
shipped.
"""

from __future__ import annotations

from typing import Any

from app.parsing.components import (
    description_text,
    entity_components,
    extract_text,
    parse_caption_dates,
    read_slots,
    sub_components,
)

# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def test_only_top_level_components_returned(experience: dict[str, Any]) -> None:
    """Two employers, not five components.

    The fixture has one multi-role employer with three nested roles and one
    single-role employer. Nested roles must not surface at the top level.
    """
    components = entity_components(experience)
    assert len(components) == 2


def test_nested_roles_reached_only_through_parent(experience: dict[str, Any]) -> None:
    components = entity_components(experience)
    northwind = next(c for c in components if read_slots(c)["title"] == "Northwind Systems")

    nested = sub_components(northwind)
    assert [read_slots(n)["title"] for n in nested] == [
        "Staff Engineer",
        "Senior Engineer",
        "Engineer",
    ]


def test_single_role_component_has_no_nested_entities(experience: dict[str, Any]) -> None:
    """Its subComponents hold a description, which is not an entity."""
    components = entity_components(experience)
    fabrikam = next(c for c in components if read_slots(c)["title"] == "Backend Engineer")
    assert sub_components(fabrikam) == []


def test_shape_fallback_when_no_entity_component_key() -> None:
    """A wrapper shape we have not seen should still yield something."""
    payload = {
        "included": [
            {
                "topComponents": [
                    {"titleV2": {"text": "Some Role"}, "subtitle": {"text": "Some Company"}}
                ]
            }
        ]
    }
    components = entity_components(payload)
    assert len(components) == 1
    assert read_slots(components[0])["title"] == "Some Role"


def test_empty_payload_yields_nothing() -> None:
    assert entity_components({"data": {}, "included": []}) == []


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def test_extract_text_handles_every_shape() -> None:
    """Bare string, {text}, and nested attributed string all mean the same."""
    assert extract_text("plain") == "plain"
    assert extract_text({"text": "wrapped"}) == "wrapped"
    assert extract_text({"text": {"text": "nested"}}) == "nested"
    assert extract_text({"accessibilityText": "a11y"}) == "a11y"


def test_extract_text_returns_none_for_empty() -> None:
    assert extract_text(None) is None
    assert extract_text("") is None
    assert extract_text("   ") is None
    assert extract_text({}) is None
    assert extract_text({"text": ""}) is None


def test_description_skips_nested_entities(experience: dict[str, Any]) -> None:
    """A role's description must not absorb its sibling roles' titles."""
    components = entity_components(experience)
    northwind = next(c for c in components if read_slots(c)["title"] == "Northwind Systems")
    nested = sub_components(northwind)

    staff_description = description_text(nested[0])
    assert staff_description is not None
    assert "streaming architecture" in staff_description
    assert "Senior Engineer" not in staff_description


# ---------------------------------------------------------------------------
# Caption dates
# ---------------------------------------------------------------------------


def test_parses_open_ended_range() -> None:
    parsed = parse_caption_dates("Jul 2024 - Present · 1 yr 2 mos")
    assert parsed == {
        "is_current": True,
        "start_year": 2024,
        "start_month": 7,
    }


def test_parses_closed_range() -> None:
    parsed = parse_caption_dates("Aug 2020 - Dec 2021 · 1 yr 5 mos")
    assert parsed["start_year"] == 2020
    assert parsed["start_month"] == 8
    assert parsed["end_year"] == 2021
    assert parsed["end_month"] == 12
    assert parsed["is_current"] is False


def test_parses_year_only_range() -> None:
    """Education captions routinely omit months; none must be invented."""
    parsed = parse_caption_dates("2016 - 2020")
    assert parsed["start_year"] == 2016
    assert parsed["end_year"] == 2020
    assert parsed["start_month"] is None
    assert parsed["end_month"] is None


def test_handles_en_dash_separator() -> None:
    parsed = parse_caption_dates("2016 – 2020")
    assert parsed["start_year"] == 2016
    assert parsed["end_year"] == 2020


def test_tolerates_unparseable_caption() -> None:
    """Lossy by nature -- it must degrade, not raise."""
    assert parse_caption_dates(None)["is_current"] is False
    assert parse_caption_dates("")["is_current"] is False
    assert parse_caption_dates("something unexpected")["is_current"] is False

"""URN graph resolution over the normalized format."""

from __future__ import annotations

from typing import Any

from app.parsing.normalized import EntityGraph, resolve_date_range, resolve_image


def test_indexes_included_by_urn(top_card: dict[str, Any]) -> None:
    graph = EntityGraph(top_card)
    entity = graph.get("urn:li:fsd_profile:ACoAAATestProfile0001")
    assert entity is not None
    assert entity["firstName"] == "Asha"


def test_finds_profile_entity(top_card: dict[str, Any]) -> None:
    profile = EntityGraph(top_card).profile()
    assert profile is not None
    assert profile["publicIdentifier"] == "test-profile"


def test_type_matching_is_suffix_based(top_card: dict[str, Any]) -> None:
    """Suffix matching survives LinkedIn moving classes between packages.

    Pinning the fully-qualified name breaks the parser on a rename that
    changed nothing about the data.
    """
    graph = EntityGraph(top_card)
    assert graph.of_type("identity.profile.Profile")
    assert graph.of_type("Profile")
    assert not graph.of_type("NoSuchType")


def test_root_elements_follow_starred_references(top_card: dict[str, Any]) -> None:
    elements = EntityGraph(top_card).root_elements()
    assert len(elements) == 1
    assert elements[0]["firstName"] == "Asha"


def test_deref_handles_urn_list() -> None:
    payload = {
        "data": {},
        "included": [
            {"entityUrn": "urn:li:a:1", "*children": ["urn:li:b:1", "urn:li:b:2"]},
            {"entityUrn": "urn:li:b:1", "name": "first"},
            {"entityUrn": "urn:li:b:2", "name": "second"},
        ],
    }
    graph = EntityGraph(payload)
    children = graph.deref(graph.get("urn:li:a:1"), "children")
    assert [c["name"] for c in children] == ["first", "second"]


def test_deref_handles_inlined_objects() -> None:
    """Responses are inconsistent about starred vs inlined; both must work."""
    payload = {
        "data": {},
        "included": [{"entityUrn": "urn:li:a:1", "children": [{"name": "inline"}]}],
    }
    graph = EntityGraph(payload)
    assert graph.deref(graph.get("urn:li:a:1"), "children")[0]["name"] == "inline"


def test_deref_skips_dangling_references() -> None:
    """A URN with no entity is common in partial responses and is not fatal."""
    payload = {
        "data": {},
        "included": [{"entityUrn": "urn:li:a:1", "*children": ["urn:li:b:missing"]}],
    }
    graph = EntityGraph(payload)
    assert graph.deref(graph.get("urn:li:a:1"), "children") == []


def test_resolves_largest_image_artifact(top_card: dict[str, Any]) -> None:
    """Root URL and path segment must be concatenated; neither works alone."""
    graph = EntityGraph(top_card)
    image = resolve_image(graph, graph.profile(), "profilePicture")

    assert image is not None
    assert image["width"] == 400  # the larger of the two artifacts
    assert image["url"].startswith("https://media.licdn.com/dms/image/")
    assert "400_400" in image["url"]


def test_resolve_image_returns_none_when_absent(top_card: dict[str, Any]) -> None:
    graph = EntityGraph(top_card)
    assert resolve_image(graph, graph.profile(), "backgroundPicture") is None


def test_date_range_keeps_missing_month_missing() -> None:
    """Defaulting an absent month to January would fabricate precision."""
    parsed = resolve_date_range({"start": {"year": 2020}, "end": {"year": 2022}})
    assert parsed["start_year"] == 2020
    assert parsed["start_month"] is None
    assert parsed["is_current"] is False


def test_date_range_marks_open_ended_as_current() -> None:
    parsed = resolve_date_range({"start": {"year": 2022, "month": 1}, "end": {}})
    assert parsed["is_current"] is True
    assert parsed["end_year"] is None


def test_date_range_tolerates_garbage() -> None:
    assert resolve_date_range(None)["is_current"] is False
    assert resolve_date_range({})["is_current"] is True

"""The queryId registry.

These tests do not check that any hash is *valid* -- only LinkedIn can say
that, and only right now. They check that the registry is coherent and that
staleness is observable, which is what the design actually promises.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.linkedin.queries import (
    PROFILE_COMPONENTS,
    REGISTRY,
    Query,
    configured_profile_components_query,
    encode_variables,
    registry_status,
)


def test_full_id_joins_name_and_hash() -> None:
    assert PROFILE_COMPONENTS.full_id == f"{PROFILE_COMPONENTS.name}.{PROFILE_COMPONENTS.query_id}"


def test_age_is_reported_in_days() -> None:
    recent = Query(
        name="x",
        query_id="abc",
        last_verified=datetime.now(UTC).date() - timedelta(days=5),
        description="",
    )
    assert recent.age_days == 5
    assert recent.is_stale is False


def test_old_query_is_flagged_stale() -> None:
    old = Query(
        name="x",
        query_id="abc",
        last_verified=date(2020, 1, 1),
        description="",
    )
    assert old.is_stale is True


def test_registry_status_surfaces_unconfigured_hashes() -> None:
    """An empty hash must be visible, not silently treated as working."""
    status = {q["key"]: q for q in registry_status()}
    assert status["profile_components"]["configured"] is False
    assert status["profile_cards"]["configured"] is False


def test_configured_component_query_is_reflected_in_health_status() -> None:
    status = {
        q["key"]: q
        for q in registry_status(
            profile_components_query_id="current-hash",
            profile_components_verified_on=date.today(),
        )
    }
    assert status["profile_components"]["configured"] is True
    assert status["profile_components"]["stale"] is False
    assert configured_profile_components_query("current-hash", date.today()).full_id.endswith(
        ".current-hash"
    )


def test_every_registry_entry_is_described() -> None:
    """A hash with no description is unmaintainable six months later."""
    for key, query in REGISTRY.items():
        assert query.description, f"{key} has no description"
        assert query.name, f"{key} has no query name"


# ---------------------------------------------------------------------------
# Rest.li encoding
# ---------------------------------------------------------------------------


def test_encodes_rest_li_not_json() -> None:
    """Parentheses and colons, not braces and quotes.

    Passing JSON here is the most common cause of an opaque 400 on a first
    attempt at the GraphQL layer.
    """
    encoded = encode_variables(profileUrn="urn:li:fsd_profile:ACoAAA123")

    assert encoded.startswith("(")
    assert encoded.endswith(")")
    assert "{" not in encoded
    assert '"' not in encoded


def test_percent_encodes_urn_colons() -> None:
    encoded = encode_variables(profileUrn="urn:li:fsd_profile:ACoAAA123")
    assert "urn%3Ali%3Afsd_profile%3AACoAAA123" in encoded


def test_multiple_variables_comma_separated() -> None:
    encoded = encode_variables(profileUrn="urn:li:fsd_profile:X", sectionType="experience")
    assert encoded == "(profileUrn:urn%3Ali%3Afsd_profile%3AX,sectionType:experience)"

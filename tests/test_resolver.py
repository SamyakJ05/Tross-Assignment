"""URL parsing.

Users paste whatever is in their address bar, so this accepts far more than
the canonical form. The unicode and trailing-hash cases matter more than
they look: restricting slugs to [a-z0-9-] silently 404s a large fraction of
real profiles.
"""

from __future__ import annotations

import pytest

from app.linkedin.resolver import InvalidProfileURL, extract_slug, normalise_urn


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://www.linkedin.com/in/samyakj05/", "samyakj05"),
        ("https://www.linkedin.com/in/samyakj05", "samyakj05"),
        ("http://linkedin.com/in/samyakj05", "samyakj05"),
        ("www.linkedin.com/in/samyakj05", "samyakj05"),
        ("linkedin.com/in/samyakj05", "samyakj05"),
        ("samyakj05", "samyakj05"),
        # Locale subdomains are common outside the US.
        ("https://in.linkedin.com/in/samyakj05", "samyakj05"),
        ("https://uk.linkedin.com/in/samyakj05", "samyakj05"),
        # Tracking parameters survive copy-paste from anywhere.
        ("https://www.linkedin.com/in/samyakj05/?originalSubdomain=in", "samyakj05"),
        ("https://www.linkedin.com/in/samyakj05/?trk=public_profile", "samyakj05"),
        # Deep links into profile subpages.
        ("https://www.linkedin.com/in/samyakj05/details/experience/", "samyakj05"),
        ("https://www.linkedin.com/in/samyakj05/recent-activity/all/", "samyakj05"),
        # LinkedIn's disambiguation suffix.
        ("https://www.linkedin.com/in/jane-doe-1a2b3c4d", "jane-doe-1a2b3c4d"),
        # Percent-encoded unicode, which LinkedIn issues for non-Latin names.
        ("https://www.linkedin.com/in/%E5%B1%B1%E7%94%B0%E5%A4%AA%E9%83%8E", "山田太郎"),
    ],
)
def test_extracts_slug(value: str, expected: str) -> None:
    assert extract_slug(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "https://example.com/in/someone",
        "https://www.linkedin.com/company/northwind",
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/posts/someone_activity-1234",
    ],
)
def test_rejects_non_profile_urls(value: str) -> None:
    with pytest.raises(InvalidProfileURL):
        extract_slug(value)


def test_company_url_error_is_actionable() -> None:
    """The message should say what was wrong, not just that something was."""
    with pytest.raises(InvalidProfileURL, match="company"):
        extract_slug("https://www.linkedin.com/company/northwind")


def test_normalise_urn_accepts_both_forms() -> None:
    bare = "ACoAAATestProfile0001"
    full = "urn:li:fsd_profile:ACoAAATestProfile0001"
    assert normalise_urn(bare) == full
    assert normalise_urn(full) == full

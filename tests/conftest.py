from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def settings() -> Settings:
    """Settings with a fake session.

    Values are obviously fake so a leaked test log cannot be mistaken for a
    real credential.
    """
    return Settings(
        linkedin_li_at="AQEDTESTTESTTESTTESTTESTTESTTEST",
        linkedin_jsessionid='"ajax:1111111111111111111"',
        api_keys="test-key-one,test-key-two",
        cache_ttl_seconds=60,
        min_request_interval_seconds=0.0,
    )


@pytest.fixture
def anon_settings() -> Settings:
    """Settings with no session, exercising the public-only path."""
    return Settings(
        linkedin_li_at="",
        linkedin_jsessionid="",
        api_keys="test-key-one",
        min_request_interval_seconds=0.0,
    )


@pytest.fixture
def top_card() -> dict[str, Any]:
    return load_json("top_card_dash.json")


@pytest.fixture
def experience() -> dict[str, Any]:
    return load_json("experience_components.json")


@pytest.fixture
def education() -> dict[str, Any]:
    return load_json("education_components.json")


@pytest.fixture
def skills() -> dict[str, Any]:
    return load_json("skills_components.json")


@pytest.fixture
def public_html() -> str:
    return load_text("public_profile.html")


@pytest.fixture
def interstitial_html() -> str:
    return load_text("interstitial.html")

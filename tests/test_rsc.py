from __future__ import annotations

import pytest

from app.config import Settings
from app.linkedin.rsc import (
    decode_flight_frames,
    fetch_languages,
    language_values,
    profile_component_payload,
    skill_names,
    text_fragments,
    visible_strings,
)


def test_profile_component_payload_is_slug_scoped() -> None:
    payload = profile_component_payload("test-person")
    client_args = payload["clientArguments"]
    profile_state = client_args["payload"]["profileComponentState"]

    assert client_args["payload"]["vanityName"] == "test-person"
    assert profile_state["profileId"] == "test-person"
    assert "test-person" in profile_state["shouldFetchFromCache"]["value"]["key"]


def test_decodes_json_frames_and_collects_visible_text() -> None:
    raw = b'1:{"text":"First role","children":[{"stringValue":"Second role"}]}\n2:I["x"]'
    frames = decode_flight_frames(raw)

    assert frames == [{"text": "First role", "children": [{"stringValue": "Second role"}]}]
    assert text_fragments(frames) == ["First role", "Second role"]


def test_visible_strings_excludes_framework_values() -> None:
    frames = [
        {
            "text": "Engineer at Northwind",
            "componentKey": "com.linkedin.sdui.profile.card.ref123",
            "url": "https://www.linkedin.com/in/example/",
        }
    ]

    assert visible_strings(frames) == ["Engineer at Northwind"]


# ---------------------------------------------------------------------------
# Skills pagination stream
#
# Live shape: each skill entity carries an endorse button whose
# aria-label is "Endorse <skill name>", nested several levels deep
# alongside a lot of unrelated SDUI scaffolding (tracking specs, trigger
# actions, className strings). skill_names must find just the skill names.
# ---------------------------------------------------------------------------


def test_skill_names_extracted_from_endorse_button_aria_labels() -> None:
    frames = [
        {
            "componentKey": "com.linkedin.sdui.profile.skill(ACoAA..., 111)",
            "children": {
                "buttonProps": {
                    "text": ["Endorse"],
                    "aria-label": "Endorse Node.js",
                },
                "className": "aa13b50b _8cd77912",
            },
        },
        [
            {
                "componentKey": "com.linkedin.sdui.profile.skill(ACoAA..., 222)",
                "buttonProps": {"aria-label": "Endorse Scrum"},
            }
        ],
    ]

    assert skill_names(frames) == ["Node.js", "Scrum"]


def test_skill_names_deduplicates_repeated_entities() -> None:
    """The multi-path SDUI tree can reach the same entity more than once."""
    frames = [
        {"buttonProps": {"aria-label": "Endorse Kubernetes"}},
        {"other": {"buttonProps": {"aria-label": "Endorse Kubernetes"}}},
    ]

    assert skill_names(frames) == ["Kubernetes"]


def test_skill_names_ignores_unrelated_aria_labels() -> None:
    frames = [{"aria-label": "Open profile photo"}, {"aria-label": "Endorse Python"}]

    assert skill_names(frames) == ["Python"]


def test_skill_names_supports_plain_text_stream_shape() -> None:
    frames = [
        {"text": "Python"},
        {"stringValue": "Java"},
        {"metadata": '{"semanticId":""}'},
    ]

    assert skill_names(frames) == ["Python", "Java"]


# ---------------------------------------------------------------------------
# Languages pagination stream
# ---------------------------------------------------------------------------


def test_language_values_preserve_names_and_optional_proficiencies() -> None:
    frames = [
        {"text": "Languages"},
        {"text": "English"},
        {"stringValue": "Full professional proficiency"},
        {"text": "Hindi"},
        {"text": "Native or bilingual proficiency"},
        {"text": "Kannada"},
    ]

    assert language_values(frames) == [
        "English",
        "Full professional proficiency",
        "Hindi",
        "Native or bilingual proficiency",
        "Kannada",
    ]


def test_language_values_deduplicate_repeated_names() -> None:
    frames = [{"text": "French"}, {"nested": {"text": "French"}}]

    assert language_values(frames) == ["French"]


@pytest.mark.asyncio
async def test_languages_request_targets_the_language_detail_pager() -> None:
    captured: dict[str, object] = {}

    class Session:
        csrf_token = "ajax:123"

        @staticmethod
        def cookie_header() -> str:
            return 'li_at=fixture; JSESSIONID="ajax:123"'

    class Client:
        settings = Settings()
        session = Session()

        async def post_stream(self, url, *, headers, payload, params):
            captured.update(url=url, headers=headers, payload=payload, params=params)
            return b'1:{"text":"English"}\n'

    frames = await fetch_languages(Client(), "fixture-person", "ACoAATEST")

    assert frames == [{"text": "English"}]
    assert captured["headers"]["referer"].endswith("/details/languages/")
    assert captured["headers"]["x-li-anchor-page-key"].endswith("languages_details")
    assert captured["params"]["sduiid"].endswith("profile.details.languages")
    assert captured["payload"]["clientArguments"]["screenId"].endswith(
        "ProfileLanguageDetails"
    )
    assert "filter" not in captured["payload"]["clientArguments"]["payload"]

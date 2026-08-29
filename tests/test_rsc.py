from __future__ import annotations

from app.linkedin.rsc import (
    decode_flight_frames,
    profile_component_payload,
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

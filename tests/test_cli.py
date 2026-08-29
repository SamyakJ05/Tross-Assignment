"""CLI contract tests without outbound requests."""

from __future__ import annotations

import json

from app import cli
from app.models.domain import Profile
from app.models.envelope import Completeness, ProfileResponse


def test_cli_prints_indented_profile_json(monkeypatch, capsys) -> None:
    async def fake_fetch(_: str) -> str:
        return ProfileResponse(
            profile=Profile(public_identifier="fixture", full_name="Asha Raman"),
            completeness=Completeness.PARTIAL,
        ).model_dump_json(indent=2)

    monkeypatch.setattr(cli, "_fetch", fake_fetch)

    assert cli.main(["https://www.linkedin.com/in/fixture/"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["profile"]["full_name"] == "Asha Raman"


def test_cli_compact_option_prints_single_line_json(monkeypatch, capsys) -> None:
    async def fake_fetch(_: str) -> str:
        return '{"profile":{"public_identifier":"fixture"}}'

    monkeypatch.setattr(cli, "_fetch", fake_fetch)

    assert cli.main(["fixture", "--compact"]) == 0
    assert "\n" not in capsys.readouterr().out.strip()

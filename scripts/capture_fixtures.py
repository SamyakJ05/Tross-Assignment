#!/usr/bin/env python3
"""Capture real Voyager responses and sanitise them into test fixtures.

The committed fixtures are hand-authored to the documented response shapes.
That proves the parser is internally consistent; it does not prove the
shapes are right. This script closes that gap.

    python scripts/capture_fixtures.py --slug your-own-profile

Raw responses land in tests/fixtures/raw/ (gitignored). Sanitised copies go
to tests/fixtures/. Read the diff before committing -- the sanitiser is
best-effort, not a guarantee.

Capture from a profile you own or have permission to use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.linkedin.client import LinkedInClient  # noqa: E402
from app.linkedin.errors import LinkedInError  # noqa: E402
from app.linkedin.fetchers import fetch_profile  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
RAW = FIXTURES / "raw"

# Values that must never survive into a committed file.
_SECRET_PATTERNS = [
    re.compile(r"AQED[A-Za-z0-9_\-]+"),  # li_at
    re.compile(r"ajax:\d+"),  # JSESSIONID / csrf-token
    re.compile(r"[?&](?:e|v|t)=[A-Za-z0-9_\-%]+"),  # signed image parameters
]

_FAKE_URN = "ACoAAATestProfile0001"


def sanitise(node: Any, replacements: dict[str, str]) -> Any:
    """Walk a structure, replacing identifying values."""
    if isinstance(node, dict):
        return {k: sanitise(v, replacements) for k, v in node.items()}
    if isinstance(node, list):
        return [sanitise(v, replacements) for v in node]
    if isinstance(node, str):
        out = node
        for original, replacement in replacements.items():
            if original:
                out = out.replace(original, replacement)
        for pattern in _SECRET_PATTERNS:
            out = pattern.sub("REDACTED", out)
        # Real member URNs become the stable fake used by the fixtures.
        out = re.sub(r"ACoAA[A-Za-z0-9_\-]+", _FAKE_URN, out)
        return out
    return node


def build_replacements(payload: dict[str, Any]) -> dict[str, str]:
    """Derive name and location replacements from the captured profile."""
    replacements: dict[str, str] = {}
    for entity in payload.get("included") or []:
        if not isinstance(entity, dict):
            continue
        if entity.get("firstName"):
            replacements[entity["firstName"]] = "Asha"
        if entity.get("lastName"):
            replacements[entity["lastName"]] = "Raman"
        if entity.get("publicIdentifier"):
            replacements[entity["publicIdentifier"]] = "test-profile"
    return replacements


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, help="Profile slug to capture.")
    parser.add_argument(
        "--no-sanitise",
        action="store_true",
        help="Write raw only. Never commit the result.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.has_session:
        print(
            "No session configured. Set LINKEDIN_LI_AT and LINKEDIN_JSESSIONID "
            "in .env before capturing.",
            file=sys.stderr,
        )
        return 1

    RAW.mkdir(parents=True, exist_ok=True)

    async with LinkedInClient(settings) as client:
        try:
            result = await fetch_profile(client, args.slug)
        except LinkedInError as exc:
            print(f"Capture failed: {exc.code}: {exc.message}", file=sys.stderr)
            return 1

    captured: dict[str, Any] = {}
    if result.top_card:
        captured["top_card_dash"] = result.top_card
    for section, payload in result.sections.items():
        captured[f"{section}_components"] = payload

    if not captured:
        print("Nothing captured. Check warnings:", file=sys.stderr)
        for w in result.warnings:
            print(f"  [{w.code}] {w.message}", file=sys.stderr)
        return 1

    replacements = build_replacements(result.top_card or {})

    for name, payload in captured.items():
        (RAW / f"{name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if not args.no_sanitise:
            cleaned = sanitise(payload, replacements)
            (FIXTURES / f"{name}.json").write_text(
                json.dumps(cleaned, indent=2) + "\n", encoding="utf-8"
            )
        print(f"captured {name}")

    if result.public_html and not args.no_sanitise:
        cleaned_html = sanitise(result.public_html, replacements)
        (FIXTURES / "public_profile.html").write_text(cleaned_html, encoding="utf-8")
        print("captured public_profile")

    print(
        f"\nRaw -> {RAW} (gitignored)\n"
        f"Sanitised -> {FIXTURES}\n\n"
        f"Review the diff before committing. The sanitiser is best-effort."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

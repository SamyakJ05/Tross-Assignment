"""Command-line access to the same direct-HTTP profile service as the API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from app.config import get_settings
from app.linkedin.errors import LinkedInError
from app.linkedin.resolver import InvalidProfileURL, extract_slug
from app.service import get_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkedin-profile",
        description="Fetch one LinkedIn profile through the direct-HTTP profile service.",
    )
    parser.add_argument("url", help="LinkedIn profile URL or public identifier")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Write compact JSON instead of indented JSON.",
    )
    return parser


async def _fetch(url: str) -> str:
    slug = extract_slug(url)
    response = await get_profile(slug, get_settings())
    return response.model_dump_json(indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a conventional process exit status."""
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        output = asyncio.run(_fetch(args.url))
    except InvalidProfileURL as exc:
        parser.error(str(exc))
    except LinkedInError as exc:
        print(
            json.dumps(
                {
                    "error": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "upstream_status": exc.status,
                }
            ),
            file=sys.stderr,
        )
        return 1

    if args.compact:
        output = json.dumps(json.loads(output), separators=(",", ":"))
    print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the entry point
    raise SystemExit(main())

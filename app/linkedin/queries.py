"""The queryId registry.

A queryId is a hash identifying a persisted GraphQL query. LinkedIn's
frontend ships with them baked in and the server accepts only hashes it
knows. They rotate whenever the relevant frontend deploys, with no registry,
no documentation and no deprecation notice.

There is exactly one way to obtain a current value: open DevTools on a real
LinkedIn profile page, filter the network tab to `graphql`, and read the
hash off a live request.

Which means every hash in this file is a dated artifact from the moment it
is committed. Treating that as the central design constraint rather than an
annoyance is the whole point of this module:

  * hashes live in config, never inline at a call site
  * each carries the date it was last confirmed working
  * `/v1/health` reports their ages, so staleness is visible before a
    consumer notices bad data
  * a rejected hash raises StaleQueryId, which names the remedy

When one breaks, the fix is a single line here plus a new date -- not a
hunt through the fetchers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

# The Rest.li-encoded variable syntax GraphQL expects. Not JSON: parentheses
# and colons, with URN colons percent-encoded. Passing JSON here is the most
# common cause of an opaque 400 on a first attempt.
#
#   correct   variables=(profileUrn:urn%3Ali%3Afsd_profile%3AACoAAA...)
#   wrong     variables={"profileUrn":"urn:li:fsd_profile:ACoAAA..."}


@dataclass(frozen=True)
class Query:
    """One persisted GraphQL query.

    `last_verified` is the date a human last saw this hash return real data.
    It is surfaced by the health endpoint rather than kept as a comment,
    because a comment cannot be monitored.
    """

    name: str
    query_id: str
    last_verified: date
    description: str

    @property
    def full_id(self) -> str:
        return f"{self.name}.{self.query_id}"

    @property
    def age_days(self) -> int:
        return (datetime.now(UTC).date() - self.last_verified).days

    @property
    def is_stale(self) -> bool:
        """Advisory only -- a hash can rotate the day after verification.

        Thirty days is a heuristic for "assume nothing", chosen because it is
        long enough not to cry wolf and short enough that an unattended
        deployment does not drift silently for a quarter.
        """
        return self.age_days > 30


# ---------------------------------------------------------------------------
# The registry
#
# UPDATE PROCEDURE
#   1. Open a LinkedIn profile in a logged-in browser.
#   2. DevTools -> Network -> filter `graphql`.
#   3. Find the request whose response carries the section you need.
#   4. Copy the hash after the `.` in queryId and replace it below.
#   5. Set last_verified to today.
#   6. Run `pytest tests/test_queries.py` to confirm the registry is coherent.
# ---------------------------------------------------------------------------

PROFILE_CARDS = Query(
    name="voyagerIdentityDashProfileCards",
    query_id="",  # capture from DevTools before first live run
    last_verified=date(2026, 8, 29),
    description="Top card plus the collapsed preview of every profile section.",
)

PROFILE_COMPONENTS = Query(
    name="voyagerIdentityDashProfileComponents",
    query_id="",  # disabled: the previous ID now redirects instead of returning data
    last_verified=date(2026, 8, 29),
    description=(
        "A single expanded section: the 'see all experience' view and its siblings. "
        "Populate only after a current direct request returns the expected payload."
    ),
)

PROFILES_BY_MEMBER_IDENTITY = Query(
    name="voyagerIdentityDashProfiles",
    query_id="",  # optional: the dash REST resolver is the primary path
    last_verified=date(2026, 8, 29),
    description="GraphQL route from vanity slug to fsd_profile URN.",
)

REGISTRY: dict[str, Query] = {
    "profile_cards": PROFILE_CARDS,
    "profile_components": PROFILE_COMPONENTS,
    "profiles_by_member_identity": PROFILES_BY_MEMBER_IDENTITY,
}


def configured_profile_components_query(
    query_id: str,
    verified_on: date | None,
) -> Query:
    """Return the deployed component-query contract without changing source.

    The actual persisted-query hash is deployment configuration because it
    can rotate independently of an application release. An empty value keeps
    detailed-section requests disabled rather than attempting a known-stale
    route.
    """
    return replace(
        PROFILE_COMPONENTS,
        query_id=query_id.strip(),
        last_verified=verified_on or PROFILE_COMPONENTS.last_verified,
    )


# ---------------------------------------------------------------------------
# Section identifiers used by the components query
#
# These are stabler than the hashes -- they name sections rather than
# encoding a compiled query -- but they are still LinkedIn's vocabulary and
# not a contract.
# ---------------------------------------------------------------------------

SECTION_KEYS = {
    "experience": "experience",
    "education": "education",
    "skills": "skills",
    "certifications": "licenses_and_certifications",
    "languages": "languages",
    "projects": "projects",
    "honors": "honors_and_awards",
    "publications": "publications",
    "volunteer": "volunteering_experience",
}


def encode_variables(**kwargs: str) -> str:
    """Encode variables in Rest.li 2.0 syntax.

    Only handles the flat string case, which is all the profile queries need.
    Nested lists and records have their own encoding and are deliberately not
    supported here rather than half-supported.
    """
    from urllib.parse import quote

    parts = [f"{k}:{quote(str(v), safe='')}" for k, v in kwargs.items()]
    return f"({','.join(parts)})"


def registry_status(
    *,
    profile_components_query_id: str = "",
    profile_components_verified_on: date | None = None,
) -> list[dict[str, object]]:
    """Registry state for the health endpoint."""
    registry = dict(REGISTRY)
    registry["profile_components"] = configured_profile_components_query(
        profile_components_query_id,
        profile_components_verified_on,
    )
    return [
        {
            "key": key,
            "name": q.name,
            "configured": bool(q.query_id),
            "last_verified": q.last_verified.isoformat(),
            "age_days": q.age_days,
            "stale": q.is_stale,
            "description": q.description,
        }
        for key, q in registry.items()
    ]

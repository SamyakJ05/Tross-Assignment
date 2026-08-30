"""The response envelope: data, plus how much to trust it.

The core assumption of this service is that a 200 is not evidence of
success. LinkedIn's dominant real-world failure is silent degradation --
a well-formed response carrying less than it should, because a section was
withheld, a queryId went stale, or an anti-abuse system truncated the
result. A client that models only success and error records that as success
and quietly corrupts its data for weeks.

So every response carries three things beyond the profile itself:

  completeness   complete | partial | needs_review
  sources        which tier answered, per section
  warnings       what specifically was missing or suspicious

`needs_review` is the important state. It means the fetch succeeded, the
parse succeeded, and the result still looks wrong -- a profile with a name
but no experience, say, which is possible but far more often means the
extraction broke rather than that the member has never worked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.domain import Profile


class Tier(StrEnum):
    """Which fetch strategy produced a section.

    Ordered most to least capable. Recorded per section rather than per
    request, because a single profile is routinely assembled from more than
    one tier when an individual section fails.
    """

    VOYAGER_GRAPHQL = "voyager_graphql"
    LINKEDIN_RSC = "linkedin_rsc"
    VOYAGER_DASH_REST = "voyager_dash_rest"
    PUBLIC_JSONLD = "public_jsonld"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"


_PRIMARY_TIERS = {Tier.VOYAGER_GRAPHQL, Tier.LINKEDIN_RSC, Tier.VOYAGER_DASH_REST}


class SectionSource(BaseModel):
    """Provenance for one section of the profile.

    This exists for two reasons that happen to coincide.

    Operationally, it is how endpoint rot is detected: when a section that
    normally arrives via GraphQL starts arriving via a fallback tier, a
    queryId has gone stale, and the drop shows up in `/v1/health` before a
    consumer notices bad data.

    Structurally, it is what makes source-scoped deletion possible. Being
    able to identify every field derived from a given upstream -- and to
    quarantine or purge by source -- is a schema decision that cannot be
    retrofitted later. See README, "Provenance".
    """

    section: str
    tier: Tier
    query_id: str | None = None
    query_id_verified_on: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    item_count: int | None = None


class Warning(BaseModel):
    code: str
    message: str
    section: str | None = None


class ValidationIssue(BaseModel):
    location: str
    message: str
    type: str


class ErrorResponse(BaseModel):
    """Stable error envelope shared by input, rate-limit, and upstream failures."""

    error: str
    message: str
    retryable: bool = False
    upstream_status: int | None = None
    details: list[ValidationIssue] | None = None


class ProfileResponse(BaseModel):
    profile: Profile
    completeness: Completeness
    sources: list[SectionSource] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    cached: bool = False
    elapsed_ms: int | None = None

    @property
    def degraded_sections(self) -> list[str]:
        """Sections that did not come from the primary authenticated tier."""
        return [s.section for s in self.sources if s.tier not in _PRIMARY_TIERS]


# ---------------------------------------------------------------------------
# Completeness assessment
# ---------------------------------------------------------------------------

# Sections whose absence is strong evidence of a broken extraction rather
# than a genuinely sparse profile. A real account essentially always has a
# name and a headline; experience is close behind. Skills and certifications
# are legitimately absent often enough that we do not flag them.
_LOAD_BEARING = ("full_name", "headline")


def assess(
    profile: Profile,
    sources: list[SectionSource],
    warnings: list[Warning],
) -> tuple[Completeness, list[Warning]]:
    """Classify a parsed profile, appending any warnings discovered.

    Returns the classification and the full warning list, so the caller does
    not have to remember to merge them.
    """
    found: list[Warning] = list(warnings)

    missing_core = [f for f in _LOAD_BEARING if not getattr(profile, f, None)]
    for field in missing_core:
        found.append(
            Warning(
                code="missing_core_field",
                message=(
                    f"{field!r} is absent. Almost every real profile has one, so this "
                    f"more likely indicates a parsing or access failure than a sparse profile."
                ),
                section=field,
            )
        )

    # A profile with identity but no history is the classic signature of a
    # stale queryId: the top card still resolves while the section fetches
    # silently return nothing.
    has_identity = bool(profile.full_name or profile.headline)
    has_history = bool(profile.position_groups or profile.educations)
    if has_identity and not has_history:
        found.append(
            Warning(
                code="identity_without_history",
                message=(
                    "Name or headline resolved but neither experience nor education did. "
                    "This is the usual signature of a stale queryId rather than an empty profile."
                ),
            )
        )

    degraded = [s for s in sources if s.tier not in _PRIMARY_TIERS]
    for s in degraded:
        found.append(
            Warning(
                code="degraded_tier",
                message=(
                    f"Section {s.section!r} was served from the {s.tier.value} fallback tier, "
                    f"which carries fewer fields than the primary path."
                ),
                section=s.section,
            )
        )

    # Ordered most severe first: any load-bearing absence outranks any
    # amount of tier degradation.
    if missing_core or (has_identity and not has_history):
        return Completeness.NEEDS_REVIEW, found
    if degraded or not sources:
        return Completeness.PARTIAL, found
    return Completeness.COMPLETE, found

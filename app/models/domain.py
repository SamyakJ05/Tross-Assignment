"""The response schema.

The brief left this open, so the shape here is a deliberate set of choices:

1.  Every list field defaults to empty rather than null. A consumer never has
    to distinguish "absent" from "empty" before iterating.

2.  Dates are a structured DateRange, not strings. LinkedIn routinely gives a
    year with no month, and a formatted string throws that precision away.
    `is_current` is stored rather than inferred from a null end date, because
    those are different facts.

3.  Positions nest under PositionGroup. LinkedIn models several roles at one
    employer as a group with children, and flattening it destroys promotion
    history -- the single most common defect in profile parsers. `positions`
    is offered alongside as a flat convenience view, derived from the groups.

4.  Nothing is required except the public identifier. A profile fetched
    through a degraded tier is still a valid Profile; how much of it arrived
    is reported by the envelope, not encoded as validation failure.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DateRange(BaseModel):
    start_year: int | None = None
    start_month: int | None = None
    end_year: int | None = None
    end_month: int | None = None
    is_current: bool = False

    @property
    def is_empty(self) -> bool:
        return self.start_year is None and self.end_year is None


class Image(BaseModel):
    """A LinkedIn-hosted image.

    LinkedIn serves images as a root URL plus a set of size-suffixed
    artifacts. We resolve the largest available and keep the rest, because
    the signed URLs expire and a consumer may want to pick a cheaper size.
    """

    url: str
    width: int | None = None
    height: int | None = None
    expires_at: int | None = None


class Location(BaseModel):
    country: str | None = None
    # LinkedIn's own label, e.g. "Bengaluru, Karnataka, India". Kept verbatim
    # rather than split, because the component order is locale-dependent.
    display: str | None = None
    postal_code: str | None = None


class Company(BaseModel):
    name: str | None = None
    urn: str | None = None
    public_identifier: str | None = None
    logo: Image | None = None
    industry: str | None = None
    staff_count: int | None = None


class Position(BaseModel):
    """A single role."""

    title: str | None = None
    company_name: str | None = None
    company: Company | None = None
    employment_type: str | None = None
    location: str | None = None
    description: str | None = None
    dates: DateRange = Field(default_factory=DateRange)
    skills: list[str] = Field(default_factory=list)


class PositionGroup(BaseModel):
    """One employer, and every role held there.

    A group with a single child is the common case and is not special-cased:
    consumers get one code path, and promotion history is preserved wherever
    LinkedIn actually reports it.
    """

    company_name: str | None = None
    company: Company | None = None
    dates: DateRange = Field(default_factory=DateRange)
    positions: list[Position] = Field(default_factory=list)

    @property
    def is_multi_role(self) -> bool:
        return len(self.positions) > 1


class Education(BaseModel):
    school_name: str | None = None
    school_urn: str | None = None
    degree_name: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    activities: str | None = None
    description: str | None = None
    logo: Image | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Skill(BaseModel):
    name: str
    # Present only when the viewer is permitted to see it, which for a
    # non-connection is usually never. Null here means "not visible", not
    # "zero endorsements" -- a distinction worth preserving.
    endorsement_count: int | None = None


class Certification(BaseModel):
    name: str | None = None
    authority: str | None = None
    license_number: str | None = None
    url: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Language(BaseModel):
    name: str
    proficiency: str | None = None


class Project(BaseModel):
    title: str | None = None
    description: str | None = None
    url: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Honor(BaseModel):
    title: str | None = None
    issuer: str | None = None
    description: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Publication(BaseModel):
    name: str | None = None
    publisher: str | None = None
    description: str | None = None
    url: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class VolunteerExperience(BaseModel):
    role: str | None = None
    organization: str | None = None
    cause: str | None = None
    description: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Profile(BaseModel):
    """A LinkedIn profile.

    Only `public_identifier` is guaranteed. Everything else depends on which
    tier answered and what that member has chosen to make visible.
    """

    public_identifier: str
    urn: str | None = None
    member_id: str | None = None

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    headline: str | None = None
    summary: str | None = None
    pronouns: str | None = None

    location: Location = Field(default_factory=Location)
    industry: str | None = None
    follower_count: int | None = None
    connection_count: int | None = None
    # LinkedIn caps the displayed count at 500 and reports the overflow
    # separately; conflating them would silently understate large networks.
    connection_count_capped: bool = False

    profile_picture: Image | None = None
    background_image: Image | None = None

    position_groups: list[PositionGroup] = Field(default_factory=list)
    educations: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    honors: list[Honor] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    volunteer_experience: list[VolunteerExperience] = Field(default_factory=list)

    @property
    def positions(self) -> list[Position]:
        """Flat view over every role, in profile order.

        Derived rather than stored, so it cannot drift from `position_groups`.
        """
        return [p for group in self.position_groups for p in group.positions]

    @property
    def current_positions(self) -> list[Position]:
        return [p for p in self.positions if p.dates.is_current]

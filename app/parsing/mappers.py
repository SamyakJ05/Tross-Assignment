"""Mapping raw payloads onto the domain model.

Three input shapes are handled, one per tier: the dash REST top card, the
GraphQL component tree, and the public page's JSON-LD. They are merged
rather than chosen between -- a field present on a richer tier always wins,
but a field only the thinner tier supplied is still kept.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.domain import (
    Certification,
    Company,
    DateRange,
    Education,
    Honor,
    Image,
    Language,
    Location,
    Position,
    PositionGroup,
    Profile,
    Project,
    Publication,
    Skill,
    VolunteerExperience,
)
from app.parsing.components import (
    description_text,
    entity_components,
    parse_caption_dates,
    read_slots,
    sub_components,
)
from app.parsing.normalized import EntityGraph, resolve_image

_DATE_CAPTION_RE = re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b.*\d{4}")
_RSC_EMPLOYMENT_TYPES = {"full-time", "part-time", "internship", "contract", "freelance"}
_RSC_EMPLOYMENT_CAPTION_RE = re.compile(
    r"^(Full-time|Part-time|Internship|Contract|Freelance)(?:\s+·\s+.*)?$",
    re.IGNORECASE,
)
_RSC_DURATION_RE = re.compile(r"\d+(?:\.\d+)?\s+(?:yrs?|mos?)(?:\s+\d+\s+mos?)?")
_RSC_COMBINED_EMPLOYMENT_DURATION_RE = re.compile(
    r"^(?:Full-time|Part-time|Internship|Contract|Freelance)\s+·\s+.*\d+\s+(?:yrs?|mos?)",
    re.IGNORECASE,
)
_RSC_LOCATION_MODE_RE = re.compile(r"\s·\s(?:On-site|Remote|Hybrid)$", re.IGNORECASE)
_RSC_IGNORED_VALUES = {"experience", "expanded", "collapsed", "show all", "show all experiences"}
_RSC_ROLE_WORD_RE = re.compile(
    r"\b(?:intern|engineer|developer|manager|analyst|architect|consultant|scientist|designer|researcher)\b",
    re.IGNORECASE,
)
_RSC_ISSUED_RE = re.compile(r"^Issued\s+(?:[A-Z][a-z]+\s+)?\d{4}$")
_RSC_DEGREE_RE = re.compile(
    r"\b(?:associate|bachelor|master|doctor|phd|btech|bsc|bcom|ba|mba|pgdm|diploma)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Tier 2: the dash REST top card
# ---------------------------------------------------------------------------


def map_top_card(payload: dict[str, Any], slug: str) -> Profile:
    """Identity fields from the resolution hop's response."""
    graph = EntityGraph(payload)
    entity = graph.profile() or {}

    first = entity.get("firstName")
    last = entity.get("lastName")
    full = " ".join(p for p in (first, last) if p) or None

    geo = entity.get("geoLocation") or entity.get("location") or {}
    if isinstance(geo, str):
        geo = graph.get(geo) or {}

    picture = resolve_image(graph, entity, "profilePicture")
    background = resolve_image(graph, entity, "backgroundPicture")

    connections = entity.get("connections") or {}
    conn_count = (
        connections.get("paging", {}).get("total") if isinstance(connections, dict) else None
    )

    return Profile(
        public_identifier=entity.get("publicIdentifier") or slug,
        urn=entity.get("entityUrn"),
        first_name=first,
        last_name=last,
        full_name=full,
        headline=entity.get("headline"),
        summary=entity.get("summary"),
        pronouns=_pronouns(entity),
        location=Location(
            display=entity.get("geoLocationName")
            or geo.get("defaultLocalizedName")
            or entity.get("locationName"),
            country=entity.get("geoCountryName") or entity.get("country"),
            postal_code=(entity.get("address") or {}).get("postalCode")
            if isinstance(entity.get("address"), dict)
            else None,
        ),
        industry=entity.get("industryName"),
        follower_count=entity.get("followerCount"),
        connection_count=conn_count,
        connection_count_capped=bool(conn_count and conn_count >= 500),
        profile_picture=Image(**picture) if picture else None,
        background_image=Image(**background) if background else None,
    )


def _pronouns(entity: dict[str, Any]) -> str | None:
    node = entity.get("pronoun") or entity.get("standardizedPronoun")
    if isinstance(node, str):
        return node.replace("_", "/").title() if "_" in node else node
    if isinstance(node, dict):
        return node.get("standardizedPronoun") or node.get("customPronoun")
    return None


# ---------------------------------------------------------------------------
# Tier 1: the GraphQL component tree
# ---------------------------------------------------------------------------


def map_experience(payload: dict[str, Any]) -> list[PositionGroup]:
    """Experience, preserving multi-role employers.

    This is the case most parsers get wrong. LinkedIn renders three
    promotions at one company as an outer component naming the company with
    nested components for each role. Flattening the outer level either loses
    the role history entirely or emits three unrelated jobs at the same
    employer.

    We detect the nested shape and build a group with children; a single-role
    employer becomes a group of one, so consumers get one code path either
    way.
    """
    groups: list[PositionGroup] = []

    for component in entity_components(payload):
        slots = read_slots(component)
        nested = sub_components(component)

        if nested:
            # Multi-role: outer names the employer, children are the roles.
            company_name = slots["title"]
            positions = [_position_from_nested(child, company_name) for child in nested]
            groups.append(
                PositionGroup(
                    company_name=company_name,
                    company=Company(name=company_name),
                    dates=DateRange(**parse_caption_dates(slots["caption"])),
                    positions=[p for p in positions if p.title],
                )
            )
        else:
            # Single role: title is the role, subtitle the employer.
            title = slots["title"]
            if not title:
                continue
            company_name = _strip_employment_type(slots["subtitle"])
            position = Position(
                title=title,
                company_name=company_name,
                company=Company(name=company_name) if company_name else None,
                employment_type=_employment_type(slots["subtitle"]),
                location=slots["metadata"],
                description=description_text(component),
                dates=DateRange(**parse_caption_dates(slots["caption"])),
            )
            groups.append(
                PositionGroup(
                    company_name=company_name,
                    company=Company(name=company_name) if company_name else None,
                    dates=position.dates,
                    positions=[position],
                )
            )

    return groups


def _position_from_nested(child: dict[str, Any], company_name: str | None) -> Position:
    slots = read_slots(child)
    return Position(
        title=slots["title"],
        company_name=company_name,
        company=Company(name=company_name) if company_name else None,
        employment_type=_employment_type(slots["subtitle"]),
        location=slots["metadata"],
        description=description_text(child),
        dates=DateRange(**parse_caption_dates(slots["caption"])),
    )


_EMPLOYMENT_TYPES = (
    "Full-time",
    "Part-time",
    "Self-employed",
    "Freelance",
    "Contract",
    "Internship",
    "Apprenticeship",
    "Seasonal",
)


def _employment_type(subtitle: str | None) -> str | None:
    """LinkedIn packs employment type into the subtitle after a middle dot."""
    if not subtitle or "·" not in subtitle:
        return None
    tail = subtitle.split("·")[-1].strip()
    return tail if tail in _EMPLOYMENT_TYPES else None


def _strip_employment_type(subtitle: str | None) -> str | None:
    if not subtitle:
        return None
    return subtitle.split("·")[0].strip() or None


def map_education(payload: dict[str, Any]) -> list[Education]:
    out: list[Education] = []
    for component in entity_components(payload):
        slots = read_slots(component)
        if not slots["title"]:
            continue
        degree, field = _split_degree(slots["subtitle"])
        out.append(
            Education(
                school_name=slots["title"],
                degree_name=degree,
                field_of_study=field,
                description=description_text(component),
                dates=DateRange(**parse_caption_dates(slots["caption"])),
            )
        )
    return out


def _split_degree(subtitle: str | None) -> tuple[str | None, str | None]:
    """Subtitle is conventionally 'Degree, Field of Study'."""
    if not subtitle:
        return None, None
    if "," in subtitle:
        degree, field = subtitle.split(",", 1)
        return degree.strip() or None, field.strip() or None
    return subtitle.strip() or None, None


def map_skills(payload: dict[str, Any]) -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for component in entity_components(payload):
        slots = read_slots(component)
        name = slots["title"]
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(Skill(name=name, endorsement_count=_endorsements(slots["subtitle"])))
    return out


def _endorsements(subtitle: str | None) -> int | None:
    """Null means 'not visible to this viewer', not zero.

    Endorsement counts are hidden from non-connections, so defaulting to 0
    would assert something false about most profiles.
    """
    if not subtitle:
        return None
    digits = "".join(c for c in subtitle if c.isdigit())
    return int(digits) if digits else None


def map_certifications(payload: dict[str, Any]) -> list[Certification]:
    out: list[Certification] = []
    for component in entity_components(payload):
        slots = read_slots(component)
        if not slots["title"]:
            continue
        out.append(
            Certification(
                name=slots["title"],
                authority=slots["subtitle"],
                license_number=_license_number(slots["metadata"]),
                dates=DateRange(**parse_caption_dates(slots["caption"])),
            )
        )
    return out


def _license_number(metadata: str | None) -> str | None:
    if not metadata:
        return None
    lowered = metadata.lower()
    for marker in ("credential id", "license"):
        if marker in lowered:
            return metadata.split(":", 1)[-1].strip() or None
    return None


def map_languages(payload: dict[str, Any]) -> list[Language]:
    out: list[Language] = []
    for component in entity_components(payload):
        slots = read_slots(component)
        if not slots["title"]:
            continue
        out.append(Language(name=slots["title"], proficiency=slots["caption"] or slots["subtitle"]))
    return out


def map_projects(payload: dict[str, Any]) -> list[Project]:
    return [
        Project(
            title=s["title"],
            description=description_text(c),
            dates=DateRange(**parse_caption_dates(s["caption"])),
        )
        for c in entity_components(payload)
        if (s := read_slots(c))["title"]
    ]


def map_honors(payload: dict[str, Any]) -> list[Honor]:
    return [
        Honor(
            title=s["title"],
            issuer=s["subtitle"],
            description=description_text(c),
            dates=DateRange(**parse_caption_dates(s["caption"])),
        )
        for c in entity_components(payload)
        if (s := read_slots(c))["title"]
    ]


def map_publications(payload: dict[str, Any]) -> list[Publication]:
    return [
        Publication(
            name=s["title"],
            publisher=s["subtitle"],
            description=description_text(c),
            dates=DateRange(**parse_caption_dates(s["caption"])),
        )
        for c in entity_components(payload)
        if (s := read_slots(c))["title"]
    ]


def map_volunteer(payload: dict[str, Any]) -> list[VolunteerExperience]:
    return [
        VolunteerExperience(
            role=s["title"],
            organization=s["subtitle"],
            cause=s["metadata"],
            description=description_text(c),
            dates=DateRange(**parse_caption_dates(s["caption"])),
        )
        for c in entity_components(payload)
        if (s := read_slots(c))["title"]
    ]


SECTION_MAPPERS = {
    "experience": map_experience,
    "education": map_education,
    "skills": map_skills,
    "certifications": map_certifications,
    "languages": map_languages,
    "projects": map_projects,
    "honors": map_honors,
    "publications": map_publications,
    "volunteer": map_volunteer,
}

_SECTION_FIELDS = {
    "experience": "position_groups",
    "education": "educations",
    "skills": "skills",
    "certifications": "certifications",
    "languages": "languages",
    "projects": "projects",
    "honors": "honors",
    "publications": "publications",
    "volunteer": "volunteer_experience",
}


def apply_sections(profile: Profile, sections: dict[str, dict[str, Any]]) -> Profile:
    """Attach every successfully fetched section to the profile."""
    for name, payload in sections.items():
        mapper = SECTION_MAPPERS.get(name)
        field = _SECTION_FIELDS.get(name)
        if not mapper or not field:
            continue
        try:
            setattr(profile, field, mapper(payload))
        except Exception:  # noqa: BLE001
            # One malformed section must not lose the rest of the profile.
            # The absence surfaces through the envelope's completeness check
            # rather than as an exception.
            continue
    return profile


def apply_rsc_experience(profile: Profile, values: list[str]) -> Profile:
    """Map the current RSC Experience card without guessing missing fields.

    Current cards normally render ``company → employment/duration → location
    → title → dates``.  Older variants render the duration separately.  Both
    forms are handled, and consecutive roles remain grouped under the same
    employer.
    """
    company: str | None = None
    positions: list[PositionGroup] = []
    seen: set[tuple[str, str, str]] = set()
    previous_date_index = -1
    date_indexes = [i for i, value in enumerate(values) if _DATE_CAPTION_RE.search(value)]

    for date_number, index in enumerate(date_indexes):
        # Everything between the last date row and this date row belongs to
        # the next role. A role description can precede a promotion title, so
        # use semantic filters rather than a fixed number of preceding items.
        preceding = values[previous_date_index + 1 : index]
        following_end = (
            date_indexes[date_number + 1] if date_number + 1 < len(date_indexes) else len(values)
        )
        following = values[index + 1 : following_end]
        previous_date_index = index

        # Employer headings use either "company → duration" or
        # "company → employment · duration". A bare "title → Internship"
        # is deliberately not a heading: that is a promotion at the active
        # employer.
        for item_index, item in enumerate(preceding[:-1]):
            next_item = preceding[item_index + 1]
            starts_company_group = _RSC_DURATION_RE.fullmatch(
                next_item
            ) or _RSC_COMBINED_EMPLOYMENT_DURATION_RE.fullmatch(next_item)
            if starts_company_group:
                company = item

        # Single-role cards commonly put the company in the subtitle as
        # "Company · Employment type" instead of emitting an outer heading.
        company_from_inline_subtitle = False
        for item in reversed(preceding):
            if " · " not in item:
                continue
            candidate, suffix = item.rsplit(" · ", 1)
            if suffix.lower() in _RSC_EMPLOYMENT_TYPES:
                company = candidate
                company_from_inline_subtitle = True
                break

        title = next((item for item in reversed(preceding) if _rsc_is_title(item)), None)
        if not title or not company:
            continue

        employment_type = _rsc_employment_type(preceding)
        location = _rsc_location(following) or (
            None if company_from_inline_subtitle else _rsc_location(preceding)
        )
        dates = DateRange(**parse_caption_dates(values[index]))
        key = (company, title, values[index])
        if key in seen:
            continue
        seen.add(key)

        description_lines = [
            item
            for item in following
            if item.startswith(("•", "-")) and not item.startswith("--")
        ]
        position = Position(
            title=title,
            company_name=company,
            company=Company(name=company),
            employment_type=employment_type,
            location=location,
            description="\n".join(description_lines) or None,
            dates=dates,
        )
        group = next((item for item in positions if item.company_name == company), None)
        if group:
            group.positions.append(position)
        else:
            positions.append(
                PositionGroup(
                    company_name=company,
                    company=Company(name=company),
                    dates=dates,
                    positions=[position],
                )
            )

    _append_undated_rsc_tail(positions, values[previous_date_index + 1 :], seen)

    if positions:
        profile.position_groups = positions
    return profile


def _rsc_is_title(value: str) -> bool:
    """Whether an RSC card string can be a role title."""
    return (
        value.lower() not in _RSC_IGNORED_VALUES
        and not value.startswith(("•", "-"))
        and not _RSC_DURATION_RE.fullmatch(value)
        and not _RSC_EMPLOYMENT_CAPTION_RE.fullmatch(value)
        and " · " not in value
        and "logo" not in value.lower()
        and not _DATE_CAPTION_RE.search(value)
    )


def _rsc_employment_type(values: list[str]) -> str | None:
    for value in values:
        match = _RSC_EMPLOYMENT_CAPTION_RE.fullmatch(value)
        if match:
            return match.group(1).capitalize()
        if " · " in value:
            suffix = value.rsplit(" · ", 1)[-1]
            if suffix.lower() in _RSC_EMPLOYMENT_TYPES:
                return suffix.capitalize()
    return None


def _rsc_location(values: list[str]) -> str | None:
    for value in values:
        if _RSC_LOCATION_MODE_RE.search(value):
            return value
    # Some older cards show a plain city/country directly after the date.
    for value in values[:3]:
        if "," in value and not value.startswith(("•", "-")):
            return value
    return None


def _append_undated_rsc_tail(
    groups: list[PositionGroup], values: list[str], seen: set[tuple[str, str, str]]
) -> None:
    """Keep a final visible role when LinkedIn omits its date row.

    Such rows appear as ``title → company → description`` after a dated role.
    Require a role-like title and a separate company label, so a free-form
    description cannot become a fabricated experience record.
    """
    for index, title in enumerate(values[:-1]):
        if not _RSC_ROLE_WORD_RE.search(title) or not _rsc_is_title(title):
            continue
        company = values[index + 1]
        if not _rsc_is_title(company) or _RSC_ROLE_WORD_RE.search(company):
            continue
        key = (company, title, "")
        if key in seen:
            continue
        seen.add(key)
        description = next(
            (
                value
                for value in values[index + 2 :]
                if _rsc_is_title(value) and not _RSC_ROLE_WORD_RE.search(value)
            ),
            None,
        )
        group = next((item for item in groups if item.company_name == company), None)
        position = Position(
            title=title,
            company_name=company,
            company=Company(name=company),
            description=description,
        )
        if group:
            group.positions.append(position)
        else:
            groups.append(
                PositionGroup(
                    company_name=company,
                    company=Company(name=company),
                    positions=[position],
                )
            )
        return


def apply_rsc_below_activity(profile: Profile, values: list[str]) -> Profile:
    """Map the visible education and certification rows from the lower RSC card.

    The current card renders these fields as display strings, not normalized
    entities. Only fields with an unambiguous neighbouring label are emitted.
    Dates are left empty when the card does not show them.
    """
    if not profile.certifications:
        certifications: list[Certification] = []
        for index, value in enumerate(values):
            if not _RSC_ISSUED_RE.fullmatch(value) or index < 2:
                continue
            name, authority = values[index - 2 : index]
            if not _rsc_is_title(name) or not _rsc_is_title(authority):
                continue
            license_number = next(
                (
                    candidate.removeprefix("Credential ID ")
                    for candidate in values[index + 1 : index + 3]
                    if candidate.startswith("Credential ID ")
                ),
                None,
            )
            certifications.append(
                Certification(
                    name=name,
                    authority=authority,
                    license_number=license_number,
                    dates=DateRange(**parse_caption_dates(value.removeprefix("Issued "))),
                )
            )
        profile.certifications = _dedupe_certifications(certifications)

    if not profile.educations:
        educations: list[Education] = []
        for index, value in enumerate(values):
            if not _RSC_DEGREE_RE.search(value) or index == 0:
                continue
            school = values[index - 1]
            if _rsc_is_title(school):
                educations.append(Education(school_name=school, degree_name=value))
        profile.educations = _dedupe_educations(educations)

    return profile


def apply_rsc_skills(profile: Profile, names: list[str]) -> Profile:
    """Attach skills discovered via the details/skills pager.

    Endorsement counts are not available through this path (see rsc.py's
    skill_names), unlike the GraphQL tier's map_skills. A later GraphQL
    section fetch, if one is ever configured again, still wins on conflict
    because this only fills an empty list.
    """
    if not profile.skills:
        profile.skills = [Skill(name=name) for name in names]
    return profile


def _dedupe_certifications(values: list[Certification]) -> list[Certification]:
    seen: set[tuple[str | None, str | None]] = set()
    return [
        value
        for value in values
        if not ((key := (value.name, value.authority)) in seen or seen.add(key))
    ]


def _dedupe_educations(values: list[Education]) -> list[Education]:
    seen: set[tuple[str | None, str | None]] = set()
    return [
        value
        for value in values
        if not ((key := (value.school_name, value.degree_name)) in seen or seen.add(key))
    ]


# ---------------------------------------------------------------------------
# Tier 3: public JSON-LD
# ---------------------------------------------------------------------------


def map_jsonld(person: dict[str, Any], slug: str) -> Profile:
    """Map the public page's schema.org Person block.

    Much thinner than Voyager: no dates, no descriptions, no skills. It
    exists so the service degrades to something useful rather than to
    nothing when the authenticated tiers are unavailable.
    """
    address = person.get("address") or {}
    location_bits = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry"),
    ]
    display = ", ".join(b for b in location_bits if b) or None

    image = person.get("image") or {}
    image_url = image.get("contentUrl") if isinstance(image, dict) else None

    groups: list[PositionGroup] = []
    for work in _as_list(person.get("worksFor")):
        name = work.get("name") if isinstance(work, dict) else None
        if not name:
            continue
        # jobTitle arrives as either a string or a list of them.
        position = Position(
            title=_first_str(person.get("jobTitle")),
            company_name=name,
            company=Company(name=name),
        )
        groups.append(
            PositionGroup(
                company_name=name,
                company=Company(name=name),
                positions=[position],
            )
        )

    educations: list[Education] = []
    for school in _as_list(person.get("alumniOf")):
        if not isinstance(school, dict):
            continue
        name = school.get("name")
        if not name:
            continue
        educations.append(Education(school_name=name, field_of_study=school.get("description")))

    return Profile(
        public_identifier=slug,
        full_name=person.get("name"),
        first_name=person.get("givenName"),
        last_name=person.get("familyName"),
        headline=_first_str(person.get("jobTitle")) or person.get("description"),
        summary=person.get("description"),
        location=Location(display=display, country=address.get("addressCountry")),
        profile_picture=Image(url=image_url) if image_url else None,
        position_groups=groups,
        educations=educations,
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return None


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge(primary: Profile, secondary: Profile) -> Profile:
    """Fill gaps in `primary` from `secondary`.

    Richer tier wins on conflict; thinner tier still contributes anything
    the richer one did not supply. Empty lists count as gaps, so a section
    that failed on tier 1 can be filled from tier 3.
    """
    # model_fields is read off the class: reading it from an instance is
    # deprecated in Pydantic 2.11+.
    for field in type(primary).model_fields:
        current = getattr(primary, field, None)
        candidate = getattr(secondary, field, None)
        if candidate in (None, "", []):
            continue
        if (
            current in (None, "", [])
            or isinstance(current, Location)
            and not any((current.display, current.country, current.postal_code))
        ):
            setattr(primary, field, candidate)
    return primary

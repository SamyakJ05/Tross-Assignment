"""Resolving LinkedIn's normalized JSON into a usable object graph.

With `accept: application/vnd.linkedin.normalized+json+2.1`, Voyager returns:

    {
      "data":     {"*elements": ["urn:li:fsd_profile:ACoAAA..."]},
      "included": [
        {"entityUrn": "urn:li:fsd_profile:ACoAAA...",
         "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
         "firstName": "...",
         "*profilePositionGroups": ["urn:li:fsd_profilePositionGroup:(...)"]},
        {"entityUrn": "urn:li:fsd_profilePositionGroup:(...)", ...}
      ]
    }

`included` is a flat array of every entity the query touched, each carrying
an `entityUrn` identity and a `$type` discriminator. Fields prefixed with
`*` are references rather than values.

So the client's job is to index by URN and walk the references -- rebuilding
a normalized graph the real frontend was expected to reassemble. This is a
better format than the nested alternative: entities appear once regardless
of how many places reference them, and cycles are expressible.

The resolution here is deliberately lazy rather than eager. Eagerly
inlining every reference on a profile with a large network produces a very
large object and can recurse indefinitely through mutual references.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class EntityGraph:
    """An index over a normalized response's `included` array."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self._by_urn: dict[str, dict[str, Any]] = {}
        self._by_type: dict[str, list[dict[str, Any]]] = {}

        for entity in payload.get("included") or []:
            if not isinstance(entity, dict):
                continue
            urn = entity.get("entityUrn")
            if urn:
                self._by_urn[urn] = entity
            etype = entity.get("$type")
            if etype:
                self._by_type.setdefault(etype, []).append(entity)

    # -- lookup -----------------------------------------------------------

    def get(self, urn: str | None) -> dict[str, Any] | None:
        if not urn:
            return None
        return self._by_urn.get(urn)

    def of_type(self, type_suffix: str) -> list[dict[str, Any]]:
        """Every entity whose $type ends with the given suffix.

        Matching on a suffix rather than the fully-qualified name is
        deliberate. LinkedIn moves classes between packages across releases
        (`voyager.identity.profile` to `voyager.dash.identity.profile`, for
        one), and pinning the full path makes the parser break on a rename
        that changed nothing about the data.
        """
        out: list[dict[str, Any]] = []
        for etype, entities in self._by_type.items():
            if etype.endswith(type_suffix):
                out.extend(entities)
        return out

    def urns_of_kind(self, kind: str) -> list[str]:
        """Every indexed URN of a given kind, e.g. 'fsd_profilePositionGroup'."""
        prefix = f"urn:li:{kind}:"
        return [u for u in self._by_urn if u.startswith(prefix)]

    # -- reference resolution ---------------------------------------------

    def deref(self, entity: dict[str, Any] | None, field: str) -> list[dict[str, Any]]:
        """Resolve a reference field to the entities it points at.

        Handles both spellings, because responses are inconsistent about it:
        `*field` holding URNs, and `field` holding either URNs or already-
        inlined objects.
        """
        if not entity:
            return []

        raw = entity.get(f"*{field}", entity.get(field))
        if raw is None:
            return []

        values = raw if isinstance(raw, list) else [raw]
        resolved: list[dict[str, Any]] = []
        for v in values:
            if isinstance(v, str):
                found = self.get(v)
                if found:
                    resolved.append(found)
            elif isinstance(v, dict):
                # Already inlined, or a wrapper carrying a nested reference.
                inner = v.get("*element") or v.get("element")
                if isinstance(inner, str):
                    found = self.get(inner)
                    if found:
                        resolved.append(found)
                else:
                    resolved.append(v)
        return resolved

    def deref_one(self, entity: dict[str, Any] | None, field: str) -> dict[str, Any] | None:
        found = self.deref(entity, field)
        return found[0] if found else None

    # -- entry points ------------------------------------------------------

    def root_elements(self) -> list[dict[str, Any]]:
        """Entities referenced by `data`, which is where a response starts."""
        data = self._payload.get("data") or {}
        raw = data.get("*elements") or data.get("elements") or []
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                found = self.get(item)
                if found:
                    out.append(found)
            elif isinstance(item, dict):
                out.append(item)
        return out

    def profile(self) -> dict[str, Any] | None:
        """The Profile entity, however this particular response reached it."""
        candidates = self.of_type("identity.profile.Profile")
        if candidates:
            return candidates[0]
        for urn in self.urns_of_kind("fsd_profile"):
            entity = self._by_urn[urn]
            if "firstName" in entity or "publicIdentifier" in entity:
                return entity
        return None

    def __len__(self) -> int:
        return len(self._by_urn)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._by_urn.values())


# ---------------------------------------------------------------------------
# Image resolution
# ---------------------------------------------------------------------------


def resolve_image(
    graph: EntityGraph,
    container: dict[str, Any] | None,
    field: str,
) -> dict[str, Any] | None:
    """Reassemble a LinkedIn image URL.

    Images are stored as a root URL plus size-suffixed artifacts, which the
    client is expected to concatenate:

        rootUrl                 https://media.licdn.com/dms/image/.../
        fileIdentifyingUrlPathSegment   400_400/0/1234?e=...&v=beta&t=...

    Neither half is a usable URL alone. We pick the largest artifact, since
    a consumer can always request a smaller one and cannot recover detail
    that was never fetched.
    """
    if not container:
        return None

    node = container.get(field) or container.get(f"*{field}")
    if isinstance(node, str):
        node = graph.get(node)
    if not isinstance(node, dict):
        return None

    vector = (
        node.get("displayImageReference", {}).get("vectorImage") or node.get("vectorImage") or node
    )
    if not isinstance(vector, dict):
        return None

    root = vector.get("rootUrl")
    artifacts = vector.get("artifacts") or []
    if not root or not artifacts:
        return None

    largest = max(
        artifacts,
        key=lambda a: a.get("width", 0) if isinstance(a, dict) else 0,
        default=None,
    )
    if not isinstance(largest, dict):
        return None

    segment = largest.get("fileIdentifyingUrlPathSegment", "")
    return {
        "url": f"{root}{segment}",
        "width": largest.get("width"),
        "height": largest.get("height"),
        "expires_at": largest.get("expiresAt"),
    }


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def resolve_date_range(node: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise LinkedIn's dateRange into our DateRange fields.

    LinkedIn routinely supplies a year with no month, so month stays
    optional rather than being defaulted to January -- a default would
    fabricate precision the source never had.

    `is_current` is derived from the absence of an end date, but stored
    explicitly, because "ongoing" and "end date unknown" are different facts
    that a null end date alone cannot distinguish.
    """
    if not isinstance(node, dict):
        return {"is_current": False}

    start = node.get("start") or {}
    end = node.get("end") or {}

    has_end = bool(end.get("year"))
    return {
        "start_year": start.get("year"),
        "start_month": start.get("month"),
        "end_year": end.get("year"),
        "end_month": end.get("month"),
        "is_current": not has_end,
    }

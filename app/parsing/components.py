"""Walking LinkedIn's profile component tree.

This is the hardest part of the extraction, and it is worth being precise
about why.

Modern profile sections do not arrive as data. They arrive as `topComponents`
-- a generic renderable UI tree of `textComponent`, `entityComponent`,
`insightComponent`, `fixedListComponent` and friends. The same visual row
can be produced by different component shapes depending on what the section
contains, and the tree carries presentation concerns (captions, subtitles,
truncation state) mixed in with the data.

So we are not parsing a data model. We are parsing LinkedIn's *render tree*,
which means a purely cosmetic redesign changes our output. That is the
structural reason this integration is fragile, and no amount of careful
coding removes it -- it can only be detected and reported, which is what the
envelope's completeness classification is for.

The strategy here is to extract by *shape* rather than by exact type name:
find entity components anywhere in the tree, then read their conventional
title/subtitle/caption slots. That survives the frequent renames and the
occasional extra wrapper layer, at the cost of being less precise than a
schema-aware parser would be if a schema existed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# The slots an entityComponent conventionally uses. Their meaning shifts by
# section -- in experience, title is the role and subtitle the company; in
# education, title is the school and subtitle the degree -- so the mapping
# to domain fields belongs in mappers.py, not here.
_TEXT_SLOTS = ("titleV2", "title", "subtitle", "caption", "metadata", "secondarySubtitle")


def walk(node: Any, depth: int = 0, max_depth: int = 40) -> Iterator[dict[str, Any]]:
    """Yield every dict in a nested structure, depth-first.

    Depth-capped because the tree contains mutual references in some shapes
    and an uncapped walk can recurse until the stack gives out.
    """
    if depth > max_depth:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value, depth + 1, max_depth)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, depth + 1, max_depth)


def extract_text(node: Any) -> str | None:
    """Pull display text out of the several shapes LinkedIn uses for it.

    Text appears as a bare string, as `{"text": "..."}`, and as an
    attributed-string object carrying inline styling. All three mean the
    same thing to us.
    """
    if node is None:
        return None
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        for key in ("text", "accessibilityText"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                inner = value.get("text")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return None


def is_entity_component(node: dict[str, Any]) -> bool:
    """Whether a node looks like a rendered profile entity.

    Identified by shape rather than by `$type`, because the type names move
    between releases while the slot convention has been comparatively
    stable.
    """
    if not isinstance(node, dict):
        return False
    if "entityComponent" in node:
        return True
    present = sum(1 for slot in _TEXT_SLOTS if slot in node)
    return present >= 2


def entity_components(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Top-level entity components in a section, in document order.

    The walk *prunes*: once a component is found, its children are not
    searched. This matters, and getting it wrong is subtle.

    A multi-role employer nests its individual roles inside the outer
    component's `subComponents`. An unpruned walk finds those nested roles
    again at the top level and emits them a second time as standalone
    single-role entries -- so a person with three promotions at one company
    yields the correct group *plus* three phantom jobs. The output looks
    plausible and is wrong, which is the worst kind of parser bug.

    Nested roles are reached deliberately, through `sub_components()`, and
    only from their parent.
    """
    found: list[dict[str, Any]] = []
    _collect_pruned(payload, found)
    if found:
        return _dedupe(found)

    # Nothing carried an explicit `entityComponent` key. Fall back to shape
    # matching, in case a response uses a wrapper we have not seen.
    return _dedupe([n for n in walk(payload) if is_entity_component(n)])


def _collect_pruned(
    node: Any,
    out: list[dict[str, Any]],
    depth: int = 0,
    max_depth: int = 40,
) -> None:
    if depth > max_depth:
        return
    if isinstance(node, dict):
        component = node.get("entityComponent")
        if isinstance(component, dict):
            out.append(component)
            return  # do not descend: children belong to this component
        for value in node.values():
            _collect_pruned(value, out, depth + 1, max_depth)
    elif isinstance(node, list):
        for item in node:
            _collect_pruned(item, out, depth + 1, max_depth)


def _dedupe(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeats, preserving order.

    The walk reaches the same node by more than one path when components are
    referenced from several places in the tree.
    """
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for node in nodes:
        marker = id(node)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(node)
    return out


def read_slots(component: dict[str, Any]) -> dict[str, str | None]:
    """Read the conventional text slots off one entity component."""
    return {
        "title": extract_text(component.get("titleV2") or component.get("title")),
        "subtitle": extract_text(component.get("subtitle")),
        "caption": extract_text(component.get("caption")),
        "metadata": extract_text(component.get("metadata")),
        "secondary_subtitle": extract_text(component.get("secondarySubtitle")),
    }


def sub_components(component: dict[str, Any]) -> list[dict[str, Any]]:
    """Nested entity components beneath one component.

    This is how LinkedIn expresses several roles at one employer: the outer
    component names the company, and the nested ones are the individual
    positions. Reading only the outer level is what destroys promotion
    history in most parsers.
    """
    container = component.get("subComponents")
    if not isinstance(container, dict):
        return []

    nested: list[dict[str, Any]] = []
    for node in walk(container.get("components", container)):
        if "entityComponent" in node and isinstance(node["entityComponent"], dict):
            nested.append(node["entityComponent"])
    return _dedupe(nested)


def description_text(component: dict[str, Any]) -> str | None:
    """The free-text description hanging off a component, if any.

    Lives among the sub-components as a text component rather than in a
    dedicated field, so it has to be distinguished from nested entities by
    the absence of entity slots.
    """
    container = component.get("subComponents")
    if not isinstance(container, dict):
        return None

    chunks: list[str] = []
    for node in walk(container):
        if not isinstance(node, dict):
            continue
        if "fixedListComponent" in node or "entityComponent" in node:
            continue
        text_node = node.get("textComponent")
        if isinstance(text_node, dict):
            text = extract_text(text_node.get("text"))
            if text:
                chunks.append(text)

    if not chunks:
        return None
    # Preserve order, drop duplicates that the multi-path walk introduces.
    seen: set[str] = set()
    ordered = [c for c in chunks if not (c in seen or seen.add(c))]
    return "\n".join(ordered) or None


def parse_caption_dates(caption: str | None) -> dict[str, Any]:
    """Best-effort date parse from a rendered caption.

    The component tree gives dates as display strings -- "Jan 2023 - Present
    · 1 yr 8 mos" -- because they were formatted for a human. Structured
    dateRange objects are available on the REST tiers but not reliably here.

    This is genuinely lossy and localised, so it is used only when a
    structured date is unavailable, and never overwrites one.
    """
    if not caption:
        return {"is_current": False}

    months = {
        m: i
        for i, m in enumerate(
            ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
            start=1,
        )
    }

    # Strip the duration suffix LinkedIn appends after a middle dot.
    span = caption.split("·")[0].strip()
    lowered = span.lower()
    is_current = "present" in lowered

    parts = [p.strip() for p in span.replace("–", "-").split("-")]
    out: dict[str, Any] = {"is_current": is_current}

    def _one(text: str) -> tuple[int | None, int | None]:
        tokens = text.lower().replace(",", " ").split()
        month = year = None
        for token in tokens:
            if token[:3] in months and month is None:
                month = months[token[:3]]
            elif token.isdigit() and len(token) == 4:
                year = int(token)
        return year, month

    if parts:
        y, m = _one(parts[0])
        out["start_year"], out["start_month"] = y, m
    if len(parts) > 1 and not is_current:
        y, m = _one(parts[1])
        out["end_year"], out["end_month"] = y, m

    return out

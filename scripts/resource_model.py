from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from game_context import GameContext


@dataclass(frozen=True)
class ResourceDescriptor:
    relative_path: Path
    category: str
    subtype: str
    container: str
    extension: str
    capability: str
    traits: frozenset[str]


def classify_resource(
    context: GameContext,
    relative_path: Path,
    *,
    traits: frozenset[str] = frozenset(),
) -> ResourceDescriptor:
    if (
        not isinstance(relative_path, Path)
        or relative_path.anchor
        or not relative_path.parts
        or ".." in relative_path.parts
    ):
        raise ValueError(
            "Resource path must be a canonical relative file path without parent traversal"
        )
    extension = relative_path.suffix.casefold()
    category = "unknown"
    subtype = "unknown"
    capability = ""
    effective_traits = set(traits)
    for group in context.resource_model.extension_groups:
        if extension in group.extensions:
            category = group.category
            subtype = group.name
            capability = group.capability
            effective_traits.update(group.default_traits.get(extension, ()))
            break

    container = ""
    protected_container = False
    f4se_container = False
    for part in relative_path.parts[:-1]:
        declared = context.resource_model.containers.get(part.casefold(), "")
        if declared:
            container = declared
            protected_container = protected_container or declared == "protected"
            f4se_container = f4se_container or declared == "f4se"
    if protected_container:
        container = "protected"
    elif f4se_container:
        container = "f4se"

    return ResourceDescriptor(
        relative_path=relative_path,
        category=category,
        subtype=subtype,
        container=container,
        extension=extension,
        capability=capability,
        traits=frozenset(effective_traits),
    )

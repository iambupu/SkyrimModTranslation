"""Calculate deterministic source identities for managed .NET adapters."""

from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from project_paths import is_under


SOURCE_SUFFIXES = {".cs", ".csproj", ".json", ".props", ".targets"}
IGNORED_DIRS = {"bin", "obj"}


def adapter_source_hash(
    adapter_project: Path,
    *,
    source_root: Path | None = None,
) -> str:
    adapter_project = adapter_project.resolve(strict=True)
    adapter_root = adapter_project.parent
    allowed_root = (
        source_root.resolve(strict=True)
        if source_root is not None
        else adapter_root.resolve(strict=True)
    )
    sources: set[Path] = set()
    for source in adapter_root.rglob("*"):
        if not source.is_file():
            continue
        if any(part.lower() in IGNORED_DIRS for part in source.relative_to(adapter_root).parts[:-1]):
            continue
        if source.suffix.lower() in SOURCE_SUFFIXES:
            sources.add(source.resolve(strict=True))

    try:
        project_xml = ElementTree.parse(adapter_project)
    except (OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"Invalid .NET adapter project: {adapter_project}: {exc}") from exc
    for element in project_xml.iter():
        include = element.attrib.get("Include", "")
        for raw_path in include.split(";"):
            raw_path = raw_path.strip()
            if not raw_path or "$(" in raw_path or any(marker in raw_path for marker in "*?"):
                continue
            candidate = adapter_root / raw_path.replace("\\", os.sep).replace("/", os.sep)
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if not is_under(resolved, allowed_root):
                raise ValueError(
                    f".NET adapter project input must stay under plugin source: {resolved}"
                )
            sources.add(resolved)

    digest = hashlib.sha256()
    for source in sorted(
        sources,
        key=lambda item: os.path.relpath(item, adapter_root).replace("\\", "/").lower(),
    ):
        relative = os.path.relpath(source, adapter_root).replace("\\", "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

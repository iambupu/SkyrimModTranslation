"""Validation helpers for the Claude Code marketplace metadata.

The Claude marketplace surface is intentionally separate from the Codex plugin
manifest. It exposes only non-GUI Skills and stays out of the Codex workflow hot
path unless called explicitly or by CI.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any
from file_utils import read_json_object_required as read_json
from project_paths import source_repo_root as repo_root


CLAUDE_PLUGIN_DIR = Path(".claude-plugin")
MARKETPLACE_JSON = CLAUDE_PLUGIN_DIR / "marketplace.json"
PLUGIN_JSON = CLAUDE_PLUGIN_DIR / "plugin.json"
PLUGIN_NAME = "skyrim-mod-chs-translation"
MARKETPLACE_NAME = "skyrim-mod-chs"
GUI_ONLY_SKILLS = {
    "lextranslator-gui-automation",
    "xtranslator-gui-automation",
}
COMPONENT_FIELDS = {
    "agents",
    "commands",
    "hooks",
    "mcpServers",
    "skills",
}





def normalize_component_path(raw_path: str) -> str:
    value = raw_path.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/")


def runtime_skill_names(root: Path) -> set[str]:
    skills_root = root / "skills"
    if not skills_root.is_dir():
        return set()
    return {
        child.name
        for child in skills_root.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    }


def _validate_marketplace(
    root: Path,
    payload: dict[str, Any],
    plugin_manifest: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if payload.get("name") != MARKETPLACE_NAME:
        errors.append(f"marketplace name must be {MARKETPLACE_NAME}.")

    owner = payload.get("owner")
    if not isinstance(owner, dict) or not str(owner.get("name", "")).strip():
        errors.append("marketplace owner.name is required.")

    plugins = payload.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1:
        errors.append("marketplace plugins must contain exactly one plugin entry.")
        return errors

    entry = plugins[0]
    if not isinstance(entry, dict):
        errors.append("marketplace plugin entry must be an object.")
        return errors
    if entry.get("name") != PLUGIN_NAME:
        errors.append(f"marketplace plugin name must be {PLUGIN_NAME}.")
    if str(entry.get("source", "")).strip() not in {".", "./"}:
        errors.append("marketplace plugin source must point at the repository root with ./.")
    if entry.get("strict") is not True:
        errors.append("marketplace plugin must use strict=true so only the curated non-GUI skill list is loaded.")
    if entry.get("defaultEnabled") is not True:
        errors.append("marketplace plugin should be defaultEnabled=true.")
    marketplace_version = str(entry.get("version", "")).strip()
    plugin_version = str(plugin_manifest.get("version", "")).strip()
    if not marketplace_version:
        errors.append("marketplace plugin version is required.")
    elif marketplace_version != plugin_version:
        errors.append(f"marketplace plugin version must match Claude plugin version {plugin_version}.")
    extra_component_fields = sorted((COMPONENT_FIELDS - {"skills"}) & set(entry))
    if extra_component_fields:
        errors.append(
            "marketplace plugin entry must not declare non-skill component fields; "
            f"only the curated non-GUI skills list is allowed: {', '.join(extra_component_fields)}."
        )

    skills = entry.get("skills")
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        errors.append("marketplace plugin skills must be an array of strings.")
        return errors

    normalized = [normalize_component_path(item) for item in skills]
    if any(path in {"skills", ""} for path in normalized):
        errors.append("marketplace skills must list individual non-GUI skill directories, not the whole skills/ root.")

    expected = {f"skills/{name}" for name in runtime_skill_names(root) - GUI_ONLY_SKILLS}
    declared = set(normalized)
    missing = sorted(expected - declared)
    extra = sorted(declared - expected)
    if missing:
        errors.append(f"marketplace skills missing non-GUI runtime skills: {', '.join(missing)}.")
    if extra:
        errors.append(f"marketplace skills include unknown or GUI-only entries: {', '.join(extra)}.")

    for path in declared:
        skill_path = root / path / "SKILL.md"
        if not skill_path.is_file():
            errors.append(f"marketplace skill path missing SKILL.md: {path}.")

    return errors


def _validate_plugin_manifest(payload: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    if payload.get("name") != PLUGIN_NAME:
        errors.append(f"Claude plugin manifest name must be {PLUGIN_NAME}.")
    if not str(payload.get("displayName", "")).strip():
        errors.append("Claude plugin manifest displayName is required.")
    if not str(payload.get("description", "")).strip():
        errors.append("Claude plugin manifest description is required.")
    version = str(payload.get("version", "")).strip()
    if not version:
        errors.append("Claude plugin manifest version is required.")
    try:
        codex_manifest = read_json(root / ".codex-plugin" / "plugin.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read Codex plugin version for comparison: {exc}")
    else:
        expected_version = str(codex_manifest.get("version", "")).strip()
        if not expected_version:
            errors.append("Codex plugin manifest version is required for cross-adapter validation.")
        elif version != expected_version:
            errors.append(f"Claude plugin version must match Codex plugin version {expected_version}.")
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
        project_version = str(pyproject.get("project", {}).get("version", "")).strip()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"cannot read pyproject version for comparison: {exc}")
    else:
        if not project_version:
            errors.append("pyproject.toml project.version is required for cross-adapter validation.")
        elif version != project_version:
            errors.append(f"Claude plugin version must match pyproject.toml version {project_version}.")
    declared_components = sorted(field for field in COMPONENT_FIELDS if field in payload)
    if declared_components:
        errors.append(
            "Claude plugin manifest must not declare component fields; "
            f"marketplace.json owns the curated non-GUI components: {', '.join(declared_components)}."
        )
    return errors


def config_validation_errors(
    marketplace_payload: dict[str, Any],
    plugin_manifest_payload: dict[str, Any],
    root: Path | None = None,
) -> list[str]:
    base = repo_root() if root is None else root
    errors = _validate_marketplace(base, marketplace_payload, plugin_manifest_payload)
    errors.extend(_validate_plugin_manifest(plugin_manifest_payload, base))
    return errors

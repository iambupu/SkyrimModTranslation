"""Agent capability manifest helpers.

This module keeps adapter capability checks small and deterministic. It is
repo-local and does not invoke any agent backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from project_paths import is_under, plugin_root


EXAMPLE_CAPABILITIES_PATH = Path("config") / "agent_capabilities.example.json"
SUPPORTED_AGENTS = {"codex", "opencode", "claude-code"}
UNSUPPORTED_AGENTS = {"gemini-cli"}
CODEX_ONLY_LEVEL = "L5"
ALLOWED_HANDOFF_FILES = {
    "codex": "qa/codex_handoff.json",
    "opencode": "qa/agent_handoff.json",
    "claude-code": "qa/agent_handoff.json",
}
OPENCODE_BOOTSTRAP_SCRIPT = "scripts/init_opencode.py"
GUI_ONLY_RUNTIME_SKILLS = {"lextranslator-gui-automation", "xtranslator-gui-automation"}
OPENCODE_REQUIRED_CONFIG_FILES = {
    "opencode.json",
    ".opencode/AGENTS.md",
    ".opencode/agents/skyrim-chs.md",
    ".opencode/commands/skyrim-chs-resume.md",
    ".opencode/commands/skyrim-chs-status.md",
    ".opencode/plugins/skyrim-chs.js",
    ".opencode/skyrim-chs-opencode.json",
}


def opencode_required_config_files(root: Path) -> set[str]:
    skills_root = root / "skills"
    native_skills = {
        child.name
        for child in skills_root.iterdir()
        if child.is_dir()
        and child.name not in GUI_ONLY_RUNTIME_SKILLS
        and (child / "SKILL.md").is_file()
    }
    return OPENCODE_REQUIRED_CONFIG_FILES | {
        f".opencode/skills/{name}/SKILL.md" for name in native_skills
    }


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload


def load_agent_capabilities(path_value: str | Path = EXAMPLE_CAPABILITIES_PATH) -> dict[str, Any]:
    root = plugin_root()
    path = resolve_agent_metadata_path(root, path_value, must_exist=True)
    return read_json(path)


def resolve_agent_metadata_path(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"agent metadata path is outside plugin root: {value}")
    return resolved


def agent_config(config: dict[str, Any], agent: str) -> dict[str, Any]:
    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        return {}
    value = agents.get(agent, {})
    return value if isinstance(value, dict) else {}


def agent_supports_gui(config: dict[str, Any], agent: str) -> bool:
    return bool(agent_config(config, agent).get("supports_gui_automation", False))


def agent_supports_computer_use(config: dict[str, Any], agent: str) -> bool:
    return bool(agent_config(config, agent).get("supports_computer_use", False))


def agent_handoff_target(config: dict[str, Any], agent: str) -> str:
    return str(agent_config(config, agent).get("gui_handoff_target", "")).strip()


def workspace_relative_json_path_errors(
    agent: str,
    field: str,
    value: Any,
    *,
    required_prefix: str,
    allowed_path: str = "",
) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return [f"adapter_manifest {field} is required: {agent}"]
    normalized = text.replace("\\", "/").strip("/")
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return [f"adapter_manifest {field} must be project-relative: {agent}"]
    if normalized != required_prefix and not normalized.startswith(required_prefix + "/"):
        return [f"adapter_manifest {field} must stay under {required_prefix}/: {agent}"]
    if Path(normalized).suffix.lower() != ".json":
        return [f"adapter_manifest {field} must be a JSON file: {agent}"]
    if allowed_path and normalized != allowed_path:
        return [f"adapter_manifest {field} must be {allowed_path}: {agent}"]
    return []


def workspace_relative_path_error(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return f"{field} is required"
    normalized = text.replace("\\", "/").strip("/")
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return f"{field} must be project-relative: {text}"
    if not normalized:
        return f"{field} is required"
    return ""


def config_validation_errors(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    root = plugin_root()
    if not isinstance(config.get("schema_version"), int):
        errors.append("schema_version must be an integer.")

    supported = config.get("supported_agents")
    if not isinstance(supported, list) or sorted(supported) != sorted(SUPPORTED_AGENTS):
        errors.append("supported_agents must be exactly: claude-code, codex, opencode.")

    unsupported = config.get("unsupported_agents")
    if not isinstance(unsupported, list) or "gemini-cli" not in {str(item) for item in unsupported}:
        errors.append("unsupported_agents must include gemini-cli.")

    levels = config.get("capability_levels")
    if not isinstance(levels, dict) or CODEX_ONLY_LEVEL not in levels:
        errors.append("capability_levels must include L5 for Codex-only GUI automation.")

    agents = config.get("agents")
    if not isinstance(agents, dict):
        errors.append("agents must be an object.")
        return errors

    declared_agents = set(str(name) for name in agents)
    if declared_agents != SUPPORTED_AGENTS:
        errors.append("agents must contain exactly: claude-code, codex, opencode.")
    if declared_agents & UNSUPPORTED_AGENTS:
        errors.append("unsupported agents must not have adapter capability entries.")

    for name in sorted(SUPPORTED_AGENTS):
        value = agents.get(name)
        if not isinstance(value, dict):
            errors.append(f"agent entry must be an object: {name}")
            continue
        if "supports_plugin" in value:
            errors.append(f"supports_plugin is ambiguous; use explicit plugin capability fields: {name}")
        if "supports_worker_mode" in value:
            errors.append(f"supports_worker_mode is not an adapter capability; task claiming belongs to subagents: {name}")
        manifest = str(value.get("adapter_manifest", "")).strip()
        manifest_payload: dict[str, Any] = {}
        if not manifest:
            errors.append(f"adapter_manifest is required: {name}")
        else:
            try:
                manifest_path = resolve_agent_metadata_path(root, manifest, must_exist=True)
            except (OSError, ValueError) as exc:
                errors.append(f"adapter_manifest is invalid for {name}: {exc}")
            else:
                if not is_under(manifest_path, root / "agents"):
                    errors.append(f"adapter_manifest must live under agents/: {name}")
                try:
                    manifest_payload = read_json(manifest_path)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"adapter_manifest JSON is invalid for {name}: {exc}")
                else:
                    if manifest_payload.get("agent") != name:
                        errors.append(f"adapter_manifest agent must match capability entry: {name}")
                    if "supports_plugin" in manifest_payload:
                        errors.append(f"adapter_manifest supports_plugin is ambiguous; use explicit plugin capability fields: {name}")
                    if "supports_worker_mode" in manifest_payload:
                        errors.append(
                            f"adapter_manifest supports_worker_mode is not allowed; task claiming belongs to subagents: {name}"
                        )
                    errors.extend(
                        workspace_relative_json_path_errors(
                            name,
                            "handoff_file",
                            manifest_payload.get("handoff_file"),
                            required_prefix="qa",
                            allowed_path=ALLOWED_HANDOFF_FILES[name],
                        )
                    )
                    if name == "opencode":
                        bootstrap = str(manifest_payload.get("bootstrap_script", "")).strip().replace("\\", "/")
                        if bootstrap != OPENCODE_BOOTSTRAP_SCRIPT:
                            errors.append(f"opencode adapter_manifest bootstrap_script must be {OPENCODE_BOOTSTRAP_SCRIPT}.")
                        else:
                            try:
                                resolve_agent_metadata_path(root, bootstrap, must_exist=True)
                            except (OSError, ValueError) as exc:
                                errors.append(f"opencode bootstrap_script is invalid: {exc}")
                        generated = manifest_payload.get("generated_config_files", [])
                        if not isinstance(generated, list) or not all(isinstance(item, str) for item in generated):
                            errors.append("opencode adapter_manifest generated_config_files must be a string array.")
                        else:
                            normalized_generated = {item.replace("\\", "/").strip("/") for item in generated}
                            missing = sorted(opencode_required_config_files(root) - normalized_generated)
                            if missing:
                                errors.append(
                                    "opencode adapter_manifest generated_config_files missing: "
                                    + ", ".join(missing)
                                )
                            for item in generated:
                                item_error = workspace_relative_path_error(
                                    "opencode adapter_manifest generated_config_files item",
                                    item,
                                )
                                if item_error:
                                    errors.append(item_error)
        levels_value = value.get("levels", [])
        if not isinstance(levels_value, list) or not all(isinstance(item, str) for item in levels_value):
            errors.append(f"levels must be a string array: {name}")
            continue
        if name == "codex":
            if value.get("supports_controller_mode") is not True:
                errors.append("codex must support controller mode.")
            if value.get("supports_gui_automation") is not True:
                errors.append("codex must support GUI automation.")
            if value.get("supports_computer_use") is not True:
                errors.append("codex must support Computer Use.")
            if value.get("supports_codex_plugin") is not True:
                errors.append("codex must explicitly support the Codex plugin.")
            if value.get("supports_opencode_local_plugins") is not False:
                errors.append("codex must not support opencode local plugins.")
            if CODEX_ONLY_LEVEL not in levels_value:
                errors.append("codex must include L5.")
        else:
            if value.get("supports_controller_mode") is not True:
                errors.append(f"{name} must support controller mode.")
            if value.get("supports_gui_automation") is not False:
                errors.append(f"{name} must not support GUI automation.")
            if value.get("supports_computer_use") is not False:
                errors.append(f"{name} must not support Computer Use.")
            if value.get("supports_codex_plugin") is not False:
                errors.append(f"{name} must not support the Codex plugin.")
            expected_opencode_plugins = name == "opencode"
            if value.get("supports_opencode_local_plugins") is not expected_opencode_plugins:
                errors.append(f"{name} opencode local plugin support must be {expected_opencode_plugins}.")
            if CODEX_ONLY_LEVEL in levels_value:
                errors.append(f"{name} must not include L5.")
            if str(value.get("gui_handoff_target", "")).strip() != "codex":
                errors.append(f"{name} GUI handoff target must be codex.")
        if name == "claude-code":
            if value.get("supports_claude_plugin_marketplace") is not True:
                errors.append("claude-code must support the Claude Code marketplace.")
            marketplace = str(value.get("plugin_marketplace_manifest", "")).strip()
            if marketplace != ".claude-plugin/marketplace.json":
                errors.append("claude-code plugin_marketplace_manifest must be .claude-plugin/marketplace.json.")
            elif not (root / marketplace).is_file():
                errors.append("claude-code plugin marketplace manifest is missing.")
        else:
            if value.get("supports_claude_plugin_marketplace") is not False:
                errors.append(f"{name} must not support the Claude Code marketplace.")

        if manifest_payload:
            for field in (
                "supports_controller_mode",
                "supports_gui_automation",
                "supports_computer_use",
                "supports_codex_plugin",
                "supports_opencode_local_plugins",
                "supports_claude_plugin_marketplace",
            ):
                if manifest_payload.get(field) != value.get(field):
                    errors.append(f"adapter_manifest {field} must match capability entry: {name}")
    return errors

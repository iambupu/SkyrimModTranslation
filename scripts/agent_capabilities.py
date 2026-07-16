"""Agent capability manifest helpers.

This module keeps adapter capability checks small and deterministic. It is
repo-local and does not invoke any agent backend.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from project_paths import is_under, plugin_root
from file_utils import read_json_object_required as read_json


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
GUI_DESKTOP_CAPABILITY = "gui:desktop"
KNOWN_AGENT_CAPABILITIES = frozenset({GUI_DESKTOP_CAPABILITY})
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


def agent_capability_satisfied(
    config: dict[str, Any],
    agent: str,
    required_capability: str,
) -> bool:
    required = required_capability.strip()
    if not required:
        return True
    value = agent_config(config, agent)
    if required == GUI_DESKTOP_CAPABILITY:
        return (
            value.get("supports_gui_automation") is True
            and value.get("supports_computer_use") is True
            and CODEX_ONLY_LEVEL in value.get("levels", [])
        )
    return False


def action_for_agent(
    action: dict[str, Any],
    config: dict[str, Any],
    agent: str,
) -> dict[str, Any]:
    adapted = dict(action)
    required = str(adapted.get("required_agent_capability", "")).strip()
    if not required:
        return adapted
    satisfied = agent_capability_satisfied(config, agent, required)
    adapted["required_agent_capability"] = required
    adapted["agent_capability_satisfied"] = satisfied
    if satisfied:
        adapted.pop("error_code", None)
        adapted.pop("handoff_target", None)
        return adapted
    adapted["allowed"] = False
    adapted["error_code"] = "agent_capability_missing"
    target = str(agent_config(config, agent).get("gui_handoff_target", "")).strip()
    adapted["handoff_target"] = target or "codex"
    return adapted


def capability_config_fingerprint(config: dict[str, Any]) -> str:
    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()








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


def _validate_opencode_manifest(root: Path, payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    bootstrap = str(payload.get("bootstrap_script", "")).strip().replace("\\", "/")
    if bootstrap != OPENCODE_BOOTSTRAP_SCRIPT:
        errors.append(f"opencode adapter_manifest bootstrap_script must be {OPENCODE_BOOTSTRAP_SCRIPT}.")
    else:
        try:
            resolve_agent_metadata_path(root, bootstrap, must_exist=True)
        except (OSError, ValueError) as exc:
            errors.append(f"opencode bootstrap_script is invalid: {exc}")

    generated = payload.get("generated_config_files", [])
    if not isinstance(generated, list) or not all(isinstance(item, str) for item in generated):
        return errors + ["opencode adapter_manifest generated_config_files must be a string array."]
    normalized_generated = {item.replace("\\", "/").strip("/") for item in generated}
    missing = sorted(opencode_required_config_files(root) - normalized_generated)
    if missing:
        errors.append("opencode adapter_manifest generated_config_files missing: " + ", ".join(missing))
    for item in generated:
        item_error = workspace_relative_path_error("opencode adapter_manifest generated_config_files item", item)
        if item_error:
            errors.append(item_error)
    return errors


def _load_adapter_manifest(root: Path, name: str, value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    manifest = str(value.get("adapter_manifest", "")).strip()
    if not manifest:
        return {}, [f"adapter_manifest is required: {name}"]
    try:
        manifest_path = resolve_agent_metadata_path(root, manifest, must_exist=True)
    except (OSError, ValueError) as exc:
        return {}, [f"adapter_manifest is invalid for {name}: {exc}"]
    if not is_under(manifest_path, root / "agents"):
        errors.append(f"adapter_manifest must live under agents/: {name}")
    try:
        payload = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, errors + [f"adapter_manifest JSON is invalid for {name}: {exc}"]
    if payload.get("agent") != name:
        errors.append(f"adapter_manifest agent must match capability entry: {name}")
    if "supports_plugin" in payload:
        errors.append(f"adapter_manifest supports_plugin is ambiguous; use explicit plugin capability fields: {name}")
    if "supports_worker_mode" in payload:
        errors.append(f"adapter_manifest supports_worker_mode is not allowed; task claiming belongs to subagents: {name}")
    errors.extend(
        workspace_relative_json_path_errors(
            name,
            "handoff_file",
            payload.get("handoff_file"),
            required_prefix="qa",
            allowed_path=ALLOWED_HANDOFF_FILES[name],
        )
    )
    if name == "opencode":
        errors.extend(_validate_opencode_manifest(root, payload))
    return payload, errors


def _validate_runtime_capabilities(name: str, value: dict[str, Any], levels: list[str]) -> list[str]:
    errors: list[str] = []
    if value.get("supports_controller_mode") is not True:
        errors.append(f"{name} must support controller mode.")
    if name == "codex":
        expected = {
            "supports_gui_automation": (True, "codex must support GUI automation."),
            "supports_computer_use": (True, "codex must support Computer Use."),
            "supports_codex_plugin": (True, "codex must explicitly support the Codex plugin."),
            "supports_opencode_local_plugins": (False, "codex must not support opencode local plugins."),
        }
        if CODEX_ONLY_LEVEL not in levels:
            errors.append("codex must include L5.")
    else:
        expected_opencode_plugins = name == "opencode"
        expected = {
            "supports_gui_automation": (False, f"{name} must not support GUI automation."),
            "supports_computer_use": (False, f"{name} must not support Computer Use."),
            "supports_codex_plugin": (False, f"{name} must not support the Codex plugin."),
            "supports_opencode_local_plugins": (
                expected_opencode_plugins,
                f"{name} opencode local plugin support must be {expected_opencode_plugins}.",
            ),
        }
        if CODEX_ONLY_LEVEL in levels:
            errors.append(f"{name} must not include L5.")
        if str(value.get("gui_handoff_target", "")).strip() != "codex":
            errors.append(f"{name} GUI handoff target must be codex.")
    for field, (expected_value, message) in expected.items():
        if value.get(field) is not expected_value:
            errors.append(message)
    return errors


def _validate_marketplace(root: Path, name: str, value: dict[str, Any]) -> list[str]:
    if name != "claude-code":
        if value.get("supports_claude_plugin_marketplace") is not False:
            return [f"{name} must not support the Claude Code marketplace."]
        return []
    errors: list[str] = []
    if value.get("supports_claude_plugin_marketplace") is not True:
        errors.append("claude-code must support the Claude Code marketplace.")
    marketplace = str(value.get("plugin_marketplace_manifest", "")).strip()
    if marketplace != ".claude-plugin/marketplace.json":
        errors.append("claude-code plugin_marketplace_manifest must be .claude-plugin/marketplace.json.")
    elif not (root / marketplace).is_file():
        errors.append("claude-code plugin marketplace manifest is missing.")
    return errors


def _manifest_capability_errors(name: str, value: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    fields = (
        "supports_controller_mode",
        "supports_gui_automation",
        "supports_computer_use",
        "supports_codex_plugin",
        "supports_opencode_local_plugins",
        "supports_claude_plugin_marketplace",
    )
    return [
        f"adapter_manifest {field} must match capability entry: {name}"
        for field in fields
        if manifest.get(field) != value.get(field)
    ]


def _top_level_config_errors(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
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
    return errors


def _agent_entry_errors(root: Path, agents: dict[str, Any], name: str) -> list[str]:
    value = agents.get(name)
    if not isinstance(value, dict):
        return [f"agent entry must be an object: {name}"]
    errors: list[str] = []
    if "supports_plugin" in value:
        errors.append(f"supports_plugin is ambiguous; use explicit plugin capability fields: {name}")
    if "supports_worker_mode" in value:
        errors.append(f"supports_worker_mode is not an adapter capability; task claiming belongs to subagents: {name}")
    manifest, manifest_errors = _load_adapter_manifest(root, name, value)
    errors.extend(manifest_errors)
    agent_levels = value.get("levels", [])
    if not isinstance(agent_levels, list) or not all(isinstance(item, str) for item in agent_levels):
        return errors + [f"levels must be a string array: {name}"]
    errors.extend(_validate_runtime_capabilities(name, value, agent_levels))
    errors.extend(_validate_marketplace(root, name, value))
    if manifest:
        errors.extend(_manifest_capability_errors(name, value, manifest))
    return errors


def config_validation_errors(config: dict[str, Any]) -> list[str]:
    errors = _top_level_config_errors(config)
    root = plugin_root()

    agents = config.get("agents")
    if not isinstance(agents, dict):
        return errors + ["agents must be an object."]
    declared_agents = {str(name) for name in agents}
    if declared_agents != SUPPORTED_AGENTS:
        errors.append("agents must contain exactly: claude-code, codex, opencode.")
    if declared_agents & UNSUPPORTED_AGENTS:
        errors.append("unsupported agents must not have adapter capability entries.")

    for name in sorted(SUPPORTED_AGENTS):
        errors.extend(_agent_entry_errors(root, agents, name))
    return errors

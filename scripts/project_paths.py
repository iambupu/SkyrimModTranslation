"""Shared path and output-layout helpers for the Skyrim translation pipeline.

This module is deliberately small and dependency-light because most workflow
scripts import it before touching user input. Keep the project-root checks here
strict: callers should fail early instead of normalizing unsafe paths later.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from game_context import (
    GameContext,
    load_game_profile,
    plugin_root as game_plugin_root,
    supported_game_ids,
)


RISKY_PATH_MARKERS = [
    "SteamLibrary",
    "steamapps",
    r"Skyrim Special Edition\Data",
    "Skyrim Special Edition/Data",
    "Skyrim Special Edition\\Data",
    "ModOrganizer",
    "Vortex",
    "AppData",
    r"Documents\My Games",
    "Documents/My Games",
]

# These names define the public output contract documented in docs/final_mod_output.md.
# Changing them requires updating the validators, README, Skills, and handoff reports.
LOCALIZATION_OUTPUT_DIR = "汉化产出"
FINAL_MOD_DIR_NAME = "final_mod"
INTERMEDIATE_OUTPUT_DIR_NAME = "intermediate"
PACKAGE_SUFFIX = "CHS"
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WINDOWS_RESERVED_FILE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def source_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def plugin_root() -> Path:
    return game_plugin_root()


def _known_game_contexts() -> tuple[GameContext, ...]:
    return tuple(load_game_profile(game_id) for game_id in supported_game_ids())


def _resource_contexts(context: GameContext | None) -> tuple[GameContext, ...]:
    return (context,) if context is not None else _known_game_contexts()


def _plugin_extensions(context: GameContext | None) -> set[str]:
    return {
        extension
        for active in _resource_contexts(context)
        for group in active.resource_model.extension_groups
        if group.category == "plugin"
        for extension in group.extensions
    }


def _data_directories(context: GameContext | None) -> set[str]:
    return {
        directory
        for active in _resource_contexts(context)
        for directory in active.resource_model.containers
    }


def _risky_path_markers(context: GameContext | None) -> list[str]:
    if context is None:
        return RISKY_PATH_MARKERS
    return list(context.risky_paths)


def find_workspace_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).expanduser().resolve(strict=False)
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / WORKSPACE_MARKER).is_file():
            return candidate
    return None


def project_root() -> Path:
    configured = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    workspace = find_workspace_root()
    if workspace is not None:
        return workspace
    return plugin_root()


def plugin_script_path(script: str | Path) -> Path:
    value = Path(script)
    if value.is_absolute():
        return value.resolve(strict=False)
    parts = value.parts
    if parts and parts[0] == "scripts":
        return (plugin_root() / value).resolve(strict=False)
    return (plugin_root() / "scripts" / value).resolve(strict=False)


def quote_command_arg(value: str | Path) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def python_executable_command() -> str:
    root = project_root()
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    workspace_python = root / "tools" / "python-venv" / scripts_dir / executable
    if workspace_python.is_file():
        return quote_command_arg(workspace_python)
    return "python"


def python_script_command(script: str | Path, *args: str) -> str:
    script_text = quote_command_arg(plugin_script_path(script))
    tail = " ".join(str(arg) for arg in args if str(arg).strip())
    return f"{python_executable_command()} {script_text}{(' ' + tail) if tail else ''}"


def normalize_python_script_command(command: str) -> str:
    text = command.strip()
    prefixes = ("python scripts/", "python ./scripts/", "python .\\scripts\\")
    for prefix in prefixes:
        if not text.lower().startswith(prefix):
            continue
        script_and_args = text[len(prefix):].strip()
        if not script_and_args:
            return text
        script_name, _, rest = script_and_args.partition(" ")
        script_name = script_name.replace("\\", "/")
        normalized_script = f"scripts/{script_name}"
        return python_script_command(normalized_script, rest)
    return text


def resolve_workspace_or_plugin_path(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=must_exist)
        if is_under(resolved, root) or is_under(resolved, plugin_root()):
            return resolved
        raise ValueError(f"path is outside workspace and plugin root: {value}")

    workspace_candidate = (root / candidate).resolve(strict=False)
    if workspace_candidate.exists() or not (plugin_root() / candidate).exists():
        if must_exist:
            workspace_candidate = (root / candidate).resolve(strict=True)
        if is_under(workspace_candidate, root):
            return workspace_candidate

    plugin_candidate = (plugin_root() / candidate).resolve(strict=must_exist)
    if is_under(plugin_candidate, plugin_root()):
        return plugin_candidate
    raise ValueError(f"path is outside workspace and plugin root: {value}")


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    child_key = os.path.normcase(os.path.normpath(str(child_resolved)))
    parent_key = os.path.normcase(os.path.normpath(str(parent_resolved)))
    try:
        common = os.path.commonpath([child_key, parent_key])
    except ValueError:
        return False
    return os.path.normcase(os.path.normpath(common)) == parent_key


def resolve_project_path(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    # Resolve first, then compare normalized absolute paths. This blocks both
    # absolute external paths and relative traversal such as ..\real-game-dir.
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root: {value}")
    return resolved


def ensure_inside_or_exit(child: Path, parent: Path) -> None:
    child_resolved = child.resolve()
    parent_resolved = parent.resolve()
    if child_resolved != parent_resolved and parent_resolved not in child_resolved.parents:
        raise SystemExit(f"unsafe path outside project: {child_resolved}")


def require_under_any(path: Path, allowed_roots: list[Path], label: str) -> None:
    if not any(is_under(path, allowed) for allowed in allowed_roots):
        allowed_text = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(f"{label} must be under one of: {allowed_text}")


def relative_path(root: Path, value: Path) -> str:
    try:
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def relative_posix_path(root: Path, value: Path) -> str:
    return relative_path(root, value).replace("\\", "/")


def relative_posix_strict(root: Path, value: Path) -> str:
    resolved_root = root.resolve(strict=True)
    resolved_value = value.resolve(strict=False)
    return str(resolved_value.relative_to(resolved_root)).replace("\\", "/")


def relative_windows_path(root: Path, value: Path) -> str:
    return relative_path(root, value).replace("/", "\\")


def is_interface_translation_path(path: Path) -> bool:
    parts = [part.casefold() for part in path.parts]
    return (
        path.suffix.casefold() == ".txt"
        and len(parts) >= 3
        and parts[-3:-1] == ["interface", "translations"]
    )


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in str(value))
    cleaned = cleaned.strip().rstrip(" .")
    if cleaned in {"", ".", ".."}:
        raise ValueError("file name cannot be empty or a relative path segment after sanitization")
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_FILE_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def mod_output_root(root: Path, mod_name: str) -> Path:
    return root / "out" / safe_file_name(mod_name)


def localization_output_root(root: Path, mod_name: str) -> Path:
    return mod_output_root(root, mod_name) / LOCALIZATION_OUTPUT_DIR


def final_mod_dir(root: Path, mod_name: str) -> Path:
    return localization_output_root(root, mod_name) / FINAL_MOD_DIR_NAME


def intermediate_output_dir(root: Path, mod_name: str) -> Path:
    return localization_output_root(root, mod_name) / INTERMEDIATE_OUTPUT_DIR_NAME


def packaged_mod_path(root: Path, mod_name: str) -> Path:
    safe_mod = safe_file_name(mod_name)
    return localization_output_root(root, safe_mod) / f"{safe_mod}_{PACKAGE_SUFFIX}.zip"


def has_data_root_markers(path: Path, context: GameContext | None = None) -> bool:
    if not path.is_dir():
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    plugin_extensions = _plugin_extensions(context)
    if any(child.is_file() and child.suffix.lower() in plugin_extensions for child in children):
        return True
    data_directories = _data_directories(context)
    return any(child.is_dir() and child.name.lower() in data_directories for child in children)


def find_data_root(path: Path, context: GameContext | None = None) -> Path:
    """Return a likely Bethesda Data root, peeling only simple wrapper directories."""
    if has_data_root_markers(path, context=context):
        return path
    data_dir = path / "Data"
    if has_data_root_markers(data_dir, context=context):
        return data_dir
    current = path
    seen: set[Path] = set()
    while current.is_dir():
        resolved = current.resolve(strict=False)
        if resolved in seen:
            break
        seen.add(resolved)
        if has_data_root_markers(current, context=context):
            return current
        explicit_data = current / "Data"
        if has_data_root_markers(explicit_data, context=context):
            return explicit_data
        try:
            children = list(current.iterdir())
        except OSError:
            break
        directory_children = [child for child in children if child.is_dir()]
        file_children = [child for child in children if child.is_file()]
        # Only peel one-directory wrappers. As soon as files or multiple
        # directories appear, keep the current directory to avoid guessing a
        # nested Data root and silently dropping sibling assets.
        if len(directory_children) != 1 or file_children:
            break
        current = directory_children[0]
    return path


def risky_marker(value: str | Path, context: GameContext | None = None) -> str:
    text = str(value)
    for marker in _risky_path_markers(context):
        if re.search(re.escape(marker), text, re.IGNORECASE):
            return marker
    return ""


def assert_no_risky_marker(value: str | Path) -> None:
    # This is a second-line guard for tool configuration and reports. It does
    # not replace project-root validation, but catches common real game paths in
    # external tool settings before an adapter can launch.
    marker = risky_marker(value)
    if marker:
        raise ValueError(f"path contains forbidden game/mod-manager marker '{marker}': {value}")


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def configured_path(root: Path, value: Any) -> Path | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        plugin_candidate = plugin_root() / candidate
        if candidate.parts and candidate.parts[0] == "scripts" and plugin_candidate.exists():
            candidate = plugin_candidate
        else:
            candidate = root / candidate
    return candidate.resolve(strict=False)


def bool_config(config: dict[str, Any], key: str, default: bool) -> bool:
    if key not in config:
        return default
    value = config[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
    return bool(value)


def append_tool_log(
    root: Path,
    *,
    tool: str,
    input_path: Path,
    mode: str,
    status: str,
    next_action: str,
    log_path: Path | None = None,
) -> None:
    log_path = log_path or root / "qa" / "tool_invocation_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text("# Tool Invocation Log\n", encoding="utf-8")
    lines = [
        "",
        f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- Tool: {tool}",
        f"- Input: {input_path}",
        f"- Mode: {mode}",
        f"- Status: {status}",
        f"- Next action: {next_action}",
    ]
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")

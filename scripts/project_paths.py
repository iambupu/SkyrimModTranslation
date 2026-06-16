import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}
COMMON_DATA_DIRS = {"interface", "scripts", "skse", "meshes", "textures", "sound", "seq", "mcm"}
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
LOCALIZATION_OUTPUT_DIR = "汉化产出"
FINAL_MOD_DIR_NAME = "final_mod"
INTERMEDIATE_OUTPUT_DIR_NAME = "intermediate"
PACKAGE_SUFFIX = "CHS"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def resolve_project_path(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root: {value}")
    return resolved


def relative_path(root: Path, value: Path) -> str:
    try:
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    return "".join("_" if char in invalid or ord(char) < 32 else char for char in value).strip()


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


def has_data_root_markers(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    if any(child.is_file() and child.suffix.lower() in PLUGIN_EXTENSIONS for child in children):
        return True
    return any(child.is_dir() and child.name.lower() in COMMON_DATA_DIRS for child in children)


def find_data_root(path: Path) -> Path:
    """Return the likely Skyrim Data root, peeling only simple wrapper directories."""
    if has_data_root_markers(path):
        return path
    data_dir = path / "Data"
    if has_data_root_markers(data_dir):
        return data_dir
    current = path
    seen: set[Path] = set()
    while current.is_dir():
        resolved = current.resolve(strict=False)
        if resolved in seen:
            break
        seen.add(resolved)
        if has_data_root_markers(current):
            return current
        explicit_data = current / "Data"
        if has_data_root_markers(explicit_data):
            return explicit_data
        try:
            children = list(current.iterdir())
        except OSError:
            break
        directory_children = [child for child in children if child.is_dir()]
        file_children = [child for child in children if child.is_file()]
        if len(directory_children) != 1 or file_children:
            break
        current = directory_children[0]
    return path


def risky_marker(value: str | Path) -> str:
    text = str(value)
    for marker in RISKY_PATH_MARKERS:
        if re.search(re.escape(marker), text, re.IGNORECASE):
            return marker
    return ""


def assert_no_risky_marker(value: str | Path) -> None:
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


def append_tool_log(root: Path, *, tool: str, input_path: Path, mode: str, status: str, next_action: str) -> None:
    log_path = root / "qa" / "tool_invocation_log.md"
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

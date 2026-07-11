from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


PLUGIN_NAME = "skyrim-mod-chs-translation"
DEFAULT_MARKETPLACE_NAME = "personal"
COPY_ROOT_ITEMS = [
    ".codex-plugin",
    ".codex",
    "adapters",
    "config",
    "docs",
    "glossary",
    "scripts",
    "skills",
    "AGENTS.md",
    "developer_guide.md",
    "LICENSE",
    "logo.png",
    "pyproject.toml",
    "README.md",
    "requirements.txt",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def personal_marketplace_path() -> Path:
    return Path.home() / ".agents" / "plugins" / "marketplace.json"


def plugin_install_path(marketplace_path: Path) -> Path:
    return marketplace_path.parent / PLUGIN_NAME


def marketplace_root(marketplace_path: Path) -> Path:
    if (
        marketplace_path.name == "marketplace.json"
        and marketplace_path.parent.name == "plugins"
        and marketplace_path.parent.parent.name == ".agents"
    ):
        return marketplace_path.parent.parent.parent
    return marketplace_path.parent


def marketplace_source_path(target_root: Path, marketplace_path: Path) -> str:
    root = marketplace_root(marketplace_path).resolve(strict=False)
    target = target_root.resolve(strict=False)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"plugin install path must be under marketplace root: {target}") from exc
    return f"./{relative.as_posix()}"


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        child_resolved.relative_to(parent_resolved)
        return True
    except ValueError:
        return False


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "name": DEFAULT_MARKETPLACE_NAME,
            "interface": {"displayName": "Personal"},
            "plugins": [],
        }
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    data.setdefault("name", DEFAULT_MARKETPLACE_NAME)
    data.setdefault("interface", {"displayName": "Personal"})
    data.setdefault("plugins", [])
    if not isinstance(data["plugins"], list):
        raise ValueError(f"{path} plugins must be a JSON array.")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def validate_source(root: Path) -> None:
    manifest_path = root / ".codex-plugin" / "plugin.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing plugin manifest: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("name") != PLUGIN_NAME:
        raise ValueError(
            f"plugin.json name must be {PLUGIN_NAME!r}, got {manifest.get('name')!r}."
        )
    skills_path = root / "skills"
    if not skills_path.exists():
        raise FileNotFoundError(f"Missing plugin skills directory: {skills_path}")


def remove_existing_install(target: Path) -> None:
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target)
        return
    target.unlink()


def copy_plugin_items(source_root: Path, target_root: Path) -> None:
    for item_name in COPY_ROOT_ITEMS:
        source = source_root / item_name
        target = target_root / item_name
        if not source.exists():
            continue
        if source.is_dir():
            ignore_patterns = [
                "__pycache__",
                "*.pyc",
                "bin",
                "obj",
            ]
            if item_name == "config":
                ignore_patterns.append("tools.local.json")
            ignore = shutil.ignore_patterns(*ignore_patterns)
            shutil.copytree(source, target, ignore=ignore)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def replace_install_preserving_old(staged_root: Path, target_root: Path, backup_root: Path) -> None:
    remove_existing_install(backup_root)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    moved_old = False
    if target_root.exists():
        shutil.move(str(target_root), str(backup_root))
        moved_old = True
    try:
        shutil.move(str(staged_root), str(target_root))
    except Exception:
        if moved_old and backup_root.exists() and not target_root.exists():
            shutil.move(str(backup_root), str(target_root))
        raise
    remove_existing_install(backup_root)


def copy_plugin_source(source_root: Path, target_root: Path) -> None:
    if source_root.resolve() == target_root.resolve():
        print(f"Plugin source is already in the install directory: {target_root}")
        return
    if is_under(target_root, source_root):
        raise ValueError(f"refusing to install plugin copy inside the source repository: {target_root}")

    staging_root = target_root.parent / f".{target_root.name}.staged"
    backup_root = target_root.parent / f".{target_root.name}.previous"
    remove_existing_install(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)
    try:
        copy_plugin_items(source_root, staging_root)
        replace_install_preserving_old(staging_root, target_root, backup_root)
    except Exception:
        remove_existing_install(staging_root)
        raise


def ensure_marketplace_entry(marketplace_path: Path, source_path: str) -> str:
    data = load_json(marketplace_path)
    plugins = data["plugins"]
    entry = {
        "name": PLUGIN_NAME,
        "source": {
            "source": "local",
            "path": source_path,
        },
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Productivity",
    }

    replaced = False
    for index, existing in enumerate(plugins):
        if isinstance(existing, dict) and existing.get("name") == PLUGIN_NAME:
            plugins[index] = entry
            replaced = True
            break
    if not replaced:
        plugins.append(entry)

    write_json(marketplace_path, data)
    return str(data["name"])


def run_codex_install(marketplace_name: str) -> int:
    command = ["codex", "plugin", "add", f"{PLUGIN_NAME}@{marketplace_name}"]
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError:
        print("Codex CLI was not found. The local marketplace entry was prepared.")
        print(f"Open Codex and install {PLUGIN_NAME} from marketplace {marketplace_name}.")
        return 127
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install this repository as a local Codex plugin."
    )
    parser.add_argument(
        "--marketplace-path",
        type=Path,
        default=personal_marketplace_path(),
        help="Path to the Codex marketplace.json file.",
    )
    parser.add_argument(
        "--skip-codex-add",
        action="store_true",
        help="Prepare the local marketplace entry without invoking Codex CLI.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = repo_root()
    marketplace_path = args.marketplace_path.expanduser().resolve()
    target_root = plugin_install_path(marketplace_path)

    validate_source(source_root)
    copy_plugin_source(source_root, target_root)
    marketplace_name = ensure_marketplace_entry(
        marketplace_path,
        marketplace_source_path(target_root, marketplace_path),
    )

    print(f"Installed plugin source: {target_root}")
    print(f"Updated marketplace: {marketplace_path}")

    if args.skip_codex_add:
        print(f"Prepared {PLUGIN_NAME}@{marketplace_name}.")
        return 0

    result = run_codex_install(marketplace_name)
    if result == 0:
        print(f"Codex plugin installed: {PLUGIN_NAME}@{marketplace_name}")
    else:
        print(f"Codex plugin add exited with code {result}.")
    return result


if __name__ == "__main__":
    raise SystemExit(main())

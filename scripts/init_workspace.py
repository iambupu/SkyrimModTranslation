#!/usr/bin/env python3
"""Initialize a clean Skyrim CHS translation workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
WORKSPACE_SCHEMA_VERSION = 1
RUNTIME_DIRS = ("mod", "source", "translated", "qa", "out", "work")
WORKSPACE_ONLY_DIRS = ("config", "glossary", *RUNTIME_DIRS)
WORKSPACE_SEED_DIRS = ("glossary",)
PLUGIN_NAME = "skyrim-mod-chs-translation"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize a clean Skyrim Mod CHS translation workspace."
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="Target workspace directory. Defaults to the current directory.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Deprecated compatibility flag. Initialization still requires an empty target directory.",
    )
    parser.add_argument(
        "--skip-initial-state",
        action="store_true",
        help="Create workspace files only; do not write initial qa/readiness and workflow reports.",
    )
    parser.add_argument(
        "--tool-setup",
        choices=("ask", "auto", "manual", "skip"),
        default="ask",
        help=(
            "Tool setup mode after workspace creation. "
            "auto installs safe non-GUI tools and writes tool reports; "
            "manual writes reports/checklists only; skip does nothing. "
            "ask prompts in an interactive terminal and defaults to manual otherwise."
        ),
    )
    return parser.parse_args()


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def ensure_empty_target(workspace: Path) -> None:
    if workspace == PROJECT_ROOT:
        raise SystemExit(
            "Refusing to initialize the plugin repository as a workspace. "
            "Choose a new empty workspace directory."
        )
    if is_under(workspace, PROJECT_ROOT):
        raise SystemExit(
            "Refusing to initialize a workspace inside the plugin repository. "
            "Choose a new empty directory outside the plugin source tree."
        )
    if workspace.exists() and not workspace.is_dir():
        raise SystemExit(f"Workspace target exists and is not a directory: {workspace}")
    if workspace.exists() and any(workspace.iterdir()):
        raise SystemExit(
            "Workspace target must be an empty directory or a non-existent path: "
            f"{workspace}"
        )


def ensure_runtime_dirs(workspace: Path) -> None:
    for name in WORKSPACE_ONLY_DIRS:
        directory = workspace / name
        directory.mkdir(parents=True, exist_ok=True)
        if name in RUNTIME_DIRS:
            gitkeep = directory / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.write_text("", encoding="utf-8")
    (workspace / "work" / "locks").mkdir(parents=True, exist_ok=True)


def copy_workspace_seed_dirs(workspace: Path) -> list[str]:
    copied: list[str] = []
    for name in WORKSPACE_SEED_DIRS:
        source_dir = PROJECT_ROOT / name
        target_dir = workspace / name
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.rglob("*")):
            relative = source.relative_to(source_dir)
            target = target_dir / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(f"{name}/{relative.as_posix()}")
    return copied


def write_tools_local(workspace: Path) -> bool:
    target = workspace / "config" / "tools.local.json"
    if target.exists():
        return False
    example = PROJECT_ROOT / "config" / "tools.example.json"
    if example.is_file():
        shutil.copy2(example, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
    return True


def write_marker(workspace: Path) -> None:
    payload = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "kind": "skyrim-mod-chs-translation-workspace",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plugin_name": PLUGIN_NAME,
        "plugin_root": str(PROJECT_ROOT),
        "runtime_dirs": list(RUNTIME_DIRS),
        "state_files": {
            "readiness": "qa/translation_readiness.json",
            "workflow_state": "qa/workflow_state.json",
            "workflow_tasks": "qa/workflow_tasks.json",
            "codex_handoff": "qa/codex_handoff.json",
        },
        "tool_config": "config/tools.local.json",
        "mod_input_root": "mod",
        "output_root": "out",
        "user_editable_roots": [
            "glossary",
            "config/tools.local.json",
            "mod",
        ],
        "glossary_root": "glossary",
        "mod_terms": "glossary/mod_terms.md",
        "additional_glossary_roots": [
            "glossary/lextranslator_dynamic_dictionaries",
        ],
        "workspace_boundary": {
            "contains_plugin_manifest": False,
            "contains_runtime_skills": False,
            "contains_workflow_scripts": False,
            "contains_user_glossary_seed": True,
        },
    }
    (workspace / WORKSPACE_MARKER).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def workspace_python(workspace: Path) -> Path:
    scripts_dir = "Scripts" if sys.platform.startswith("win") else "bin"
    executable = "python.exe" if sys.platform.startswith("win") else "python"
    candidate = workspace / "tools" / "python-venv" / scripts_dir / executable
    return candidate if candidate.is_file() else Path(sys.executable)


def run_initial_state(workspace: Path) -> list[str]:
    python_executable = workspace_python(workspace)
    commands = [
        ([str(python_executable), str(PROJECT_ROOT / "scripts" / "audit_translation_readiness.py")], False),
        ([str(python_executable), str(PROJECT_ROOT / "scripts" / "write_workflow_state.py")], False),
        ([str(python_executable), str(PROJECT_ROOT / "scripts" / "write_workflow_tasks.py")], False),
        ([str(python_executable), str(PROJECT_ROOT / "scripts" / "test_workflow_health.py")], True),
        ([str(python_executable), str(PROJECT_ROOT / "scripts" / "write_codex_handoff.py")], False),
    ]
    output: list[str] = []
    env = {
        **__import__("os").environ,
        WORKSPACE_ROOT_ENV: str(workspace),
        PLUGIN_ROOT_ENV: str(PROJECT_ROOT),
    }
    for command, allow_nonzero in commands:
        result = subprocess.run(
            command,
            cwd=workspace,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output.append(f"$ {' '.join(command)}")
        if result.stdout.strip():
            output.append(result.stdout.strip())
        if result.returncode != 0 and allow_nonzero:
            output.append(f"Non-zero exit code recorded: {result.returncode}")
        elif result.returncode != 0:
            raise RuntimeError(
                f"Initial state command failed with exit code {result.returncode}: {' '.join(command)}"
            )
    return output


def resolve_tool_setup_mode(requested: str) -> str:
    if requested != "ask":
        return requested
    if not sys.stdin.isatty():
        return "manual"

    print("Tool setup options:")
    print("  1) auto   - install safe non-GUI tools and write tool detection reports")
    print("  2) manual - write tool detection reports and let me fill external tool paths")
    print("  3) skip   - do not run tool setup now")
    try:
        choice = input("Select tool setup mode [manual]: ").strip().lower()
    except EOFError:
        return "manual"
    if choice in {"1", "a", "auto"}:
        return "auto"
    if choice in {"3", "s", "skip"}:
        return "skip"
    return "manual"


def run_tool_setup(workspace: Path, mode: str) -> tuple[list[str], int]:
    if mode == "skip":
        return ["Tool setup skipped."], 0
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "setup_workspace_tools.py"),
        "--mode",
        mode,
    ]
    env = {
        **__import__("os").environ,
        WORKSPACE_ROOT_ENV: str(workspace),
        PLUGIN_ROOT_ENV: str(PROJECT_ROOT),
    }
    result = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = [f"$ {' '.join(command)}"]
    if result.stdout.strip():
        output.append(result.stdout.strip())
    if result.returncode != 0:
        output.append(f"Tool setup reported blocking issues: {result.returncode}")
    return output, result.returncode


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    ensure_empty_target(workspace)

    workspace.mkdir(parents=True, exist_ok=True)
    ensure_runtime_dirs(workspace)
    copied_seed_files = copy_workspace_seed_dirs(workspace)
    tools_created = write_tools_local(workspace)
    write_marker(workspace)

    print(f"Workspace initialized: {workspace}")
    print(f"Workspace files created: {', '.join(WORKSPACE_ONLY_DIRS)}, {WORKSPACE_MARKER}")
    print("Plugin source files copied: no")
    print(f"Workspace glossary seed files copied: {len(copied_seed_files)}")
    print(f"Runtime directories: {', '.join(RUNTIME_DIRS)}")
    print(f"Workspace marker: {WORKSPACE_MARKER}")
    print(f"Tools config created: {'yes' if tools_created else 'already present'}")

    tool_setup_mode = resolve_tool_setup_mode(args.tool_setup)
    print(f"Tool setup mode: {tool_setup_mode}")
    tool_setup_output, tool_setup_returncode = run_tool_setup(workspace, tool_setup_mode)
    for line in tool_setup_output:
        print(line)

    if not args.skip_initial_state:
        print("Initial state refresh:")
        for line in run_initial_state(workspace):
            print(line)
    return tool_setup_returncode


if __name__ == "__main__":
    raise SystemExit(main())

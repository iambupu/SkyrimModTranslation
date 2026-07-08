#!/usr/bin/env python3
"""Initialize and optionally launch opencode for a Skyrim CHS workspace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"
LATEST_CONTEXT_PATH = "qa/agent_context_prompts/latest.opencode.context.md"
OPENCODE_AGENT_NAME = "skyrim-chs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create opencode adapter config for a Skyrim CHS workspace and optionally launch it."
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="Initialized workspace path, or an empty/non-existent directory to initialize first.",
    )
    parser.add_argument(
        "--tool-setup",
        choices=("auto", "manual", "skip"),
        default="manual",
        help="Tool setup mode when the workspace must be created first.",
    )
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Only write opencode config; do not refresh readiness/state/tasks/handoff/context.",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Write config and context only; do not start opencode.",
    )
    parser.add_argument(
        "--launch-mode",
        choices=("tui", "run"),
        default="tui",
        help="Start the interactive TUI or a non-interactive opencode run after initialization.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Pass --auto to opencode so non-denied permissions can run without prompts.",
    )
    parser.add_argument(
        "--opencode-command",
        default="opencode",
        help="opencode executable name or absolute path.",
    )
    parser.add_argument(
        "--prompt",
        default=(
            "读取 .opencode/AGENTS.md 和 qa/agent_context_prompts/latest.opencode.context.md，"
            "然后按 Skyrim CHS workflow 状态推进允许的非 GUI 下一步。"
        ),
        help="Prompt passed to opencode when launching.",
    )
    return parser.parse_args()


def is_under(path: Path, parent: Path) -> bool:
    path_resolved = path.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(path_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def marker_path(workspace: Path) -> Path:
    return workspace / WORKSPACE_MARKER


def directory_is_empty(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def run_checked(command: list[str], *, cwd: Path, env: dict[str, str], allow_nonzero: bool = False) -> int:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(f"$ {' '.join(command)}")
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0 and not allow_nonzero:
        raise RuntimeError(f"command failed with exit code {result.returncode}: {' '.join(command)}")
    return result.returncode


def ensure_workspace(workspace: Path, *, tool_setup: str) -> Path:
    resolved = workspace.expanduser().resolve(strict=False)
    if resolved == PROJECT_ROOT or is_under(resolved, PROJECT_ROOT):
        raise RuntimeError("refusing to initialize opencode inside the plugin source repository")
    if marker_path(resolved).is_file():
        return resolved
    if resolved.exists() and not resolved.is_dir():
        raise RuntimeError(f"workspace target exists and is not a directory: {resolved}")
    if resolved.exists() and not directory_is_empty(resolved):
        raise RuntimeError(
            "workspace is not initialized and is not empty. Run scripts\\init_workspace.py first: "
            f"{resolved}"
        )

    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "init_workspace.py"),
        str(resolved),
        "--tool-setup",
        tool_setup,
    ]
    env = workspace_env(resolved)
    run_checked(command, cwd=PROJECT_ROOT, env=env)
    if not marker_path(resolved).is_file():
        raise RuntimeError(f"workspace initialization did not create {WORKSPACE_MARKER}: {resolved}")
    return resolved


def workspace_env(workspace: Path) -> dict[str, str]:
    return {
        **os.environ,
        PLUGIN_ROOT_ENV: str(PROJECT_ROOT),
        WORKSPACE_ROOT_ENV: str(workspace),
        "OPENCODE_CONFIG_DIR": str(workspace / ".opencode"),
    }


def opencode_json() -> str:
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": OPENCODE_AGENT_NAME,
        "instructions": [
            ".opencode/AGENTS.md",
            LATEST_CONTEXT_PATH,
        ],
        "watcher": {
            "ignore": [
                ".git/**",
                ".workflow/**",
                "out/**",
                "source/**",
                "translated/**",
                "traces/**",
                "tools/**",
                "work/**",
                "qa/agent_context_prompts/**",
            ]
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def opencode_rules(workspace: Path) -> str:
    return f"""# Skyrim CHS opencode Rules

This workspace is controlled by the Skyrim CHS workflow core.

- Plugin root: `{PROJECT_ROOT}`
- Workspace root: `{workspace}`
- First read: `{LATEST_CONTEXT_PATH}`, then `qa/agent_handoff.json`, `qa/workflow_state.json`, and `qa/workflow_tasks.json`.
- Use plugin-source Python scripts through `{PROJECT_ROOT / "scripts"}`. Do not create workspace-local copies of scripts or runtime Skills.
- opencode is a full non-GUI top-level adapter. It can run non-GUI workflow Python entrypoints, update text/report artifacts inside the workspace, and coordinate controller-spawned subagents through the documented project protocol.
- opencode itself must not directly claim `qa/workflow_tasks.json` tasks. Task claiming belongs only to controller-spawned subagents.
- Do not access real Skyrim, MO2, Vortex, Steam, AppData, or `Documents/My Games` paths.
- Do not modify `.esp`, `.esm`, `.esl`, `.bsa`, `.ba2`, `.pex`, `.dll`, `.exe`, or other binary files.
- GUI, Computer Use, pywinauto, UI Automation, LexTranslator/xTranslator desktop automation, and `gui:desktop` locks are Codex-only.
- If the next required step is GUI-only, mark the workflow blocked, record `handoff_target=codex`, and stop.
- Commands run in Windows. Prefer project Python entrypoints; do not introduce Bash, WSL, or Unix-style wrapper commands.
"""


def opencode_agent_markdown(workspace: Path) -> str:
    return f"""---
description: Skyrim CHS non-GUI workflow controller
mode: primary
permission:
  read: allow
  grep: allow
  glob: allow
  edit: allow
  bash: ask
  webfetch: ask
  websearch: ask
  task: ask
  todowrite: allow
  skill: allow
---

# Skyrim CHS opencode Controller

You are the non-GUI top-level adapter for this Skyrim CHS workspace.

Before taking action, read `.opencode/AGENTS.md` and `{LATEST_CONTEXT_PATH}`. If the context packet is stale or missing, ask the user to rerun:

```powershell
python "{PROJECT_ROOT / "scripts" / "init_opencode.py"}" "{workspace}" --no-launch
```

Use the shared workflow core:

- `qa/agent_handoff.json`
- `qa/workflow_state.json`
- `qa/workflow_tasks.json`
- `config/workflow_policy.json` from the plugin source
- root runtime Skills from `{PROJECT_ROOT / "skills"}`

You may run allowed non-GUI Python workflow entrypoints from the plugin source against this workspace. Do not use GUI automation, real game paths, or direct binary edits.
"""


def command_resume_markdown() -> str:
    return f"""---
description: Resume the Skyrim CHS non-GUI workflow from the exported handoff
agent: {OPENCODE_AGENT_NAME}
subtask: false
---

Read `.opencode/AGENTS.md` and `{LATEST_CONTEXT_PATH}`. Then read the referenced workflow state and QA files, choose only an allowed non-GUI next action, and run it through the plugin-source Python entrypoints. If the next action requires GUI-only capability, record blocked with `handoff_target=codex`.
"""


def command_status_markdown() -> str:
    return f"""---
description: Summarize current Skyrim CHS workflow state from handoff and progress card
agent: {OPENCODE_AGENT_NAME}
subtask: false
---

Read `{LATEST_CONTEXT_PATH}`, `.workflow/progress_card.md`, `qa/agent_handoff.json`, and `qa/workflow_state.json`. Summarize the current state, blockers, and the safest next non-GUI action. Do not rescan the entire workspace unless the handoff says the context is stale.
"""


def write_text(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == content:
        return False
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def write_opencode_config(workspace: Path) -> list[str]:
    changed: list[str] = []
    files = {
        workspace / "opencode.json": opencode_json(),
        workspace / ".opencode" / "AGENTS.md": opencode_rules(workspace),
        workspace / ".opencode" / "agents" / f"{OPENCODE_AGENT_NAME}.md": opencode_agent_markdown(workspace),
        workspace / ".opencode" / "commands" / "skyrim-chs-resume.md": command_resume_markdown(),
        workspace / ".opencode" / "commands" / "skyrim-chs-status.md": command_status_markdown(),
    }
    for path, content in files.items():
        if write_text(path, content):
            changed.append(str(path))

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "adapter": "opencode",
        "plugin_root": str(PROJECT_ROOT),
        "workspace_root": str(workspace),
        "agent": OPENCODE_AGENT_NAME,
        "context_packet": LATEST_CONTEXT_PATH,
        "non_gui_only": True,
        "gui_handoff_target": "codex",
    }
    manifest_path = workspace / ".opencode" / "skyrim-chs-opencode.json"
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if write_text(manifest_path, manifest_text):
        changed.append(str(manifest_path))
    return changed


def refresh_handoff_and_context(workspace: Path) -> None:
    env = workspace_env(workspace)
    python_executable = sys.executable
    commands = [
        [python_executable, str(PROJECT_ROOT / "scripts" / "validate_agent_capabilities.py"), "--example"],
        [python_executable, str(PROJECT_ROOT / "scripts" / "audit_translation_readiness.py")],
        [python_executable, str(PROJECT_ROOT / "scripts" / "write_workflow_state.py")],
        [python_executable, str(PROJECT_ROOT / "scripts" / "write_workflow_tasks.py")],
        [python_executable, str(PROJECT_ROOT / "scripts" / "write_agent_handoff.py")],
        [python_executable, str(PROJECT_ROOT / "scripts" / "write_codex_handoff.py")],
        [
            python_executable,
            str(PROJECT_ROOT / "scripts" / "export_agent_context.py"),
            "--agent",
            "opencode",
            "--output",
            LATEST_CONTEXT_PATH,
        ],
    ]
    for command in commands:
        run_checked(command, cwd=workspace, env=env)


def opencode_exists(command: str) -> bool:
    if Path(command).is_absolute():
        return Path(command).is_file()
    return shutil.which(command) is not None


def launch_opencode(workspace: Path, *, command: str, mode: str, prompt: str, auto: bool) -> int:
    env = workspace_env(workspace)
    if not opencode_exists(command):
        print("opencode executable was not found on PATH. Config and context were written, but launch was skipped.")
        print("Install or add opencode to PATH, then rerun this script.")
        return 2

    if mode == "run":
        opencode_command = [command, "run", "--dir", str(workspace), "--agent", OPENCODE_AGENT_NAME]
        if auto:
            opencode_command.append("--auto")
        opencode_command.extend(["--file", str(workspace / LATEST_CONTEXT_PATH), prompt])
    else:
        opencode_command = [command, str(workspace), "--agent", OPENCODE_AGENT_NAME, "--prompt", prompt]
        if auto:
            opencode_command.append("--auto")

    print(f"$ {' '.join(opencode_command)}")
    return subprocess.call(opencode_command, cwd=workspace, env=env)


def main() -> int:
    args = parse_args()
    try:
        workspace = ensure_workspace(Path(args.workspace), tool_setup=args.tool_setup)
        changed = write_opencode_config(workspace)
        print(f"opencode config initialized: {workspace}")
        print(f"Config files changed: {len(changed)}")
        for path in changed:
            print(f"- {path}")
        if args.skip_refresh:
            print("Handoff/context refresh skipped.")
        else:
            refresh_handoff_and_context(workspace)
            print(f"opencode context ready: {workspace / LATEST_CONTEXT_PATH}")
        if args.no_launch:
            print("opencode launch skipped.")
            return 0
        return launch_opencode(
            workspace,
            command=args.opencode_command,
            mode=args.launch_mode,
            prompt=args.prompt,
            auto=args.auto,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

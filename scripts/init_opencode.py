"""Initialize and optionally launch opencode for a profile-aware CHS workspace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from list_agent_skills import skill_rows
from game_context import game_display_label, load_game_context, supported_game_ids
from managed_tool_resolver import leased_payload_path, load_workspace_tool_config
from project_paths import is_under


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"
LATEST_CONTEXT_PATH = "qa/agent_context_prompts/latest.opencode.context.md"
OPENCODE_AGENT_NAME = "skyrim-chs"
OPENCODE_LOCAL_PLUGIN_PATH = ".opencode/plugins/skyrim-chs.js"
MANAGED_RULES_START = "<!-- skyrim-chs:managed:start -->"
MANAGED_RULES_END = "<!-- skyrim-chs:managed:end -->"
GENERATED_SKILL_POINTER_MARKER = "<!-- skyrim-chs:generated-skill-pointer -->"
FRESH_CHECKPOINT_ENV = "SKYRIM_CHS_FRESH_CHECKPOINT_CREDENTIAL"
CREDENTIAL_OUTPUT_PREFIX = "AGENT_HANDOFF_CREDENTIAL="
DEFAULT_INSTRUCTIONS = [
    ".opencode/AGENTS.md",
    LATEST_CONTEXT_PATH,
]
DEFAULT_WATCHER_IGNORES = [
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create opencode adapter config for the workspace Game Profile and optionally launch it."
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
        "--game",
        choices=supported_game_ids(),
        default="",
        help=(
            "Game Profile for a new workspace. Existing markers remain authoritative. "
            "When creating a workspace without this option, init_workspace.py requires interactive selection and confirmation."
        ),
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
            "确认 workspace marker 中由 init_opencode --game 选择的 Game Profile 后，按当前 workflow 状态推进允许的非 GUI 下一步；"
            "不要根据 Mod 名猜游戏。"
        ),
        help="Prompt passed to opencode when launching.",
    )
    return parser.parse_args()




def marker_path(workspace: Path) -> Path:
    return workspace / WORKSPACE_MARKER


def directory_is_empty(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    allow_nonzero: bool = False,
) -> subprocess.CompletedProcess[str]:
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
    return result


def checkpoint_credential_from_output(stdout: str) -> str:
    credentials = [
        line[len(CREDENTIAL_OUTPUT_PREFIX) :].strip()
        for line in stdout.splitlines()
        if line.startswith(CREDENTIAL_OUTPUT_PREFIX)
    ]
    if len(credentials) != 1:
        raise RuntimeError("write_agent_handoff.py did not emit exactly one checkpoint credential")
    credential = credentials[0]
    digest = credential.removeprefix("v1:")
    if not credential.startswith("v1:") or len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        raise RuntimeError("write_agent_handoff.py emitted an invalid checkpoint credential")
    return credential


def marker_game_id(workspace: Path) -> str:
    path = marker_path(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read workspace marker: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"workspace marker must contain a JSON object: {path}")
    game_id = payload.get("game_id", "skyrim-se")
    if not isinstance(game_id, str) or game_id not in supported_game_ids():
        raise RuntimeError(f"workspace marker has unsupported game_id: {path}")
    game_profile = payload.get("game_profile", game_id)
    if not isinstance(game_profile, str) or game_profile != game_id:
        raise RuntimeError(f"workspace marker game_profile conflicts with game_id: {path}")
    return game_id


def ensure_workspace(workspace: Path, *, tool_setup: str, game: str = "") -> Path:
    resolved = workspace.expanduser().resolve(strict=False)
    if resolved == PROJECT_ROOT or is_under(resolved, PROJECT_ROOT):
        raise RuntimeError("refusing to initialize opencode inside the plugin source repository")
    if marker_path(resolved).is_file():
        existing_game = marker_game_id(resolved)
        if game and game != existing_game:
            raise RuntimeError(
                f"requested game '{game}' conflicts with existing workspace marker game '{existing_game}': {resolved}"
            )
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
    if game:
        command.extend(["--game", game])
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


@contextmanager
def leased_workspace_python_executable(workspace: Path) -> Iterator[str]:
    """Use the managed binding for workflow children, never a legacy venv."""

    binding_path = workspace / ".workflow" / "managed-tools.json"
    if not os.path.lexists(binding_path):
        yield sys.executable
        return
    config = load_workspace_tool_config(workspace)
    with leased_payload_path(
        workspace,
        config,
        "PythonRuntimePath",
        timeout_seconds=30.0,
        command="refresh opencode handoff and context",
    ) as runtime:
        if runtime.path is None:
            raise RuntimeError("managed Python binding has no executable")
        yield str(runtime.path)


def opencode_config_payload() -> dict[str, object]:
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": OPENCODE_AGENT_NAME,
        "instructions": list(DEFAULT_INSTRUCTIONS),
        "watcher": {
            "ignore": list(DEFAULT_WATCHER_IGNORES),
        },
    }
    return payload


def merge_unique_strings(existing: object, required: list[str], *, field: str) -> list[str]:
    if existing is None:
        values: list[str] = []
    elif isinstance(existing, list) and all(isinstance(item, str) for item in existing):
        values = list(existing)
    else:
        raise RuntimeError(f"existing opencode.json field must be a string array: {field}")
    for item in required:
        if item not in values:
            values.append(item)
    return values


def merged_opencode_json(path: Path) -> str:
    generated = opencode_config_payload()
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot merge existing opencode.json: {exc}") from exc
        if not isinstance(existing, dict):
            raise RuntimeError("existing opencode.json must contain a JSON object")
        payload = dict(existing)
    else:
        payload = {}

    payload.setdefault("$schema", generated["$schema"])
    payload.setdefault("default_agent", generated["default_agent"])
    payload["instructions"] = merge_unique_strings(
        payload.get("instructions"),
        DEFAULT_INSTRUCTIONS,
        field="instructions",
    )

    watcher = payload.get("watcher")
    if watcher is None:
        watcher_payload: dict[str, object] = {}
    elif isinstance(watcher, dict):
        watcher_payload = dict(watcher)
    else:
        raise RuntimeError("existing opencode.json field must be an object: watcher")
    watcher_payload["ignore"] = merge_unique_strings(
        watcher_payload.get("ignore"),
        DEFAULT_WATCHER_IGNORES,
        field="watcher.ignore",
    )
    payload["watcher"] = watcher_payload
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def workspace_profile_prompt(workspace: Path) -> str:
    if marker_path(workspace).is_file():
        context = load_game_context(workspace)
        return (
            f"Current workspace Game Profile: {game_display_label(context)}. "
            f"Support level: {context.support_level}. The marker and Profile capabilities are authoritative."
        )
    return (
        "This directory has no valid workspace Game Profile yet. Ask the user in natural language to choose "
        "from the installed Game Profiles, then initialize with an explicit --game value."
    )


def opencode_rules(workspace: Path) -> str:
    profile_prompt = workspace_profile_prompt(workspace)
    return f"""# Bethesda Mod CHS opencode Rules

This workspace is controlled by the profile-aware Bethesda Mod CHS workflow core.

{profile_prompt} Do not infer the game from a Mod name, directory name, or archive name.

- Plugin root: `{PROJECT_ROOT}`
- Workspace root: `{workspace}`
- The top-level adapter uses only `python "{PROJECT_ROOT / "scripts" / "smt.py"}" --format json status|resume|doctor|output`; a first translation uses the same public controller with `run <ModPath> --game <GameId>`.
- Read the public JSON result. Do not use handoff, workflow state, or workflow tasks as a second top-level command source.
- Do not create workspace-local copies of plugin scripts or runtime Skills.
- opencode itself must not directly claim workflow tasks. Task claiming belongs only to controller-spawned subagents inside the authorized runtime protocol.
- Do not access real game, MO2, Vortex, Steam, AppData, or `Documents/My Games` paths.
- Do not modify `.esp`, `.esm`, `.esl`, `.bsa`, `.ba2`, `.pex`, `.dll`, `.exe`, or other binary files.
- GUI, Computer Use, pywinauto, UI Automation, LexTranslator/xTranslator desktop automation, and `gui:desktop` locks are Codex-only.
- If the next required step is GUI-only, mark the workflow blocked, record `handoff_target=codex`, and stop.
- Commands run in Windows. Prefer project Python entrypoints; do not introduce Bash, WSL, or Unix-style wrapper commands.
"""


def merge_managed_rules(existing: str, managed_content: str) -> str:
    managed_block = f"{MANAGED_RULES_START}\n{managed_content.rstrip()}\n{MANAGED_RULES_END}"
    start_count = existing.count(MANAGED_RULES_START)
    end_count = existing.count(MANAGED_RULES_END)
    if start_count != end_count or start_count > 1:
        raise RuntimeError("existing .opencode/AGENTS.md has malformed Skyrim CHS managed markers")
    start = existing.find(MANAGED_RULES_START)
    end = existing.find(MANAGED_RULES_END)
    if start_count == 1 and end < start:
        raise RuntimeError("existing .opencode/AGENTS.md has reversed Skyrim CHS managed markers")
    if start >= 0 and end >= start:
        end += len(MANAGED_RULES_END)
        return (existing[:start] + managed_block + existing[end:]).rstrip() + "\n"
    if not existing.strip():
        return managed_block + "\n"
    if existing.rstrip() == managed_content.rstrip():
        return managed_block + "\n"
    return existing.rstrip() + "\n\n" + managed_block + "\n"


def opencode_agent_markdown(workspace: Path) -> str:
    profile_prompt = workspace_profile_prompt(workspace)
    return f"""---
description: Profile-aware Bethesda Mod CHS non-GUI workflow controller
mode: primary
permission:
  read: allow
  grep: allow
  glob: allow
  edit:
    "*": allow
    "{PROJECT_ROOT.as_posix()}/**": deny
  external_directory:
    "{PROJECT_ROOT.as_posix()}/**": allow
  bash: ask
  webfetch: ask
  websearch: ask
  task: ask
  todowrite: allow
  skill: allow
---

# Bethesda Mod CHS opencode Controller

You are the non-GUI top-level adapter for this Bethesda CHS workspace.
{profile_prompt} Never infer the game from a Mod name or use a CLI prompt
instead of the agent conversation.

Read `.opencode/AGENTS.md`, then use only the public controller:

```powershell
python "{PROJECT_ROOT / "scripts" / "smt.py"}" --format json status
python "{PROJECT_ROOT / "scripts" / "smt.py"}" --format json resume
python "{PROJECT_ROOT / "scripts" / "smt.py"}" --format json doctor
python "{PROJECT_ROOT / "scripts" / "smt.py"}" --format json output
```

A first translation uses `run <ModPath> --game <GameId>`. Read the public JSON
result and act only on `next_action.artifacts`. Do not select commands directly
from handoff, workflow state, workflow tasks, or policy. Do not use GUI
automation, real game paths, or direct binary edits.
"""


def opencode_skill_pointer(row: dict[str, object]) -> str:
    skill_dir = str(row.get("skill_dir", "")).strip()
    name = str(row.get("name", "")).strip() or skill_dir
    description = str(row.get("description", "")).strip() or f"Shared Bethesda Mod CHS Skill: {skill_dir}"
    source = PROJECT_ROOT / "skills" / skill_dir / "SKILL.md"
    return f"""---
name: {name}
description: {json.dumps(description, ensure_ascii=False)}
---

{GENERATED_SKILL_POINTER_MARKER}

# Shared Bethesda Mod CHS Skill

Read and follow `{source}` completely before acting.

This file is a lightweight OpenCode discovery pointer. The authoritative Skill instructions remain in the plugin source and must not be copied into the workspace.
"""


def command_resume_markdown() -> str:
    return f"""---
description: Resume the profile-aware Bethesda Mod CHS workflow through the public controller
agent: {OPENCODE_AGENT_NAME}
subtask: false
---

Read `.opencode/AGENTS.md`, then run
`python "{PROJECT_ROOT / "scripts" / "smt.py"}" --format json resume`.
Read the single JSON result. Act only on `next_action.artifacts`; if the result
requires GUI capability, stop and hand off to Codex.
"""


def command_status_markdown() -> str:
    return f"""---
description: Read the current Bethesda Mod CHS status from the public controller
agent: {OPENCODE_AGENT_NAME}
subtask: false
---

Run `python "{PROJECT_ROOT / "scripts" / "smt.py"}" --format json status`.
Render the returned `progress_card` and use the same JSON object's `outcome`,
`next_action`, and `diagnostics`. Do not read internal state or progress files
as a substitute.
"""


def js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def opencode_plugin_js(workspace: Path) -> str:
    plugin_root = js_string(str(PROJECT_ROOT))
    workspace_root = js_string(str(workspace))
    config_dir = js_string(str(workspace / ".opencode"))
    context_path = js_string(LATEST_CONTEXT_PATH)
    return f"""// Generated by scripts/init_opencode.py. Do not edit by hand.
const PLUGIN_ROOT = {plugin_root}
const WORKSPACE_ROOT = {workspace_root}
const OPENCODE_CONFIG_DIR = {config_dir}
const CONTEXT_PACKET = {context_path}

export const SkyrimChsWorkspace = async () => {{
  return {{
    "shell.env": async (_input, output) => {{
      output.env = output.env || {{}}
      output.env.SKYRIM_CHS_PLUGIN_ROOT = PLUGIN_ROOT
      output.env.SKYRIM_CHS_WORKSPACE_ROOT = WORKSPACE_ROOT
      output.env.OPENCODE_CONFIG_DIR = OPENCODE_CONFIG_DIR
    }},
    "experimental.session.compacting": async (_input, output) => {{
      if (!Array.isArray(output.context)) return
      output.context.push(`## Bethesda Mod CHS Resume

- Workspace root: ${{WORKSPACE_ROOT}}
- Plugin root: ${{PLUGIN_ROOT}}
- Resume through the public SMT JSON controller; do not use the context packet or internal workflow files as a second top-level command source.
- Treat the workspace marker and exported Game Profile as authoritative; never infer the game from a Mod name.
- Run only smt.py run, status, resume, doctor, or output at the top level.
- GUI, Computer Use, LexTranslator/xTranslator desktop automation, and gui:desktop locks must be blocked with handoff_target=codex.
`)
    }},
  }}
}}
"""


def write_text(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == content:
        return False
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def prune_stale_skill_pointers(workspace: Path, current_skill_dirs: set[str]) -> list[str]:
    removed: list[str] = []
    skills_root = workspace / ".opencode" / "skills"
    if not skills_root.is_dir():
        return removed
    for pointer in skills_root.glob("*/SKILL.md"):
        if pointer.parent.name in current_skill_dirs:
            continue
        try:
            content = pointer.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        if GENERATED_SKILL_POINTER_MARKER not in content:
            continue
        try:
            pointer.unlink()
        except OSError as exc:
            raise RuntimeError(f"cannot remove stale generated opencode Skill pointer: {pointer}: {exc}") from exc
        try:
            pointer.parent.rmdir()
        except OSError:
            pass
        removed.append(str(pointer))
    return removed


def write_opencode_config(workspace: Path) -> list[str]:
    changed: list[str] = []
    config_path = workspace / "opencode.json"
    rules_path = workspace / ".opencode" / "AGENTS.md"
    existing_rules = rules_path.read_text(encoding="utf-8-sig") if rules_path.is_file() else ""
    files = {
        config_path: merged_opencode_json(config_path),
        rules_path: merge_managed_rules(existing_rules, opencode_rules(workspace)),
        workspace / ".opencode" / "agents" / f"{OPENCODE_AGENT_NAME}.md": opencode_agent_markdown(workspace),
        workspace / ".opencode" / "commands" / "skyrim-chs-resume.md": command_resume_markdown(),
        workspace / ".opencode" / "commands" / "skyrim-chs-status.md": command_status_markdown(),
        workspace / OPENCODE_LOCAL_PLUGIN_PATH: opencode_plugin_js(workspace),
    }
    current_skill_dirs: set[str] = set()
    for row in skill_rows("opencode"):
        if not bool(row.get("usable")):
            continue
        skill_dir = str(row.get("skill_dir", "")).strip()
        if not skill_dir:
            continue
        current_skill_dirs.add(skill_dir)
        files[workspace / ".opencode" / "skills" / skill_dir / "SKILL.md"] = opencode_skill_pointer(row)
    for path, content in files.items():
        if write_text(path, content):
            changed.append(str(path))
    changed.extend(f"removed:{path}" for path in prune_stale_skill_pointers(workspace, current_skill_dirs))

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "adapter": "opencode",
        "plugin_root": str(PROJECT_ROOT),
        "workspace_root": str(workspace),
        "agent": OPENCODE_AGENT_NAME,
        "context_packet": LATEST_CONTEXT_PATH,
        "local_plugin": OPENCODE_LOCAL_PLUGIN_PATH,
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
    env.pop(FRESH_CHECKPOINT_ENV, None)
    with leased_workspace_python_executable(workspace) as python_executable:
        commands_before_handoff = [
            [python_executable, str(PROJECT_ROOT / "scripts" / "validate_agent_capabilities.py"), "--example"],
            [python_executable, str(PROJECT_ROOT / "scripts" / "audit_translation_readiness.py")],
            [python_executable, str(PROJECT_ROOT / "scripts" / "write_workflow_state.py")],
            [python_executable, str(PROJECT_ROOT / "scripts" / "write_workflow_tasks.py")],
            [python_executable, str(PROJECT_ROOT / "scripts" / "write_codex_handoff.py")],
        ]
        for command in commands_before_handoff:
            run_checked(command, cwd=workspace, env=env)
        handoff_result = run_checked(
            [
                python_executable,
                str(PROJECT_ROOT / "scripts" / "write_agent_handoff.py"),
                "--agent",
                "opencode",
            ],
            cwd=workspace,
            env=env,
        )
        credential = checkpoint_credential_from_output(handoff_result.stdout)
        export_env = {**env, FRESH_CHECKPOINT_ENV: credential}
        run_checked(
            [
                python_executable,
                str(PROJECT_ROOT / "scripts" / "export_agent_context.py"),
                "--agent",
                "opencode",
                "--output",
                LATEST_CONTEXT_PATH,
            ],
            cwd=workspace,
            env=export_env,
        )


def resolve_opencode_command(command: str) -> str:
    candidate = Path(command).expanduser()
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else ""
    return shutil.which(command) or ""




def launch_opencode(workspace: Path, *, command: str, mode: str, prompt: str, auto: bool) -> int:
    env = workspace_env(workspace)
    resolved_command = resolve_opencode_command(command)
    if not resolved_command:
        print("opencode executable was not found on PATH. Config and context were written, but launch was skipped.")
        print("Install or add opencode to PATH, then rerun this script.")
        return 2

    if mode == "run":
        opencode_command = [resolved_command, "run", "--dir", str(workspace), "--agent", OPENCODE_AGENT_NAME]
        if auto:
            opencode_command.append("--auto")
        opencode_command.extend(["--file", str(workspace / LATEST_CONTEXT_PATH), prompt])
    else:
        opencode_command = [resolved_command, str(workspace), "--agent", OPENCODE_AGENT_NAME, "--prompt", prompt]
        if auto:
            opencode_command.append("--auto")

    print(f"$ {' '.join(opencode_command)}")
    try:
        return subprocess.call(opencode_command, cwd=workspace, env=env)
    except OSError as exc:
        print(f"opencode launch failed: {exc}")
        return 2


def main() -> int:
    args = parse_args()
    try:
        workspace = ensure_workspace(Path(args.workspace), tool_setup=args.tool_setup, game=args.game)
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

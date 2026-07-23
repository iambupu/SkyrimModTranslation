"""Run deterministic project-local effect regression checks.

This entrypoint validates tracked fixture cases under samples/effect_regression.
It does not call real game installations, MO2/Vortex, GUI tools, LLM APIs, or external
translation services.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ci_validate_repo
import smt_cli
from smt_windows import ProcessResult
from workflow_refresh import CORE_REFRESH_STEPS
from project_paths import relative_posix_path as relative_path
from file_utils import read_json_unchecked as read_json, write_json_sorted as write_json
from project_paths import source_repo_root as repo_root


CASE_ROOT = Path("samples") / "effect_regression" / "cases"
CASE_MANIFEST = "case.json"
EXPECTED_SUMMARY = Path("expected") / "summary.json"


@dataclass(frozen=True)
class RegressionCase:
    name: str
    root: Path
    manifest: dict[str, Any]





def list_cases(root: Path) -> list[str]:
    cases_dir = root / CASE_ROOT
    if not cases_dir.is_dir():
        return []
    return sorted(
        item.name
        for item in cases_dir.iterdir()
        if item.is_dir() and (item / CASE_MANIFEST).is_file()
    )


def load_case(root: Path, name: str) -> RegressionCase:
    case_root = root / CASE_ROOT / name
    manifest_path = case_root / CASE_MANIFEST
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing case manifest: {relative_path(root, manifest_path)}")
    manifest = read_json(manifest_path)
    return RegressionCase(name=name, root=case_root, manifest=manifest)


def run_command(root: Path, name: str, command: list[str]) -> dict[str, Any]:
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "SKYRIM_CHS_EFFECT_REGRESSION": "1",
        "SKYRIM_CHS_NO_EXTERNAL_TOOLS": "1",
    }
    completed = subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "name": name,
        "returncode": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
        "stdout_tail": tail_lines(completed.stdout),
        "stderr_tail": tail_lines(completed.stderr),
    }


def tail_lines(text: str, limit: int = 12) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def stable_command_summary(command_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": row["name"],
            "returncode": row["returncode"],
            "status": row["status"],
        }
        for row in command_results
    ]


def count_skill_frontmatter(
    root: Path,
    rel_root: Path,
    *,
    skip_local_tool_skills: bool = False,
) -> int:
    count = 0
    for skill_dir in ci_validate_repo.iter_direct_skill_dirs(
        root,
        rel_root,
        skip_local_tool_skills=skip_local_tool_skills,
    ):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        metadata = ci_validate_repo.parse_frontmatter(skill_file)
        if metadata and metadata.get("name") and metadata.get("description"):
            count += 1
    return count


def banned_wrapper_count(root: Path) -> int:
    count = 0
    for path in ci_validate_repo.iter_repo_source_files(root):
        if path.suffix.lower() not in ci_validate_repo.BANNED_WRAPPER_EXTENSIONS:
            continue
        if any(
            ci_validate_repo.is_relative_to_path(path, (root / prefix).resolve(strict=False))
            for prefix in ci_validate_repo.THIRD_PARTY_SOURCE_PREFIXES
        ):
            continue
        count += 1
    return count


def collect_repo_contract(root: Path, case: RegressionCase) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plugin = read_json(root / ".codex-plugin" / "plugin.json")
    policy = read_json(root / "config" / "workflow_policy.json")
    script_refs = sorted(set(ci_validate_repo.find_script_refs(policy)))
    command_results = [
        run_command(root, "ci_validate_repo", [sys.executable, "scripts/ci_validate_repo.py", "--strict"]),
        run_command(root, "compileall_scripts", [sys.executable, "-m", "compileall", "scripts"]),
        run_command(
            root,
            "workflow_health_repo_only",
            [sys.executable, "scripts/test_workflow_health.py", "--repo-only", "--strict"],
        ),
    ]
    summary = {
        "schema_version": 1,
        "case": case.name,
        "case_type": "repo-contract",
        "commands": stable_command_summary(command_results),
        "repository": {
            "plugin_name": plugin.get("name", ""),
            "plugin_version_supported": bool(ci_validate_repo.PLUGIN_VERSION_RE.fullmatch(str(plugin.get("version", "")))),
            "plugin_skills_path": str(plugin.get("skills", "")),
            "runtime_skill_count": count_skill_frontmatter(root, Path("skills")),
            "meta_skill_count": count_skill_frontmatter(
                root,
                Path(".codex") / "skills",
                skip_local_tool_skills=True,
            ),
            "workflow_policy_script_ref_count": len(script_refs),
            "banned_wrapper_count": banned_wrapper_count(root),
            "ci_workflow_exists": (root / ".github" / "workflows" / "ci.yml").is_file(),
            "effect_regression_doc_exists": (root / "docs" / "effect_regression_workflow.md").is_file(),
            "uses_real_game_or_gui_tools": False,
        },
    }
    return summary, command_results


def _write_smt_effect_zip(fixture_root: Path, target: Path, *, changed: bool) -> None:
    """Create deterministic ZIP bytes from the tracked safe fixture tree."""

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for source in sorted(
            (path for path in fixture_root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(fixture_root).as_posix(),
        ):
            relative = source.relative_to(fixture_root).as_posix()
            payload = source.read_bytes()
            if changed and relative.endswith("example_english.txt"):
                payload += b"$EXAMPLE_CHANGED\tChanged fixture content.\n"
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 0
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)


def _write_smt_effect_marker(workspace: Path, game_id: str, tool_setup: str) -> None:
    if tool_setup != "skip":
        raise AssertionError("SMT effect initialization must use --tool-setup skip")
    if not workspace.is_dir():
        raise AssertionError("SMT controller must create the workspace before initialization")
    if any(workspace.iterdir()):
        raise AssertionError("SMT effect initializer requires an empty workspace")
    for relative in (".workflow", "mod", "qa"):
        (workspace / relative).mkdir()
    (workspace / smt_cli.WORKSPACE_MARKER).write_text(
        json.dumps(
            {
                "schema_version": smt_cli.WORKSPACE_MARKER_SCHEMA_VERSION,
                "kind": smt_cli.WORKSPACE_KIND,
                "game_id": game_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _SmtEffectRunner:
    """In-process workflow stub which refuses every external/tool action."""

    _ALLOWED_SCRIPTS = {
        "run_translation_queue.py",
        *(step.script for step in CORE_REFRESH_STEPS),
    }

    def __init__(self) -> None:
        self.uses_real_tools = False

    @staticmethod
    def _write_agent_pause(workspace: Path) -> None:
        session = smt_cli.validate_session(workspace)
        artifact = f"work/normalized/{session.mod_name}/translation_candidates.jsonl"
        generated_at = "2026-07-22 12:34:56"
        state = {
            "schema_version": 1,
            "game_id": session.game_id,
            "game_profile_version": 2,
            "game_display_name": "Skyrim Special Edition",
            "support_level": "stable",
            "interface_translation_encoding": "utf-16-le-bom",
            "generated_at": generated_at,
            "policy_path": "config/workflow_policy.json",
            "policy_sha256": "0" * 64,
            "project_state": "candidates_extracted",
            "states": [
                {
                    "mod": session.mod_name,
                    "state": "candidates_extracted",
                    "last_success_stage": "candidates_extracted",
                    "blocking_checks": [],
                    "blocking_issues": [],
                    "next_actions": [],
                    "allowed_scripts": [],
                    "required_files": [],
                    "evidence": {"candidate": artifact},
                    "recommended_actions": [],
                    "repair_candidates": [],
                    "stop_conditions": [],
                    "retry_count": 0,
                    "last_attempt": {},
                }
            ],
        }
        tasks = {
            "schema_version": 1,
            "generated_at": generated_at,
            "tasks": [
                {
                    "task_id": f"agent-translation:{session.mod_name}",
                    "mod": session.mod_name,
                    "stage": "candidates_extracted",
                    "kind": "agent_translation",
                    "status": "pending_manual",
                    "reason": "generate and review player-visible translations",
                    "risk": "semantic",
                    "command": "",
                    "executable": False,
                    "can_run_parallel": False,
                    "dependencies": [],
                    "resource_locks": [f"mod:{session.mod_name}"],
                    "evidence": artifact,
                }
            ],
        }
        (workspace / "qa" / "workflow_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        (workspace / "qa" / "workflow_tasks.json").write_text(
            json.dumps(tasks, ensure_ascii=False), encoding="utf-8"
        )
        (workspace / ".workflow" / "progress_card.md").write_text(
            "# [SMT 进度]\n\n等待 Agent 生成并校对玩家可见译文。\n",
            encoding="utf-8",
        )

    def run(self, argv: Sequence[object], **kwargs: object) -> ProcessResult:
        environment = kwargs.get("env")
        if not isinstance(environment, dict) or environment.get(
            "SKYRIM_CHS_NO_EXTERNAL_TOOLS"
        ) != "1":
            raise AssertionError("SMT effect runner requires external tools to be disabled")
        script_name = Path(str(argv[1])).name
        if script_name not in self._ALLOWED_SCRIPTS:
            self.uses_real_tools = True
            raise AssertionError(f"unexpected real tool or process request: {script_name}")
        workspace = Path(str(kwargs["cwd"]))
        self._write_agent_pause(workspace)
        return ProcessResult(exit_code=0, output_tail=(f"stub:{script_name}",))


def _smt_effect_run(
    source: Path,
    *,
    workspace_root: Path,
    state_root: Path,
    game_id: str,
    tool_setup: str,
    runner: _SmtEffectRunner,
) -> smt_cli.CliResult:
    if tool_setup != "skip":
        raise ValueError("smt-single-entry effect case requires tool_setup=skip")
    request = smt_cli.RunRequest(
        source=source,
        game_id=game_id,
        workspace_root=workspace_root,
        local_state_root=state_root,
        cwd=workspace_root.parent,
        tool_setup="skip",
        timeout_seconds=60,
        initializer=_write_smt_effect_marker,
    )
    services = smt_cli.SmtServices(
        runner=runner,
        max_steps=2,
        attempt_logger=lambda **_fields: None,
    )
    return smt_cli.run_command(request, services)


def collect_smt_single_entry(
    root: Path, case: RegressionCase
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if case.manifest.get("schema_version") != 1:
        raise ValueError("smt-single-entry case schema_version must be 1")
    game_id = str(case.manifest.get("game_id", ""))
    tool_setup = str(case.manifest.get("tool_setup", ""))
    if not game_id or tool_setup != "skip":
        raise ValueError("smt-single-entry case requires game_id and tool_setup=skip")
    fixture_root = case.root / "fixtures" / "ExampleMod"
    if not fixture_root.is_dir():
        raise FileNotFoundError("smt-single-entry fixture tree is missing")

    previous_no_tools = os.environ.get("SKYRIM_CHS_NO_EXTERNAL_TOOLS")
    os.environ["SKYRIM_CHS_NO_EXTERNAL_TOOLS"] = "1"
    try:
        with tempfile.TemporaryDirectory(
            prefix="effect-smt-single-entry-", dir=root.parent
        ) as temp_dir:
            temporary_root = Path(temp_dir)
            source = temporary_root / "inputs" / "ExampleMod.zip"
            workspace_root = temporary_root / "workspaces"
            state_root = temporary_root / "state"
            _write_smt_effect_zip(fixture_root, source, changed=False)
            runner = _SmtEffectRunner()

            first = _smt_effect_run(
                source,
                workspace_root=workspace_root,
                state_root=state_root,
                game_id=game_id,
                tool_setup=tool_setup,
                runner=runner,
            )
            second = _smt_effect_run(
                source,
                workspace_root=workspace_root,
                state_root=state_root,
                game_id=game_id,
                tool_setup=tool_setup,
                runner=runner,
            )
            _write_smt_effect_zip(fixture_root, source, changed=True)
            changed = _smt_effect_run(
                source,
                workspace_root=workspace_root,
                state_root=state_root,
                game_id=game_id,
                tool_setup=tool_setup,
                runner=runner,
            )

            if not all(result.workspace for result in (first, second, changed)):
                raise AssertionError("SMT effect runs must all resolve a workspace")
            first_workspace = Path(str(first.workspace))
            second_workspace = Path(str(second.workspace))
            changed_workspace = Path(str(changed.workspace))
            first_session = smt_cli.validate_session(first_workspace)
            second_session = smt_cli.validate_session(second_workspace)
            changed_session = smt_cli.validate_session(changed_workspace)
            summary = {
                "schema_version": 1,
                "case": case.name,
                "case_type": "smt-single-entry",
                "game_id": game_id,
                "tool_setup": tool_setup,
                "uses_real_tools": runner.uses_real_tools,
                "runs": [
                    {
                        "name": "first",
                        "outcome": first.outcome,
                        "exit_code": first.exit_code,
                        "workspace": relative_path(workspace_root, first_workspace),
                        "import_path": first_session.import_relative_path,
                    },
                    {
                        "name": "same-input",
                        "outcome": second.outcome,
                        "exit_code": second.exit_code,
                        "workspace": relative_path(workspace_root, second_workspace),
                        "import_path": second_session.import_relative_path,
                    },
                    {
                        "name": "changed-input",
                        "outcome": changed.outcome,
                        "exit_code": changed.exit_code,
                        "workspace": relative_path(workspace_root, changed_workspace),
                        "import_path": changed_session.import_relative_path,
                    },
                ],
                "assertions": {
                    "first_paused_for_agent_translation": (
                        first.outcome == "needs_agent_translation"
                        and first.exit_code == smt_cli.EXIT_SAFE_STOP
                    ),
                    "same_identity_reused": (
                        first_session.input_identity == second_session.input_identity
                    ),
                    "same_workspace_reused": first_workspace == second_workspace,
                    "changed_identity_is_new": (
                        first_session.input_identity != changed_session.input_identity
                    ),
                    "changed_workspace_is_new": first_workspace != changed_workspace,
                },
            }
            if not all(summary["assertions"].values()) or summary["uses_real_tools"]:
                raise AssertionError("SMT single-entry effect assertions failed")
            return summary, []
    finally:
        if previous_no_tools is None:
            os.environ.pop("SKYRIM_CHS_NO_EXTERNAL_TOOLS", None)
        else:
            os.environ["SKYRIM_CHS_NO_EXTERNAL_TOOLS"] = previous_no_tools


def run_case(root: Path, case: RegressionCase) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_type = str(case.manifest.get("type", ""))
    if case_type == "repo-contract":
        return collect_repo_contract(root, case)
    if case_type == "smt-single-entry":
        return collect_smt_single_entry(root, case)
    raise ValueError(f"unsupported effect regression case type: {case_type}")


def compare_payload(actual: Any, expected: Any, path: str = "$") -> list[str]:
    if type(actual) is not type(expected):
        return [f"{path}: expected {type(expected).__name__}, got {type(actual).__name__}"]
    if isinstance(expected, dict):
        differences: list[str] = []
        actual_keys = set(actual)
        expected_keys = set(expected)
        for key in sorted(expected_keys - actual_keys):
            differences.append(f"{path}.{key}: missing actual key")
        for key in sorted(actual_keys - expected_keys):
            differences.append(f"{path}.{key}: unexpected actual key")
        for key in sorted(actual_keys & expected_keys):
            differences.extend(compare_payload(actual[key], expected[key], f"{path}.{key}"))
        return differences
    if isinstance(expected, list):
        differences = []
        if len(actual) != len(expected):
            differences.append(f"{path}: expected {len(expected)} item(s), got {len(actual)}")
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            differences.extend(compare_payload(actual_item, expected_item, f"{path}[{index}]"))
        return differences
    if actual != expected:
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return []


def print_command_failures(command_results: list[dict[str, Any]]) -> None:
    for row in command_results:
        if row["returncode"] == 0:
            continue
        print(f"Command failed: {row['name']} (exit {row['returncode']})")
        if row["stdout_tail"]:
            print("  stdout tail:")
            for line in row["stdout_tail"]:
                print(f"    {line}")
        if row["stderr_tail"]:
            print("  stderr tail:")
            for line in row["stderr_tail"]:
                print(f"    {line}")


def selected_case_names(root: Path, args: argparse.Namespace) -> list[str]:
    if args.all:
        names = list_cases(root)
        if not names:
            raise ValueError("no effect regression cases found")
        return names
    if args.case:
        return list(args.case)
    raise ValueError("select at least one case with --case <name> or --all")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run project-local effect regression fixture checks.")
    parser.add_argument("--case", action="append", default=[], help="Run one named case. Can be repeated.")
    parser.add_argument("--all", action="store_true", help="Run every tracked case.")
    parser.add_argument("--list", action="store_true", help="List available cases and exit.")
    parser.add_argument("--ci", action="store_true", help="CI mode: never update expected snapshots.")
    parser.add_argument("--update-expected", action="store_true", help="Update expected snapshots from current output.")
    parser.add_argument(
        "--keep-failed-workspace",
        action="store_true",
        help="Reserved for fixture-workspace cases; repo-contract does not create a workspace.",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    available = list_cases(root)
    if args.list:
        for name in available:
            print(name)
        return 0
    if args.ci and args.update_expected:
        print("FAIL: --ci cannot be combined with --update-expected")
        return 2

    try:
        names = selected_case_names(root, args)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2

    missing = sorted(name for name in names if name not in available)
    if missing:
        print(f"FAIL: unknown case(s): {', '.join(missing)}")
        return 2

    failures: list[str] = []
    for name in names:
        case = load_case(root, name)
        expected_path = case.root / EXPECTED_SUMMARY
        print(f"[effect-regression] {name}")
        actual, command_results = run_case(root, case)
        print_command_failures(command_results)
        if args.update_expected:
            if any(row["returncode"] != 0 for row in command_results):
                failures.append(f"{name}: refusing to update expected snapshot because a command failed")
                continue
            write_json(expected_path, actual)
            print(f"  updated {relative_path(root, expected_path)}")
            continue
        if not expected_path.is_file():
            failures.append(f"{name}: missing expected snapshot: {relative_path(root, expected_path)}")
            continue
        expected = read_json(expected_path)
        differences = compare_payload(actual, expected)
        if differences:
            failures.append(f"{name}: {len(differences)} difference(s)")
            for diff in differences[:20]:
                print(f"  DIFF {diff}")
            if len(differences) > 20:
                print(f"  ... {len(differences) - 20} more difference(s)")
        else:
            print("  PASS")

    if failures:
        print("")
        print("FAIL: effect regression checks failed")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("")
    print(f"PASS: {len(names)} effect regression case(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

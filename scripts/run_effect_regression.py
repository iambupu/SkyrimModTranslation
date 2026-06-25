"""Run deterministic project-local effect regression checks.

This entrypoint validates tracked fixture cases under samples/effect_regression.
It does not call real Skyrim, MO2/Vortex, GUI tools, LLM APIs, or external
translation services.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ci_validate_repo


CASE_ROOT = Path("samples") / "effect_regression" / "cases"
CASE_MANIFEST = "case.json"
EXPECTED_SUMMARY = Path("expected") / "summary.json"


@dataclass(frozen=True)
class RegressionCase:
    name: str
    root: Path
    manifest: dict[str, Any]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.as_posix()


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


def count_skill_frontmatter(root: Path, rel_root: Path) -> int:
    count = 0
    skill_root = root / rel_root
    if not skill_root.is_dir():
        return 0
    for skill_dir in skill_root.iterdir():
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
            "meta_skill_count": count_skill_frontmatter(root, Path(".codex") / "skills"),
            "workflow_policy_script_ref_count": len(script_refs),
            "banned_wrapper_count": banned_wrapper_count(root),
            "ci_workflow_exists": (root / ".github" / "workflows" / "ci.yml").is_file(),
            "effect_regression_doc_exists": (root / "docs" / "effect_regression_workflow.md").is_file(),
            "uses_real_game_or_gui_tools": False,
        },
    }
    return summary, command_results


def run_case(root: Path, case: RegressionCase) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_type = str(case.manifest.get("type", ""))
    if case_type == "repo-contract":
        return collect_repo_contract(root, case)
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

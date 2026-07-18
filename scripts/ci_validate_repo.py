"""CI-only repository validation for the Skyrim CHS Codex plugin.

The checks in this script are intentionally repo-local and deterministic. They
do not inspect a user's game installation, Mod manager, GUI tools, AppData, or
translation services.
"""

from __future__ import annotations

import argparse
import ast
import compileall
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agent_capabilities import config_validation_errors as agent_capability_validation_errors
from adapter_registry import validate_profile_adapters as registry_validate_profile_adapters
from audit_mod_scale import load_scale_config
from capability_promotion_gates import validation_errors as capability_promotion_gate_errors
from claude_plugin_marketplace import config_validation_errors as claude_marketplace_validation_errors
from file_utils import parse_simple_frontmatter as parse_frontmatter
from file_utils import read_text_utf8_sig_strict as read_text
from game_context import (
    PLUGIN_ROOT_ENV,
    load_game_profile,
    supported_game_ids,
)
from project_paths import source_repo_root as repo_root
from pex_visible_api_registry import load_pex_visible_api_registry


SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
FOUR_PART_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PLUGIN_VERSION_RE = re.compile(rf"(?:{SEMVER_RE.pattern})|(?:{FOUR_PART_VERSION_RE.pattern})")
SCRIPT_REF_RE = re.compile(r"scripts[/\\][A-Za-z0-9_.\-/\\]+?\.py")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
NON_WINDOWS_FENCE_RE = re.compile(r"^```(?:bash|sh|shell|zsh|console)\s*$", re.IGNORECASE | re.MULTILINE)
BANNED_WRAPPER_EXTENSIONS = {".ps1", ".bat", ".cmd"}
PLUGIN_JSON = Path(".codex-plugin") / "plugin.json"
CLAUDE_MARKETPLACE_JSON = Path(".claude-plugin") / "marketplace.json"
CLAUDE_PLUGIN_JSON = Path(".claude-plugin") / "plugin.json"
WORKFLOW_POLICY_JSON = Path("config") / "workflow_policy.json"
TOOLS_EXAMPLE_JSON = Path("config") / "tools.example.json"
AGENT_CAPABILITIES_EXAMPLE_JSON = Path("config") / "agent_capabilities.example.json"
MOD_SCALE_PROFILES_JSON = Path("config") / "mod_scale_profiles.json"
CAPABILITY_PROMOTION_GATES_JSON = Path("config") / "capability_promotion_gates.json"
FALLOUT4_PEX_VISIBLE_APIS_JSON = Path("config") / "pex_visible_apis" / "fallout4.json"
PYPROJECT_TOML = Path("pyproject.toml")
REQUIREMENTS_TXT = Path("requirements.txt")
UV_LOCK = Path("uv.lock")
GAME_PROFILE_DIR = Path("config") / "game_profiles"
GAME_AGNOSTIC_CORE_SCRIPTS = (
    Path("scripts") / "export_esp_strings.py",
    Path("scripts") / "run_plugin_translation_stage.py",
    Path("scripts") / "invoke_mutagen_plugin_text_tool.py",
    Path("scripts") / "invoke_mutagen_pex_string_tool.py",
    Path("scripts") / "new_final_binary_review_packet.py",
    Path("scripts") / "prepare_pex_tool_output.py",
    Path("scripts") / "verify_pex_output.py",
    Path("scripts") / "audit_pex_delivery.py",
    Path("scripts") / "audit_translation_readiness.py",
    Path("scripts") / "write_workflow_state.py",
    Path("scripts") / "write_workflow_tasks.py",
    Path("scripts") / "run_non_gui_qa_gates.py",
    Path("scripts") / "run_non_gui_translation_workflow.py",
    Path("scripts") / "verify_plugin_output.py",
    Path("scripts") / "audit_archive_coverage.py",
    Path("scripts") / "audit_mod_scale.py",
    Path("scripts") / "mod_scale_policy.py",
    Path("scripts") / "mod_materialization.py",
    Path("scripts") / "translation_candidate_shards.py",
    Path("scripts") / "archive_execution_policy.py",
    Path("scripts") / "bethesda_archive_adapter.py",
    Path("scripts") / "aggregate_translation_projects.py",
    Path("scripts") / "build_final_mod.py",
    Path("scripts") / "route_translation_task.py",
    Path("scripts") / "capability_resolver.py",
    Path("scripts") / "adapter_registry.py",
    Path("scripts") / "capability_promotion_gates.py",
    Path("scripts") / "used_capabilities.py",
)
REQUIRED_PLUGIN_FIELDS = {
    "name",
    "version",
    "description",
    "repository",
    "skills",
    "license",
}
ALLOWED_META_SKILLS = {
    "skyrim-mod-chs-install",
    "skyrim-mod-chs-maintenance",
    "skyrim-mod-chs-usage",
}
LOCAL_TOOL_META_SKILL_PREFIXES = ("openspec-",)
SOURCE_SCAN_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".workflow",
    "__pycache__",
    "mod",
    "out",
    "qa",
    "source",
    "traces",
    "translated",
    "venv",
    "work",
}
THIRD_PARTY_SOURCE_PREFIXES = (
    Path("scripts") / "vendor",
    Path("tools") / "_downloads",
    Path("tools") / "_snapshots",
    Path("tools") / "adapters",
    Path("tools") / "BSAFileExtractor",
    Path("tools") / "Champollion",
    Path("tools") / "dotnet-adapters",
    Path("tools") / "dotnet-sdk",
    Path("tools") / "Mutagen",
    Path("tools") / "SSEEdit 4.1.5f",
)
REMOVED_EXTERNAL_TASK_RUNNER_SURFACES = {
    Path("config") / "external_agents.example.json",
    Path("config") / "external_agents.local.json",
    Path("scripts") / "external_agent_providers.py",
    Path("scripts") / "run_agent_worker_task.py",
    Path("scripts") / "run_external_agent_task.py",
    Path("scripts") / "validate_external_agents_config.py",
    Path("docs") / "external_agent_workers.md",
}
REMOVED_EXTERNAL_TASK_RUNNER_TEXT_REFERENCES = {
    "config/external_agents.example.json",
    "config/external_agents.local.json",
    "external_agents.example.json",
    "external_agents.local.json",
    "scripts/external_agent_providers.py",
    "scripts/run_agent_worker_task.py",
    "scripts/run_external_agent_task.py",
    "scripts/validate_external_agents_config.py",
    "docs/external_agent_workers.md",
    "external_agent_providers.py",
    "run_agent_worker_task.py",
    "run_external_agent_task.py",
    "validate_external_agents_config.py",
    "external_agent_workers.md",
    "外部 agent worker",
}
TEXT_REFERENCE_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


class Reporter:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append(CheckResult(name=name, passed=passed, detail=detail))
        status = "PASS" if passed else "FAIL"
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {name}{suffix}")

    @property
    def failed(self) -> list[CheckResult]:
        return [result for result in self.results if not result.passed]




def display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.as_posix()


def is_relative_to_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def resolve_repo_path(root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def load_json_file(root: Path, rel_path: Path, reporter: Reporter) -> Any | None:
    path = root / rel_path
    if not path.is_file():
        reporter.check(f"JSON exists: {rel_path.as_posix()}", False, "missing file")
        return None
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        reporter.check(f"JSON parses: {rel_path.as_posix()}", False, f"{exc.msg} at line {exc.lineno}")
        return None
    reporter.check(f"JSON parses: {rel_path.as_posix()}", True)
    return payload


def is_git_ignored(root: Path, path: Path) -> bool:
    try:
        rel_path = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", rel_path.as_posix()],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def is_ignored_local_tool_meta_skill(root: Path, path: Path) -> bool:
    return path.name.startswith(LOCAL_TOOL_META_SKILL_PREFIXES) and is_git_ignored(root, path)


def iter_direct_skill_dirs(root: Path, skill_root: Path, *, skip_local_tool_skills: bool = False) -> list[Path]:
    absolute = root / skill_root
    if not absolute.is_dir():
        return []
    skill_dirs = []
    for item in absolute.iterdir():
        if not item.is_dir():
            continue
        if skip_local_tool_skills and is_ignored_local_tool_meta_skill(root, item):
            continue
        skill_dirs.append(item)
    return sorted(skill_dirs, key=lambda item: item.name.lower())


def validate_plugin_manifest(root: Path, payload: Any, reporter: Reporter) -> Path | None:
    if not isinstance(payload, dict):
        reporter.check("plugin manifest is an object", False)
        return None
    missing = sorted(field for field in REQUIRED_PLUGIN_FIELDS if not payload.get(field))
    reporter.check(
        "plugin manifest required fields",
        not missing,
        "present" if not missing else f"missing: {', '.join(missing)}",
    )
    version = str(payload.get("version", ""))
    reporter.check("plugin manifest version is supported", bool(PLUGIN_VERSION_RE.fullmatch(version)), version)

    skills_value = str(payload.get("skills", "")).strip()
    skills_path = resolve_repo_path(root, skills_value) if skills_value else root / "skills"
    reporter.check(
        "plugin manifest skills path exists",
        skills_path.is_dir() and is_relative_to_path(skills_path, root),
        display_path(root, skills_path),
    )

    interface = payload.get("interface")
    if isinstance(interface, dict) and interface.get("logo"):
        logo_path = resolve_repo_path(root, str(interface["logo"]))
        reporter.check(
            "plugin manifest interface.logo path exists",
            logo_path.is_file() and is_relative_to_path(logo_path, root),
            display_path(root, logo_path),
        )
    else:
        reporter.check("plugin manifest interface.logo path exists", True, "not declared")
    return skills_path


def validate_skill_tree(root: Path, rel_skill_root: Path, label: str, reporter: Reporter) -> dict[str, list[Path]]:
    skill_dirs = iter_direct_skill_dirs(
        root,
        rel_skill_root,
        skip_local_tool_skills=label == "meta",
    )
    if label == "runtime":
        reporter.check("runtime skills exist", bool(skill_dirs), rel_skill_root.as_posix())
    skill_names: dict[str, list[Path]] = defaultdict(list)
    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        rel_file = display_path(root, skill_file)
        if not skill_file.is_file():
            reporter.check(f"{label} skill has SKILL.md: {skill_dir.name}", False, rel_file)
            continue
        metadata = parse_frontmatter(skill_file)
        reporter.check(f"{label} skill has YAML frontmatter: {skill_dir.name}", metadata is not None, rel_file)
        if metadata is None:
            continue
        missing = [key for key in ("name", "description") if not metadata.get(key)]
        reporter.check(
            f"{label} skill frontmatter name/description: {skill_dir.name}",
            not missing,
            rel_file if not missing else f"{rel_file}: missing {', '.join(missing)}",
        )
        skill_text = read_text(skill_file)
        reporter.check(
            f"{label} skill declares Windows runtime: {skill_dir.name}",
            "Windows" in skill_text,
            rel_file,
        )
        name = metadata.get("name", "")
        if name:
            skill_names[name].append(skill_file)
    return skill_names


def validate_skills(root: Path, manifest_skills_path: Path | None, reporter: Reporter) -> None:
    expected_runtime_root = (root / "skills").resolve(strict=False)
    if manifest_skills_path is not None:
        reporter.check(
            "plugin manifest points to runtime skills/",
            manifest_skills_path.resolve(strict=False) == expected_runtime_root,
            display_path(root, manifest_skills_path),
        )

    runtime_names = validate_skill_tree(root, Path("skills"), "runtime", reporter)
    meta_names = validate_skill_tree(root, Path(".codex") / "skills", "meta", reporter)

    runtime_duplicates = sorted(name for name, paths in runtime_names.items() if len(paths) > 1)
    runtime_duplicate_detail = []
    for name in runtime_duplicates:
        runtime_duplicate_detail.append(
            f"{name}: {', '.join(display_path(root, path) for path in runtime_names[name])}"
        )
    reporter.check(
        "runtime skill names are unique",
        not runtime_duplicates,
        "unique" if not runtime_duplicates else "; ".join(runtime_duplicate_detail),
    )

    all_names: dict[str, list[Path]] = defaultdict(list)
    for name, paths in runtime_names.items():
        all_names[name].extend(paths)
    for name, paths in meta_names.items():
        all_names[name].extend(paths)
    mixed_duplicates = {name: paths for name, paths in all_names.items() if len(paths) > 1}
    reporter.check(
        "runtime and meta skills are not mixed",
        not mixed_duplicates,
        "separate" if not mixed_duplicates else ", ".join(sorted(mixed_duplicates)),
    )

    unexpected_meta = sorted(name for name in meta_names if name not in ALLOWED_META_SKILLS)
    reporter.check(
        "meta skills stay in .codex/skills only",
        not unexpected_meta,
        "allowed meta set" if not unexpected_meta else ", ".join(unexpected_meta),
    )
    runtime_meta_names = sorted(name for name in runtime_names if name in ALLOWED_META_SKILLS)
    reporter.check(
        "runtime skills do not contain meta skills",
        not runtime_meta_names,
        "runtime/meta split clean" if not runtime_meta_names else ", ".join(runtime_meta_names),
    )


def find_script_refs(value: Any, context: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from find_script_refs(child, f"{context}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from find_script_refs(child, f"{context}[{index}]")
    elif isinstance(value, str):
        for match in SCRIPT_REF_RE.finditer(value):
            yield match.group(0).replace("\\", "/"), context


def validate_workflow_policy(root: Path, payload: Any, reporter: Reporter) -> None:
    if not isinstance(payload, dict):
        reporter.check("workflow policy is an object", False)
        return
    control_model = payload.get("control_model")
    if isinstance(control_model, dict):
        codex_specific = "codex" in control_model
        has_controller = control_model.get("active_controller_agent") == "accurate and flexible orchestration"
        reporter.check(
            "workflow policy uses active controller model",
            has_controller and not codex_specific,
            "active_controller_agent" if has_controller and not codex_specific else "expected active_controller_agent and no codex control-model key",
        )
    else:
        reporter.check("workflow policy uses active controller model", False, "missing control_model object")
    agent_policy = payload.get("agent_orchestration_policy")
    mode = agent_policy.get("mode") if isinstance(agent_policy, dict) else ""
    reporter.check(
        "workflow policy orchestration mode is controller-generic",
        mode == "controller_flexible_with_state_guardrails",
        str(mode or "missing"),
    )
    refs = sorted(set(find_script_refs(payload)))
    missing: list[str] = []
    for script_ref, context in refs:
        script_path = root / Path(*script_ref.split("/"))
        if not script_path.is_file():
            missing.append(f"{script_ref} ({context})")
    reporter.check(
        "workflow policy script references exist",
        not missing,
        f"{len(refs)} script reference(s)" if not missing else "; ".join(missing),
    )


def validate_no_external_adapter_task_runner_surface(root: Path, reporter: Reporter) -> None:
    present = sorted(path.as_posix() for path in REMOVED_EXTERNAL_TASK_RUNNER_SURFACES if (root / path).exists())
    reporter.check(
        "legacy adapter task-runner surfaces are absent",
        not present,
        "removed legacy task-runner surfaces absent" if not present else ", ".join(present),
    )


def validate_no_legacy_adapter_task_runner_text(root: Path, reporter: Reporter) -> None:
    allowed_paths = {
        (root / "scripts" / "ci_validate_repo.py").resolve(strict=False),
    }
    offenders: list[str] = []
    for path in iter_repo_source_files(root):
        resolved = path.resolve(strict=False)
        if resolved in allowed_paths or is_relative_to_path(resolved, root / "tests"):
            continue
        if path.suffix.lower() not in TEXT_REFERENCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        normalized_text = text.replace("\\", "/")
        matches = sorted(term for term in REMOVED_EXTERNAL_TASK_RUNNER_TEXT_REFERENCES if term in normalized_text)
        if matches:
            offenders.append(f"{display_path(root, path)}: {', '.join(matches)}")
    reporter.check(
        "legacy adapter task-runner text references are absent",
        not offenders,
        "clean" if not offenders else "; ".join(offenders),
    )


def validate_subagent_claim_contract(reporter: Reporter) -> None:
    root = repo_root()
    required = [
        root / "scripts" / "claim_workflow_task.py",
        root / "scripts" / "run_workflow_tasks.py",
        root / "scripts" / "write_workflow_tasks.py",
    ]
    missing = [display_path(root, path) for path in required if not path.is_file()]
    reporter.check(
        "subagent task claim scripts exist",
        not missing,
        "claim protocol present" if not missing else ", ".join(missing),
    )
    policy_path = root / WORKFLOW_POLICY_JSON
    policy_text = policy_path.read_text(encoding="utf-8") if policy_path.is_file() else ""
    forbidden = [
        "scripts/run_agent_worker_task.py",
        "scripts/run_external_agent_task.py",
        "scripts/validate_external_agents_config.py",
    ]
    found = [item for item in forbidden if item in policy_text]
    reporter.check(
        "workflow policy has no legacy adapter task-runner entrypoints",
        not found,
        "clean" if not found else ", ".join(found),
    )


def validate_agent_capabilities_example(payload: Any, reporter: Reporter) -> None:
    if not isinstance(payload, dict):
        reporter.check("agent capabilities example schema", False, "not an object")
        return
    errors = agent_capability_validation_errors(payload)
    reporter.check(
        "agent capabilities example schema",
        not errors,
        "valid" if not errors else "; ".join(errors),
    )


def validate_claude_marketplace(
    marketplace_payload: Any,
    plugin_payload: Any,
    root: Path,
    reporter: Reporter,
) -> None:
    if not isinstance(marketplace_payload, dict):
        reporter.check("Claude marketplace schema", False, "marketplace is not an object")
        return
    if not isinstance(plugin_payload, dict):
        reporter.check("Claude marketplace schema", False, "plugin manifest is not an object")
        return
    errors = claude_marketplace_validation_errors(marketplace_payload, plugin_payload, root=root)
    reporter.check(
        "Claude marketplace schema",
        not errors,
        "valid" if not errors else "; ".join(errors),
    )


def should_skip_dir(root: Path, path: Path) -> bool:
    rel = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    if path.name in SOURCE_SCAN_SKIP_DIRS:
        return True
    if rel in THIRD_PARTY_SOURCE_PREFIXES:
        return True
    return any(prefix in rel.parents for prefix in THIRD_PARTY_SOURCE_PREFIXES)


def iter_source_files(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir():
                if should_skip_dir(root, child):
                    continue
                stack.append(child)
            elif child.is_file():
                yield child


def iter_git_source_files(root: Path) -> Iterator[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return
    if result.returncode != 0:
        return
    for raw_item in result.stdout.split(b"\0"):
        if not raw_item:
            continue
        try:
            rel_path = Path(raw_item.decode("utf-8"))
        except UnicodeDecodeError:
            rel_path = Path(raw_item.decode(sys.getfilesystemencoding(), errors="replace"))
        yield (root / rel_path).resolve(strict=False)


def iter_repo_source_files(root: Path) -> Iterator[Path]:
    git_files = list(iter_git_source_files(root))
    if git_files:
        yield from (path for path in git_files if path.is_file())
        return
    yield from iter_source_files(root)


def validate_no_source_wrappers(root: Path, reporter: Reporter) -> None:
    offenders = [
        display_path(root, path)
        for path in iter_repo_source_files(root)
        if path.suffix.lower() in BANNED_WRAPPER_EXTENSIONS
        and not any(
            is_relative_to_path(path, (root / prefix).resolve(strict=False))
            for prefix in THIRD_PARTY_SOURCE_PREFIXES
        )
    ]
    reporter.check(
        "no source .ps1/.bat/.cmd wrapper scripts",
        not offenders,
        "source tree clean" if not offenders else "; ".join(offenders),
    )


def validate_windows_runtime_contract(root: Path, reporter: Reporter) -> None:
    python_shebangs: list[str] = []
    shell_true_calls: list[str] = []
    non_windows_fences: list[str] = []
    for path in iter_repo_source_files(root):
        if any(
            is_relative_to_path(path, (root / prefix).resolve(strict=False))
            for prefix in THIRD_PARTY_SOURCE_PREFIXES
        ):
            continue
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
        if relative.parts and relative.parts[0] == "scripts" and path.suffix.lower() == ".py":
            text = read_text(path)
            if text.startswith("#!"):
                python_shebangs.append(display_path(root, path))
            tree = ast.parse(text, filename=str(path))
            if any(
                isinstance(node, ast.Call)
                and any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                )
                for node in ast.walk(tree)
            ):
                shell_true_calls.append(display_path(root, path))
        if path.suffix.lower() == ".md":
            if NON_WINDOWS_FENCE_RE.search(read_text(path)):
                non_windows_fences.append(display_path(root, path))

    reporter.check(
        "Python entrypoints do not use Unix shebangs",
        not python_shebangs,
        "Windows Python invocation only" if not python_shebangs else "; ".join(python_shebangs),
    )
    reporter.check(
        "Python subprocesses do not use shell=True",
        not shell_true_calls,
        "argument-vector execution only" if not shell_true_calls else "; ".join(shell_true_calls),
    )
    reporter.check(
        "documentation command fences are PowerShell-specific",
        not non_windows_fences,
        "no Bash/sh/console fences" if not non_windows_fences else "; ".join(non_windows_fences),
    )


def _references_game_id(node: ast.AST) -> bool:
    return any(
        (isinstance(child, ast.Name) and child.id == "game_id")
        or (isinstance(child, ast.Attribute) and child.attr == "game_id")
        for child in ast.walk(node)
    )


def _is_string_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_string_literal(item) for item in node.elts)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        return _is_string_literal(node.args[0])
    return False


def _string_literal_values(node: ast.AST) -> set[str]:
    return {
        str(child.value)
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _references_adapter_selector(node: ast.AST) -> bool:
    return any(
        (
            isinstance(child, ast.Name)
            and "adapter" in child.id.casefold()
        )
        or (
            isinstance(child, ast.Attribute)
            and "adapter" in child.attr.casefold()
        )
        for child in ast.walk(node)
    )


def _assigned_name_targets(node: ast.AST) -> set[str]:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.NamedExpr):
        targets = [node.target]
    return {
        child.id
        for target in targets
        for child in ast.walk(target)
        if isinstance(child, ast.Name)
    }


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        return node.value
    return None


def _is_selector_alias(
    node: ast.AST,
    *,
    aliases: set[str],
    attribute_names: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in attribute_names
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"casefold", "lower", "strip"}
        and not node.args
        and not node.keywords
    ):
        return _is_selector_alias(
            node.func.value,
            aliases=aliases,
            attribute_names=attribute_names,
        )
    return False


def _is_static_string_container(node: ast.AST) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set))
    )


def _has_direct_game_id_condition(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "game_id"
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _has_direct_game_id_condition(node.operand, aliases)
    if isinstance(node, ast.BoolOp):
        return any(_has_direct_game_id_condition(value, aliases) for value in node.values)
    return False


def game_specific_branch_findings(path: Path) -> list[str]:
    """Return concrete game branches forbidden from capability-driven core scripts."""
    tree = ast.parse(read_text(path), filename=str(path))
    findings: list[str] = []
    concrete_game_ids = set(supported_game_ids())
    game_aliases = {"game_id"}
    adapter_aliases = {"adapter_id"}
    game_dispatch_tables: set[str] = set()
    changed = True
    while changed:
        changed = False
        for candidate in ast.walk(tree):
            value = _assignment_value(candidate)
            targets = _assigned_name_targets(candidate)
            if value is None or not targets:
                continue
            direct_game_alias = _is_selector_alias(
                value,
                aliases=game_aliases,
                attribute_names={"game_id"},
            )
            if direct_game_alias:
                before = len(game_aliases)
                game_aliases.update(targets)
                changed = changed or len(game_aliases) != before
            direct_adapter_alias = _is_selector_alias(
                value,
                aliases=adapter_aliases,
                attribute_names={"adapter_id", "plugin_adapter"},
            )
            if direct_adapter_alias:
                before = len(adapter_aliases)
                adapter_aliases.update(targets)
                changed = changed or len(adapter_aliases) != before
            if _is_static_string_container(value) and (
                _string_literal_values(value) & concrete_game_ids
            ):
                game_dispatch_tables.update(targets)

    def references_game_selector(node: ast.AST) -> bool:
        return _references_game_id(node) or any(
            isinstance(child, ast.Name) and child.id in game_aliases
            for child in ast.walk(node)
        )

    def references_adapter_selector(node: ast.AST) -> bool:
        return _references_adapter_selector(node) or any(
            isinstance(child, ast.Name) and child.id in adapter_aliases
            for child in ast.walk(node)
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            literal_values = set().union(*(_string_literal_values(item) for item in operands))
            compares_game_literal = any(references_game_selector(item) for item in operands) and any(
                _is_string_literal(item) for item in operands
            )
            if compares_game_literal and any(
                isinstance(operator, (ast.Eq, ast.NotEq, ast.In, ast.NotIn))
                for operator in node.ops
            ):
                findings.append(f"line {node.lineno}: game_id compared with a literal")
            if literal_values & concrete_game_ids:
                findings.append(f"line {node.lineno}: concrete game id used for dispatch")
            if any(references_game_selector(item) for item in operands) and any(
                isinstance(child, ast.Name) and child.id in game_dispatch_tables
                for item in operands
                for child in ast.walk(item)
            ):
                findings.append(f"line {node.lineno}: game selector used with a dispatch table")
            if any(references_adapter_selector(item) for item in operands) and any(
                _is_string_literal(item) for item in operands
            ):
                findings.append(f"line {node.lineno}: adapter id compared with a literal")
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            if _has_direct_game_id_condition(node.test, game_aliases):
                findings.append(f"line {node.lineno}: context.game_id used directly as a condition")
        if isinstance(node, ast.Match):
            pattern_literals = set().union(
                *(_string_literal_values(case.pattern) for case in node.cases)
            )
            if references_game_selector(node.subject) or references_adapter_selector(node.subject):
                if pattern_literals:
                    findings.append(f"line {node.lineno}: game/adapter selector used in match dispatch")
            elif pattern_literals & concrete_game_ids:
                findings.append(f"line {node.lineno}: concrete game id used in match dispatch")
        if isinstance(node, ast.Dict):
            key_literals = {
                str(key.value)
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if key_literals & concrete_game_ids:
                findings.append(f"line {node.lineno}: concrete game id used as dispatch-table key")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if references_game_selector(node.func.value) and any(
                _is_string_literal(argument) for argument in node.args
            ):
                findings.append(f"line {node.lineno}: game_id method called with a literal")
    return sorted(set(findings))


def validate_game_agnostic_core(root: Path, reporter: Reporter) -> None:
    findings: list[str] = []
    for relative_path in GAME_AGNOSTIC_CORE_SCRIPTS:
        path = root / relative_path
        if not path.is_file():
            findings.append(f"{relative_path.as_posix()}: missing")
            continue
        try:
            path_findings = game_specific_branch_findings(path)
        except (OSError, SyntaxError) as exc:
            findings.append(f"{relative_path.as_posix()}: {exc}")
            continue
        findings.extend(f"{relative_path.as_posix()}: {finding}" for finding in path_findings)
    reporter.check(
        "capability-driven core has no concrete game branches",
        not findings,
        "clean" if not findings else "; ".join(findings),
    )


def validate_mod_scale_profiles(root: Path, payload: Any, reporter: Reporter) -> None:
    if payload is None:
        return
    try:
        load_scale_config(root / MOD_SCALE_PROFILES_JSON)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reporter.check("Mod scale profile schema", False, str(exc))
    else:
        reporter.check("Mod scale profile schema", True, "valid")


def validate_pex_visible_api_registry(root: Path, reporter: Reporter) -> None:
    try:
        load_pex_visible_api_registry(
            root / FALLOUT4_PEX_VISIBLE_APIS_JSON,
            expected_game_id="fallout4",
        )
    except (OSError, ValueError) as exc:
        reporter.check("Fallout 4 PEX visible API registry", False, str(exc))
    else:
        reporter.check("Fallout 4 PEX visible API registry", True, "valid")


def normalize_markdown_target(raw_target: str) -> str:
    target = raw_target.strip()
    if " " in target and not target.startswith("<"):
        target = target.split(" ", 1)[0]
    target = target.strip("<>")
    return unquote(target)


def validate_readme_links(root: Path, reporter: Reporter) -> None:
    readme = root / "README.md"
    if not readme.is_file():
        reporter.check("README exists", False, "README.md")
        return
    broken: list[str] = []
    for match in MARKDOWN_LINK_RE.finditer(read_text(readme)):
        target = normalize_markdown_target(match.group(1))
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        target_path = (root / path_part).resolve(strict=False)
        if not is_relative_to_path(target_path, root) or not target_path.exists():
            broken.append(path_part)
    reporter.check(
        "README local links point to existing paths",
        not broken,
        "local links valid" if not broken else "; ".join(sorted(set(broken))),
    )


def validate_compileall(root: Path, reporter: Reporter) -> None:
    scripts_dir = root / "scripts"
    reporter.check("scripts directory exists", scripts_dir.is_dir(), "scripts")
    if scripts_dir.is_dir():
        passed = compileall.compile_dir(str(scripts_dir), quiet=1)
        reporter.check("python compileall scripts", bool(passed), "compileall.compile_dir")


def requirement_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        text = raw.strip()
        if text and not text.startswith("#"):
            lines.append(text)
    return lines


def validate_python_project_metadata(root: Path, reporter: Reporter) -> None:
    pyproject_path = root / PYPROJECT_TOML
    requirements_path = root / REQUIREMENTS_TXT
    uv_lock_path = root / UV_LOCK
    if not pyproject_path.is_file():
        reporter.check("pyproject.toml exists for uv support", False, "missing")
        return
    try:
        payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        reporter.check("pyproject.toml parses", False, f"{exc}")
        return
    reporter.check("pyproject.toml parses", True)
    project = payload.get("project", {})
    dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        reporter.check("pyproject dependencies schema", False, "project.dependencies must be a string array")
        return
    requirements = requirement_lines(requirements_path)
    reporter.check(
        "pyproject dependencies match requirements.txt",
        sorted(dependencies, key=str.lower) == sorted(requirements, key=str.lower),
        f"{len(dependencies)} dependency entry(s)",
    )
    tool_uv = payload.get("tool", {}).get("uv", {}) if isinstance(payload.get("tool", {}), dict) else {}
    reporter.check("pyproject uv package mode disabled", tool_uv.get("package") is False, "tool.uv.package=false")
    if not uv_lock_path.is_file():
        reporter.check("uv.lock exists for uv support", False, "missing")
        return
    try:
        uv_payload = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        reporter.check("uv.lock parses", False, f"{exc}")
        return
    reporter.check("uv.lock parses", True)
    reporter.check("uv.lock has lockfile version", isinstance(uv_payload.get("version"), int), "version")


def validate_game_profile_adapters(root: Path, reporter: Reporter) -> None:
    profile_dir = root / GAME_PROFILE_DIR
    profile_paths = sorted(profile_dir.glob("*.json"), key=lambda path: path.name.lower())
    reporter.check(
        "game profiles discovered for adapter validation",
        bool(profile_paths),
        f"{len(profile_paths)} profile(s) under {GAME_PROFILE_DIR.as_posix()}",
    )
    previous_plugin_root = os.environ.get(PLUGIN_ROOT_ENV)
    os.environ[PLUGIN_ROOT_ENV] = str(root)
    try:
        for profile_path in profile_paths:
            game_id = profile_path.stem
            try:
                context = load_game_profile(game_id)
                adapter_errors = registry_validate_profile_adapters(context)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                adapter_errors = (f"game_id={game_id}: {exc}",)
            reporter.check(
                f"game profile adapter registry: {game_id}",
                not adapter_errors,
                "valid" if not adapter_errors else "; ".join(adapter_errors),
            )
    finally:
        if previous_plugin_root is None:
            os.environ.pop(PLUGIN_ROOT_ENV, None)
        else:
            os.environ[PLUGIN_ROOT_ENV] = previous_plugin_root


def validate_capability_promotion_gates(
    root: Path,
    payload: Any,
    reporter: Reporter,
) -> None:
    errors = capability_promotion_gate_errors(root, payload)
    reporter.check(
        "advanced capability promotion gates",
        not errors,
        "valid" if not errors else "; ".join(errors),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the repository-only CI contract.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Compatibility flag for CI callers; current checks are strict by default.",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    reporter = Reporter()
    print(f"Repository: {root}")
    if args.strict:
        print("Mode: strict repo-only validation")

    plugin_payload = load_json_file(root, PLUGIN_JSON, reporter)
    claude_marketplace_payload = load_json_file(root, CLAUDE_MARKETPLACE_JSON, reporter)
    claude_plugin_payload = load_json_file(root, CLAUDE_PLUGIN_JSON, reporter)
    policy_payload = load_json_file(root, WORKFLOW_POLICY_JSON, reporter)
    if (root / TOOLS_EXAMPLE_JSON).is_file():
        load_json_file(root, TOOLS_EXAMPLE_JSON, reporter)
    else:
        reporter.check(f"JSON optional: {TOOLS_EXAMPLE_JSON.as_posix()}", True, "not present")
    agent_capabilities_payload = load_json_file(root, AGENT_CAPABILITIES_EXAMPLE_JSON, reporter)
    scale_profiles_payload = load_json_file(root, MOD_SCALE_PROFILES_JSON, reporter)
    validate_mod_scale_profiles(root, scale_profiles_payload, reporter)
    validate_pex_visible_api_registry(root, reporter)
    promotion_gates_payload = load_json_file(root, CAPABILITY_PROMOTION_GATES_JSON, reporter)
    if promotion_gates_payload is not None:
        validate_capability_promotion_gates(root, promotion_gates_payload, reporter)

    manifest_skills_path = validate_plugin_manifest(root, plugin_payload, reporter)
    validate_skills(root, manifest_skills_path, reporter)
    validate_workflow_policy(root, policy_payload, reporter)
    if agent_capabilities_payload is not None:
        validate_agent_capabilities_example(agent_capabilities_payload, reporter)
    validate_claude_marketplace(claude_marketplace_payload, claude_plugin_payload, root, reporter)
    validate_no_external_adapter_task_runner_surface(root, reporter)
    validate_no_legacy_adapter_task_runner_text(root, reporter)
    validate_subagent_claim_contract(reporter)
    validate_no_source_wrappers(root, reporter)
    validate_windows_runtime_contract(root, reporter)
    validate_game_agnostic_core(root, reporter)
    validate_readme_links(root, reporter)
    validate_python_project_metadata(root, reporter)
    validate_game_profile_adapters(root, reporter)
    validate_compileall(root, reporter)

    failed = reporter.failed
    print("")
    if failed:
        print(f"FAIL: {len(failed)} check(s) failed.")
        for result in failed:
            suffix = f" - {result.detail}" if result.detail else ""
            print(f"  - {result.name}{suffix}")
        return 1
    print(f"PASS: {len(reporter.results)} check(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

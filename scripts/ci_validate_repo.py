"""CI-only repository validation for the Skyrim CHS Codex plugin.

The checks in this script are intentionally repo-local and deterministic. They
do not inspect a user's game installation, Mod manager, GUI tools, AppData, or
translation services.
"""

from __future__ import annotations

import argparse
import compileall
import json
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
from claude_plugin_marketplace import config_validation_errors as claude_marketplace_validation_errors


SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
FOUR_PART_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PLUGIN_VERSION_RE = re.compile(rf"(?:{SEMVER_RE.pattern})|(?:{FOUR_PART_VERSION_RE.pattern})")
FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---(?:\s*\r?\n|$)", re.DOTALL)
SCRIPT_REF_RE = re.compile(r"scripts[/\\][A-Za-z0-9_.\-/\\]+?\.py")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
BANNED_WRAPPER_EXTENSIONS = {".ps1", ".bat", ".cmd"}
PLUGIN_JSON = Path(".codex-plugin") / "plugin.json"
CLAUDE_MARKETPLACE_JSON = Path(".claude-plugin") / "marketplace.json"
CLAUDE_PLUGIN_JSON = Path(".claude-plugin") / "plugin.json"
WORKFLOW_POLICY_JSON = Path("config") / "workflow_policy.json"
TOOLS_EXAMPLE_JSON = Path("config") / "tools.example.json"
AGENT_CAPABILITIES_EXAMPLE_JSON = Path("config") / "agent_capabilities.example.json"
PYPROJECT_TOML = Path("pyproject.toml")
REQUIREMENTS_TXT = Path("requirements.txt")
UV_LOCK = Path("uv.lock")
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


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


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


def parse_frontmatter(path: Path) -> dict[str, str] | None:
    text = read_text(path)
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        normalized = value.strip().strip("'\"")
        metadata[key.strip()] = normalized
    return metadata


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
        yield from git_files
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
    validate_readme_links(root, reporter)
    validate_python_project_metadata(root, reporter)
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

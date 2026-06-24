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
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote


SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---(?:\s*\r?\n|$)", re.DOTALL)
SCRIPT_REF_RE = re.compile(r"scripts[/\\][A-Za-z0-9_.\-/\\]+?\.py")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
BANNED_WRAPPER_EXTENSIONS = {".ps1", ".bat", ".cmd"}
PLUGIN_JSON = Path(".codex-plugin") / "plugin.json"
WORKFLOW_POLICY_JSON = Path("config") / "workflow_policy.json"
TOOLS_EXAMPLE_JSON = Path("config") / "tools.example.json"
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


def iter_direct_skill_dirs(root: Path, skill_root: Path) -> list[Path]:
    absolute = root / skill_root
    if not absolute.is_dir():
        return []
    return sorted((item for item in absolute.iterdir() if item.is_dir()), key=lambda item: item.name.lower())


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
    reporter.check("plugin manifest version is SemVer", bool(SEMVER_RE.fullmatch(version)), version)

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
    skill_dirs = iter_direct_skill_dirs(root, rel_skill_root)
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


def iter_tracked_files(root: Path) -> Iterator[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            text=False,
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
    tracked_files = list(iter_tracked_files(root))
    if tracked_files:
        yield from tracked_files
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
    policy_payload = load_json_file(root, WORKFLOW_POLICY_JSON, reporter)
    if (root / TOOLS_EXAMPLE_JSON).is_file():
        load_json_file(root, TOOLS_EXAMPLE_JSON, reporter)
    else:
        reporter.check(f"JSON optional: {TOOLS_EXAMPLE_JSON.as_posix()}", True, "not present")

    manifest_skills_path = validate_plugin_manifest(root, plugin_payload, reporter)
    validate_skills(root, manifest_skills_path, reporter)
    validate_workflow_policy(root, policy_payload, reporter)
    validate_no_source_wrappers(root, reporter)
    validate_readme_links(root, reporter)
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

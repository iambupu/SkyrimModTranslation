"""Audit final_mod Interface/translations TXT files using the active Game Profile.

Interface translation tables are runtime resources, not generic text files.
Their encoding policy comes from GameContext and their rows stay tab-separated.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from project_paths import project_root
from route_translation_task import current_game_context


@dataclass
class Issue:
    Severity: str
    File: str
    Message: str


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def resolve_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
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


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def is_interface_translation(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    return (
        path.suffix.lower() == ".txt"
        and len(parts) >= 3
        and parts[-3] == "interface"
        and parts[-2] == "translations"
    )


def audit_file(final_mod: Path, path: Path, encoding_policy: str) -> tuple[int, list[Issue]]:
    issues: list[Issue] = []
    relative = str(path.relative_to(final_mod)).replace("/", "\\")
    data = path.read_bytes()
    if encoding_policy != "utf-16-le-bom":
        raise ValueError(f"Unsupported interface_translation_encoding policy: {encoding_policy}")
    if not data.startswith(b"\xff\xfe"):
        issues.append(Issue("error", relative, "Interface translation file must be UTF-16 LE with BOM."))
        return 0, issues
    try:
        text = data.decode("utf-16")
    except UnicodeError as exc:
        issues.append(Issue("error", relative, f"Interface translation file is not valid UTF-16: {exc}"))
        return 0, issues
    lines = text.splitlines()
    if not lines:
        issues.append(Issue("error", relative, "Interface translation file is empty."))
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if "\t" not in line:
            issues.append(Issue("error", relative, f"Line {line_number} has no tab separator."))
            continue
        key, value = line.split("\t", 1)
        if not key.startswith("$"):
            issues.append(Issue("error", relative, f"Line {line_number} key does not start with '$'."))
        if value == "":
            issues.append(Issue("warning", relative, f"Line {line_number} has an empty translation value."))
    return len(lines), issues


def write_report(
    root: Path,
    mod_name: str,
    final_mod: Path,
    report_path: Path,
    files: list[Path],
    line_counts: dict[str, int],
    issues: list[Issue],
    game_id: str,
    encoding_policy: str,
) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    lines = [
        "# Final Interface Translation Runtime Audit",
        "",
        f"- ModName: {mod_name}",
        f"- GameId: {game_id}",
        f"- Encoding policy: {encoding_policy}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- FinalModDir: {relative_path(root, final_mod)}",
        f"- Interface translation files checked: {len(files)}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Verdict",
        "",
        "PASS: Interface translation runtime files are loadable." if blocking == 0 else "FAIL: Interface translation runtime files have blocking issues.",
        "",
        "## Files",
        "",
    ]
    if not files:
        lines.append("No Interface/translations TXT files found in final_mod.")
    else:
        lines.extend(["| File | Lines |", "|---|---:|"])
        for file_path in files:
            rel = str(file_path.relative_to(final_mod)).replace("/", "\\")
            lines.append(f"| {markdown_cell(rel)} | {line_counts.get(rel, 0)} |")
    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No runtime Interface translation issues.")
    else:
        lines.extend(["| Severity | File | Message |", "|---|---|---|"])
        for issue in issues:
            lines.append(f"| {issue.Severity} | {markdown_cell(issue.File)} | {markdown_cell(issue.Message)} |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This audit is read-only.",
            "- This audit only reads project-local final_mod files.",
            "- This audit does not access real game, Steam, MO2/Vortex, AppData, or Documents/My Games paths.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit final_mod Interface/translations files using the active Game Profile.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--final-mod-dir", required=True)
    parser.add_argument("--report-output-path", default="")
    args = parser.parse_args()

    root = project_root()
    context = current_game_context(root)
    final_mod = resolve_project_path(root, args.final_mod_dir, must_exist=True)
    if not final_mod.is_dir():
        raise ValueError(f"FinalModDir must be a directory: {args.final_mod_dir}")
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{args.mod_name}.final_interface_runtime.md", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    files = sorted(path for path in final_mod.rglob("*.txt") if path.is_file() and is_interface_translation(path))
    line_counts: dict[str, int] = {}
    issues: list[Issue] = []
    for file_path in files:
        count, file_issues = audit_file(final_mod, file_path, context.interface_translation_encoding)
        line_counts[str(file_path.relative_to(final_mod)).replace("/", "\\")] = count
        issues.extend(file_issues)
    write_report(
        root,
        args.mod_name,
        final_mod,
        report_path,
        files,
        line_counts,
        issues,
        context.game_id,
        context.interface_translation_encoding,
    )

    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Final Interface translation runtime audit written to: {report_path}")
    print(f"Interface translation files checked: {len(files)}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())

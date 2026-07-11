"""Create an agent model-review prompt packet from translation intermediates."""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from project_paths import project_root
from update_model_review_contract import build_contract_block


SOURCE_FIELDS = ("Source", "source", "original", "Original", "text", "Text")
TARGET_FIELDS = ("Result", "result", "Target", "target", "translation", "Dest", "dest")
RISK_FIELDS = ("risk", "Risk")
TYPE_FIELDS = ("Type", "type", "record_type", "RecordType")
CONTEXT_FIELDS = ("function_name", "editor_id", "EditorID", "subrecord_type", "SubrecordType", "reason", "notes")


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
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True))).replace("\\", "/")
    except ValueError:
        return str(value)


def json_value_any(row: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value is not None:
            return str(value)
    return ""


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def read_input_list(root: Path, input_paths: list[str], input_list_path: str) -> list[str]:
    effective = [item for item in input_paths if item.strip()]
    if input_list_path.strip():
        list_path = resolve_project_path(root, input_list_path, must_exist=True)
        for line in list_path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if stripped:
                effective.append(stripped)
    if not effective:
        raise ValueError("At least one --input-path or --input-list-path entry is required.")
    return effective


def iter_json_files(root: Path, input_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for value in input_paths:
        path = resolve_project_path(root, value, must_exist=True)
        if path.is_dir():
            files.extend(sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in {".jsonl", ".json"}))
        else:
            files.append(path)
    return files


def collect_rows(root: Path, files: list[Path], include_protected_rows: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for file in files:
        line_number = 0
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            line_number += 1
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            risk = json_value_any(row, RISK_FIELDS)
            source = json_value_any(row, SOURCE_FIELDS)
            target = json_value_any(row, TARGET_FIELDS)
            if not include_protected_rows and risk.lower() in {"protected", "protected-logic"} and not target.strip():
                continue
            if not source.strip() and not target.strip():
                continue
            context_values: list[str] = []
            for field in CONTEXT_FIELDS:
                value = json_value_any(row, (field,))
                if value.strip():
                    context_values.append(f"{field}={value}")
            rows.append(
                {
                    "File": relative_path(root, file),
                    "Line": line_number,
                    "Type": json_value_any(row, TYPE_FIELDS),
                    "Risk": risk,
                    "Context": "; ".join(context_values),
                    "Source": source,
                    "Target": target,
                }
            )
    return rows


def write_packet(root: Path, mod_name: str, output_path: Path, review_path: Path, rows: list[dict[str, object]]) -> None:
    lines = [
        f"# Model Review Packet: {mod_name}",
        "",
        f"- Created at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Rows for agent model review: {len(rows)}",
        f"- Review output: {relative_path(root, review_path)}",
        "",
        "## Review Instructions",
        "",
        "The reviewing agent must use model judgment here. Do not treat regex/script checks as semantic proof.",
        "",
        "Check:",
        "",
        "- Whether the Chinese is natural Simplified Chinese game localization.",
        "- Whether UI/MCM text is short, clear, and not wordy.",
        "- Whether fantasy/game terms are consistent.",
        "- Whether anything protected was translated by mistake.",
        "- Whether English should intentionally remain, such as mod/tool names, plugin names, acronyms, or filenames.",
        "- Whether concatenated PEX fragments still read naturally when combined.",
        "",
        "The review output must include these exact final claims when the review passes:",
        "",
        "- `No runtime-impacting issues remain`",
        "- `No required translation candidates remain untranslated`",
        "- `No semantic quality blockers remain`",
        "- `All changed final_mod files listed in the review packets were reviewed`",
        "",
        "Write findings to the review output with severity, file, line, issue, and proposed target.",
        "",
        "## Rows",
        "",
        "| File | Line | Type | Risk | Context | Source | Target |",
        "|---|---:|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(row["File"]),
                    str(row["Line"]),
                    markdown_cell(row["Type"]),
                    markdown_cell(row["Risk"]),
                    markdown_cell(row["Context"]),
                    markdown_cell(row["Source"]),
                    markdown_cell(row["Target"]),
                ]
            )
            + " |"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_review_template(root: Path, mod_name: str, output_path: Path, review_path: Path) -> None:
    if review_path.exists():
        return
    review = [
        f"# Model Translation Review: {mod_name}",
        "",
        "- Reviewed at: TODO",
        "- Reviewer: Agent model",
        f"- Packet: {relative_path(root, output_path)}",
        "",
        build_contract_block(root, mod_name),
        "",
        "## Verdict",
        "",
        "TODO",
        "",
        "Required final claims when passing:",
        "",
        "- No runtime-impacting issues remain",
        "- No required translation candidates remain untranslated",
        "- No semantic quality blockers remain",
        "- All changed final_mod files listed in the review packets were reviewed",
        "- Mechanical checks do not replace agent model semantic review",
        "- Final review quality audit has 0 blocking issues and 0 warnings",
        "",
        "## Findings",
        "",
        "| Severity | File | Line | Issue | Proposed target |",
        "|---|---|---:|---|---|",
        "",
        "## Notes",
        "",
        "TODO",
    ]
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("\n".join(review) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an agent model review packet from project-local JSONL translation files.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--input-path", action="append", default=[])
    parser.add_argument("--input-list-path", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--review-output-path", default="")
    parser.add_argument("--include-protected-rows", action="store_true")
    args = parser.parse_args()

    root = project_root()
    output_path = resolve_project_path(root, args.output_path or f"qa/{args.mod_name}.model_review_packet.md", must_exist=False)
    review_path = resolve_project_path(root, args.review_output_path or f"qa/{args.mod_name}.model_review.md", must_exist=False)
    if not is_under(output_path, root / "qa") or not is_under(review_path, root / "qa"):
        raise ValueError("Output paths must be under qa/.")

    input_paths = read_input_list(root, args.input_path, args.input_list_path)
    files = iter_json_files(root, input_paths)
    rows = collect_rows(root, files, args.include_protected_rows)
    write_packet(root, args.mod_name, output_path, review_path, rows)
    write_review_template(root, args.mod_name, output_path, review_path)

    print(f"Model review packet written to: {output_path}")
    print(f"Model review output path: {review_path}")
    print(f"Rows: {len(rows)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Model review packet failed: {exc}", file=sys.stderr)
        sys.exit(1)

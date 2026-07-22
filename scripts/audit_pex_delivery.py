"""Audit PEX writeback evidence before and after final_mod assembly.

The script is read-only for PEX binaries. It checks that translation JSONL rows
produce changed project-local tool outputs, and that post-build final_mod files
match the tool_outputs copies byte-for-byte.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from project_paths import project_root
from project_paths import resolve_project_path, relative_windows_path as relative_path
from file_utils import discover_regular_files, sha256_file_upper as sha256_file
from pex_translation_safety import pex_row_is_writable_candidate, pex_translation_skip_reason
from report_utils import markdown_cell
from translation_text import row_value


@dataclass
class DeliveryRow:
    Script: str
    TranslationJsonl: str
    RowsParsed: int
    TranslatedRows: int
    OriginalPex: str
    ToolOutputPex: str
    FinalModPex: str
    OriginalSHA256: str
    ToolOutputSHA256: str
    FinalModSHA256: str
    ToolOutputHashChanged: bool | None
    FinalMatchesToolOutput: bool | None
    Status: str
    Message: str









def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            rows.append({"_invalid": f"line {line_number}: {exc}"})
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            rows.append({"_invalid": f"line {line_number}: JSONL row is not an object"})
    return rows



def translated_row_summary(rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
    count = 0
    unauthorized: list[str] = []
    for index, row in enumerate(rows, start=1):
        source = row_value(row, "Source", "source")
        target = row_value(row, "Target", "target", "Result", "result", "translation")
        if not source.strip() or not target.strip() or source == target:
            continue
        if pex_row_is_writable_candidate(row):
            count += 1
            continue
        unauthorized.append(
            f"row {index}: {pex_translation_skip_reason(row) or 'not authorized by the PEX writeback contract'}"
        )
    return count, unauthorized


def pex_map(workspace: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not workspace.is_dir():
        return result
    for pex in discover_regular_files(workspace, label="PEX delivery workspace"):
        if pex.suffix.casefold() != ".pex":
            continue
        result.setdefault(pex.stem.lower(), pex)
    return result


def translation_inputs(root: Path, mod_name: str) -> list[Path]:
    pex_apply = root / "work" / "normalized" / mod_name / "pex_apply"
    if not pex_apply.is_dir():
        return []
    return sorted(pex_apply.glob("*.translation.jsonl"), key=lambda item: item.name.lower())


def expected_tool_output_roots(root: Path, mod_name: str) -> list[Path]:
    return [
        root / "out" / mod_name / "tool_outputs",
        root / "translated" / "tool_outputs" / mod_name,
    ]


def tool_output_files(root: Path, mod_name: str) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for root_dir in expected_tool_output_roots(root, mod_name):
        if not root_dir.is_dir():
            continue
        for path in discover_regular_files(root_dir, label="PEX tool output directory"):
            if path.suffix.casefold() != ".pex":
                continue
            files.append((root_dir, path))
    return files


def expected_outputs_for_translation(root: Path, mod_name: str, workspace: Path, original_pex: Path) -> list[Path]:
    rel_pex = original_pex.resolve(strict=False).relative_to(workspace.resolve(strict=True))
    return [tool_root / rel_pex for tool_root in expected_tool_output_roots(root, mod_name)]


def select_tool_output_for_translation(root: Path, mod_name: str, workspace: Path, original_pex: Path) -> Path:
    candidates = expected_outputs_for_translation(root, mod_name, workspace, original_pex)
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if not existing:
        return candidates[0]
    return max(existing, key=lambda candidate: candidate.stat().st_mtime)


def add_translation_rows(root: Path, mod_name: str, workspace: Path, rows: list[DeliveryRow]) -> None:
    originals = pex_map(workspace)
    for translation in translation_inputs(root, mod_name):
        script = translation.name.removesuffix(".translation.jsonl")
        parsed = read_jsonl(translation)
        invalid_rows = [row for row in parsed if "_invalid" in row]
        translated, unauthorized = translated_row_summary(parsed)
        original = originals.get(script.lower())
        if original is None:
            rows.append(
                DeliveryRow(
                    Script=script,
                    TranslationJsonl=relative_path(root, translation),
                    RowsParsed=len(parsed),
                    TranslatedRows=translated,
                    OriginalPex="",
                    ToolOutputPex="",
                    FinalModPex="",
                    OriginalSHA256="",
                    ToolOutputSHA256="",
                    FinalModSHA256="",
                    ToolOutputHashChanged=None,
                    FinalMatchesToolOutput=None,
                    Status="blocking",
                    Message="Translation JSONL exists, but matching original PEX was not found in the workspace.",
                )
            )
            continue

        output = select_tool_output_for_translation(root, mod_name, workspace, original)
        original_hash = sha256_file(original)
        output_hash = sha256_file(output) if output.is_file() else ""
        hash_changed = output_hash != "" and output_hash != original_hash
        status = "ok"
        messages: list[str] = []
        if invalid_rows:
            status = "blocking"
            messages.append(f"{len(invalid_rows)} invalid JSONL row(s)")
        if unauthorized:
            status = "blocking"
            messages.append(
                f"{len(unauthorized)} translated row(s) are not authorized for PEX writeback: "
                + "; ".join(unauthorized[:3])
            )
        if translated > 0 and not output.is_file():
            status = "blocking"
            messages.append("translated rows exist, but tool output PEX is missing")
        if translated > 0 and output.is_file() and not hash_changed:
            status = "blocking"
            messages.append("translated rows exist, but tool output PEX hash is unchanged")
        if translated == 0:
            status = "warning" if status == "ok" else status
            messages.append("no changed translation rows were parsed")
        rows.append(
            DeliveryRow(
                Script=script,
                TranslationJsonl=relative_path(root, translation),
                RowsParsed=len(parsed),
                TranslatedRows=translated,
                OriginalPex=relative_path(root, original),
                ToolOutputPex=relative_path(root, output),
                FinalModPex="",
                OriginalSHA256=original_hash,
                ToolOutputSHA256=output_hash,
                FinalModSHA256="",
                ToolOutputHashChanged=hash_changed if output.is_file() else None,
                FinalMatchesToolOutput=None,
                Status=status,
                Message="; ".join(messages) if messages else "pre-build PEX delivery evidence is complete",
            )
        )


def add_post_build_rows(root: Path, mod_name: str, final_mod: Path, rows: list[DeliveryRow]) -> None:
    existing_keys = {(row.Script.lower(), row.ToolOutputPex.lower()) for row in rows if row.ToolOutputPex}
    for tool_root, tool_output in tool_output_files(root, mod_name):
        relative = tool_output.resolve(strict=True).relative_to(tool_root.resolve(strict=True))
        final_pex = final_mod / relative
        key = (tool_output.stem.lower(), relative_path(root, tool_output).lower())
        tool_hash = sha256_file(tool_output)
        final_hash = sha256_file(final_pex) if final_pex.is_file() else ""
        matches = final_hash != "" and final_hash == tool_hash
        status = "ok"
        message = "tool output PEX was copied into final_mod with matching SHA256"
        if not final_pex.is_file():
            status = "blocking"
            message = "tool output PEX is missing from final_mod at the same relative path"
        elif not matches:
            status = "blocking"
            message = "final_mod PEX SHA256 does not match the tool output PEX"

        if key in existing_keys:
            for row in rows:
                if row.Script.lower() == key[0] and row.ToolOutputPex.lower() == key[1]:
                    row.FinalModPex = relative_path(root, final_pex)
                    row.FinalModSHA256 = final_hash
                    row.FinalMatchesToolOutput = matches
                    if status == "blocking":
                        row.Status = status
                        row.Message = f"{row.Message}; {message}"
                    else:
                        row.Message = f"{row.Message}; {message}"
                    break
            continue

        rows.append(
            DeliveryRow(
                Script=tool_output.stem,
                TranslationJsonl="",
                RowsParsed=0,
                TranslatedRows=0,
                OriginalPex="",
                ToolOutputPex=relative_path(root, tool_output),
                FinalModPex=relative_path(root, final_pex),
                OriginalSHA256="",
                ToolOutputSHA256=tool_hash,
                FinalModSHA256=final_hash,
                ToolOutputHashChanged=None,
                FinalMatchesToolOutput=matches,
                Status=status,
                Message=message,
            )
        )



def write_reports(root: Path, mod_name: str, phase: str, rows: list[DeliveryRow]) -> tuple[Path, Path, int, int]:
    blocking = sum(1 for row in rows if row.Status == "blocking")
    warnings = sum(1 for row in rows if row.Status == "warning")
    report_path = root / "qa" / f"{mod_name}.pex_delivery_{phase.replace('-', '_')}.md"
    json_path = report_path.with_suffix(".json")
    lines = [
        "# PEX Delivery Audit",
        "",
        f"- ModName: {mod_name}",
        f"- Phase: {phase}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Rows checked: {len(rows)}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Rows",
        "",
        "| Script | Rows parsed | Translated rows | Tool output hash changed | Final matches tool output | Status | Message |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(row.Script),
                    str(row.RowsParsed),
                    str(row.TranslatedRows),
                    markdown_cell(row.ToolOutputHashChanged),
                    markdown_cell(row.FinalMatchesToolOutput),
                    markdown_cell(row.Status),
                    markdown_cell(row.Message),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Evidence",
            "",
            "| Script | Translation JSONL | Original PEX | Tool output PEX | Final PEX |",
            "|---|---|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(row.Script),
                    markdown_cell(row.TranslationJsonl),
                    markdown_cell(row.OriginalPex),
                    markdown_cell(row.ToolOutputPex),
                    markdown_cell(row.FinalModPex),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script only reads project-local PEX files and writes QA reports.",
            "- This script does not edit, decompile, recompile, or save PEX binaries.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {
        "ModName": mod_name,
        "Phase": phase,
        "CheckedAt": datetime.now().isoformat(timespec="seconds"),
        "RowsChecked": len(rows),
        "BlockingIssues": blocking,
        "Warnings": warnings,
        "Rows": [asdict(row) for row in rows],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path, json_path, blocking, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit PEX delivery evidence around final_mod assembly.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", required=True)
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--phase", choices=["pre-build", "post-build"], required=True)
    args = parser.parse_args()

    root = project_root()
    workspace = find_data_root(resolve_project_path(root, args.workspace_path, must_exist=True)).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir, must_exist=False) if args.final_mod_dir else default_final_mod_dir(root, args.mod_name)
    if args.phase == "post-build" and not final_mod.is_dir():
        raise FileNotFoundError(f"final_mod directory is required for post-build audit: {relative_path(root, final_mod)}")

    rows: list[DeliveryRow] = []
    add_translation_rows(root, args.mod_name, workspace, rows)
    if args.phase == "post-build":
        add_post_build_rows(root, args.mod_name, final_mod, rows)
    if not rows:
        rows.append(
            DeliveryRow(
                Script="",
                TranslationJsonl="",
                RowsParsed=0,
                TranslatedRows=0,
                OriginalPex="",
                ToolOutputPex="",
                FinalModPex="",
                OriginalSHA256="",
                ToolOutputSHA256="",
                FinalModSHA256="",
                ToolOutputHashChanged=None,
                FinalMatchesToolOutput=None,
                Status="ok",
                Message="no PEX translation JSONL or tool output PEX files were found",
            )
        )

    report_path, json_path, blocking, warnings = write_reports(root, args.mod_name, args.phase, rows)
    print(f"PEX delivery audit written to: {report_path}")
    print(f"PEX delivery audit JSON written to: {json_path}")
    if blocking:
        print(f"PEX delivery audit found {blocking} blocking issue(s).")
        return 1
    if warnings:
        print(f"PEX delivery audit found {warnings} warning(s).")
    else:
        print("PEX delivery audit passed with no warnings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

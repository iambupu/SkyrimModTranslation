"""Verify project-local PEX output with byte-level string probes.

This catches missing translated strings and unchanged source strings quickly.
It is not a Papyrus behavior proof; final gates still require PEX re-read
exports, model review, and player-operated runtime testing when desired.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from game_context import GameContext, load_game_context, load_game_profile
from pex_translation_safety import SOURCE_FIELDS, TARGET_FIELDS, pex_translation_skip_reason, row_value
from project_paths import project_root


@dataclass
class ProbeRow:
    Source: str
    Target: str
    SourcePresentInOutput: bool
    TargetPresentInOutput: bool
    TargetCjkTokenPresentInOutput: bool


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
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True))).replace("/", "\\")
    except ValueError:
        return str(value).replace("/", "\\")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def has_byte_pattern(data: bytes, pattern: bytes) -> bool:
    return bool(pattern) and data.find(pattern) >= 0


def text_variants(text: str) -> list[str]:
    # Translation rows may represent newlines as literal escapes or actual line
    # breaks. Probe both encodings before deciding a string is absent.
    if not text:
        return []
    variants: list[str] = []
    candidates = [
        text,
        text.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\r", "\r"),
        text.replace("\r\n", "\\r\\n").replace("\n", "\\n").replace("\r", "\\r"),
    ]
    for candidate in candidates:
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def text_in_bytes(data: bytes, text: str) -> bool:
    for variant in text_variants(text):
        if has_byte_pattern(data, variant.encode("utf-8")) or has_byte_pattern(data, variant.encode("utf-16-le")):
            return True
    return False


def cjk_tokens(text: str) -> list[str]:
    if not text.strip():
        return []
    tokens: list[str] = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        token = match.group(0)
        if token not in tokens:
            tokens.append(token)
    return tokens


def any_cjk_token_in_bytes(data: bytes, text: str) -> bool:
    # Some PEX writers may normalize or split strings; a CJK token probe gives a
    # weaker but useful signal when the complete target string is hard to match.
    return any(text_in_bytes(data, token) for token in cjk_tokens(text))


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def resolve_game_context(root: Path, explicit_game: str) -> GameContext:
    marker_exists = (root / ".skyrim-chs-workspace.json").is_file()
    marker_context = load_game_context(root) if marker_exists else load_game_profile("skyrim-se")
    if marker_exists and explicit_game and explicit_game != marker_context.game_id:
        raise ValueError(
            f"explicit game '{explicit_game}' conflicts with workspace marker game '{marker_context.game_id}'"
        )
    return load_game_profile(explicit_game) if explicit_game else marker_context


def ensure_distinct_paths(paths: list[tuple[str, Path]]) -> None:
    for left_index, (left_label, left_path) in enumerate(paths):
        for right_label, right_path in paths[left_index + 1 :]:
            same_path = left_path == right_path
            if not same_path and left_path.exists() and right_path.exists():
                try:
                    same_path = os.path.samefile(left_path, right_path)
                except OSError:
                    same_path = False
            if same_path:
                raise ValueError(
                    f"path collision: {left_label} and {right_label} must use distinct paths"
                )


def report_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^-\s+([^:]+):\s*(.*)$", line)
        if match:
            values[match.group(1).strip().lower()] = match.group(2).strip()
    return values


def validate_experimental_apply_report(
    path: Path | None,
    context: GameContext,
    root: Path,
    original: Path,
    output: Path,
    translation_jsonl: Path,
    issues: list[str],
) -> None:
    if path is None or not path.is_file():
        issues.append("Fallout 4 experimental PEX verification requires the Apply report.")
        return
    values = report_values(path)
    expected = {
        "game_id": context.game_id,
        "pex_category": context.pex_category,
        "writeback_status": "experimental",
        "experimental_opt_in": "True",
        "validation errors": "0",
        "conflicting source rows": "0",
        "missing usable rows": "0",
        "structure preserved": "True",
        "output published": "True",
    }
    for key, value in expected.items():
        actual = values.get(key, "")
        if actual.lower() != value.lower():
            issues.append(f"Experimental Apply report field '{key}' must be '{value}', found '{actual}'.")
    expected_paths = {
        "input pex": relative_path(root, original),
        "output pex": relative_path(root, output),
        "translation jsonl": relative_path(root, translation_jsonl),
    }
    for key, expected_path in expected_paths.items():
        actual_path = values.get(key, "")
        normalized_actual = actual_path.replace("\\", "/").lstrip("./").casefold()
        normalized_expected = expected_path.replace("\\", "/").lstrip("./").casefold()
        if normalized_actual != normalized_expected:
            issues.append(
                f"Experimental Apply report field '{key}' refers to '{actual_path}', "
                f"expected '{expected_path}'."
            )
    expected_hashes = {
        "input sha256": sha256_file(original),
        "translation jsonl sha256": sha256_file(translation_jsonl),
        "output sha256": sha256_file(output),
    }
    for key, expected_hash in expected_hashes.items():
        actual_hash = values.get(key, "")
        if re.fullmatch(r"[0-9A-Fa-f]{64}", actual_hash) is None:
            issues.append(
                f"Experimental Apply report field '{key}' must be a 64-character SHA256 hash."
            )
        elif actual_hash.upper() != expected_hash.upper():
            issues.append(
                f"Experimental Apply report field '{key}' SHA256 does not match the current file."
            )
    for label in ("objects", "states", "functions", "instructions"):
        input_value = values.get(f"input {label}", "")
        output_value = values.get(f"output {label}", "")
        if not input_value or input_value != output_value:
            issues.append(
                f"Experimental Apply report structure count mismatch for {label}: "
                f"input='{input_value}' output='{output_value}'."
            )


def parse_translation_jsonl(path: Path, output_bytes: bytes, issues: list[str]) -> tuple[list[ProbeRow], int, int]:
    rows: list[ProbeRow] = []
    total_rows = 0
    skipped_rows = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        total_rows += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"Invalid JSONL at line {line_number}: {exc}")
            continue
        if not isinstance(row, dict):
            continue
        source = row_value(row, *SOURCE_FIELDS)
        target = row_value(row, *TARGET_FIELDS)
        if not source.strip() and not target.strip():
            continue
        skip_reason = pex_translation_skip_reason(row)
        if skip_reason:
            skipped_rows += 1
            continue
        rows.append(
            ProbeRow(
                source,
                target,
                text_in_bytes(output_bytes, source),
                text_in_bytes(output_bytes, target),
                any_cjk_token_in_bytes(output_bytes, target),
            )
        )
    return rows, skipped_rows, total_rows


def write_report(
    root: Path,
    original: Path,
    output: Path,
    translation_jsonl: Path,
    report: Path,
    original_hash: str,
    output_hash: str,
    hash_changed: bool,
    rows: list[ProbeRow],
    skipped_rows: int,
    total_rows: int,
    issues: list[str],
    warnings: list[str],
    parse_check_jsonl: Path | None,
    parse_check_report: Path | None,
    parse_check_error: str,
    context: GameContext,
    apply_report: Path | None,
    independent_report: Path | None,
    independent_error: str,
) -> None:
    original_item = original.stat()
    output_item = output.stat()
    lines: list[str] = [
        "# PEX Output Verification",
        "",
        f"- game_id: {context.game_id}",
        f"- pex_category: {context.pex_category}",
        f"- pex_writeback_status: {context.pex_writeback_status}",
        f"- Original: {relative_path(root, original)}",
        f"- Output: {relative_path(root, output)}",
        f"- TranslationJsonlPath: {relative_path(root, translation_jsonl)}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Original SHA256: {original_hash}",
        f"- Output SHA256: {output_hash}",
        f"- Hash changed: {hash_changed}",
        f"- Original size: {original_item.st_size}",
        f"- Output size: {output_item.st_size}",
        f"- Output parseable: {not parse_check_error}",
        f"- Output parse check JSONL: {relative_path(root, parse_check_jsonl) if parse_check_jsonl else ''}",
        f"- Output parse check report: {relative_path(root, parse_check_report) if parse_check_report else ''}",
        f"- Apply report: {relative_path(root, apply_report) if apply_report else ''}",
        f"- Apply report SHA256: {sha256_file(apply_report) if apply_report and apply_report.is_file() else ''}",
        f"- Independent PEX verification report: {relative_path(root, independent_report) if independent_report else ''}",
        f"- Independent PEX verification passed: {bool(independent_report) and not independent_error}",
        f"- Rows parsed: {total_rows}",
        f"- Rows checked: {len(rows)}",
        f"- Rows skipped as protected or non-writable: {skipped_rows}",
        "",
        "## Translation String Probe",
        "",
    ]
    if not rows:
        if total_rows > 0:
            lines.append("No writable rows were checked; all parsed rows were skipped as protected or non-writable.")
        else:
            lines.append("No rows were parsed.")
    else:
        lines.extend(["| Source | Target | Source present | Target present | Target CJK token present |", "|---|---|---:|---:|---:|"])
        for row in rows:
            lines.append(
                f"| {markdown_cell(row.Source)} | {markdown_cell(row.Target)} | {row.SourcePresentInOutput} | {row.TargetPresentInOutput} | {row.TargetCjkTokenPresentInOutput} |"
            )

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No blocking issues.")
    else:
        lines.extend(f"- {issue}" for issue in issues)

    lines.extend(["", "## Warnings", ""])
    if not warnings:
        lines.append("No warnings.")
    else:
        lines.extend(f"- {warning}" for warning in warnings)

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script only read project-local PEX files.",
            "- This script did not modify PEX binaries.",
            "- This script did not decompile, compile, patch, or save scripts.",
            "- This script did not access real Skyrim/Fallout 4, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
        ]
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def output_parse_check_paths(root: Path, output: Path, game: str) -> tuple[Path, Path]:
    path_key = hashlib.sha256(
        "\0".join(
            [
                game,
                str(output.resolve(strict=False)).casefold(),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", output.stem)
    output_jsonl = (
        root
        / "source"
        / "pex_exports"
        / "_verification"
        / f"{safe_stem}.{path_key}.pex_strings.jsonl"
    )
    report = root / "qa" / "_pex_parse_checks" / f"{safe_stem}.{path_key}.md"
    return (
        resolve_project_path(root, str(output_jsonl), must_exist=False),
        resolve_project_path(root, str(report), must_exist=False),
    )


def verify_output_parseable(
    root: Path,
    output: Path,
    game: str,
    output_jsonl: Path | None = None,
    parse_report: Path | None = None,
) -> tuple[Path, Path, str]:
    planned_jsonl, planned_report = output_parse_check_paths(root, output, game)
    output_jsonl = output_jsonl or planned_jsonl
    parse_report = parse_report or planned_report
    ensure_distinct_paths(
        [
            ("output PEX", output),
            ("output parse check JSONL", output_jsonl),
            ("output parse check report", parse_report),
        ]
    )

    script = Path(__file__).resolve().with_name("invoke_mutagen_pex_string_tool.py")
    command = [
        sys.executable,
        str(script),
        "--mode",
        "Export",
        "--game",
        game,
        "--input-pex-path",
        relative_path(root, output),
        "--output-jsonl-path",
        relative_path(root, output_jsonl),
        "--report-path",
        relative_path(root, parse_report),
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return output_jsonl, parse_report, ""
    error = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    return output_jsonl, parse_report, error or f"PEX parse check failed with exit code {completed.returncode}."


def independent_verification_report_path(
    root: Path,
    original: Path,
    output: Path,
    translation_jsonl: Path,
    game: str,
) -> Path:
    evidence_key = hashlib.sha256(
        "\0".join(
            [
                game,
                str(original.resolve(strict=False)).casefold(),
                str(output.resolve(strict=False)).casefold(),
                str(translation_jsonl.resolve(strict=False)).casefold(),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", output.stem)
    report = root / "qa" / "_pex_independent_checks" / f"{safe_stem}.{evidence_key}.md"
    return resolve_project_path(root, str(report), must_exist=False)


def verify_output_independently(
    root: Path,
    original: Path,
    output: Path,
    translation_jsonl: Path,
    game: str,
    report: Path | None = None,
) -> tuple[Path, str]:
    report = report or independent_verification_report_path(
        root, original, output, translation_jsonl, game
    )
    ensure_distinct_paths(
        [
            ("original PEX", original),
            ("output PEX", output),
            ("translation JSONL", translation_jsonl),
            ("independent verification report", report),
        ]
    )
    script = Path(__file__).resolve().with_name("invoke_mutagen_pex_string_tool.py")
    command = [
        sys.executable,
        str(script),
        "--mode",
        "Verify",
        "--game",
        game,
        "--input-pex-path",
        relative_path(root, original),
        "--output-pex-path",
        relative_path(root, output),
        "--translation-jsonl-path",
        relative_path(root, translation_jsonl),
        "--report-path",
        relative_path(root, report),
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return report, ""
    error = "\n".join(
        part for part in [completed.stdout.strip(), completed.stderr.strip()] if part
    ).strip()
    return report, error or f"independent PEX verification failed with exit code {completed.returncode}."


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a project-local PEX output contains expected translated strings.")
    parser.add_argument("--original-pex-path", required=True)
    parser.add_argument("--output-pex-path", required=True)
    parser.add_argument("--translation-jsonl-path", required=True)
    parser.add_argument("--report-output-path", default="qa/pex_output_verification.md")
    parser.add_argument("--apply-report-path", default="")
    parser.add_argument("--game", choices=("skyrim-se", "fallout4"), default="")
    parser.add_argument("--allow-unchanged", action="store_true")
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()

    root = project_root()
    original = resolve_project_path(root, args.original_pex_path, must_exist=False)
    output = resolve_project_path(root, args.output_pex_path, must_exist=False)
    translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=False)
    report = resolve_project_path(root, args.report_output_path, must_exist=False)
    apply_report = (
        resolve_project_path(root, args.apply_report_path, must_exist=False)
        if args.apply_report_path
        else None
    )
    if not (is_under(report, root / "qa") or is_under(report, root / "out")):
        raise ValueError(f"ReportOutputPath must be under qa/ or out/: {args.report_output_path}")
    if report.suffix.lower() != ".md":
        raise ValueError(f"ReportOutputPath must be .md: {args.report_output_path}")
    if original.suffix.lower() != ".pex":
        raise ValueError(f"OriginalPexPath must be .pex: {args.original_pex_path}")
    if output.suffix.lower() != ".pex":
        raise ValueError(f"OutputPexPath must be .pex: {args.output_pex_path}")
    if apply_report is not None and not (is_under(apply_report, root / "qa") or is_under(apply_report, root / "out")):
        raise ValueError(f"ApplyReportPath must be under qa/ or out/: {args.apply_report_path}")
    if apply_report is not None and apply_report.suffix.lower() != ".md":
        raise ValueError(f"ApplyReportPath must be .md: {args.apply_report_path}")

    distinct_paths = [
        ("original PEX", original),
        ("output PEX", output),
        ("translation JSONL", translation_jsonl),
        ("verification report", report),
    ]
    if apply_report is not None:
        distinct_paths.append(("Apply report", apply_report))
    ensure_distinct_paths(distinct_paths)
    for label, path in (
        ("OriginalPexPath", original),
        ("OutputPexPath", output),
        ("TranslationJsonlPath", translation_jsonl),
    ):
        if not path.is_file():
            raise ValueError(f"{label} must exist: {path}")

    path_game = args.game or "workspace"
    parse_check_jsonl, parse_check_report = output_parse_check_paths(
        root,
        output,
        path_game,
    )
    planned_independent_report = independent_verification_report_path(
        root,
        original,
        output,
        translation_jsonl,
        path_game,
    )
    generated_evidence_paths = [
        ("output parse check JSONL", parse_check_jsonl),
        ("output parse check report", parse_check_report),
        ("independent verification report", planned_independent_report),
    ]
    ensure_distinct_paths(distinct_paths + generated_evidence_paths)

    context = resolve_game_context(root, args.game)
    independent_report: Path | None = (
        planned_independent_report
        if context.pex_writeback_status == "experimental"
        else None
    )

    issues: list[str] = []
    warnings: list[str] = []
    mod_root = resolve_project_path(root, "mod", must_exist=False)
    out_root = resolve_project_path(root, "out", must_exist=False)
    translated_tool_root = resolve_project_path(root, "translated/tool_outputs", must_exist=False)
    if is_under(output, mod_root):
        issues.append(f"OutputPexPath points under mod/ and must not be modified: {relative_path(root, output)}")
    relative_out = relative_path(out_root, output) if is_under(output, out_root) else ""
    is_known_out_root = re.match(
        r"^[^\\]+\\(tool_outputs|final_mod|汉化产出\\final_mod)(\\|$)",
        relative_out,
        re.IGNORECASE,
    ) is not None
    if not (is_known_out_root or is_under(output, translated_tool_root)):
        warnings.append(f"OutputPexPath is project-local but outside the usual tool/final output roots: {relative_path(root, output)}")

    original_hash = sha256_file(original)
    output_hash = sha256_file(output)
    hash_changed = original_hash != output_hash
    if not hash_changed and not args.allow_unchanged:
        issues.append("Output PEX hash is unchanged from original.")

    output_bytes = output.read_bytes()
    parse_check_jsonl, parse_check_report, parse_check_error = verify_output_parseable(
        root,
        output,
        context.game_id,
        parse_check_jsonl,
        parse_check_report,
    )
    if parse_check_error:
        issues.append(f"Output PEX could not be re-read by the PEX adapter: {parse_check_error}")
    if context.pex_writeback_status == "experimental":
        validate_experimental_apply_report(
            apply_report,
            context,
            root,
            original,
            output,
            translation_jsonl,
            issues,
        )
    independent_error = ""
    if context.pex_writeback_status == "experimental":
        independent_report, independent_error = verify_output_independently(
            root,
            original,
            output,
            translation_jsonl,
            context.game_id,
            independent_report,
        )
        if independent_error:
            issues.append(f"Independent PEX verification failed: {independent_error}")

    rows, skipped_rows, total_rows = parse_translation_jsonl(translation_jsonl, output_bytes, issues)
    if rows:
        source_still_present = sum(1 for row in rows if row.SourcePresentInOutput)
        target_present = sum(1 for row in rows if row.TargetPresentInOutput)
        target_missing_after_source_gone = sum(
            1 for row in rows if not row.SourcePresentInOutput and not row.TargetPresentInOutput and not row.TargetCjkTokenPresentInOutput
        )
        target_partial_after_source_gone = sum(
            1 for row in rows if not row.SourcePresentInOutput and not row.TargetPresentInOutput and row.TargetCjkTokenPresentInOutput
        )
        if target_present == 0 and source_still_present > 0:
            issues.append("No translated target strings were found in the output PEX, while source strings remain.")
        elif source_still_present > 0:
            warnings.append(f"Some source strings are still present in the output PEX: {source_still_present}")
        if target_missing_after_source_gone > 0:
            issues.append(f"Some source strings are gone but the expected target string was not directly found: {target_missing_after_source_gone}")
        if target_partial_after_source_gone > 0:
            issues.append(f"Some source strings are gone and only a CJK token from the expected target was found: {target_partial_after_source_gone}")
    elif total_rows == 0:
        if context.pex_writeback_status == "experimental":
            issues.append("Experimental PEX verification requires at least one writable translation row.")
        else:
            warnings.append("No translation rows were parsed from TranslationJsonlPath.")

    if context.pex_writeback_status == "experimental" and args.allow_unchanged:
        issues.append("--allow-unchanged cannot relax experimental PEX verification.")

    write_report(
        root,
        original,
        output,
        translation_jsonl,
        report,
        original_hash,
        output_hash,
        hash_changed,
        rows,
        skipped_rows,
        total_rows,
        issues,
        warnings,
        parse_check_jsonl,
        parse_check_report,
        parse_check_error,
        context,
        apply_report,
        independent_report,
        independent_error,
    )
    print(f"PEX verification written to: {report}")
    if issues:
        print(f"PEX verification found {len(issues)} issue(s).")
        return 0 if args.warn_only and context.pex_writeback_status != "experimental" else 1
    print("PEX verification passed with no blocking issues.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

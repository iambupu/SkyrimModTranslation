from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ProbeRow:
    Source: str
    Target: str
    SourcePresentInOutput: bool
    TargetPresentInOutput: bool
    TargetCjkTokenPresentInOutput: bool


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
    return any(text_in_bytes(data, token) for token in cjk_tokens(text))


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def row_value(row: dict, *names: str) -> str:
    for name in names:
        if name in row and row[name] is not None:
            return str(row[name])
    return ""


def parse_translation_jsonl(path: Path, output_bytes: bytes, issues: list[str]) -> list[ProbeRow]:
    rows: list[ProbeRow] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"Invalid JSONL at line {line_number}: {exc}")
            continue
        if not isinstance(row, dict):
            continue
        source = row_value(row, "Source", "source")
        target = row_value(row, "Result", "target")
        if not source.strip() and not target.strip():
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
    return rows


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
    issues: list[str],
    warnings: list[str],
) -> None:
    original_item = original.stat()
    output_item = output.stat()
    lines: list[str] = [
        "# PEX Output Verification",
        "",
        f"- Original: {relative_path(root, original)}",
        f"- Output: {relative_path(root, output)}",
        f"- TranslationJsonlPath: {relative_path(root, translation_jsonl)}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Original SHA256: {original_hash}",
        f"- Output SHA256: {output_hash}",
        f"- Hash changed: {hash_changed}",
        f"- Original size: {original_item.st_size}",
        f"- Output size: {output_item.st_size}",
        f"- Rows parsed: {len(rows)}",
        "",
        "## Translation String Probe",
        "",
    ]
    if not rows:
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
            "- This script did not access real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
        ]
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a project-local PEX output contains expected translated strings.")
    parser.add_argument("--original-pex-path", required=True)
    parser.add_argument("--output-pex-path", required=True)
    parser.add_argument("--translation-jsonl-path", required=True)
    parser.add_argument("--report-output-path", default="qa/pex_output_verification.md")
    parser.add_argument("--allow-unchanged", action="store_true")
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()

    root = project_root()
    original = resolve_project_path(root, args.original_pex_path, must_exist=True)
    output = resolve_project_path(root, args.output_pex_path, must_exist=True)
    translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
    report = resolve_project_path(root, args.report_output_path, must_exist=False)
    if not (is_under(report, root / "qa") or is_under(report, root / "out")):
        raise ValueError(f"ReportOutputPath must be under qa/ or out/: {args.report_output_path}")
    if original.suffix.lower() != ".pex":
        raise ValueError(f"OriginalPexPath must be .pex: {args.original_pex_path}")
    if output.suffix.lower() != ".pex":
        raise ValueError(f"OutputPexPath must be .pex: {args.output_pex_path}")

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
    rows = parse_translation_jsonl(translation_jsonl, output_bytes, issues)
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
    else:
        warnings.append("No translation rows were parsed from TranslationJsonlPath.")

    write_report(root, original, output, translation_jsonl, report, original_hash, output_hash, hash_changed, rows, issues, warnings)
    print(f"PEX verification written to: {report}")
    if issues:
        print(f"PEX verification found {len(issues)} issue(s).")
        return 0 if args.warn_only else 1
    print("PEX verification passed with no blocking issues.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

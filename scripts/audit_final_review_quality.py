"""Mechanical quality audit over final text/binary review packet rows.

The input is final_mod review evidence, not draft translation tables. Findings
here block release until model review and/or translations are corrected.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import project_root, relative_path, resolve_project_path
from proofread_translation import (
    FORBIDDEN_STYLE_TERMS,
    load_allowed_words,
    placeholder_tokens,
    protected_tokens,
    remove_allowed_ascii_tokens,
    token_count,
)


@dataclass
class QualityFinding:
    Severity: str
    File: str
    Line: int
    Code: str
    Message: str
    Source: str
    Final: str


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def cjk_present(text: str) -> bool:
    return re.search(r"[\u3400-\u9fff]", text) is not None


def english_present(text: str) -> bool:
    return re.search(r"[A-Za-z]{3,}", text) is not None


def source_name_allowlist(source: str, final: str, context: str) -> set[str]:
    # Proper nouns copied from source are often intentional. Allow only names
    # that appear in the same source/context rather than globally permitting
    # arbitrary residual English.
    words: set[str] = set()
    for match in re.finditer(r"\b[A-Z][A-Za-z][A-Za-z'\-]*\b", source):
        word = match.group(0).strip("'")
        if word and re.search(rf"\b{re.escape(word)}\b", final):
            words.add(word)
    if re.search(r"\bconsole\b|控制台", f"{source} {final} {context}", re.IGNORECASE):
        for match in re.finditer(r"\b[A-Za-z][A-Za-z'\-]{1,}\b", source):
            word = match.group(0).strip("'")
            if word and re.search(rf"\b{re.escape(word)}\b", final):
                words.add(word)
    for match in re.finditer(r"\b_[A-Za-z][A-Za-z0-9_]*\b", source):
        word = match.group(0)
        if word and word in final:
            words.add(word)
    return words


def quality_tokens(tokens: list[str]) -> list[str]:
    return [token for token in tokens if not re.fullmatch(r"%\s+[A-Za-z]", token)]


def add_finding(
    findings: list[QualityFinding],
    *,
    severity: str,
    file: str,
    line: int,
    code: str,
    message: str,
    source: str,
    final: str,
) -> None:
    findings.append(QualityFinding(severity, file, line, code, message, source, final))


def should_audit_untranslated_review(file_value: str, kind: str) -> bool:
    normalized_file = file_value.replace("\\", "/").lower()
    normalized_kind = kind.strip().lower()
    if normalized_kind == "plugin-binary":
        return True
    if "/interface/translations/" in normalized_file and "_chinese." in normalized_file:
        return True
    return False


def read_jsonl(path: Path, findings: list[QualityFinding]) -> list[tuple[int, dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    if not path.is_file():
        return rows
    for line_number, line in enumerate(read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            add_finding(
                findings,
                severity="error",
                file=str(path),
                line=line_number,
                code="invalid-json",
                message=str(exc),
                source="",
                final="",
            )
            continue
        if not isinstance(row, dict):
            add_finding(
                findings,
                severity="error",
                file=str(path),
                line=line_number,
                code="invalid-row",
                message="Final review row is not a JSON object.",
                source="",
                final="",
            )
            continue
        rows.append((line_number, row))
    return rows


def audit_row(
    root: Path,
    item_path: Path,
    line_number: int,
    row: dict[str, Any],
    findings: list[QualityFinding],
    allowed_words: set[str],
) -> None:
    # Each row represents a changed delivered value. Treat protected-review,
    # unchanged English, missing placeholders, and residual English as release
    # risks because they are already in final_mod.
    source = str(row.get("Source", ""))
    final = str(row.get("Final", ""))
    risk = str(row.get("Risk", "")).strip().lower()
    kind = str(row.get("Kind", "")).strip()
    context = str(row.get("Context", ""))
    file_value = str(row.get("File", "")).strip() or relative_path(root, item_path)
    evidence = f"{relative_path(root, item_path)}:{line_number}"

    if risk == "untranslated-review" and not should_audit_untranslated_review(file_value, kind):
        return

    if risk == "protected-review":
        add_finding(
            findings,
            severity="error",
            file=file_value,
            line=line_number,
            code="protected-review-changed",
            message="Protected or logic-like final_mod text changed and must be explicitly removed or reclassified before delivery.",
            source=source,
            final=final,
        )

    if source.strip() and not final.strip():
        add_finding(
            findings,
            severity="error",
            file=file_value,
            line=line_number,
            code="empty-final",
            message="Final delivered text is empty for a changed review row.",
            source=source,
            final=final,
        )
        return

    if source == final and english_present(source) and risk != "protected-review":
        add_finding(
            findings,
            severity="error",
            file=file_value,
            line=line_number,
            code="unchanged-english",
            message=f"English source appears unchanged in final_mod review item: {evidence}",
            source=source,
            final=final,
        )
        return

    source_placeholders = quality_tokens(placeholder_tokens(source))
    final_placeholders = quality_tokens(placeholder_tokens(final))
    for token in dict.fromkeys(source_placeholders):
        if token_count(final_placeholders, token) < token_count(source_placeholders, token):
            add_finding(
                findings,
                severity="error",
                file=file_value,
                line=line_number,
                code="missing-placeholder",
                message=f"Final delivered text is missing placeholder/control token: {token}",
                source=source,
                final=final,
            )

    source_protected = quality_tokens(protected_tokens(source))
    for token in dict.fromkeys(source_protected):
        if final.count(token) < token_count(source_protected, token):
            add_finding(
                findings,
                severity="error",
                file=file_value,
                line=line_number,
                code="missing-protected-token",
                message=f"Final delivered text is missing protected token: {token}",
                source=source,
                final=final,
            )

    if english_present(source) and english_present(final) and not cjk_present(final):
        add_finding(
            findings,
            severity="error",
            file=file_value,
            line=line_number,
            code="final-without-chinese",
            message="English source changed, but final delivered text contains no Chinese characters.",
            source=source,
            final=final,
        )

    row_allowed_words = set(allowed_words)
    row_allowed_words.update(source_name_allowlist(source, final, context))
    remaining_final = remove_allowed_ascii_tokens(final, row_allowed_words)
    english_words = [
        match.group(0)
        for match in re.finditer(r"[A-Za-z][A-Za-z'\-]{3,}", remaining_final)
        if not re.fullmatch(r"true|false|null|none", match.group(0), re.IGNORECASE)
    ]
    if english_words:
        add_finding(
            findings,
            severity="warning",
            file=file_value,
            line=line_number,
            code="residual-english",
            message=f"Final delivered text contains non-allowlisted English word(s): {', '.join(english_words)}",
            source=source,
            final=final,
        )

    for term in FORBIDDEN_STYLE_TERMS:
        if term.lower() in final.lower():
            add_finding(
                findings,
                severity="warning",
                file=file_value,
                line=line_number,
                code="style-term",
                message=f"Final delivered text contains informal/modern style term: {term}",
                source=source,
                final=final,
            )


def audit_file(root: Path, path: Path, findings: list[QualityFinding], allowed_words: set[str]) -> int:
    rows = read_jsonl(path, findings)
    for line_number, row in rows:
        audit_row(root, path, line_number, row, findings, allowed_words)
    return len(rows)


def write_reports(
    root: Path,
    mod_name: str,
    report_path: Path,
    json_path: Path,
    item_paths: list[Path],
    rows_checked: int,
    findings: list[QualityFinding],
) -> None:
    blocking = sum(1 for finding in findings if finding.Severity == "error")
    warnings = sum(1 for finding in findings if finding.Severity == "warning")
    status = "passed" if blocking == 0 and warnings == 0 else "failed"
    lines = [
        f"# Final Review Quality Audit: {mod_name}",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Item files checked: {len(item_paths)}",
        f"- Rows checked: {rows_checked}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        f"- Status: {status}",
        "",
        "## Scope",
        "",
        "- Reads actual final_mod review item JSONL files generated from final text and final ESP/PEX binary re-read packets.",
        "- Checks empty final text, unchanged English, missing placeholders, missing protected tokens, residual English, protected-review drift, and informal style terms.",
        "- This audit does not translate text or modify plugin, PEX, archive, package, or final_mod files.",
        "",
        "## Inputs",
        "",
    ]
    for path in item_paths:
        lines.append(f"- `{relative_path(root, path)}`")

    lines.extend(["", "## Findings", ""])
    if not findings:
        lines.append("No final review quality findings.")
    else:
        lines.extend(["| Severity | File | Line | Code | Message |", "|---|---|---:|---|---|"])
        for finding in findings:
            lines.append(
                f"| {finding.Severity} | {markdown_cell(finding.File)} | {finding.Line} | {finding.Code} | {markdown_cell(finding.Message)} |"
            )

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- All inputs and reports are project-local.",
            "- Real Skyrim, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "ProjectRoot": str(root),
                "ModName": mod_name,
                "CheckedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Status": status,
                "BlockingIssues": blocking,
                "Warnings": warnings,
                "RowsChecked": rows_checked,
                "ItemFiles": [relative_path(root, path) for path in item_paths],
                "Findings": [asdict(finding) for finding in findings],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit actual final_mod review rows for residual translation quality issues.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--final-text-items-path", default="")
    parser.add_argument("--final-binary-items-path", default="")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--json-output-path", default="")
    args = parser.parse_args()

    root = project_root()
    mod_name = args.mod_name
    text_items = resolve_project_path(root, args.final_text_items_path or f"qa/{mod_name}.final_text_review_items.jsonl", must_exist=True)
    binary_items = resolve_project_path(root, args.final_binary_items_path or f"qa/{mod_name}.final_binary_review_items.jsonl", must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.final_review_quality.md", must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path or f"qa/{mod_name}.final_review_quality.json", must_exist=False)

    findings: list[QualityFinding] = []
    allowed_words = load_allowed_words(root)
    item_paths = [text_items, binary_items]
    rows_checked = sum(audit_file(root, path, findings, allowed_words) for path in item_paths)
    write_reports(root, mod_name, report_path, json_path, item_paths, rows_checked, findings)
    blocking = sum(1 for finding in findings if finding.Severity == "error")
    warnings = sum(1 for finding in findings if finding.Severity == "warning")
    print(f"Final review quality audit written to: {report_path}")
    print(f"Final review quality audit JSON written to: {json_path}")
    print(f"Rows checked: {rows_checked}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking or warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())

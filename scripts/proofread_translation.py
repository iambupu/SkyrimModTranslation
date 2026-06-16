"""Mechanical proofread for translation JSON/JSONL intermediates.

This script checks placeholders, protected tokens, empty targets, residual
English, and informal style. It is a safety net, not a substitute for Codex
model semantic review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


@dataclass
class Finding:
    severity: str
    file: str
    line: int
    code: str
    message: str
    source: str
    target: str


SOURCE_FIELDS = ("Source", "source", "original", "Original", "text", "Text")
TARGET_FIELDS = ("Result", "result", "Target", "target", "translation", "Dest", "dest")
RISK_FIELDS = ("risk", "Risk")

PLACEHOLDER_PATTERNS = [
    re.compile(r"%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.\d+)?[sdifcoxXeEgG]"),
    re.compile(r"\{[A-Za-z0-9_]+\}"),
    re.compile(r"<Alias=[^>\r\n]+>"),
    re.compile(r"<font\b[^>\r\n]*>"),
    re.compile(r"</font>"),
    re.compile(r"<color\b[^>\r\n]*>"),
    re.compile(r"</color>"),
    re.compile(r"\$[^\W\d]\w*", re.UNICODE),
    re.compile(r"\\r\\n"),
    re.compile(r"\\n"),
    re.compile(r"\\r"),
    re.compile(r"\r\n"),
    re.compile(r"\n"),
    re.compile(r"\r"),
]

PROTECTED_PATTERNS = [
    re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"\b[\w.-]+\.(?:esp|esm|esl|pex|psc|dll|exe|bsa|ba2|json|jsonl|xml|txt|ini)\b", re.IGNORECASE),
    re.compile(r"\b(?:Data|Scripts|Interface|MCM|SKSE|Meshes|Textures|Sound|Seq|Fomod)[\\/][^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\[^\s\"'<>]+"),
    re.compile(r"\b(?:0x)?[0-9A-Fa-f]{8}\b"),
    re.compile(r"<[^>\r\n]+>"),
    re.compile(r"\$[^\W\d]\w*", re.UNICODE),
    re.compile(r"%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.\d+)?[sdifcoxXeEgG]"),
    re.compile(r"\{[A-Za-z0-9_]+\}"),
]

ALLOW_WORDS = (
    "DAK",
    "MFEE",
    "MCM",
    "NPC",
    "SKSE",
    "PEX",
    "ESP",
    "ESM",
    "ESL",
    "SE",
    "AE",
    "SSE",
    "JSON",
    "XML",
    "INI",
    "Papyrus",
    "Mod",
)

FORBIDDEN_STYLE_TERMS = (
    "小可爱",
    "亲亲",
    "亲爱的",
    "宝宝",
    "老铁",
    "牛逼",
    "给力",
    "666",
    "yyds",
    "安排上",
    "冲鸭",
)


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
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def ensure_report_path(root: Path, value: str) -> Path:
    resolved = resolve_project_path(root, value, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    out_root = resolve_project_path(root, "out", must_exist=False)
    if not (is_under(resolved, qa_root) or is_under(resolved, out_root)):
        raise ValueError(f"report paths must be under qa/ or out/: {value}")
    return resolved


def get_value(row: dict[str, Any], names: Iterable[str]) -> str:
    for name in names:
        if name in row and row[name] is not None:
            value = row[name]
            if isinstance(value, str):
                return value
            return str(value)
    return ""


def token_matches(text: str, patterns: Iterable[re.Pattern[str]]) -> list[str]:
    tokens: list[str] = []
    for pattern in patterns:
        tokens.extend(match.group(0) for match in pattern.finditer(text) if match.group(0))
    return tokens


def placeholder_tokens(text: str) -> list[str]:
    return token_matches(text, PLACEHOLDER_PATTERNS)


def protected_tokens(text: str) -> list[str]:
    return token_matches(text, PROTECTED_PATTERNS)


def token_count(tokens: list[str], needle: str) -> int:
    return sum(1 for token in tokens if token == needle)


def is_protected_only_source(text: str, risk: str) -> bool:
    trimmed = text.strip()
    if not trimmed:
        return False
    if risk.lower() in {"protected", "protected-logic"}:
        return True
    if re.fullmatch(r"[\w.-]+\.(?:esp|esm|esl|pex|psc|dll|exe|bsa|ba2|json|jsonl|xml|txt|ini)", trimmed, re.IGNORECASE):
        return True
    if re.match(r"^[A-Za-z]:\\", trimmed):
        return True
    if re.match(r"^(?:Data|Scripts|Interface|MCM|SKSE|Meshes|Textures|Sound|Seq|Fomod)[\\/]", trimmed, re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:0x)?[0-9A-Fa-f]{8}", trimmed):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)+", trimmed):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+", trimmed):
        return True
    return False


def remove_known_ascii_tokens(text: str) -> str:
    clean = text
    for token in protected_tokens(text):
        clean = clean.replace(token, "")
    for word in ALLOW_WORDS:
        clean = re.sub(rf"\b{re.escape(word)}\b", "", clean, flags=re.IGNORECASE)
    return clean


def load_allowed_words(root: Path) -> set[str]:
    # Allowlisted English comes from glossary files so Mod-specific names can be
    # approved without weakening the global residual-English check.
    words = set(ALLOW_WORDS)
    for relative in ("glossary/skyrim_cn_glossary.md", "glossary/mod_terms.md", "qa/unresolved_terms.md"):
        path = root / relative
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.startswith("|") or line.startswith("|---"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not cells or not cells[0] or cells[0].lower() in {"english", "source"}:
                continue
            for match in re.finditer(r"[A-Za-z][A-Za-z'\-]{1,}", cells[0]):
                word = match.group(0).strip("'")
                if word:
                    words.add(word)
    return words


def remove_allowed_ascii_tokens(text: str, allowed_words: set[str]) -> str:
    clean = text
    for token in protected_tokens(text):
        clean = clean.replace(token, "")
    for word in sorted(allowed_words, key=len, reverse=True):
        clean = re.sub(rf"\b{re.escape(word)}\b", "", clean, flags=re.IGNORECASE)
    return clean


def add_finding(
    findings: list[Finding],
    severity: str,
    file: str,
    line: int,
    code: str,
    message: str,
    source: str = "",
    target: str = "",
) -> None:
    findings.append(Finding(severity, file, line, code, message, source, target))


def iter_jsonl_rows(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, str]]:
    for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            yield index, None, str(exc)
            continue
        if not isinstance(row, dict):
            yield index, None, "JSONL row is not an object."
            continue
        yield index, row, ""


def coerce_json_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("rows", "items", "strings", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value]
    return []


def iter_json_rows(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, str]]:
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        yield from iter_jsonl_rows(path)
        return
    rows = coerce_json_rows(data)
    if not rows:
        yield 1, None, "JSON document does not contain object rows."
        return
    for index, row in enumerate(rows, start=1):
        yield index, row, ""


def iter_rows(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, str]]:
    if path.suffix.lower() == ".json":
        yield from iter_json_rows(path)
    else:
        yield from iter_jsonl_rows(path)


def collect_input_files(root: Path, input_paths: list[str], input_list_path: str) -> list[Path]:
    effective: list[str] = [value for value in input_paths if value.strip()]
    if input_list_path.strip():
        list_path = resolve_project_path(root, input_list_path, must_exist=True)
        for line in list_path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                effective.append(line.strip())
    if not effective:
        raise ValueError("At least one --input-path or --input-list-path entry is required.")

    input_files: list[Path] = []
    for value in effective:
        item = resolve_project_path(root, value, must_exist=True)
        if item.is_dir():
            input_files.extend(sorted(child for child in item.rglob("*") if child.is_file() and child.suffix.lower() in {".jsonl", ".json"}))
        elif item.suffix.lower() in {".jsonl", ".json"}:
            input_files.append(item)
        else:
            raise ValueError(f"proofread input must be .jsonl, .json, or a directory containing those files: {value}")

    unique: dict[str, Path] = {}
    for path in input_files:
        unique[str(path.resolve(strict=False)).lower()] = path
    return list(unique.values())


def proofread_file(root: Path, file_path: Path, findings: list[Finding], allowed_words: set[str]) -> int:
    # Row schemas differ between tools; field aliases let one checker cover
    # LexTranslator JSONL, xTranslator-derived JSON, and project-normalized rows.
    relative = relative_path(root, file_path)
    rows_checked = 0
    for line_number, row, error in iter_rows(file_path):
        if row is None:
            add_finding(findings, "error", relative, line_number, "invalid-json", error)
            continue

        source = get_value(row, SOURCE_FIELDS)
        target = get_value(row, TARGET_FIELDS)
        risk = get_value(row, RISK_FIELDS)
        if not source.strip() and not target.strip():
            continue

        rows_checked += 1
        if not target.strip():
            if risk.lower() in {"protected", "protected-logic", "manual-review", "review"}:
                continue
            add_finding(findings, "error", relative, line_number, "empty-target", "Candidate Target/Result is empty.", source, target)
            continue

        source_placeholder_tokens = placeholder_tokens(source)
        target_placeholder_tokens = placeholder_tokens(target)
        for token in dict.fromkeys(source_placeholder_tokens):
            source_count = token_count(source_placeholder_tokens, token)
            target_count = token_count(target_placeholder_tokens, token)
            if target_count < source_count:
                add_finding(findings, "error", relative, line_number, "missing-placeholder", f"Missing placeholder/control token: {token}", source, target)

        source_protected_tokens = protected_tokens(source)
        for token in dict.fromkeys(source_protected_tokens):
            source_count = token_count(source_protected_tokens, token)
            target_count = target.count(token)
            if target_count < source_count:
                add_finding(findings, "error", relative, line_number, "missing-protected-token", f"Protected token missing from target: {token}", source, target)

        if is_protected_only_source(source, risk) and source != target:
            add_finding(findings, "error", relative, line_number, "protected-source-translated", "Protected/key-like source was translated or changed.", source, target)
        elif risk.lower() in {"manual-review", "review"} and source != target:
            add_finding(findings, "warning", relative, line_number, "manual-review-translated", "Review string was translated; confirm it is player-visible.", source, target)

        target_for_english_check = remove_allowed_ascii_tokens(target, allowed_words)
        english_words = [
            match.group(0)
            for match in re.finditer(r"[A-Za-z][A-Za-z'\-]{3,}", target_for_english_check)
            if not re.fullmatch(r"true|false|null|none", match.group(0), re.IGNORECASE)
        ]
        if english_words:
            add_finding(
                findings,
                "warning",
                relative,
                line_number,
                "residual-english",
                f"Target contains non-allowlisted English word(s): {', '.join(english_words)}",
                source,
                target,
            )

        for term in FORBIDDEN_STYLE_TERMS:
            if term.lower() in target.lower():
                add_finding(findings, "warning", relative, line_number, "style-term", f"Target contains modern/informal style term: {term}", source, target)

        if re.search(r"[A-Za-z]{4,}", source) and target == source and not is_protected_only_source(source, risk):
            add_finding(findings, "warning", relative, line_number, "unchanged-english", "English source was left unchanged; confirm this is intentional.", source, target)

    return rows_checked


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_reports(report_path: Path, issues_jsonl_path: Path, input_files: list[Path], rows_checked: int, findings: list[Finding]) -> None:
    issue_count = sum(1 for finding in findings if finding.severity == "error")
    warning_count = sum(1 for finding in findings if finding.severity == "warning")
    lines: list[str] = [
        "# Translation Proofread Report",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Files checked: {len(input_files)}",
        f"- Rows checked: {rows_checked}",
        f"- Blocking issues: {issue_count}",
        f"- Warnings: {warning_count}",
        "",
        "## Scope",
        "",
        "- JSON/JSONL translation intermediates only.",
        "- Checks placeholders, control tokens, protected filenames/paths/FormIDs/keys, empty targets, residual English, and informal style terms.",
        "- This script does not modify translations or binary files.",
        "",
        "## Findings",
        "",
    ]
    if not findings:
        lines.append("No proofread findings.")
    else:
        lines.extend(["| Severity | File | Line | Code | Message |", "|---|---|---:|---|---|"])
        for finding in findings:
            lines.append(
                f"| {finding.severity} | {markdown_cell(finding.file)} | {finding.line} | {finding.code} | {markdown_cell(finding.message)} |"
            )

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- All inputs and reports are project-local.",
            "- No real Skyrim, Steam, MO2/Vortex, AppData, or Documents/My Games path was accessed.",
        ]
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    issues_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    issues_jsonl_path.write_text(
        "".join(json.dumps(asdict(finding), ensure_ascii=False, separators=(",", ":")) + "\n" for finding in findings),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Proofread Skyrim translation JSON/JSONL intermediates without modifying inputs.")
    parser.add_argument("--input-path", action="append", default=[])
    parser.add_argument("--input-list-path", default="")
    parser.add_argument("--report-output-path", default="qa/translation_proofread.md")
    parser.add_argument("--issues-jsonl-path", default="qa/translation_proofread_issues.jsonl")
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()

    root = project_root()
    report_path = ensure_report_path(root, args.report_output_path)
    issues_jsonl_path = ensure_report_path(root, args.issues_jsonl_path)
    input_files = collect_input_files(root, args.input_path, args.input_list_path)

    findings: list[Finding] = []
    allowed_words = load_allowed_words(root)
    rows_checked = 0
    for input_file in input_files:
        rows_checked += proofread_file(root, input_file, findings, allowed_words)

    write_reports(report_path, issues_jsonl_path, input_files, rows_checked, findings)
    issue_count = sum(1 for finding in findings if finding.severity == "error")
    warning_count = sum(1 for finding in findings if finding.severity == "warning")
    print(f"Proofread report written to: {report_path}")
    print(f"Proofread findings JSONL written to: {issues_jsonl_path}")
    print(f"Blocking issues: {issue_count}")
    print(f"Warnings: {warning_count}")
    if issue_count > 0 and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

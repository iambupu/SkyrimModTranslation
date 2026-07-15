"""Apply a reviewed source->target map to exported plugin string JSONL.

This produces translated JSONL for a later controlled writer. It does not save
or patch ESP/ESM/ESL binaries.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from capability_resolver import resolve_capability
from game_context import load_game_context, load_game_profile
from project_paths import is_under, project_root, resolve_project_path, safe_file_name
from project_paths import relative_posix_path as relative_path
from report_utils import markdown_cell_plain as markdown_cell


TOKEN_PATTERNS = (
    r"%[0-9.+\-# ]*[A-Za-z]",
    r"\{[A-Za-z0-9_.:-]+\}",
    r"<[^>]+>",
    r"\\r\\n|\\n|\\r",
    r"\$[A-Za-z_][A-Za-z0-9_]*",
)


def require_under(path: Path, allowed_roots: list[Path], label: str) -> None:
    if not any(is_under(path, root) for root in allowed_roots):
        allowed = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(f"{label} must be under one of: {allowed}")




def infer_mod_name(root: Path, export_path: Path) -> str:
    source_root = root / "source"
    try:
        relative = export_path.resolve(strict=False).relative_to(source_root.resolve(strict=False))
    except ValueError:
        return ""
    parts = relative.parts
    if len(parts) >= 2 and parts[0].lower() == "plugin_exports":
        return parts[1]
    return ""


def protected_tokens(value: str) -> list[str]:
    tokens: set[str] = set()
    for pattern in TOKEN_PATTERNS:
        tokens.update(match.group(0) for match in re.finditer(pattern, value or ""))
    tokens = {token for token in tokens if not re.fullmatch(r"%\s+[A-Za-z]", token)}
    return sorted(tokens)


IDENTITY_FIELDS = (
    "game_id",
    "plugin",
    "record_type",
    "form_id",
    "editor_id",
    "field_path",
    "subrecord_type",
    "subrecord_index",
    "source",
)


def row_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple("" if row.get(field) is None else str(row.get(field)) for field in IDENTITY_FIELDS)


def read_translation_map(path: Path) -> tuple[dict[str, str], dict[tuple[str, ...], str]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("TranslationMapPath must contain a JSON object mapping source text to translated text.")
    translations = data.get("translations")
    if translations is not None:
        if not isinstance(translations, list) or not all(isinstance(row, dict) for row in translations):
            raise ValueError("v2 TranslationMapPath translations must be a list of row objects.")
        identity_map: dict[tuple[str, ...], str] = {}
        for row in translations:
            identity = row_identity(row)
            if identity in identity_map:
                raise ValueError(f"duplicate translation identity: {'|'.join(identity)}")
            identity_map[identity] = "" if row.get("target") is None else str(row.get("target"))
        return {}, identity_map
    return ({str(key): "" if value is None else str(value) for key, value in data.items()}, {})


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"invalid JSONL at line {line_number}: row must be an object")
        rows.append(row)
    return rows


def write_jsonl_if_changed(path: Path, rows: list[dict[str, Any]]) -> bool:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return True



def write_report(
    root: Path,
    report_path: Path,
    mod_name: str,
    export_path: Path,
    map_path: Path,
    output_path: Path,
    candidate_count: int,
    applied_count: int,
    missing: list[str],
    token_issues: list[str],
    game_id: str,
    game_profile_version: int,
    support_level: str,
    plugin_adapter: str,
    plugin_adapter_version: int,
) -> None:
    lines = [
        "# Plugin Translation Map Report",
        "",
        f"- game_id: {game_id}",
        f"- game_profile_version: {game_profile_version}",
        f"- plugin_adapter: {plugin_adapter}",
        f"- plugin_adapter_version: {plugin_adapter_version}",
        f"- support_level: {support_level}",
        f"- ModName: {mod_name}",
        f"- Export: {relative_path(root, export_path)}",
        f"- Translation map: {relative_path(root, map_path)}",
        f"- Output: {relative_path(root, output_path)}",
        f"- Candidate rows: {candidate_count}",
        f"- Applied rows: {applied_count}",
        f"- Missing rows: {len(missing)}",
        f"- Protected token issues: {len(token_issues)}",
        "",
        "## Missing Sources",
        "",
    ]
    if missing:
        for item in sorted(set(missing)):
            lines.append(f"- {markdown_cell(item)}")
    else:
        lines.append("No missing candidate translations.")
    lines.extend(["", "## Protected Token Issues", ""])
    if token_issues:
        for issue in token_issues:
            lines.append(f"- {markdown_cell(issue)}")
    else:
        lines.append("No protected token issues detected.")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script does not read or write plugin binaries.",
            "- It only writes a translated JSONL middle file under translated/.",
            "- Plugin writeback still requires a controlled Mutagen/xEdit adapter.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a source-to-target JSON map to a project-local ESP/ESM/ESL string export JSONL.")
    parser.add_argument("--export-path", required=True)
    parser.add_argument("--translation-map-path", required=True)
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    root = project_root()
    export_path = resolve_project_path(root, args.export_path, must_exist=True)
    map_path = resolve_project_path(root, args.translation_map_path, must_exist=True)
    require_under(export_path, [root / "source"], "ExportPath")
    require_under(map_path, [root / "work", root / "translated"], "TranslationMapPath")

    mod_name = safe_file_name(args.mod_name or infer_mod_name(root, export_path))
    base_name = export_path.name
    if base_name.lower().endswith(".jsonl"):
        base_name = base_name[:-6]
    output_path = resolve_project_path(
        root,
        args.output_path or f"translated/plugin_exports/{mod_name}/{base_name}.zh.jsonl",
        must_exist=False,
    )
    report_path = resolve_project_path(
        root,
        args.report_path or f"qa/{base_name}.translation_map_report.md",
        must_exist=False,
    )
    require_under(output_path, [root / "translated"], "OutputPath")
    require_under(report_path, [root / "qa"], "ReportPath")

    source_map, identity_map = read_translation_map(map_path)
    context = load_game_context(root) if (root / ".skyrim-chs-workspace.json").is_file() else None
    rows = read_jsonl_rows(export_path)
    candidate_count = 0
    applied_count = 0
    missing: list[str] = []
    token_issues: list[str] = []

    for row in rows:
        row.setdefault("target", "")
        if str(row.get("risk", "")) != "candidate":
            continue
        candidate_count += 1
        source = "" if row.get("source") is None else str(row.get("source"))
        identity = row_identity(row)
        if identity in identity_map or source in source_map:
            target = identity_map[identity] if identity in identity_map else source_map[source]
            row["target"] = target
            applied_count += 1
            missing_tokens = [token for token in protected_tokens(source) if token not in protected_tokens(target)]
            if missing_tokens:
                token_issues.append(
                    f"{row.get('plugin', '')} {row.get('form_id', '')} {row.get('subrecord_type', '')}: "
                    f"missing protected token(s) {', '.join(missing_tokens)}"
                )
        else:
            missing.append(source)

    output_changed = write_jsonl_if_changed(output_path, rows)
    first_game_id = next((str(row.get("game_id")) for row in rows if row.get("game_id")), "skyrim-se")
    game_id = context.game_id if context else first_game_id
    context = context or load_game_profile(game_id)
    profile_version = context.schema_version
    support_level = context.support_level
    plugin_adapter = resolve_capability(context, "plugin_text", "read").adapter_id or ""
    write_report(
        root,
        report_path,
        mod_name,
        export_path,
        map_path,
        output_path,
        candidate_count,
        applied_count,
        missing,
        token_issues,
        game_id,
        profile_version,
        support_level,
        plugin_adapter,
        context.capability_option_positive_int(
            "plugin_text", "adapter_contract_version"
        ),
    )

    print(f"Translated plugin middle file: {output_path}")
    print(f"Plugin translation map report: {report_path}")
    print(f"Output changed: {output_changed}")
    print(f"Applied rows: {applied_count} / {candidate_count}")
    return 2 if missing or token_issues else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Plugin translation map failed: {exc}", file=sys.stderr)
        sys.exit(1)

"""Extract visible MCM text from project-local Interface/MCM resources.

The extractor preserves keys and structure so translated rows can be checked
before being overlaid into final_mod.
"""

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import safe_file_name

from project_paths import is_under, project_root, relative_path, resolve_project_path
from route_translation_task import current_game_context
from file_utils import read_text_auto_cp936 as read_text_auto


SUPPORTED_EXTENSIONS = {".json", ".ini"}


@dataclass
class Candidate:
    game_id: str
    source_file: str
    selector: str
    key: str
    source: str
    target: str
    kind: str
    notes: str


@dataclass
class Reference:
    source_file: str
    selector: str
    key: str
    token: str


@dataclass
class ExtractionState:
    candidates: list[Candidate]
    references: list[Reference]
    issues: list[str]
    protected_string_count: int = 0


@dataclass(frozen=True)
class McmSchema:
    schema_version: int
    game_id: str
    translate_fields: frozenset[str]
    protected_fields: frozenset[str]


def _require_string_list(data: dict[str, Any], key: str) -> frozenset[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"MCM schema field '{key}' must be a list of non-empty strings")
    return frozenset(item.strip() for item in value)


def load_mcm_schema(root: Path) -> McmSchema:
    context = current_game_context(root)
    schema_path = context.plugin_root / "config" / "mcm_schemas" / f"{context.game_id}.json"
    if not schema_path.is_file():
        raise ValueError(f"Missing MCM schema for game '{context.game_id}': {schema_path}")
    data = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"MCM schema must contain an object: {schema_path}")
    schema_version = data.get("schema_version")
    if schema_version != 1:
        raise ValueError(f"MCM schema field 'schema_version' must equal 1: {schema_path}")
    game_id = data.get("game_id")
    if game_id != context.game_id:
        raise ValueError(f"MCM schema game_id mismatch: expected '{context.game_id}', found '{game_id}'")
    translate_fields = _require_string_list(data, "translate_fields")
    protected_fields = _require_string_list(data, "protected_fields")
    return McmSchema(
        schema_version=1,
        game_id=context.game_id,
        translate_fields=translate_fields,
        protected_fields=protected_fields,
    )


def looks_like_path_or_identifier(value: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9_:.\\/|\-]+", value):
        return True
    return re.search(r"\.(esp|esm|esl|pex|psc|dds|png|json|ini|txt)$", value, re.IGNORECASE) is not None


def add_candidate(
    root: Path,
    schema: McmSchema,
    state: ExtractionState,
    file_path: Path,
    selector: str,
    key: str,
    value: str,
    kind: str,
    notes: str,
) -> None:
    if not value.strip():
        return
    state.candidates.append(
        Candidate(
            game_id=schema.game_id,
            source_file=relative_path(root, file_path),
            selector=selector,
            key=key,
            source=value,
            target="",
            kind=kind,
            notes=notes,
        )
    )


def add_reference(root: Path, state: ExtractionState, file_path: Path, selector: str, key: str, value: str) -> None:
    if not value.strip() or not value.startswith("$"):
        return
    state.references.append(
        Reference(
            source_file=relative_path(root, file_path),
            selector=selector,
            key=key,
            token=value,
        )
    )


def walk_json_value(
    root: Path,
    state: ExtractionState,
    file_path: Path,
    value: Any,
    selector: str,
    key_name: str,
    schema: McmSchema,
) -> None:
    if value is None:
        return

    if isinstance(value, str):
        if value.startswith("$"):
            add_reference(root, state, file_path, selector, key_name, value)
            return
        if key_name in schema.translate_fields and not looks_like_path_or_identifier(value):
            add_candidate(root, schema, state, file_path, selector, key_name, value, "json_visible_text", "Visible MCM field.")
        elif re.search(r"\.valueOptions\.options\[\d+\]$", selector) and not looks_like_path_or_identifier(value):
            add_candidate(
                root,
                schema,
                state,
                file_path,
                selector,
                key_name,
                value,
                "json_option_review",
                "Menu option value; review before translating.",
            )
        else:
            state.protected_string_count += 1
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            walk_json_value(root, state, file_path, item, f"{selector}[{index}]", key_name, schema)
        return

    if isinstance(value, dict):
        for key, child in value.items():
            child_selector = key if not selector else f"{selector}.{key}"
            if key in schema.protected_fields and isinstance(child, str):
                state.protected_string_count += 1
                continue
            walk_json_value(root, state, file_path, child, child_selector, key, schema)



def extract_json_file(root: Path, state: ExtractionState, file_path: Path, schema: McmSchema) -> None:
    try:
        data = json.loads(read_text_auto(file_path))
    except Exception as exc:
        state.issues.append(f"Invalid JSON: {relative_path(root, file_path)}: {exc}")
        return
    walk_json_value(root, state, file_path, data, "", "", schema)


def extract_ini_file(root: Path, state: ExtractionState, file_path: Path, schema: McmSchema) -> None:
    translate_fields = {item.casefold() for item in schema.translate_fields}
    protected_fields = {item.casefold() for item in schema.protected_fields}
    section = ""
    for raw_line in read_text_auto(file_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        section_match = re.fullmatch(r"\[(.+)\]", line)
        if section_match:
            section = section_match.group(1)
            continue
        key_value = re.match(r"^([^=]+)=(.*)$", line)
        if not key_value:
            continue
        key = key_value.group(1).strip()
        value = key_value.group(2).strip()
        normalized_key = key.casefold()
        if normalized_key in protected_fields:
            state.protected_string_count += 1
        elif (
            normalized_key in translate_fields
            and re.search(r"[A-Za-z]", value)
            and not looks_like_path_or_identifier(value)
        ):
            selector = f"{section}.{key}" if section else key
            add_candidate(
                root,
                schema,
                state,
                file_path,
                selector,
                key,
                value,
                "ini_value_review",
                "INI value with text; review before translating.",
            )
        else:
            state.protected_string_count += 1


def infer_mod_name(input_path: Path) -> str:
    parts = list(input_path.parts)
    lowered = [part.lower() for part in parts]
    if "extracted_mods" in lowered:
        index = lowered.index("extracted_mods")
        if index + 1 < len(parts):
            return parts[index + 1]
    return input_path.name if input_path.is_dir() else input_path.stem


def collect_input_files(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        return sorted(
            (item for item in input_path.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS),
            key=lambda item: str(item).lower(),
        )
    if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Input file must be one of {sorted(SUPPORTED_EXTENSIONS)}: {input_path}")
    return [input_path]


def find_workspace_root(input_path: Path) -> Path | None:
    parts = list(input_path.parts)
    lowered = [part.lower() for part in parts]
    if "mcm" not in lowered:
        return None
    index = lowered.index("mcm")
    if index == 0:
        return None
    return Path(*parts[:index])


def load_interface_tokens(workspace_root: Path | None) -> set[str]:
    tokens: set[str] = set()
    if workspace_root is None:
        return tokens
    translation_dir = workspace_root / "interface" / "translations"
    if not translation_dir.is_dir():
        return tokens
    for file_path in sorted(translation_dir.glob("*.txt"), key=lambda item: item.name.lower()):
        for line in read_text_auto(file_path).splitlines():
            match = re.match(r"^(\$[^\t]+)\t", line)
            if match:
                tokens.add(match.group(1))
    return tokens


def write_jsonl(path: Path, candidates: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(asdict(candidate), ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        for candidate in candidates
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def write_report(
    root: Path,
    schema: McmSchema,
    input_path: Path,
    output_path: Path,
    report_path: Path,
    files: list[Path],
    state: ExtractionState,
    interface_tokens: set[str],
) -> None:
    missing_references = [reference for reference in state.references if reference.token not in interface_tokens] if interface_tokens else []
    lines = [
        "# MCM Text Extraction Report",
        "",
        f"- GameId: {schema.game_id}",
        f"- Input: {relative_path(root, input_path)}",
        f"- Output: {relative_path(root, output_path)}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Files scanned: {len(files)}",
        f"- Visible candidates: {len(state.candidates)}",
        f"- Translation token references: {len(state.references)}",
        f"- Protected strings/values: {state.protected_string_count}",
        "",
        "## Notes",
        "",
        "- JSON keys, MCM ids, script names, function names, forms, paths, and setting keys are not translation targets.",
        "- `$Token references in MCM JSON should usually be translated in Interface/translations files, not in config.json.",
        "- INI values are extracted only for review and are not rewritten by this script.",
        "",
        "## Missing Interface References",
        "",
    ]
    if not interface_tokens:
        lines.append("No Interface/translations directory was found for cross-reference.")
    elif not missing_references:
        lines.append("No missing `$token references found.")
    else:
        lines.extend(f"- {reference.token} in {reference.source_file} at {reference.selector}" for reference in missing_references)
    lines.extend(["", "## Issues", ""])
    if state.issues:
        lines.extend(f"- {issue}" for issue in state.issues)
    else:
        lines.append("No blocking issues.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract visible Skyrim MCM text candidates from project-local JSON/INI files.")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--report-output-path", default="qa/mcm_extraction_report.md")
    args = parser.parse_args()

    root = project_root()
    input_path = resolve_project_path(root, args.input_path, must_exist=True)
    if not input_path.is_dir() and not input_path.is_file():
        raise ValueError(f"InputPath must be a project-local file or directory: {args.input_path}")

    mod_root = resolve_project_path(root, "mod", must_exist=False)
    work_root = resolve_project_path(root, "work", must_exist=False)
    if not is_under(input_path, mod_root) and not is_under(input_path, work_root):
        raise ValueError("InputPath must be under project mod/ or work/.")

    mod_name = safe_file_name(args.mod_name.strip() or infer_mod_name(input_path))
    if not mod_name:
        raise ValueError("ModName could not be inferred.")

    output_path = resolve_project_path(
        root,
        args.output_path or str(Path("work") / "normalized" / mod_name / "mcm_text_candidates.jsonl"),
        must_exist=False,
    )
    normalized_root = resolve_project_path(root, "work/normalized", must_exist=False)
    if not is_under(output_path, normalized_root):
        raise ValueError(f"OutputPath must be under work/normalized/: {args.output_path}")

    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    schema = load_mcm_schema(root)
    files = collect_input_files(input_path)
    state = ExtractionState(candidates=[], references=[], issues=[])
    for file_path in files:
        extension = file_path.suffix.lower()
        if extension == ".json":
            extract_json_file(root, state, file_path, schema)
        elif extension == ".ini":
            extract_ini_file(root, state, file_path, schema)

    workspace_root = find_workspace_root(input_path)
    interface_tokens = load_interface_tokens(workspace_root)
    write_jsonl(output_path, state.candidates)
    write_report(root, schema, input_path, output_path, report_path, files, state, interface_tokens)

    print(f"MCM extraction written to: {output_path}")
    print(f"MCM extraction report written to: {report_path}")
    return 1 if state.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())

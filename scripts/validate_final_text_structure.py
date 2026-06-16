"""Validate that translated final_mod text files keep their original structure.

This script permits text-value changes but treats structural changes to keys,
tags, section names, headers, and protected paths as blocking issues.
"""

import argparse
import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from xml.etree import ElementTree


SUPPORTED_EXTENSIONS = {".txt", ".json", ".jsonl", ".xml", ".ini", ".csv", ".psc"}
PLACEHOLDER_PATTERNS = (
    r"%[sdf]",
    r"\{(?:0|1|name)\}",
    r"<[^>\r\n]+>",
    r"\$[\w\u4e00-\u9fff]+",
    r"\\r\\n",
    r"\\n",
)
PROTECTED_NAME_RE = re.compile(
    r"(?i)(^|[_\-.:])(id|key|path|file|filename|script|formid|editorid|plugin|state|event|function|property|variable|storageutil|jsonutil|folder|directory)([_\-.:]|$)"
)
PROTECTED_TEXT_RE = re.compile(r"(?i)\.(esp|esm|esl|pex|psc|bsa|ba2|dll|exe|dds|png|nif|hkx|swf|gfx|json|xml|ini|txt)$")
RESOURCE_XML_DIR_NAMES = {"meshes", "textures", "facegendata"}


@dataclass
class Issue:
    severity: str
    file: str
    message: str


class Validator:
    def __init__(self, root: Path, mod_name: str, workspace: Path, final_mod: Path):
        self.root = root
        self.mod_name = mod_name
        self.workspace = workspace
        self.final_mod = final_mod
        self.issues: list[Issue] = []
        self.files_checked = 0
        self.interface_files_checked = 0
        self.json_files_checked = 0
        self.jsonl_files_checked = 0
        self.xml_files_checked = 0
        self.ini_files_checked = 0
        self.csv_files_checked = 0
        self.protected_exact_files_checked = 0

    def add_issue(self, severity: str, relative_path: str, message: str) -> None:
        self.issues.append(Issue(severity, relative_path, message))

    def compare_placeholder_tokens(self, relative_path: str, context: str, source_text: object, final_text: object) -> None:
        source_tokens = placeholder_tokens("" if source_text is None else str(source_text))
        final_tokens = placeholder_tokens("" if final_text is None else str(final_text))
        for token in sorted(set(source_tokens)):
            if final_tokens.count(token) < source_tokens.count(token):
                self.add_issue("error", relative_path, f"{context} placeholder or tag missing from final text: {token}")

    def compare_interface_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        # Interface translation files are positional: key, tab separator, and
        # line count must survive exactly or the game may load wrong strings.
        self.interface_files_checked += 1
        source_lines = read_lines(source_path)
        final_lines = read_lines(final_path)
        if len(source_lines) != len(final_lines):
            self.add_issue(
                "error",
                relative_path,
                f"Interface translation line count changed. source={len(source_lines)}, final={len(final_lines)}",
            )

        for index in range(max(len(source_lines), len(final_lines))):
            line_number = index + 1
            if index >= len(source_lines):
                self.add_issue("error", relative_path, f"Line {line_number} is an extra final line.")
                continue
            if index >= len(final_lines):
                self.add_issue("error", relative_path, f"Line {line_number} is missing in final file.")
                continue

            source_line = split_translation_line(source_lines[index])
            final_line = split_translation_line(final_lines[index])
            if not source_line["has_tab"]:
                self.add_issue("error", relative_path, f"Line {line_number} source has no tab separator.")
                continue
            if not final_line["has_tab"]:
                self.add_issue("error", relative_path, f"Line {line_number} final has no tab separator.")
                continue
            if source_line["key"] != final_line["key"]:
                self.add_issue(
                    "error",
                    relative_path,
                    f"Line {line_number} key changed. source='{source_line['key']}' final='{final_line['key']}'",
                )
            if source_line["text"].strip() and not final_line["text"].strip():
                self.add_issue("error", relative_path, f"Line {line_number} final translation text is empty.")
            self.compare_placeholder_tokens(relative_path, f"Line {line_number}", source_line["text"], final_line["text"])

    def compare_json_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        self.json_files_checked += 1
        source_hash = sha256(source_path)
        final_hash = sha256(final_path)
        try:
            source_json = json.loads(read_text(source_path))
            final_json = json.loads(read_text(final_path))
        except Exception as exc:
            if source_hash == final_hash:
                self.add_issue("warning", relative_path, f"JSON could not be parsed but file is unchanged, so structure check was skipped: {exc}")
            else:
                self.add_issue("error", relative_path, f"JSON changed and could not be parsed for structural validation: {exc}")
            return
        self.compare_json_value(source_json, final_json, relative_path, "$", "")

    def compare_jsonl_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        self.jsonl_files_checked += 1
        source_lines = [line for line in read_lines(source_path) if line.strip()]
        final_lines = [line for line in read_lines(final_path) if line.strip()]
        if len(source_lines) != len(final_lines):
            self.add_issue("error", relative_path, f"JSONL record count changed. source={len(source_lines)}, final={len(final_lines)}")
        for index in range(min(len(source_lines), len(final_lines))):
            line_number = index + 1
            try:
                source_json = json.loads(source_lines[index])
                final_json = json.loads(final_lines[index])
            except Exception as exc:
                self.add_issue("error", relative_path, f"Line {line_number} JSONL record could not be parsed: {exc}")
                continue
            self.compare_json_value(source_json, final_json, relative_path, f"$[{index}]", "")

    def compare_json_value(self, source_value: object, final_value: object, relative_path: str, json_path: str, key_name: str) -> None:
        # JSON structure is invariant. Only string values can differ, and even
        # then protected key/path-looking values must stay byte-for-byte equal.
        source_kind = json_kind(source_value)
        final_kind = json_kind(final_value)
        if source_kind != final_kind:
            self.add_issue("error", relative_path, f"{json_path} JSON type changed. source={source_kind}, final={final_kind}")
            return

        if source_kind == "object":
            assert isinstance(source_value, dict)
            assert isinstance(final_value, dict)
            source_names = list(source_value.keys())
            final_names = list(final_value.keys())
            for name in source_names:
                if name not in final_value:
                    self.add_issue("error", relative_path, f"{json_path} missing JSON key in final file: {name}")
            for name in final_names:
                if name not in source_value:
                    self.add_issue("error", relative_path, f"{json_path} has extra JSON key in final file: {name}")
            for name in source_names:
                if name in final_value:
                    next_path = f"$.{name}" if json_path == "$" else f"{json_path}.{name}"
                    self.compare_json_value(source_value[name], final_value[name], relative_path, next_path, name)
            return

        if source_kind == "array":
            assert isinstance(source_value, list)
            assert isinstance(final_value, list)
            if len(source_value) != len(final_value):
                self.add_issue("error", relative_path, f"{json_path} JSON array length changed. source={len(source_value)}, final={len(final_value)}")
            for index in range(min(len(source_value), len(final_value))):
                self.compare_json_value(source_value[index], final_value[index], relative_path, f"{json_path}[{index}]", key_name)
            return

        if source_kind == "string":
            source_text = str(source_value)
            final_text = str(final_value)
            if is_protected_name(key_name) or is_protected_text_value(source_text):
                if source_text != final_text:
                    self.add_issue("error", relative_path, f"{json_path} protected JSON string changed.")
            self.compare_placeholder_tokens(relative_path, json_path, source_text, final_text)
            return

        if str(source_value) != str(final_value):
            self.add_issue("error", relative_path, f"{json_path} non-string JSON value changed. source='{source_value}' final='{final_value}'")

    def compare_xml_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        self.xml_files_checked += 1
        source_hash = sha256(source_path)
        final_hash = sha256(final_path)
        try:
            source_root = ElementTree.parse(source_path).getroot()
            final_root = ElementTree.parse(final_path).getroot()
        except Exception as exc:
            if source_hash == final_hash:
                self.add_issue("warning", relative_path, f"XML could not be parsed but file is unchanged, so structure check was skipped: {exc}")
            else:
                self.add_issue("error", relative_path, f"XML changed and could not be parsed for structural validation: {exc}")
            return
        self.compare_xml_element(source_root, final_root, relative_path, f"/{source_root.tag}")

    def compare_xml_element(self, source_element: ElementTree.Element, final_element: ElementTree.Element, relative_path: str, xml_path: str) -> None:
        if source_element.tag != final_element.tag:
            self.add_issue("error", relative_path, f"{xml_path} XML element name changed. source='{source_element.tag}' final='{final_element.tag}'")
            return

        source_attrs = set(source_element.attrib.keys())
        final_attrs = set(final_element.attrib.keys())
        for name in sorted(source_attrs - final_attrs):
            self.add_issue("error", relative_path, f"{xml_path} missing XML attribute in final file: {name}")
        for name in sorted(final_attrs - source_attrs):
            self.add_issue("error", relative_path, f"{xml_path} has extra XML attribute in final file: {name}")
        for name in sorted(source_attrs & final_attrs):
            source_value = source_element.attrib[name]
            final_value = final_element.attrib[name]
            if is_protected_name(name) or is_protected_text_value(source_value):
                if source_value != final_value:
                    self.add_issue("error", relative_path, f"{xml_path} protected XML attribute changed: {name}")
            self.compare_placeholder_tokens(relative_path, f"{xml_path}@{name}", source_value, final_value)

        source_children = [item for item in list(source_element) if isinstance(item.tag, str)]
        final_children = [item for item in list(final_element) if isinstance(item.tag, str)]
        if len(source_children) != len(final_children):
            self.add_issue(
                "error",
                relative_path,
                f"{xml_path} XML child element count changed. source={len(source_children)}, final={len(final_children)}",
            )
        for index in range(min(len(source_children), len(final_children))):
            self.compare_xml_element(source_children[index], final_children[index], relative_path, f"{xml_path}/{source_children[index].tag}[{index}]")

        if not source_children and is_protected_name(source_element.tag):
            if (source_element.text or "") != (final_element.text or ""):
                self.add_issue("error", relative_path, f"{xml_path} protected XML element text changed.")
        if not source_children:
            self.compare_placeholder_tokens(relative_path, xml_path, source_element.text or "", final_element.text or "")

    def compare_ini_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        self.ini_files_checked += 1
        source_entries = ini_entries(source_path)
        final_entries = ini_entries(final_path)
        if len(source_entries) != len(final_entries):
            self.add_issue("error", relative_path, f"INI section/key count changed. source={len(source_entries)}, final={len(final_entries)}")
        for index in range(min(len(source_entries), len(final_entries))):
            source_entry = source_entries[index]
            final_entry = final_entries[index]
            if source_entry["kind"] != final_entry["kind"] or source_entry["id"] != final_entry["id"]:
                self.add_issue(
                    "error",
                    relative_path,
                    f"INI entry changed at ordinal {index + 1}. source='{source_entry['id']}' final='{final_entry['id']}'",
                )
                continue
            if source_entry["kind"] == "key" and (is_protected_name(source_entry["name"]) or is_protected_text_value(source_entry["value"])):
                if source_entry["value"] != final_entry["value"]:
                    self.add_issue("error", relative_path, f"INI protected value changed for key '{source_entry['id']}'.")
            if source_entry["kind"] == "key":
                self.compare_placeholder_tokens(relative_path, source_entry["id"], source_entry["value"], final_entry["value"])

    def compare_csv_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        self.csv_files_checked += 1
        source_lines = read_lines(source_path)
        final_lines = read_lines(final_path)
        if len(source_lines) != len(final_lines):
            self.add_issue("error", relative_path, f"CSV line count changed. source={len(source_lines)}, final={len(final_lines)}")
        if source_lines and final_lines and source_lines[0] != final_lines[0]:
            self.add_issue("error", relative_path, "CSV header changed.")

    def compare_protected_exact_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        self.protected_exact_files_checked += 1
        if sha256(source_path) != sha256(final_path):
            self.add_issue(
                "error",
                relative_path,
                "Protected text file changed. PSC files must remain exact copies unless manually reviewed outside Codex writeback.",
            )

    def compare_text_structure_file(self, source_path: Path, final_path: Path, relative_path: str) -> None:
        self.files_checked += 1
        normalized = relative_path.replace("/", "\\")
        suffix = final_path.suffix.lower()
        if suffix == ".psc":
            self.compare_protected_exact_file(source_path, final_path, relative_path)
        elif suffix == ".xml" and is_resource_metadata_xml(relative_path):
            self.compare_protected_exact_file(source_path, final_path, relative_path)
        elif suffix == ".txt" and re.match(r"(?i)^interface\\translations\\[^\\]+\.txt$", normalized):
            self.compare_interface_file(source_path, final_path, relative_path)
        elif suffix == ".json":
            self.compare_json_file(source_path, final_path, relative_path)
        elif suffix == ".jsonl":
            self.compare_jsonl_file(source_path, final_path, relative_path)
        elif suffix == ".xml":
            self.compare_xml_file(source_path, final_path, relative_path)
        elif suffix == ".ini":
            self.compare_ini_file(source_path, final_path, relative_path)
        elif suffix == ".csv":
            self.compare_csv_file(source_path, final_path, relative_path)


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


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def read_lines(path: Path) -> list[str]:
    return read_text(path).splitlines()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def is_protected_name(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and PROTECTED_NAME_RE.search(value) is not None


def is_protected_text_value(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    return "\\" in value or "/" in value or PROTECTED_TEXT_RE.search(value) is not None


def is_resource_metadata_xml(relative_path: str) -> bool:
    parts = [part.lower() for part in relative_path.replace("/", "\\").split("\\") if part]
    return any(part in RESOURCE_XML_DIR_NAMES for part in parts)


def placeholder_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for pattern in PLACEHOLDER_PATTERNS:
        tokens.extend(match.group(0) for match in re.finditer(pattern, text))
    return tokens


def split_translation_line(line: str) -> dict[str, object]:
    if "\t" not in line:
        return {"has_tab": False, "key": line, "text": ""}
    key, text = line.split("\t", 1)
    return {"has_tab": True, "key": key, "text": text}


def json_kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "scalar"


def ini_entries(path: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    section = ""
    for line_number, line in enumerate(read_lines(path), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        match = re.match(r"^\[(.+)\]$", stripped)
        if match:
            section = match.group(1)
            entries.append({"kind": "section", "id": f"[{section}]", "name": section, "value": "", "line": line_number})
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            entries.append({"kind": "key", "id": f"[{section}]{name}", "name": name, "value": value, "line": line_number})
    return entries


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_report(root: Path, validator: Validator, report_path: Path, source_count: int, final_count: int) -> None:
    blocking = sum(1 for issue in validator.issues if issue.severity == "error")
    warnings = sum(1 for issue in validator.issues if issue.severity == "warning")
    lines = [
        "# Final Text Structure Validation",
        "",
        f"- ModName: {validator.mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {relative_path(root, validator.workspace)}",
        f"- FinalModDir: {relative_path(root, validator.final_mod)}",
        f"- Source text files indexed: {source_count}",
        f"- Final text files indexed: {final_count}",
        f"- Files checked: {validator.files_checked}",
        f"- Interface files checked: {validator.interface_files_checked}",
        f"- JSON files checked: {validator.json_files_checked}",
        f"- JSONL files checked: {validator.jsonl_files_checked}",
        f"- XML files checked: {validator.xml_files_checked}",
        f"- INI files checked: {validator.ini_files_checked}",
        f"- CSV files checked: {validator.csv_files_checked}",
        f"- Protected exact files checked: {validator.protected_exact_files_checked}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Verdict",
        "",
        "PASS: final_mod text structure has no blocking issues." if blocking == 0 else "FAIL: final_mod text structure has blocking issues.",
        "",
        "## Issues",
        "",
    ]
    if not validator.issues:
        lines.append("No structure issues.")
    else:
        lines.extend(["| Severity | File | Message |", "|---|---|---|"])
        for issue in validator.issues:
            lines.append(f"| {issue.severity} | {markdown_cell(issue.file)} | {markdown_cell(issue.message)} |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This validation is read-only.",
            "- This validation compares only project-local workspace and final_mod files.",
            "- This validation does not write plugin or PEX binaries.",
            "- This validation does not access real Skyrim, Steam, MO2/Vortex, AppData, or Documents/My Games paths.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate final_mod text structure against the project-local workspace copy.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--report-output-path", default="")
    args = parser.parse_args()

    root = project_root()
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{args.mod_name}", must_exist=True)
    workspace = find_data_root(workspace).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_path(root, default_final_mod_dir(root, args.mod_name)), must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{args.mod_name}.final_text_structure.md", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not workspace.is_dir():
        raise ValueError(f"WorkspacePath must be a directory: {args.workspace_path}")
    if not final_mod.is_dir():
        raise ValueError(f"FinalModDir must be a directory: {args.final_mod_dir}")
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    source_files = [item for item in workspace.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS]
    final_files_all = [item for item in final_mod.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS]
    final_files = [item for item in final_files_all if not relative_path(final_mod, item).replace("/", "\\").lower().startswith("meta\\")]

    source_by_relative = {relative_path(workspace, item).replace("/", "\\").lower(): item for item in source_files}
    final_by_relative = {relative_path(final_mod, item).replace("/", "\\").lower(): item for item in final_files}

    validator = Validator(root, args.mod_name, workspace, final_mod)
    for key, source_path in sorted(source_by_relative.items()):
        if key not in final_by_relative:
            validator.add_issue("error", relative_path(workspace, source_path).replace("/", "\\"), "Source text file is missing from final_mod.")
    for key, final_path in sorted(final_by_relative.items()):
        relative = relative_path(final_mod, final_path).replace("/", "\\")
        source_path = source_by_relative.get(key)
        if source_path is None:
            validator.add_issue("warning", relative, "Text file exists in final_mod without a source counterpart. Confirm it is an intentional generated output.")
            continue
        validator.compare_text_structure_file(source_path, final_path, relative)

    write_report(root, validator, report_path, len(source_files), len(final_files_all))
    blocking = sum(1 for issue in validator.issues if issue.severity == "error")
    warnings = sum(1 for issue in validator.issues if issue.severity == "warning")
    print(f"Final text structure validation written to: {report_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())

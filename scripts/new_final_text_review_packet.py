"""Create a model-review packet from text differences in delivered final_mod.

This packet is intentionally generated after final_mod assembly. It reviews the
files the user will actually install, not draft translation tables.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from file_utils import discover_regular_files, write_text_lines_if_changed as write_text_if_changed
from game_context import GameContext, game_display_label
from model_review_contract import model_claim_lines
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from proofread_translation import load_allowed_words, remove_allowed_ascii_tokens
from xml.dom import Node, minidom
from project_paths import project_root
from project_paths import is_under, resolve_project_path, relative_path
from report_utils import markdown_text_cell_backslash as markdown_cell
from route_translation_task import current_game_context
from translation_context import (
    aggregate_review_rows,
    append_review_group_sections,
    append_review_groups_table,
    validated_translation_context,
)
from translation_text import cjk_present, english_present


SUPPORTED_EXTENSIONS = {".txt", ".md", ".json", ".jsonl", ".xml", ".ini", ".csv"}
VISIBLE_JSON_TEXT_KEYS = {
    "text",
    "help",
    "description",
    "desc",
    "title",
    "label",
    "tooltip",
    "message",
    "displayname",
    "pagedisplayname",
}
PROTECTED_EXACT_KEYS = {
    "action",
    "defaultvalue",
    "form",
    "function",
    "modname",
    "order",
    "param",
    "params",
    "sourcetype",
    "type",
    "value",
}


@dataclass(frozen=True)
class ReviewItem:
    File: str
    Kind: str
    Context: str
    Source: str
    Final: str
    Risk: str = "review"








def ensure_qa_output(root: Path, value: str) -> Path:
    output = resolve_project_path(root, value, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(output, qa_root):
        raise ValueError(f"output path must be under qa/: {value}")
    return output


def read_text_auto(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    last_error: UnicodeError | None = None
    for encoding in ("utf-8", "utf-16", "cp936", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeError as exc:
            last_error = exc
            continue
    assert last_error is not None
    raise last_error


def read_lines_auto(path: Path) -> list[str]:
    return read_text_auto(path).splitlines()



def string_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_protected_name(name: str | None) -> bool:
    # Key-like names mark structure rather than player text. If a changed value
    # sits under one of these keys, the review packet flags it as protected.
    if not name or not name.strip():
        return False
    if name.strip().lower() in PROTECTED_EXACT_KEYS:
        return True
    return re.search(
        r"(^|[_\-\.:])(id|key|path|file|filename|script|form|formid|editorid|plugin|state|event|function|property|variable|storageutil|jsonutil|folder|directory|source|destination|schema|version)([_\-\.:]|$)",
        name,
        re.IGNORECASE,
    ) is not None


def is_protected_text_value(text: str | None) -> bool:
    if not text or not text.strip():
        return False
    stripped = text.strip()
    identifier_like = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(:[A-Za-z0-9_]+)?", stripped) is not None and (
        ":" in stripped or "_" in stripped or re.search(r"[A-Z]", stripped[1:]) is not None
    )
    return (
        re.search(r"[\\/]", stripped) is not None
        or re.search(r"\.(esp|esm|esl|pex|psc|bsa|ba2|dll|exe|dds|png|nif|hkx|swf|gfx|json|xml|ini|txt)(\||$)", stripped, re.IGNORECASE)
        is not None
        or re.fullmatch(r"[0-9A-Fa-f]{6,8}", stripped) is not None
        or re.fullmatch(r"\$[A-Za-z0-9_]+", stripped) is not None
        or (stripped.startswith("$") and any(ch.isalpha() for ch in stripped))
        or re.fullmatch(r"\{\d+\}\s*[A-Za-z%]+", stripped) is not None
        or re.fullmatch(r"[A-Z][A-Z0-9 ]{2,}", stripped) is not None
        or re.fullmatch(r"[A-Za-z0-9 ]+,\s+by\s+[A-Za-z0-9_ -]+", stripped, re.IGNORECASE) is not None
        or re.fullmatch(r"ID\s+\d+\s+-\s+[A-Za-z0-9_]+", stripped, re.IGNORECASE) is not None
        or identifier_like
    )



def is_untranslated_candidate_scope(file: str, kind: str, key_name: str) -> bool:
    # FOMOD and resource metadata can contain English that should not be counted
    # as missed in-game translation unless another rule marks it visible.
    normalized_file = file.replace("/", "\\").lower()
    normalized_key = key_name.strip().lower()
    if normalized_file.startswith("meshes\\"):
        return False
    if normalized_file.startswith("fomod\\"):
        return False
    if kind == "json-string":
        return normalized_key in VISIBLE_JSON_TEXT_KEYS
    return True


def likely_untranslated_candidate(text: str, file: str, kind: str, key_name: str, allowed_words: set[str]) -> bool:
    trimmed = text.strip()
    if not trimmed or cjk_present(trimmed) or not english_present(trimmed):
        return False
    if not is_untranslated_candidate_scope(file, kind, key_name):
        return False
    if is_protected_name(key_name) or is_protected_text_value(trimmed):
        return False
    remaining = remove_allowed_ascii_tokens(trimmed, allowed_words)
    return english_present(remaining)


def add_review_item(
    items: list[ReviewItem],
    file: str,
    kind: str,
    context: str,
    source_text: Any,
    final_text: Any,
    allowed_words: set[str],
    key_name: str = "",
    risk: str = "review",
) -> None:
    source = "" if source_text is None else str(source_text)
    final = "" if final_text is None else str(final_text)
    if source == final:
        if not likely_untranslated_candidate(final, file, kind, key_name, allowed_words):
            return
        risk = "untranslated-review"
    if not source.strip() and not final.strip():
        return
    items.append(ReviewItem(file, kind, context, source, final, risk))


def split_translation_line(line: str) -> tuple[bool, str, str]:
    index = line.find("\t")
    if index < 0:
        return False, line, ""
    return True, line[:index], line[index + 1 :]


def collect_interface_items(source_path: Path, final_path: Path, relative: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    source_lines = read_lines_auto(source_path)
    final_lines = read_lines_auto(final_path)
    for index in range(min(len(source_lines), len(final_lines))):
        source_has_tab, source_key, source_text = split_translation_line(source_lines[index])
        final_has_tab, final_key, final_text = split_translation_line(final_lines[index])
        if not source_has_tab or not final_has_tab or source_key != final_key:
            continue
        add_review_item(items, relative, "interface-text", f"line {index + 1}; key={source_key}", source_text, final_text, allowed_words)


def json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "scalar"


def collect_json_items(source_value: Any, final_value: Any, relative: str, json_path: str, key_name: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    source_kind = json_kind(source_value)
    final_kind = json_kind(final_value)
    if source_kind != final_kind:
        return

    if source_kind == "object":
        for key, nested_source in source_value.items():
            if key not in final_value:
                continue
            next_path = f"$.{key}" if json_path == "$" else f"{json_path}.{key}"
            collect_json_items(nested_source, final_value[key], relative, next_path, key, items, allowed_words)
        return

    if source_kind == "array":
        for index in range(min(len(source_value), len(final_value))):
            collect_json_items(source_value[index], final_value[index], relative, f"{json_path}[{index}]", key_name, items, allowed_words)
        return

    if source_kind == "string":
        risk = "protected-review" if is_protected_name(key_name) or is_protected_text_value(source_value) else "review"
        add_review_item(items, relative, "json-string", json_path, source_value, final_value, allowed_words, key_name, risk)


def collect_json_file_items(source_path: Path, final_path: Path, relative: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    try:
        source_json = json.loads(read_text_auto(source_path))
        final_json = json.loads(read_text_auto(final_path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Final text review cannot parse JSON file {relative}: {exc}") from exc
    collect_json_items(source_json, final_json, relative, "$", "", items, allowed_words)


def collect_jsonl_file_items(source_path: Path, final_path: Path, relative: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    source_lines = [line for line in read_lines_auto(source_path) if line.strip()]
    final_lines = [line for line in read_lines_auto(final_path) if line.strip()]
    if len(source_lines) != len(final_lines):
        raise ValueError(
            f"Final text review JSONL row count differs for {relative}: "
            f"source={len(source_lines)} final={len(final_lines)}"
        )
    for index in range(len(source_lines)):
        try:
            source_json = json.loads(source_lines[index])
            final_json = json.loads(final_lines[index])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Final text review cannot parse JSONL file {relative} line {index + 1}: {exc}") from exc
        collect_json_items(source_json, final_json, relative, f"$[{index}]", "", items, allowed_words)


def element_children(node: Node) -> list[Node]:
    return [child for child in node.childNodes if child.nodeType == Node.ELEMENT_NODE]


def node_name(node: Node) -> str:
    return node.nodeName


def node_inner_text(node: Node) -> str:
    text_parts: list[str] = []
    for child in node.childNodes:
        if child.nodeType in {Node.TEXT_NODE, Node.CDATA_SECTION_NODE}:
            text_parts.append(child.data)
        elif child.nodeType == Node.ELEMENT_NODE:
            text_parts.append(node_inner_text(child))
    return "".join(text_parts)


def collect_xml_element_items(source_element: Node, final_element: Node, relative: str, xml_path: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    if node_name(source_element) != node_name(final_element):
        return

    if source_element.attributes is not None and final_element.attributes is not None:
        for index in range(source_element.attributes.length):
            source_attr = source_element.attributes.item(index)
            if source_attr is None:
                continue
            final_attr = final_element.attributes.get(source_attr.name)
            if final_attr is None:
                continue
            risk = "protected-review" if is_protected_name(source_attr.name) or is_protected_text_value(source_attr.value) else "review"
            add_review_item(items, relative, "xml-attribute", f"{xml_path}@{source_attr.name}", source_attr.value, final_attr.value, allowed_words, source_attr.name, risk)

    source_children = element_children(source_element)
    final_children = element_children(final_element)
    if not source_children and not final_children:
        risk = "protected-review" if is_protected_name(node_name(source_element)) else "review"
        add_review_item(items, relative, "xml-text", xml_path, node_inner_text(source_element), node_inner_text(final_element), allowed_words, node_name(source_element), risk)
        return

    for index in range(min(len(source_children), len(final_children))):
        child = source_children[index]
        collect_xml_element_items(child, final_children[index], relative, f"{xml_path}/{node_name(child)}[{index}]", items, allowed_words)


def collect_xml_file_items(source_path: Path, final_path: Path, relative: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    try:
        source_doc = minidom.parseString(read_text_auto(source_path).encode("utf-8"))
        final_doc = minidom.parseString(read_text_auto(final_path).encode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Final text review cannot parse XML file {relative}: {exc}") from exc
    if source_doc.documentElement is None or final_doc.documentElement is None:
        raise ValueError(f"Final text review XML file has no document element: {relative}")
    collect_xml_element_items(source_doc.documentElement, final_doc.documentElement, relative, f"/{node_name(source_doc.documentElement)}", items, allowed_words)


def ini_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    section = ""
    for line_number, line in enumerate(read_lines_auto(path), start=1):
        trimmed = line.strip()
        if not trimmed or trimmed.startswith(";") or trimmed.startswith("#"):
            continue
        match = re.fullmatch(r"\[(.+)\]", trimmed)
        if match:
            section = match.group(1)
            continue
        index = line.find("=")
        if index >= 0:
            name = line[:index].strip()
            value = line[index + 1 :].strip()
            entries.append({"Id": f"[{section}]{name}", "Name": name, "Value": value, "Line": line_number})
    return entries


def collect_ini_file_items(source_path: Path, final_path: Path, relative: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    source_entries = ini_entries(source_path)
    final_entries = ini_entries(final_path)
    for index in range(min(len(source_entries), len(final_entries))):
        source_entry = source_entries[index]
        final_entry = final_entries[index]
        if source_entry["Id"] != final_entry["Id"]:
            continue
        risk = "protected-review" if is_protected_name(source_entry["Name"]) or is_protected_text_value(source_entry["Value"]) else "review"
        add_review_item(
            items,
            relative,
            "ini-value",
            f"{source_entry['Id']}; line {source_entry['Line']}",
            source_entry["Value"],
            final_entry["Value"],
            allowed_words,
            source_entry["Name"],
            risk,
        )

    source_lines = read_lines_auto(source_path)
    final_lines = read_lines_auto(final_path)
    for index in range(min(len(source_lines), len(final_lines))):
        source_comment = source_lines[index].strip()
        final_comment = final_lines[index].strip()
        if not source_comment.startswith((";", "#")):
            continue
        if not final_comment.startswith((";", "#")) or source_comment == final_comment:
            continue
        add_review_item(
            items,
            relative,
            "ini-comment",
            f"source_line={index + 1}; target_line={index + 1}",
            source_comment[1:].strip(),
            final_comment[1:].strip(),
            allowed_words,
        )


def collect_line_items(source_path: Path, final_path: Path, relative: str, kind: str, items: list[ReviewItem], allowed_words: set[str]) -> None:
    source_lines = read_lines_auto(source_path)
    final_lines = read_lines_auto(final_path)
    line_total = max(len(source_lines), len(final_lines))
    for index in range(line_total):
        source_text = source_lines[index] if index < len(source_lines) else ""
        final_text = final_lines[index] if index < len(final_lines) else ""
        section_heading = source_section_heading(source_lines, index)
        context = (
            f"source_line={index + 1 if index < len(source_lines) else 'missing'}; "
            f"target_line={index + 1 if index < len(final_lines) else 'missing'}; "
            f"section={section_heading}; section_hash={string_sha256(section_heading)[:12]}"
        )
        if len(source_lines) != len(final_lines):
            context += f"; line_mapping=source:{len(source_lines)} target:{len(final_lines)}"
        add_review_item(items, relative, kind, context, source_text, final_text, allowed_words)


def source_section_heading(lines: list[str], index: int) -> str:
    if not lines:
        return "root"
    safe_index = min(max(index, 0), len(lines) - 1)
    for line in reversed(lines[: safe_index + 1]):
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or "root"
    return "root"


def collect_supported_files(root_dir: Path) -> list[Path]:
    return [
        path
        for path in discover_regular_files(root_dir, label="Final text review input directory")
        if path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def build_source_index(workspace: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in collect_supported_files(workspace):
        relative = relative_path(workspace, path).replace("/", "\\")
        index[relative.lower()] = path
    return index


def collect_review_items(workspace: Path, final_mod: Path) -> tuple[int, list[ReviewItem]]:
    source_by_relative = build_source_index(workspace)
    allowed_words = load_allowed_words(project_root())
    files_compared = 0
    items: list[ReviewItem] = []
    for final_file in collect_supported_files(final_mod):
        relative = relative_path(final_mod, final_file).replace("/", "\\")
        if re.match(r"(?i)^meta\\", relative):
            continue
        source_file = source_by_relative.get(relative.lower())
        if source_file is None:
            continue
        files_compared += 1
        extension = final_file.suffix.lower()
        if extension == ".txt" and re.match(r"(?i)^interface\\translations\\[^\\]+\.txt$", relative):
            collect_interface_items(source_file, final_file, relative, items, allowed_words)
        elif extension == ".json":
            collect_json_file_items(source_file, final_file, relative, items, allowed_words)
        elif extension == ".jsonl":
            collect_jsonl_file_items(source_file, final_file, relative, items, allowed_words)
        elif extension == ".xml":
            collect_xml_file_items(source_file, final_file, relative, items, allowed_words)
        elif extension == ".ini":
            collect_ini_file_items(source_file, final_file, relative, items, allowed_words)
        elif extension == ".csv":
            collect_line_items(source_file, final_file, relative, "csv-line", items, allowed_words)
        elif extension in {".txt", ".md"}:
            collect_line_items(source_file, final_file, relative, "text-line", items, allowed_words)
    return files_compared, sorted(items, key=lambda item: (item.File, item.Kind, item.Context, item.Source, item.Final))


def write_packet(
    root: Path,
    mod_name: str,
    workspace: Path,
    final_mod: Path,
    packet_path: Path,
    items_path: Path,
    files_compared: int,
    review_items: list[ReviewItem],
    *,
    game_context: GameContext | None = None,
    context_payload: dict[str, object] | None = None,
    context_path: Path | None = None,
) -> tuple[bool, bool, str]:
    jsonl_lines = [json.dumps(asdict(item), ensure_ascii=False, separators=(",", ":")) for item in review_items]
    items_text = "\n".join(jsonl_lines) + "\n"
    items_hash = string_sha256(items_text)
    items_changed = write_text_if_changed(items_path, jsonl_lines)

    protected_count = sum(1 for item in review_items if item.Risk == "protected-review")
    grouped_rows = aggregate_review_rows(
        [
            {
                "File": item.File,
                "Line": 0,
                "Type": item.Kind,
                "Risk": item.Risk,
                "Context": item.Context,
                "Source": item.Source,
                "Target": item.Final,
            }
            for item in review_items
        ]
    )
    context_payload = context_payload or {}
    context_status = str(context_payload.get("status", "missing")).strip() or "missing"
    packet_lines: list[str] = [
        f"# Final Text Model Review Packet: {mod_name}",
        "",
        f"- Game: {game_display_label(game_context)}" if game_context else "- Game: current workspace Game Profile",
        f"- Items SHA256: {items_hash}",
        f"- Workspace: {relative_path(root, workspace)}",
        f"- FinalModDir: {relative_path(root, final_mod)}",
        f"- Files compared: {files_compared}",
        f"- Review items: {len(review_items)}",
        f"- Aggregated review groups: {len(grouped_rows)}",
        f"- Protected review items: {protected_count}",
        f"- Items JSONL: {relative_path(root, items_path)}",
        f"- Mod context: {relative_path(root, context_path)}" if context_path else "- Mod context: missing",
        f"- Mod context status: {context_status}",
        f"- Mod summary: {context_payload.get('summary', '')}",
        "",
        "## Review Instructions",
        "",
        "The reviewing agent must review these rows with model judgment because they are actual final_mod text rows: changed text plus suspicious unchanged English candidates, not just intermediate translation tables.",
        "",
        "Check:",
        "",
        "- The final Chinese is natural Simplified Chinese game localization.",
        "- UI/MCM text stays short, clear, and not wordy.",
        "- Terminology, tone, and world context fit the current Game Profile and evidence-bound Mod summary; do not impose an unsupported genre or setting.",
        "- Subjects, objects, actions, control ownership, and functional relationships remain complete instead of being translated word by word.",
        "- Short labels are understandable with related help text and use the same terminology.",
        "- Semantic-focus groups receive targeted review, especially conflicting targets, short UI labels, and long help text.",
        "- Any English left in final text is intentional: mod/tool name, acronym, URL, plugin/file/path, or protected token.",
        "- Rows marked protected-review are safe because the value is visible text, or else they must be reverted before delivery.",
        "- Do not mark this packet passed only because mechanical QA is green.",
        "- Rows marked untranslated-review are unchanged English candidates and must be translated or explicitly justified before delivery.",
        "- The final model review must explicitly mention every final_mod text file listed in the JSONL packet.",
        "",
        "The model review output must mention this packet, the JSONL path, the Items SHA256, every reviewed file, and these exact passing claims:",
        "",
        *model_claim_lines(code=True),
        "",
        "## Aggregated Rows",
        "",
        "Only rows with the same source, final text, kind, risk, and context are compressed. The raw Items JSONL remains complete occurrence-level evidence. A conclusion for a group ID covers all listed occurrences unless the finding names an exception.",
        "",
    ]
    if not grouped_rows:
        packet_lines.append("No changed final text rows were found.")
    else:
        append_review_groups_table(
            packet_lines,
            grouped_rows,
            kind_heading="Kind",
            target_heading="Final",
            include_line=False,
            cell=markdown_cell,
        )
    append_review_group_sections(packet_lines, grouped_rows)
    packet_changed = write_text_if_changed(packet_path, packet_lines)
    return packet_changed, items_changed, items_hash


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an agent model review packet from actual final_mod text differences.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--packet-output-path", default="")
    parser.add_argument("--items-jsonl-path", default="")
    args = parser.parse_args()

    root = project_root()
    mod_name = args.mod_name
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{mod_name}", must_exist=True)
    workspace = find_data_root(workspace).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_path(root, default_final_mod_dir(root, mod_name)), must_exist=True)
    if not workspace.is_dir():
        raise ValueError(f"WorkspacePath must be a directory: {args.workspace_path or workspace}")
    if not final_mod.is_dir():
        raise ValueError(f"FinalModDir must be a directory: {args.final_mod_dir or final_mod}")

    packet_path = ensure_qa_output(root, args.packet_output_path or f"qa/{mod_name}.final_text_review_packet.md")
    items_path = ensure_qa_output(root, args.items_jsonl_path or f"qa/{mod_name}.final_text_review_items.jsonl")
    files_compared, review_items = collect_review_items(workspace, final_mod)
    context_path = root / "qa" / f"{mod_name}.translation_context.json"
    game_context = current_game_context(root)
    context_payload, _context_issues = validated_translation_context(root, mod_name, game_context)
    packet_changed, items_changed, _items_hash = write_packet(
        root,
        mod_name,
        workspace,
        final_mod,
        packet_path,
        items_path,
        files_compared,
        review_items,
        game_context=game_context,
        context_payload=context_payload,
        context_path=context_path,
    )
    protected_count = sum(1 for item in review_items if item.Risk == "protected-review")

    print(f"Final text review packet written to: {packet_path}")
    print(f"Final text review items written to: {items_path}")
    print(f"Files compared: {files_compared}")
    print(f"Review items: {len(review_items)}")
    print(f"Protected review items: {protected_count}")
    print(f"Packet changed: {packet_changed}")
    print(f"Items changed: {items_changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

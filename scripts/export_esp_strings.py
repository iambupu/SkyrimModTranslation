import argparse
import json
import os
import re
import string
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path


VISIBLE_SUBRECORDS = {"FULL", "DESC", "ITXT"}
CONDITIONAL_VISIBLE_SUBRECORDS = {
    ("MGEF", "DNAM"): "magic-effect-description",
}
CHINESE_PUNCTUATION = "，。！？、；：‘’“”（）《》〈〉【】「」『』—…·"
PROTECTED_SUBRECORDS = {
    "EDID",
    "MAST",
    "MODL",
    "ICON",
    "MICO",
    "SNAM",
    "SCRI",
    "WNAM",
    "ANAM",
}
PLUGIN_EXTENSIONS = (".esp", ".esm", ".esl")
RISKY_PATH_MARKERS = (
    "SteamLibrary",
    "steamapps",
    "Skyrim Special Edition\\Data",
    "ModOrganizer",
    "Vortex",
    "AppData",
    "Documents\\My Games",
)


@dataclass
class RecordContext:
    record_type: str
    form_id: int
    editor_id: str
    group_path: str
    offset: int


class EspParseError(Exception):
    pass


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def sig(data: bytes, offset: int) -> str:
    return data[offset : offset + 4].decode("ascii", errors="replace")


def rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def ensure_inside(child: Path, parent: Path) -> None:
    if not is_under(child, parent):
        child_resolved = child.resolve(strict=False)
        raise SystemExit(f"unsafe path outside project: {child_resolved}")


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
    ensure_inside(resolved, root)
    if has_risky_path_marker(str(resolved)):
        raise SystemExit(f"refusing risky path: {resolved}")
    return resolved


def safe_file_name(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().strip(".")
    if not safe:
        raise SystemExit("ModName cannot be empty after sanitization.")
    return safe


def infer_mod_name(root: Path, plugin_path: Path) -> str:
    work_root = root / "work" / "extracted_mods"
    if is_under(plugin_path, work_root):
        relative = Path(os.path.relpath(str(plugin_path), str(work_root)))
        if relative.parts:
            return relative.parts[0]
    return plugin_path.stem


def validate_plugin_location(root: Path, plugin_path: Path, *, allow_generated_plugin: bool) -> None:
    if plugin_path.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise SystemExit(f"PluginPath must be .esp, .esm, or .esl: {plugin_path}")
    original_roots = [root / "work" / "extracted_mods", root / "mod"]
    generated_roots = [root / "out", root / "translated" / "tool_outputs"]
    is_original = any(is_under(plugin_path, allowed_root) for allowed_root in original_roots)
    is_generated = any(is_under(plugin_path, allowed_root) for allowed_root in generated_roots)
    if not (is_original or (allow_generated_plugin and is_generated)):
        raise SystemExit(
            "PluginPath must be under project mod/ or work/extracted_mods/ unless "
            "--allow-generated-plugin is set for out/ or translated/tool_outputs/."
        )


def has_risky_path_marker(value: str) -> bool:
    return any(marker.lower() in value.lower() for marker in RISKY_PATH_MARKERS)


def decode_possible_string(payload: bytes) -> str:
    raw = payload.rstrip(b"\x00")
    if not raw:
        return ""
    for encoding in ("utf-8", "cp1252"):
        try:
            value = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        value = raw.decode("utf-8", errors="replace")
    value = value.replace("\x00", "")
    return value


def is_probable_text(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 2 and not any("\u4e00" <= ch <= "\u9fff" for ch in stripped):
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    if any(ord(ch) < 32 and ch not in "\t\r\n" for ch in value):
        return False
    control_count = sum(
        1
        for ch in value
        if ch not in string.printable and not ("\u4e00" <= ch <= "\u9fff") and ch not in CHINESE_PUNCTUATION
    )
    return control_count <= max(1, len(value) // 10)


def classify_string(record_type: str, subrecord_type: str, value: str) -> tuple[str, str]:
    stripped = value.strip()
    if not stripped:
        return "skip", "empty"
    if subrecord_type in PROTECTED_SUBRECORDS:
        return "protected", f"protected-subrecord-{subrecord_type}"
    if stripped.endswith(PLUGIN_EXTENSIONS) or "\\" in stripped or "/" in stripped:
        return "protected", "file-or-plugin-name"
    if record_type == "HDPT" and subrecord_type == "FULL":
        return "review", "headpart-display-name-not-required"
    if subrecord_type in VISIBLE_SUBRECORDS:
        return "candidate", f"visible-subrecord-{subrecord_type}"
    conditional_reason = CONDITIONAL_VISIBLE_SUBRECORDS.get((record_type, subrecord_type))
    if conditional_reason:
        return "candidate", conditional_reason
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:.+-]*", stripped) and " " not in stripped:
        return "protected", "identifier-like"
    if " " in stripped and any(ch.isalpha() for ch in stripped):
        return "review", "human-readable-unknown-subrecord"
    return "review", "uncertain"


def parse_subrecords(record_data: bytes) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    next_size: int | None = None
    index = 0
    while offset + 6 <= len(record_data):
        sub_type = sig(record_data, offset)
        size = u16(record_data, offset + 4)
        offset += 6
        if sub_type == "XXXX":
            if size >= 4 and offset + size <= len(record_data):
                next_size = u32(record_data, offset)
            offset += size
            continue
        if next_size is not None:
            size = next_size
            next_size = None
        if size < 0 or offset + size > len(record_data):
            break
        payload = record_data[offset : offset + size]
        text = decode_possible_string(payload)
        if is_probable_text(text):
            rows.append(
                {
                    "subrecord_type": sub_type,
                    "subrecord_index": index,
                    "source": text,
                    "payload_size": size,
                }
            )
        offset += size
        index += 1
    return rows


def find_editor_id(subrecords: list[dict]) -> str:
    for subrecord in subrecords:
        if subrecord["subrecord_type"] == "EDID":
            return subrecord["source"]
    return ""


def group_label(data: bytes, offset: int, group_type: int) -> str:
    label = data[offset + 8 : offset + 12]
    if group_type == 0:
        return label.decode("ascii", errors="replace")
    return "0x" + label[::-1].hex().upper()


def parse_elements(data: bytes, start: int, end: int, group_stack: list[str], rows: list[dict], stats: dict) -> None:
    offset = start
    while offset + 24 <= end:
        marker = data[offset : offset + 4]
        marker_text = marker.decode("ascii", errors="replace")
        if marker == b"GRUP":
            size = u32(data, offset + 4)
            if size < 24 or offset + size > len(data):
                stats["invalid_groups"] += 1
                return
            group_type = u32(data, offset + 12)
            label = group_label(data, offset, group_type)
            stats["groups"] += 1
            parse_elements(data, offset + 24, offset + size, group_stack + [label], rows, stats)
            offset += size
            continue
        if not re.fullmatch(r"[A-Z0-9]{4}", marker_text):
            stats["parse_stops"] += 1
            return
        record_type = marker_text
        data_size = u32(data, offset + 4)
        flags = u32(data, offset + 8)
        form_id = u32(data, offset + 12)
        record_data_start = offset + 24
        record_data_end = record_data_start + data_size
        if record_data_end > len(data) or record_data_end > end:
            stats["invalid_records"] += 1
            return
        record_data = data[record_data_start:record_data_end]
        compressed = bool(flags & 0x00040000)
        if compressed:
            stats["compressed_records"] += 1
            try:
                uncompressed_size = u32(record_data, 0)
                record_data = zlib.decompress(record_data[4:])
                if len(record_data) != uncompressed_size:
                    stats["compressed_size_mismatch"] += 1
            except Exception:
                offset = record_data_end
                continue
        stats["records"] += 1
        subrecords = parse_subrecords(record_data)
        editor_id = find_editor_id(subrecords)
        context = RecordContext(
            record_type=record_type,
            form_id=form_id,
            editor_id=editor_id,
            group_path="/".join(group_stack),
            offset=offset,
        )
        for subrecord in subrecords:
            risk, reason = classify_string(context.record_type, subrecord["subrecord_type"], subrecord["source"])
            if risk == "skip":
                continue
            stats["strings"] += 1
            stats[f"risk_{risk}"] = stats.get(f"risk_{risk}", 0) + 1
            rows.append(
                {
                    "file": "",
                    "plugin": "",
                    "record_type": context.record_type,
                    "form_id": f"{context.form_id:08X}",
                    "editor_id": context.editor_id,
                    "group_path": context.group_path,
                    "record_offset": context.offset,
                    "subrecord_type": subrecord["subrecord_type"],
                    "subrecord_index": subrecord["subrecord_index"],
                    "payload_size": subrecord["payload_size"],
                    "source": subrecord["source"],
                    "target": "",
                    "risk": risk,
                    "reason": reason,
                }
            )
        offset = record_data_end


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read project-local Skyrim ESP/ESM/ESL text candidates into JSONL.")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--plugin-path", required=True)
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--allow-generated-plugin", action="store_true")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve(strict=True) if args.project_root else Path(__file__).resolve().parents[1]
    if has_risky_path_marker(str(project_root)):
        raise SystemExit(f"refusing risky project root: {project_root}")
    plugin_path = resolve_project_path(project_root, args.plugin_path, must_exist=True)
    validate_plugin_location(project_root, plugin_path, allow_generated_plugin=args.allow_generated_plugin)
    mod_name = safe_file_name(args.mod_name or infer_mod_name(project_root, plugin_path))
    output_path = resolve_project_path(
        project_root,
        args.output_path or f"source/plugin_exports/{mod_name}/{plugin_path.stem}.esp_strings.jsonl",
        must_exist=False,
    )
    report_path = resolve_project_path(
        project_root,
        args.report_path or f"qa/{plugin_path.stem}.esp_export_report.md",
        must_exist=False,
    )
    if not is_under(output_path, project_root / "source"):
        raise SystemExit(f"OutputPath must be under source/: {output_path}")
    if not is_under(report_path, project_root / "qa"):
        raise SystemExit(f"ReportPath must be under qa/: {report_path}")

    data = plugin_path.read_bytes()
    if len(data) < 24 or data[:4] != b"TES4":
        raise SystemExit("not a supported TES4-family plugin")

    stats: dict[str, int] = {
        "groups": 0,
        "records": 0,
        "strings": 0,
        "invalid_groups": 0,
        "invalid_records": 0,
        "compressed_records": 0,
        "compressed_size_mismatch": 0,
        "parse_stops": 0,
        "risk_candidate": 0,
        "risk_protected": 0,
        "risk_review": 0,
    }
    rows: list[dict] = []
    parse_elements(data, 0, len(data), [], rows, stats)
    for row in rows:
        row["file"] = rel(project_root, plugin_path)
        row["plugin"] = plugin_path.name

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_path, rows)

    masters = [row["source"] for row in rows if row["subrecord_type"] == "MAST"]
    candidate_rows = [row for row in rows if row["risk"] == "candidate"]
    protected_rows = [row for row in rows if row["risk"] == "protected"]
    review_rows = [row for row in rows if row["risk"] == "review"]

    report = [
        "# ESP String Export Report",
        "",
        f"- ModName: {mod_name}",
        f"- Plugin: {rel(project_root, plugin_path)}",
        f"- Output: {rel(project_root, output_path)}",
        f"- File size: {len(data)}",
        f"- Records parsed: {stats['records']}",
        f"- Groups parsed: {stats['groups']}",
        f"- String rows: {len(rows)}",
        f"- Translation candidates: {len(candidate_rows)}",
        f"- Protected strings: {len(protected_rows)}",
        f"- Review strings: {len(review_rows)}",
        f"- Masters detected: {len(masters)}",
        "",
        "## Masters",
        "",
    ]
    if masters:
        report.extend(f"- {master}" for master in masters)
    else:
        report.append("No master strings were detected.")
    report.extend(
        [
            "",
            "## Candidate Strings",
            "",
            "| Record | FormID | Subrecord | EditorID | Source |",
            "|---|---|---|---|---|",
        ]
    )
    for row in candidate_rows:
        source = row["source"].replace("|", "\\|").replace("\n", "\\n")
        editor_id = row["editor_id"].replace("|", "\\|")
        report.append(f"| {row['record_type']} | {row['form_id']} | {row['subrecord_type']} | {editor_id} | {source} |")
    report.extend(
        [
            "",
            "## Parser Stats",
            "",
        ]
    )
    for key in sorted(stats):
        report.append(f"- {key}: {stats[key]}")
    report.extend(
        [
            "",
            "## Safety",
            "",
            "- This exporter is read-only.",
            "- It does not need real Skyrim, MO2/Vortex, Steam, AppData, or Documents/My Games paths.",
            "- It does not load masters, write back plugins, patch binaries, or install files.",
            "- Output is a translation middle file only; final plugin writeback still requires a controlled adapter.",
        ]
    )
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"ESP string export: {output_path}")
    print(f"ESP export report: {report_path}")
    print(f"Translation candidates: {len(candidate_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

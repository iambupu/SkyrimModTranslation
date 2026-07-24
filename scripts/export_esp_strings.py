"""Read visible string candidates from project-local ESP/ESM/ESL files.

This lightweight parser is read-only. It does not load masters, save plugins, or
claim writeback support; uncertain records are skipped instead of guessed.
"""

import argparse
import os
import re
import string
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from project_paths import is_under, resolve_project_path as resolve_workspace_path

from adapter_registry import require_adapter
from capability_resolver import CapabilityDecision, resolve_capability
from game_context import GameContext, resolve_workspace_game_context, supported_game_ids
from project_paths import project_root as default_project_root
from project_paths import safe_file_name
from file_utils import write_jsonl_sorted as write_jsonl
from managed_tool_resolver import (
    adapter_uses_managed_binding,
    leased_payload_path,
    load_workspace_tool_config,
)
from project_paths import relative_posix_strict as rel


VISIBLE_SUBRECORDS = {"FULL", "DESC", "ITXT"}
CONDITIONAL_VISIBLE_SUBRECORDS = {
    ("MGEF", "DNAM"): "magic-effect-description",
    ("INFO", "NAM1"): "dialog-response-text",
    ("INFO", "RNAM"): "dialog-prompt-text",
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
LOCALIZED_FLAG = 0x00000080
FIELD_PATHS = {
    "FULL": "Name",
    "DESC": "Description",
    "DNAM": "Description",
    "ITXT": "MenuButtons",
    "NAM1": "Responses",
    "RNAM": "Prompt",
}
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


def ensure_inside(child: Path, parent: Path) -> None:
    if not is_under(child, parent):
        child_resolved = child.resolve(strict=False)
        raise SystemExit(f"unsafe path outside project: {child_resolved}")


def resolve_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    resolved = resolve_workspace_path(root, value, must_exist=must_exist)
    ensure_inside(resolved, root)
    if has_risky_path_marker(str(resolved)):
        raise SystemExit(f"refusing risky path: {resolved}")
    return resolved


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


def has_risky_path_marker(value: str, context: GameContext | None = None) -> bool:
    markers = context.risky_paths if context else RISKY_PATH_MARKERS
    return any(marker.lower() in value.lower() for marker in markers)


def resolve_game_context(root: Path, explicit_game: str) -> GameContext:
    try:
        return resolve_workspace_game_context(root, explicit_game)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def run_mutagen_export(
    root: Path,
    input_plugin: Path,
    output_jsonl: Path,
    report_path: Path,
    context: GameContext,
    decision: CapabilityDecision,
    master_style_manifest: Path | None,
) -> int:
    config = load_workspace_tool_config(root)
    with leased_payload_path(
        root,
        config,
        "MutagenCliPath",
        command="export plugin strings",
    ) as adapter_resolution, leased_payload_path(
        root,
        config,
        "DotNetSdkPath",
        command="export plugin strings",
        managed_only=adapter_uses_managed_binding(
            root,
            config,
            "MutagenCliPath",
        ),
    ) as dotnet_resolution:
        if dotnet_resolution.path is None or adapter_resolution.path is None:
            raise FileNotFoundError("managed plugin exporter binding is unavailable")
        command = [
            str(dotnet_resolution.path),
            str(adapter_resolution.path),
            "export",
            "--game",
            context.game_id,
            "--mutagen-release",
            str(decision.adapter_options["mutagen_release"]),
            "--capability-level",
            decision.level,
            "--project-root",
            str(root),
            "--input-plugin",
            str(input_plugin),
            "--output-jsonl",
            str(output_jsonl),
            "--report",
            str(report_path),
        ]
        if master_style_manifest is not None:
            command.extend(["--master-style-manifest", str(master_style_manifest)])
        return subprocess.run(command, cwd=str(root), check=False).returncode


def write_blocked_report(
    report_path: Path,
    context: GameContext,
    adapter_id: str,
    capability_level: str,
    reason: str,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(
            [
                "# ESP String Export Report",
                "",
                f"- game_id: {context.game_id}",
                f"- game_profile_version: {context.schema_version}",
                f"- plugin_adapter: {adapter_id}",
                f"- plugin_text_capability_level: {capability_level}",
                "- Status: blocked",
                f"- Reason: {reason}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def field_path(record_type: str, subrecord_type: str) -> str:
    path = FIELD_PATHS.get(subrecord_type, subrecord_type)
    if subrecord_type == "ITXT":
        return "MenuButtons[].Text"
    if subrecord_type == "NAM1":
        return "Responses[].Text"
    return path


def writeback_status() -> str:
    # The fallback parser is discovery-only. Writable rows come from the
    # controlled adapter so export and apply share one field contract.
    return "unsupported"


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


def is_internal_identifier_like(value: str) -> bool:
    stripped = value.strip()
    if " " in stripped:
        return False
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:.+-]*", stripped):
        return False
    return any(marker in stripped for marker in ("_", ".", ":"))


def classify_string(record_type: str, subrecord_type: str, value: str) -> tuple[str, str]:
    stripped = value.strip()
    if not stripped:
        return "skip", "empty"
    if subrecord_type in PROTECTED_SUBRECORDS:
        return "protected", f"protected-subrecord-{subrecord_type}"
    if stripped.endswith(PLUGIN_EXTENSIONS) or "\\" in stripped or "/" in stripped:
        return "protected", "file-or-plugin-name"
    if is_internal_identifier_like(stripped):
        return "protected", "identifier-like"
    if record_type == "HDPT" and subrecord_type == "FULL":
        return "review", "headpart-display-name-not-required"
    if subrecord_type in VISIBLE_SUBRECORDS:
        return "candidate", f"visible-subrecord-{subrecord_type}"
    conditional_reason = CONDITIONAL_VISIBLE_SUBRECORDS.get((record_type, subrecord_type))
    if conditional_reason:
        return "candidate", conditional_reason
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


def parse_elements(
    data: bytes,
    start: int,
    end: int,
    group_stack: list[str],
    rows: list[dict],
    stats: dict,
    game_id: str,
) -> None:
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
            parse_elements(data, offset + 24, offset + size, group_stack + [label], rows, stats, game_id)
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
        # Compressed records carry an uncompressed-size prefix followed by zlib
        # data. If this probe fails, skip the record so the parser does not drift
        # into later records and invent false candidates.
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
                    "schema_version": 2,
                    "game_id": game_id,
                    "file": "",
                    "plugin": "",
                    "record_type": context.record_type,
                    "form_id": f"{context.form_id:08X}",
                    "editor_id": context.editor_id,
                    "field_path": field_path(context.record_type, subrecord["subrecord_type"]),
                    "group_path": context.group_path,
                    "record_offset": context.offset,
                    "subrecord_type": subrecord["subrecord_type"],
                    "subrecord_index": subrecord["subrecord_index"],
                    "payload_size": subrecord["payload_size"],
                    "source": subrecord["source"],
                    "target": "",
                    "risk": risk,
                    "writeback": writeback_status(),
                    "reason": reason,
                }
            )
        offset = record_data_end


def main() -> int:
    parser = argparse.ArgumentParser(description="Read project-local plugin text candidates through the current Game Profile.")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--plugin-path", required=True)
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--master-style-manifest", default="")
    parser.add_argument("--allow-generated-plugin", action="store_true")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve(strict=True) if args.project_root else default_project_root()
    context = resolve_game_context(project_root, args.game)
    if has_risky_path_marker(str(project_root), context):
        raise SystemExit(f"refusing risky project root: {project_root}")
    plugin_path = resolve_project_path(project_root, args.plugin_path, must_exist=True)
    validate_plugin_location(project_root, plugin_path, allow_generated_plugin=args.allow_generated_plugin)
    master_style_manifest = (
        resolve_project_path(project_root, args.master_style_manifest, must_exist=True)
        if args.master_style_manifest.strip()
        else None
    )
    if master_style_manifest is not None:
        if master_style_manifest.suffix.lower() != ".json":
            raise SystemExit(
                f"MasterStyleManifest must be a JSON file: {master_style_manifest}"
            )
        if not is_under(master_style_manifest, project_root / "work" / "plugin_context"):
            raise SystemExit(
                "MasterStyleManifest must be under work/plugin_context/: "
                f"{master_style_manifest}"
            )
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

    decision = resolve_capability(context, "plugin_text", "read")
    adapter_id = decision.adapter_id or ""
    if not decision.supported or not adapter_id:
        output_path.unlink(missing_ok=True)
        write_blocked_report(
            report_path,
            context,
            adapter_id or "<none>",
            decision.level,
            decision.reason if not decision.supported else "Supported plugin_text capability has no adapter.",
        )
        return 2
    try:
        require_adapter(adapter_id, "extract")
    except ValueError as exc:
        output_path.unlink(missing_ok=True)
        write_blocked_report(
            report_path,
            context,
            adapter_id,
            decision.level,
            str(exc),
        )
        return 2
    mutagen_release = str(decision.adapter_options.get("mutagen_release") or "").strip()
    extract_backend = str(decision.adapter_options.get("extract_backend") or "").strip()
    localized_plugin_policy = str(
        decision.adapter_options.get("localized_plugin_policy") or ""
    ).strip()
    supported_backends = {"builtin-tes4-parser", "mutagen-adapter"}
    supported_localized_policies = {"allow", "block"}
    if (
        not mutagen_release
        or extract_backend not in supported_backends
        or localized_plugin_policy not in supported_localized_policies
    ):
        output_path.unlink(missing_ok=True)
        write_blocked_report(
            report_path,
            context,
            adapter_id,
            decision.level,
            "plugin_text options require mutagen_release plus a supported "
            "extract_backend and localized_plugin_policy.",
        )
        return 2

    data = plugin_path.read_bytes()
    if len(data) < 24 or data[:4] != b"TES4":
        raise SystemExit("not a supported TES4-family plugin")

    header_flags = u32(data, 8)
    if localized_plugin_policy == "block" and header_flags & LOCALIZED_FLAG:
        if output_path.exists():
            output_path.unlink()
        write_blocked_report(
            report_path,
            context,
            adapter_id,
            decision.level,
            "Localized plugin must use the localized_delivery composite adapter; generic plugin export is blocked.",
        )
        print("Localized plugin export is blocked by the active Game Profile.", file=sys.stderr)
        return 2

    if extract_backend == "mutagen-adapter":
        return run_mutagen_export(
            project_root,
            plugin_path,
            output_path,
            report_path,
            context,
            decision,
            master_style_manifest,
        )

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
    parse_elements(data, 0, len(data), [], rows, stats, context.game_id)
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
        f"- game_id: {context.game_id}",
        f"- game_profile_version: {context.schema_version}",
        f"- plugin_adapter: {adapter_id}",
        "- plugin_adapter_version: "
        f"{context.capability_option_positive_int('plugin_text', 'adapter_contract_version')}",
        f"- plugin_text_capability_level: {decision.level}",
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
            "- It does not need real game installation, MO2/Vortex, Steam, AppData, or Documents/My Games paths.",
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

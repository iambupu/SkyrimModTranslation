"""Prepare checked PEX translation rows for controlled tool writeback."""

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from adapter_registry import require_capability_script_entrypoint
from game_context import (
    game_context_metadata,
    resolve_workspace_game_context as resolve_game_context,
    supported_game_ids,
)
from project_paths import project_root, safe_file_name
from project_paths import is_under, resolve_project_path, relative_path
from file_utils import sha256_file_upper as sha256
from report_utils import write_text_lines as write_text









def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def infer_script_names(records: list[dict[str, Any]], source_mod_dir: Path) -> list[str]:
    names: set[str] = set()
    for record in records:
        for key in ("source_file", "SourceFile", "file", "File", "ModName", "object_name", "ObjectName"):
            value = str(record.get(key, "")).strip()
            if not value:
                continue
            path = Path(value.replace("/", "\\"))
            suffix = path.suffix.lower()
            if suffix in {".psc", ".pex"}:
                names.add(path.stem)
            elif key.lower() in {"object_name", "objectname"} and value:
                names.add(Path(value).stem)
    if not names:
        script_dir = source_mod_dir / "Scripts"
        if script_dir.is_dir():
            names.update(path.stem for path in script_dir.glob("*.pex"))
    return sorted(names, key=str.lower)


def find_translation_pairs(root: Path, mod_name: str) -> Path | None:
    lex_dir = root / "translated" / "lextranslator_ready" / mod_name
    if not lex_dir.is_dir():
        return None
    candidates = [
        path
        for path in lex_dir.iterdir()
        if path.is_file()
        and "pex" in path.name.lower()
        and path.suffix.lower() in {".jsonl", ".json", ".xml", ".csv", ".txt"}
    ]
    return sorted(candidates, key=lambda item: item.name.lower())[0] if candidates else None


def ensure_tool_output_path(root: Path, full_path: Path, mod_name: str) -> None:
    allowed_roots = [
        resolve_project_path(root, f"out/{mod_name}/tool_outputs", must_exist=False),
        resolve_project_path(root, f"translated/tool_outputs/{mod_name}", must_exist=False),
        resolve_project_path(root, "translated/tool_outputs", must_exist=False),
    ]
    if not any(is_under(full_path, allowed) or full_path.resolve(strict=False) == allowed.resolve(strict=False) for allowed in allowed_roots):
        raise ValueError(f"PEX tool output must be under out/<ModName>/tool_outputs/ or translated/tool_outputs/: {relative_path(root, full_path)}")



def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare project-local PEX copies for controlled tool writeback or GUI fallback.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--source-mod-dir", default="")
    parser.add_argument("--visible-strings-path", default="")
    parser.add_argument("--translation-pairs-path", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = project_root()
    context = resolve_game_context(root, args.game)
    pex_write, apply_entrypoint = require_capability_script_entrypoint(
        context,
        "pex",
        "write",
        "apply",
    )
    primary_writer = (
        f"python scripts/{apply_entrypoint} --mode Apply --game {context.game_id}"
    )
    safe_mod_name = safe_file_name(args.mod_name)
    if not safe_mod_name:
        raise ValueError("ModName cannot be empty after sanitization.")
    source_value = args.source_mod_dir or f"work/extracted_mods/{safe_mod_name}"
    visible_value = args.visible_strings_path or f"work/normalized/{safe_mod_name}/pex_visible_strings.jsonl"
    output_value = args.output_dir or f"out/{safe_mod_name}/tool_outputs/Scripts"

    source_dir = resolve_project_path(root, source_value, must_exist=True)
    visible_path = resolve_project_path(root, visible_value, must_exist=True)
    output_dir = resolve_project_path(root, output_value, must_exist=False)
    if not source_dir.is_dir():
        raise ValueError(f"SourceModDir must be a directory: {source_value}")
    ensure_tool_output_path(root, output_dir, safe_mod_name)

    translation_path: Path | None = None
    if args.translation_pairs_path.strip():
        translation_path = resolve_project_path(root, args.translation_pairs_path, must_exist=True)
    else:
        translation_path = find_translation_pairs(root, safe_mod_name)

    records = read_jsonl(visible_path)
    script_names = infer_script_names(records, source_dir)
    if not script_names:
        raise ValueError(f"No PEX script names could be inferred from {visible_value}")

    output_dir.mkdir(parents=True, exist_ok=True)
    copies: list[dict[str, object]] = []
    missing: list[str] = []
    for script_name in script_names:
        source_pex = source_dir / "Scripts" / f"{script_name}.pex"
        if not source_pex.is_file():
            missing.append(f"Scripts/{script_name}.pex")
            continue
        target_pex = output_dir / f"{script_name}.pex"
        status = "created"
        if target_pex.exists():
            if not args.force:
                status = "exists_not_overwritten"
            else:
                shutil.copy2(source_pex, target_pex)
                status = "overwritten_from_project_source"
        else:
            shutil.copy2(source_pex, target_pex)
        source_hash = sha256(source_pex)
        target_hash = sha256(target_pex)
        copies.append(
            {
                "ScriptName": script_name,
                "SourcePex": relative_path(root, source_pex),
                "TargetPex": relative_path(root, target_pex),
                "Status": status,
                "SourceSha256": source_hash,
                "TargetSha256": target_hash,
                "SameAsSource": source_hash == target_hash,
                "WritebackStatus": "not_written_prepared_copy",
            }
        )

    manifest_dir = output_dir.parent / "meta"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "pex_writeback_manifest.json"
    manifest = {
        "ModName": safe_mod_name,
        **game_context_metadata(context),
        "pex_category": context.capability_option_text("pex", "pex_category"),
        "pex_writeback_status": context.capability_write_status("pex"),
        "PreparedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "SourceModDir": relative_path(root, source_dir),
        "VisibleStringsPath": relative_path(root, visible_path),
        "TranslationPairsPath": relative_path(root, translation_path) if translation_path else "",
        "OutputDir": relative_path(root, output_dir),
        "PrimaryNonGuiWriterAdapter": pex_write.adapter_id,
        "PrimaryNonGuiWriter": primary_writer,
        "GuiFallbackTool": "LexTranslator",
        "GuiFallbackSecondary": "xTranslator PapyrusPex",
        "Copies": copies,
        "MissingPex": missing,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    qa_path = resolve_project_path(root, "qa/pex_tool_writeback.md", must_exist=False)
    report = [
        "# PEX Tool Writeback Preparation",
        "",
        f"- ModName: {safe_mod_name}",
        f"- game_id: {context.game_id}",
        f"- pex_category: {context.capability_option_text('pex', 'pex_category')}",
        f"- pex_writeback_status: {context.capability_write_status('pex')}",
        f"- Prepared at: {manifest['PreparedAt']}",
        f"- SourceModDir: {relative_path(root, source_dir)}",
        f"- VisibleStringsPath: {relative_path(root, visible_path)}",
        f"- TranslationPairsPath: {relative_path(root, translation_path) if translation_path else 'not found'}",
        f"- OutputDir: {relative_path(root, output_dir)}",
        f"- Primary non-GUI writer adapter: {pex_write.adapter_id}",
        f"- Primary non-GUI writer: {primary_writer}",
        "- GUI fallback tool: LexTranslator",
        "- GUI fallback secondary: xTranslator PapyrusPex",
        "",
        "## PEX Copies",
        "",
        "| Script | Source | Target | Status | SameAsSource |",
        "|---|---|---|---|---|",
    ]
    for row in copies:
        report.append(f"| {row['ScriptName']} | {row['SourcePex']} | {row['TargetPex']} | {row['Status']} | {row['SameAsSource']} |")
    if missing:
        report.extend(["", "## Missing PEX", ""])
        report.extend([f"- {item}" for item in missing])
    report.extend(
        [
            "",
            "## Required Tool Boundary",
            "",
            "- This script prepares project-local PEX copies only. It does not modify PEX contents.",
            "- Every prepared copy has writeback status `not_written_prepared_copy`; preparation is not writeback evidence.",
            "- Prefer the non-GUI Mutagen PEX Apply path before using these GUI fallback copies.",
            "- If GUI fallback is required, LexTranslator is tried before xTranslator PapyrusPex.",
            f"- Save targets must remain under `out/{safe_mod_name}/tool_outputs/Scripts/` or `translated/tool_outputs/{safe_mod_name}/Scripts/`.",
            "- Do not open or save real game installation, MO2/Vortex, Steam, AppData, or Documents/My Games paths.",
            "",
            "## Next Commands",
            "",
            "```console",
        ]
    )
    translation_arg = f' --TranslationPairsPath ".\\{relative_path(root, translation_path)}"' if translation_path else ""
    for row in copies:
        target = f".\\{row['TargetPex']}"
        report.append(f'python .\\scripts\\invoke_lextranslator_gui.py --input-path "{target}" --mode inspect{translation_arg.replace(" --TranslationPairsPath", " --translation-pairs-path")}')
        report.append(f'python .\\scripts\\invoke_lextranslator_gui.py --input-path "{target}" --mode open{translation_arg.replace(" --TranslationPairsPath", " --translation-pairs-path")}')
    report.extend(["```", "", "`open` only proves the tool loaded the project-local PEX copy. It is not proof that translation or save completed."])
    write_text(qa_path, report)

    print(f"PEX tool writeback preparation written to: {qa_path}")
    print(f"PEX writeback manifest written to: {manifest_path}")
    if missing:
        print(f"Missing PEX files: {len(missing)}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())

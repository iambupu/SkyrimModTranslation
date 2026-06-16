import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir

TEXT_EXTENSIONS = {".json", ".jsonl", ".xml", ".csv", ".txt", ".md", ".ini", ".py"}
BINARY_EXTENSIONS = {".esp", ".esm", ".esl", ".pex", ".bsa", ".ba2", ".dll", ".exe"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
BACKUP_EXTENSIONS = {".backup", ".bak", ".old", ".tmp"}


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


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    return "".join("_" if char in invalid or ord(char) < 32 else char for char in value).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_backup_artifact(path: Path) -> bool:
    lowered = path.name.lower()
    if path.suffix.lower() in BACKUP_EXTENSIONS:
        return True
    return any(f".{ext[1:]}." in lowered for ext in BINARY_EXTENSIONS)


def copy_recovered_file(root: Path, source_file: Path, relative: Path, destination_root: Path, force: bool) -> dict[str, str]:
    destination = (destination_root / relative).resolve(strict=False)
    if not is_under(destination, destination_root):
        raise ValueError(f"Unsafe recovered destination rejected: {destination}")
    if destination.exists() and not force:
        raise ValueError(f"Recovered destination already exists. Re-run with --force to overwrite: {relative_path(root, destination)}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, destination)
    return {"Source": relative_path(root, source_file), "Destination": relative_path(root, destination)}


def write_text(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover changed final_mod files into project-local overlay/tool output roots.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--source-mod-dir", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = project_root()
    safe_mod_name = safe_file_name(args.mod_name)
    if not safe_mod_name:
        raise ValueError("ModName cannot be empty after sanitization.")
    source_value = args.source_mod_dir or f"work/extracted_mods/{safe_mod_name}"
    final_value = args.final_mod_dir or relative_path(root, default_final_mod_dir(root, safe_mod_name))

    source_dir = resolve_project_path(root, source_value, must_exist=True)
    final_dir = resolve_project_path(root, final_value, must_exist=True)
    out_root = resolve_project_path(root, f"out/{safe_mod_name}", must_exist=False)
    if not source_dir.is_dir():
        raise ValueError(f"SourceModDir must be a directory: {source_value}")
    if not final_dir.is_dir():
        raise ValueError(f"FinalModDir must be a directory: {final_value}")
    if not is_under(final_dir, out_root):
        raise ValueError(f"FinalModDir must be under out/{safe_mod_name}/: {final_value}")

    text_overlay_root = resolve_project_path(root, f"translated/final_mod/{safe_mod_name}", must_exist=False)
    binary_tool_root = resolve_project_path(root, f"out/{safe_mod_name}/tool_outputs", must_exist=False)
    text_overlay_root.mkdir(parents=True, exist_ok=True)
    binary_tool_root.mkdir(parents=True, exist_ok=True)

    recovered_text: list[dict[str, str]] = []
    recovered_binary: list[dict[str, str]] = []
    skipped: list[str] = []

    for file_path in sorted(path for path in final_dir.rglob("*") if path.is_file()):
        relative = file_path.resolve(strict=True).relative_to(final_dir.resolve(strict=True))
        relative_text = str(relative).replace("/", "\\")
        if relative.parts and relative.parts[0].lower() == "meta":
            skipped.append(f"meta skipped: {relative_path(root, file_path)}")
            continue
        if is_backup_artifact(file_path):
            skipped.append(f"backup/history skipped: {relative_path(root, file_path)}")
            continue
        extension = file_path.suffix.lower()
        if extension in ARCHIVE_EXTENSIONS:
            skipped.append(f"archive skipped: {relative_path(root, file_path)}")
            continue

        source_peer = source_dir / relative
        changed = True
        if source_peer.is_file():
            changed = sha256(source_peer) != sha256(file_path)
        if not changed:
            continue

        if extension in BINARY_EXTENSIONS:
            recovered_binary.append(copy_recovered_file(root, file_path, relative, binary_tool_root, args.force))
        elif extension in TEXT_EXTENSIONS:
            recovered_text.append(copy_recovered_file(root, file_path, relative, text_overlay_root, args.force))
        else:
            skipped.append(f"unsupported changed file skipped: {relative_path(root, file_path)}")

    report_dir = resolve_project_path(root, f"out/{safe_mod_name}/qa", must_exist=False)
    report_path = report_dir / "recovered_final_mod_overlays.md"
    lines = [
        "# Recovered Final Mod Overlays",
        "",
        f"- ModName: {safe_mod_name}",
        f"- SourceModDir: {relative_path(root, source_dir)}",
        f"- FinalModDir: {relative_path(root, final_dir)}",
        f"- TextOverlayRoot: {relative_path(root, text_overlay_root)}",
        f"- BinaryToolRoot: {relative_path(root, binary_tool_root)}",
        f"- Recovered text overlays: {len(recovered_text)}",
        f"- Recovered binary tool outputs: {len(recovered_binary)}",
        f"- Skipped files: {len(skipped)}",
        f"- Recovered at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Text Overlays",
        "",
    ]
    lines.extend([f"- {item['Source']} -> {item['Destination']}" for item in recovered_text] or ["No changed text overlays were recovered."])
    lines.extend(["", "## Binary Tool Outputs", ""])
    lines.extend([f"- {item['Source']} -> {item['Destination']}" for item in recovered_binary] or ["No changed binary outputs were recovered."])
    lines.extend(["", "## Skipped", ""])
    lines.extend([f"- {item}" for item in skipped] or ["No files were skipped."])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script copies project-local final_mod differences into project-local overlay/tool output roots.",
            "- Binary files are copied byte-for-byte; this script does not edit, patch, decompile, compile, or save plugins/scripts.",
            "- Recovered binary outputs are provenance snapshots, not proof that a non-GUI writer generated them.",
            "- No real Skyrim, MO2/Vortex, Steam, AppData, or Documents/My Games directory is accessed.",
        ]
    )
    write_text(report_path, lines)
    manifest_path = report_dir / "recovered_final_mod_overlays.json"
    manifest_path.write_text(
        json.dumps(
            {
                "ModName": safe_mod_name,
                "SourceModDir": relative_path(root, source_dir),
                "FinalModDir": relative_path(root, final_dir),
                "TextOverlayRoot": relative_path(root, text_overlay_root),
                "BinaryToolRoot": relative_path(root, binary_tool_root),
                "RecoveredText": recovered_text,
                "RecoveredBinary": recovered_binary,
                "Skipped": skipped,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Recovered text overlays: {len(recovered_text)}")
    print(f"Recovered binary tool outputs: {len(recovered_binary)}")
    print(f"Recovery report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

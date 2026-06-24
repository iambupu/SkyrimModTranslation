"""Create a model-review packet by re-reading delivered ESP/PEX outputs.

The packet is not a writeback tool. It exports visible strings from final_mod so
model review can inspect what actually landed in binary deliverables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from project_paths import plugin_root as default_plugin_root
from project_paths import plugin_script_path
from project_paths import project_root
from proofread_translation import load_allowed_words, remove_allowed_ascii_tokens


@dataclass(frozen=True)
class ReviewItem:
    File: str
    Kind: str
    Context: str
    Source: str
    Final: str
    Risk: str
    Identity: str


@dataclass(frozen=True)
class ExportFailure:
    Kind: str
    File: str
    Stage: str
    Message: str


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


def require_under(path: Path, root: Path, label: str) -> None:
    # Export helpers can call external adapters, so every generated path is
    # constrained before the subprocess is launched.
    if not is_under(path, root):
        raise ValueError(f"{label} must be under {relative_project_path(project_root(), root)}: {path}")


def relative_path(base: Path, target: Path) -> str:
    try:
        return str(target.resolve(strict=False).relative_to(base.resolve(strict=True))).replace("/", "\\")
    except ValueError:
        return str(target).replace("/", "\\")


def relative_project_path(root: Path, target: Path) -> str:
    return relative_path(root, target)


def markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_text_if_changed(path: Path, lines: list[str]) -> bool:
    text = "\n".join(lines) + ("\n" if lines else "")
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def string_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_report_metric(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^- {re.escape(name)}:\s*(.+)$")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def binary_fingerprints(final_mod: Path) -> dict[str, str]:
    paths = sorted(
        (
            path
            for path in final_mod.rglob("*")
            if path.is_file() and path.suffix.lower() in {".esp", ".esm", ".esl", ".pex"}
        ),
        key=lambda path: relative_path(final_mod, path).lower(),
    )
    return {relative_path(final_mod, path): file_sha256(path) for path in paths}


def cached_packet_is_current(cache_path: Path, packet_path: Path, items_path: Path, fingerprints: dict[str, str]) -> bool:
    if not cache_path.is_file() or not packet_path.is_file() or not items_path.is_file():
        return False
    cache = read_json(cache_path)
    return cache.get("FinalBinaryFingerprints") == fingerprints


def write_cache(cache_path: Path, fingerprints: dict[str, str], items_hash: str) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "FinalBinaryFingerprints": fingerprints,
                "ItemsSHA256": items_hash,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def tools_config(root: Path, config_path: str) -> dict[str, Any]:
    path = resolve_project_path(root, config_path, must_exist=True)
    return read_json(path)


def dotnet_path(root: Path, config: dict[str, Any]) -> Path:
    decoder_tools = config.get("DecoderTools")
    configured = ""
    if isinstance(decoder_tools, dict):
        configured = str(decoder_tools.get("DotNetSdkPath") or "")
    return resolve_project_path(root, configured or "tools/dotnet-sdk/dotnet.exe", must_exist=True)


def process_failure_message(result: subprocess.CompletedProcess[str]) -> str:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    if not lines:
        return f"process exited with code {result.returncode}"
    return " ".join(lines[:8])


def run_esp_export(root: Path, plugin_path: Path, mod_name: str, output_rel: str, report_rel: str) -> subprocess.CompletedProcess[str]:
    # Use the same project-local read-only exporter as earlier stages. This
    # checks final_mod content without opening the real game Data directory.
    source_root = default_plugin_root()
    script = plugin_script_path("export_esp_strings.py")
    if not script.is_file():
        raise FileNotFoundError("missing plugin script: scripts/export_esp_strings.py")
    output_path = resolve_project_path(root, output_rel, must_exist=False)
    report_path = resolve_project_path(root, report_rel, must_exist=False)
    require_under(output_path, root / "source", "ESP export output")
    require_under(report_path, root / "qa", "ESP export report")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--project-root",
            str(root),
            "--plugin-path",
            str(plugin_path),
            "--mod-name",
            mod_name,
            "--output-path",
            str(output_path),
            "--report-path",
            str(report_path),
            "--allow-generated-plugin",
        ],
        cwd=str(root),
        env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def build_pex_adapter(source_root: Path, dotnet: Path) -> Path:
    # PEX export goes through the Mutagen adapter. Failures are recorded in the
    # review packet instead of being hidden as "no strings found".
    adapter_project = source_root / "adapters" / "SkyrimPexStringTool" / "SkyrimPexStringTool.csproj"
    if not adapter_project.is_file():
        raise FileNotFoundError("missing adapters/SkyrimPexStringTool/SkyrimPexStringTool.csproj")
    result = subprocess.run(
        [
            str(dotnet),
            "build",
            str(adapter_project),
            "--framework",
            "net8.0",
            "-p:TargetFrameworks=net8.0",
        ],
        cwd=str(source_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PEX adapter build failed: {process_failure_message(result)}")
    adapter_dll = source_root / "adapters" / "SkyrimPexStringTool" / "bin" / "Debug" / "net8.0" / "SkyrimPexStringTool.dll"
    if not adapter_dll.is_file():
        raise FileNotFoundError("missing built SkyrimPexStringTool.dll")
    return adapter_dll


def run_pex_export(root: Path, dotnet: Path, adapter_dll: Path, pex_path: Path, output_rel: str, report_rel: str) -> subprocess.CompletedProcess[str]:
    # Run the already-built adapter directly; rebuilding for every PEX makes
    # strict binary review prohibitively slow on script-heavy mods.
    output_path = resolve_project_path(root, output_rel, must_exist=False)
    report_path = resolve_project_path(root, report_rel, must_exist=False)
    require_under(output_path, root / "source" / "pex_exports", "PEX export output")
    if not (is_under(report_path, root / "qa") or is_under(report_path, root / "out")):
        raise ValueError(f"PEX export report must be under qa/ or out/: {report_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            str(dotnet),
            str(adapter_dll),
            "export",
            "--project-root",
            str(root),
            "--input-pex",
            str(pex_path),
            "--report",
            str(report_path),
            "--output-jsonl",
            str(output_path),
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def count_jsonl_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip())


def value(row: dict[str, Any], name: str) -> str:
    item = row.get(name)
    return "" if item is None else str(item)


def plugin_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            value(row, "plugin"),
            value(row, "record_type"),
            value(row, "form_id"),
            value(row, "editor_id"),
            value(row, "subrecord_type"),
            value(row, "subrecord_index"),
        ]
    )


def plugin_logical_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            value(row, "plugin"),
            value(row, "record_type"),
            value(row, "form_id"),
            value(row, "editor_id"),
            value(row, "subrecord_type"),
        ]
    )


def pex_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            value(row, "ModName"),
            value(row, "object_name"),
            value(row, "state_name"),
            value(row, "function_name"),
            value(row, "opcode"),
            value(row, "instruction_index"),
            value(row, "argument_index"),
        ]
    )


def review_risk(risk: str) -> str:
    normalized = risk.strip().lower()
    if not normalized:
        return "review"
    if normalized.startswith("protected"):
        return "protected-review"
    if "manual" in normalized or "review" in normalized:
        return "manual-review"
    return "review"


def cjk_present(text: str) -> bool:
    return re.search(r"[\u3400-\u9fff]", text) is not None


def english_present(text: str) -> bool:
    return re.search(r"[A-Za-z]{3,}", text) is not None


def protected_binary_value(text: str, context: str) -> bool:
    trimmed = text.strip()
    normalized_context = context.lower()
    normalized_text = trimmed.lower()
    if re.search(r"[\\/]", trimmed):
        return True
    if "kind=pex-binary" in normalized_context or ".pex" in normalized_context or "opcode=" in normalized_context:
        if "opcode=cmp_" in normalized_context:
            return True
        diagnostic_markers = (
            " controller",
            " exists",
            " is none",
            " initialized",
            " mismatch",
            " restarting ",
            " stopping",
            " starting",
            "vanilla=",
            "local=",
        )
        if any(marker in normalized_text for marker in diagnostic_markers):
            return True
    if re.search(r"\.(esp|esm|esl|pex|psc|bsa|ba2|dll|exe|json|xml|ini|txt)(\||$)", trimmed, re.IGNORECASE):
        return True
    if re.fullmatch(r"\$[A-Za-z0-9_]+", trimmed):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*:[A-Za-z0-9_]+", trimmed):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", trimmed) and (
        "_" in trimmed or re.search(r"[A-Z]", trimmed[1:]) or "opcode=cmp_eq" in normalized_context or "opcode=callmethod" in normalized_context
    ):
        return True
    return False


def likely_untranslated_candidate(text: str, risk: str, context: str, allowed_words: set[str]) -> bool:
    trimmed = text.strip()
    normalized_risk = risk.strip().lower()
    if not trimmed or cjk_present(trimmed) or not english_present(trimmed):
        return False
    if normalized_risk.startswith("protected"):
        return False
    if normalized_risk == "manual-review":
        return False
    if re.search(r"\brecord=TES4\b", context, re.IGNORECASE) and re.search(r"\bsubrecord=CNAM\b", context, re.IGNORECASE):
        return False
    if protected_binary_value(trimmed, context):
        return False
    remaining = remove_allowed_ascii_tokens(trimmed, allowed_words)
    return english_present(remaining)


def add_review_item(
    items: list[ReviewItem],
    file: str,
    kind: str,
    context: str,
    source_text: str,
    final_text: str,
    risk: str,
    identity: str,
    allowed_words: set[str],
) -> None:
    if source_text == final_text:
        if not likely_untranslated_candidate(final_text, risk, context, allowed_words):
            return
        risk = "untranslated-review"
    if not source_text.strip() and not final_text.strip():
        return
    items.append(ReviewItem(file, kind, context, source_text, final_text, risk, identity))


def collect_plugin_items(root: Path, workspace: Path, final_mod: Path, mod_name: str, allowed_words: set[str]) -> tuple[int, list[ReviewItem], list[ExportFailure]]:
    items: list[ReviewItem] = []
    failures: list[ExportFailure] = []
    plugin_files = sorted(
        (path for path in final_mod.iterdir() if path.is_file() and path.suffix.lower() in {".esp", ".esm", ".esl"}),
        key=lambda path: path.name.lower(),
    )
    for plugin in plugin_files:
        original_plugin = workspace / plugin.name
        relative_plugin = relative_path(final_mod, plugin)
        if not original_plugin.is_file():
            failures.append(ExportFailure("plugin", relative_plugin, "match-original", "Original plugin not found in workspace."))
            continue

        original_export = f"source/plugin_exports/{mod_name}/{plugin.name}.original_binary_review.esp_strings.jsonl"
        final_export = f"source/plugin_exports/{mod_name}/{plugin.name}.final_binary_review.esp_strings.jsonl"
        original_report = f"qa/{plugin.name}.original_binary_review_esp_export_report.md"
        final_report = f"qa/{plugin.name}.final_binary_review_esp_export_report.md"

        original_run = run_esp_export(root, original_plugin, mod_name, original_export, original_report)
        if original_run.returncode != 0:
            failures.append(ExportFailure("plugin", relative_plugin, "export-original", process_failure_message(original_run)))
            continue
        final_run = run_esp_export(root, plugin, mod_name, final_export, final_report)
        if final_run.returncode != 0:
            failures.append(ExportFailure("plugin", relative_plugin, "export-final", process_failure_message(final_run)))
            continue

        try:
            original_rows = read_jsonl_rows(root / original_export)
            final_rows = read_jsonl_rows(root / final_export)
        except json.JSONDecodeError as exc:
            failures.append(ExportFailure("plugin", relative_plugin, "read-export", str(exc)))
            continue

        final_by_key: dict[str, dict[str, Any]] = {}
        for row in final_rows:
            final_by_key.setdefault(plugin_identity(row), row)
        final_protected_values: dict[str, set[str]] = {}
        for row in final_rows:
            if review_risk(value(row, "risk")) == "protected-review":
                final_protected_values.setdefault(plugin_logical_identity(row), set()).add(value(row, "source"))
        for original_row in original_rows:
            identity = plugin_identity(original_row)
            final_row = final_by_key.get(identity)
            if final_row is None:
                continue
            source_text = value(original_row, "source")
            final_text = value(final_row, "source")
            risk = review_risk(value(original_row, "risk"))
            if risk == "protected-review" and source_text != final_text:
                # Mutagen can reorder repeated non-visible protected subrecords
                # while preserving their values. Treat these as unchanged when
                # the same protected value still exists in the same record field.
                logical_identity = plugin_logical_identity(original_row)
                if source_text in final_protected_values.get(logical_identity, set()):
                    continue
            context = (
                f"record={value(original_row, 'record_type')}; "
                f"form_id={value(original_row, 'form_id')}; "
                f"subrecord={value(original_row, 'subrecord_type')}; "
                f"editor_id={value(original_row, 'editor_id')}"
            )
            add_review_item(items, relative_plugin, "plugin-binary", context, source_text, final_text, risk, identity, allowed_words)
    return len(plugin_files), items, failures


def collect_pex_items(root: Path, workspace: Path, final_mod: Path, mod_name: str, dotnet: Path, adapter_dll: Path, allowed_words: set[str]) -> tuple[int, list[ReviewItem], list[ExportFailure]]:
    items: list[ReviewItem] = []
    failures: list[ExportFailure] = []
    pex_files = sorted((path for path in final_mod.rglob("*") if path.is_file() and path.suffix.lower() == ".pex"), key=lambda path: str(path).lower())
    for pex in pex_files:
        relative_pex = relative_path(final_mod, pex)
        original_pex = workspace / relative_pex
        if not original_pex.is_file():
            failures.append(ExportFailure("pex", relative_pex, "match-original", "Original PEX not found in workspace."))
            continue

        original_export = f"source/pex_exports/{mod_name}/{pex.stem}.original_binary_review.pex_strings.jsonl"
        final_export = f"source/pex_exports/{mod_name}/{pex.stem}.final_binary_review.pex_strings.jsonl"
        original_report = f"qa/{pex.stem}.original_binary_review_pex_export_report.md"
        final_report = f"qa/{pex.stem}.final_binary_review_pex_export_report.md"

        original_run = run_pex_export(root, dotnet, adapter_dll, original_pex, original_export, original_report)
        if original_run.returncode != 0:
            failures.append(ExportFailure("pex", relative_pex, "export-original", process_failure_message(original_run)))
            continue
        final_run = run_pex_export(root, dotnet, adapter_dll, pex, final_export, final_report)
        if final_run.returncode != 0:
            failures.append(ExportFailure("pex", relative_pex, "export-final", process_failure_message(final_run)))
            continue

        try:
            original_rows = read_jsonl_rows(root / original_export)
            final_rows = read_jsonl_rows(root / final_export)
        except json.JSONDecodeError as exc:
            failures.append(ExportFailure("pex", relative_pex, "read-export", str(exc)))
            continue

        final_by_key: dict[str, dict[str, Any]] = {}
        for row in final_rows:
            final_by_key.setdefault(pex_identity(row), row)
        for original_row in original_rows:
            identity = pex_identity(original_row)
            final_row = final_by_key.get(identity)
            if final_row is None:
                continue
            source_text = value(original_row, "Source")
            final_text = value(final_row, "Source")
            risk = review_risk(value(original_row, "risk"))
            context = (
                f"object={value(original_row, 'object_name')}; "
                f"function={value(original_row, 'function_name')}; "
                f"opcode={value(original_row, 'opcode')}; "
                f"instruction={value(original_row, 'instruction_index')}; "
                f"argument={value(original_row, 'argument_index')}"
            )
            add_review_item(items, relative_pex, "pex-binary", context, source_text, final_text, risk, identity, allowed_words)
    return len(pex_files), items, failures


def write_reports(
    root: Path,
    mod_name: str,
    workspace: Path,
    final_mod: Path,
    packet_path: Path,
    items_path: Path,
    plugin_count: int,
    pex_count: int,
    review_items: list[ReviewItem],
    failures: list[ExportFailure],
) -> str:
    sorted_items = sorted(review_items, key=lambda item: (item.File, item.Kind, item.Identity, item.Context, item.Source, item.Final))
    item_lines = [json.dumps(asdict(item), ensure_ascii=False, separators=(",", ":")) for item in sorted_items]
    item_text = "\n".join(item_lines) + ("\n" if item_lines else "")
    items_hash = string_sha256(item_text)
    write_text_if_changed(items_path, item_lines)

    protected_count = sum(1 for item in sorted_items if item.Risk == "protected-review")
    manual_count = sum(1 for item in sorted_items if item.Risk == "manual-review")

    lines: list[str] = [
        "# Final Binary Review Packet",
        "",
        f"- ModName: {mod_name}",
        f"- Workspace: {relative_project_path(root, workspace)}",
        f"- FinalModDir: {relative_project_path(root, final_mod)}",
        f"- Items JSONL: {relative_project_path(root, items_path)}",
        f"- Items SHA256: {items_hash}",
        f"- Plugin files checked: {plugin_count}",
        f"- PEX files checked: {pex_count}",
        f"- Review items: {len(sorted_items)}",
        f"- Manual review items: {manual_count}",
        f"- Protected review items: {protected_count}",
        f"- Export failures: {len(failures)}",
        "",
        "## Review Instructions",
        "",
        "- This packet compares original workspace ESP/PEX strings with strings re-read from `final_mod` binaries.",
        "- Review the `Final` column as the actual delivered text, not as an intermediate translation table.",
        "- `protected-review` means a protected or logic-like original string changed in final_mod and must be treated as blocking until explained.",
        "- `untranslated-review` means an English string was unchanged in final_mod and must be translated or explicitly justified before delivery.",
        "- The final model review must explicitly mention every final_mod ESP/PEX file listed in the JSONL packet.",
        "- This script is read-only for ESP/PEX binaries and writes only source/pex_exports, source/plugin_exports, and qa reports.",
        "",
        "The model review output must mention this packet, the JSONL path, the Items SHA256, every reviewed file, and these exact passing claims:",
        "",
        "- `No runtime-impacting issues remain`",
        "- `No required translation candidates remain untranslated`",
        "- `No semantic quality blockers remain`",
        "- `All changed final_mod files listed in the review packets were reviewed`",
        "- `Mechanical checks do not replace Codex model semantic review`",
        "- `Final review quality audit has 0 blocking issues and 0 warnings`",
        "",
        "## Changed Binary Text",
        "",
    ]
    if not sorted_items:
        lines.append("No changed ESP/PEX text rows were detected.")
    else:
        lines.extend(["| Kind | File | Context | Source | Final | Risk |", "|---|---|---|---|---|---|"])
        for item in sorted_items:
            lines.append(
                f"| {item.Kind} | {markdown_cell(item.File)} | {markdown_cell(item.Context)} | {markdown_cell(item.Source)} | {markdown_cell(item.Final)} | {item.Risk} |"
            )

    lines.extend(["", "## Export Failures", ""])
    if not failures:
        lines.append("No export failures.")
    else:
        lines.extend(["| Kind | File | Stage | Message |", "|---|---|---|---|"])
        for failure in sorted(failures, key=lambda item: (item.Kind, item.File, item.Stage)):
            lines.append(f"| {failure.Kind} | {markdown_cell(failure.File)} | {failure.Stage} | {markdown_cell(failure.Message)} |")

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This packet generator does not translate text.",
            "- This packet generator does not write plugin or PEX binaries.",
            "- It reads only project-local workspace/final_mod inputs and writes project-local QA/source reports.",
            "- Real Skyrim, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    write_text_if_changed(packet_path, lines)
    return items_hash


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Codex model review packet from actual final_mod ESP/PEX text differences.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--packet-output-path", default="")
    parser.add_argument("--items-jsonl-path", default="")
    parser.add_argument("--cache-path", default="")
    parser.add_argument("--reuse-current-if-unchanged", action="store_true")
    parser.add_argument("--config-path", default="config/tools.local.json")
    args = parser.parse_args()

    root = project_root()
    mod_name = args.mod_name
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{mod_name}", must_exist=True)
    workspace = find_data_root(workspace).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_project_path(root, default_final_mod_dir(root, mod_name)), must_exist=True)
    packet_path = resolve_project_path(root, args.packet_output_path or f"qa/{mod_name}.final_binary_review_packet.md", must_exist=False)
    items_path = resolve_project_path(root, args.items_jsonl_path or f"qa/{mod_name}.final_binary_review_items.jsonl", must_exist=False)
    cache_path = resolve_project_path(root, args.cache_path or f"qa/{mod_name}.final_binary_review_cache.json", must_exist=False)

    require_under(workspace, root / "work" / "extracted_mods", "WorkspacePath")
    require_under(final_mod, root / "out", "FinalModDir")
    require_under(packet_path, root / "qa", "PacketOutputPath")
    require_under(items_path, root / "qa", "ItemsJsonlPath")
    require_under(cache_path, root / "qa", "CachePath")
    if not workspace.is_dir():
        raise ValueError(f"WorkspacePath must be a directory: {workspace}")
    if not final_mod.is_dir():
        raise ValueError(f"FinalModDir must be a directory: {final_mod}")

    fingerprints = binary_fingerprints(final_mod)
    if args.reuse_current_if_unchanged and cached_packet_is_current(cache_path, packet_path, items_path, fingerprints):
        print(f"Final binary review packet written to: {packet_path}")
        print(f"Final binary review items written to: {items_path}")
        print(f"Review items: {read_report_metric(packet_path, 'Review items') or count_jsonl_rows(items_path)}")
        print(f"Protected review items: {read_report_metric(packet_path, 'Protected review items') or 0}")
        print(f"Export failures: {read_report_metric(packet_path, 'Export failures') or 0}")
        print("Reused current final binary review packet cache.")
        return 0

    if not fingerprints:
        items_hash = write_reports(root, mod_name, workspace, final_mod, packet_path, items_path, 0, 0, [], [])
        write_cache(cache_path, fingerprints, items_hash)
        print(f"Final binary review packet written to: {packet_path}")
        print(f"Final binary review items written to: {items_path}")
        print("Review items: 0")
        print("Protected review items: 0")
        print("Export failures: 0")
        return 0

    source_root = default_plugin_root()
    config = tools_config(root, args.config_path)
    dotnet = dotnet_path(root, config)
    pex_adapter_dll = build_pex_adapter(source_root, dotnet)
    allowed_words = load_allowed_words(root)
    plugin_count, plugin_items, plugin_failures = collect_plugin_items(root, workspace, final_mod, mod_name, allowed_words)
    pex_count, pex_items, pex_failures = collect_pex_items(root, workspace, final_mod, mod_name, dotnet, pex_adapter_dll, allowed_words)
    review_items = plugin_items + pex_items
    failures = plugin_failures + pex_failures
    items_hash = write_reports(root, mod_name, workspace, final_mod, packet_path, items_path, plugin_count, pex_count, review_items, failures)
    write_cache(cache_path, fingerprints, items_hash)
    protected_count = sum(1 for item in review_items if item.Risk == "protected-review")

    print(f"Final binary review packet written to: {packet_path}")
    print(f"Final binary review items written to: {items_path}")
    print(f"Review items: {len(review_items)}")
    print(f"Protected review items: {protected_count}")
    print(f"Export failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

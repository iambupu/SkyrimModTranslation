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
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any

from adapter_registry import require_capability_script_entrypoint
from file_utils import (
    read_json_object_or_empty_with_parse_errors as read_json,
    write_text_lines_if_changed,
)
from model_review_contract import model_claim_lines, read_jsonl_objects, read_report_metric
from game_context import GameContext, game_context_metadata as context_metadata, resolve_workspace_game_context, supported_game_ids
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from project_paths import plugin_root as default_plugin_root
from project_paths import plugin_script_path
from project_paths import project_root
from proofread_translation import load_allowed_words, remove_allowed_ascii_tokens
from pex_translation_safety import (
    pex_logic_protection_reason,
    pex_translation_skip_reason,
    row_value as pex_row_value,
)
from project_paths import is_under, resolve_project_path, relative_windows_path as relative_path
from report_utils import markdown_text_cell_backslash as markdown_cell
from translation_context import (
    aggregate_review_rows,
    append_review_group_sections,
    append_review_groups_table,
    validated_translation_context,
)
from translation_text import cjk_present, english_present


write_text_if_changed = partial(write_text_lines_if_changed, newline_if_empty=False)


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






def require_under(path: Path, root: Path, label: str) -> None:
    # Export helpers can call tool adapters, so every generated path is
    # constrained before the subprocess is launched.
    if not is_under(path, root):
        raise ValueError(f"{label} must be under {relative_path(project_root(), root)}: {path}")




def string_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def cached_packet_is_current(
    cache_path: Path,
    packet_path: Path,
    items_path: Path,
    final_fingerprints: dict[str, str],
    original_fingerprints: dict[str, str],
    game_metadata: dict[str, object],
) -> bool:
    if not cache_path.is_file() or not packet_path.is_file() or not items_path.is_file():
        return False
    cache = read_json(cache_path)
    return (
        cache.get("CacheSchemaVersion") == 2
        and cache.get("FinalBinaryFingerprints") == final_fingerprints
        and cache.get("OriginalBinaryFingerprints") == original_fingerprints
        and cache.get("PacketSHA256") == file_sha256(packet_path)
        and cache.get("ItemsSHA256") == file_sha256(items_path)
        and cache.get("GameContext") == game_metadata
    )


def write_cache(
    cache_path: Path,
    packet_path: Path,
    items_path: Path,
    final_fingerprints: dict[str, str],
    original_fingerprints: dict[str, str],
    game_metadata: dict[str, object],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "CacheSchemaVersion": 2,
                "FinalBinaryFingerprints": final_fingerprints,
                "OriginalBinaryFingerprints": original_fingerprints,
                "PacketSHA256": file_sha256(packet_path),
                "ItemsSHA256": file_sha256(items_path),
                "GameContext": game_metadata,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def game_context_metadata(context: GameContext) -> dict[str, object]:
    return context_metadata(context)


def process_failure_message(result: subprocess.CompletedProcess[str]) -> str:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    if not lines:
        return f"process exited with code {result.returncode}"
    return " ".join(lines[:8])


def run_esp_export(
    root: Path,
    plugin_path: Path,
    mod_name: str,
    output_rel: str,
    report_rel: str,
    game_id: str,
) -> subprocess.CompletedProcess[str]:
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
            "--game",
            game_id,
        ],
        cwd=str(root),
        env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def run_pex_export(
    root: Path,
    pex_path: Path,
    output_rel: str,
    report_rel: str,
    context: GameContext,
) -> subprocess.CompletedProcess[str]:
    try:
        _decision, extract_entrypoint = require_capability_script_entrypoint(
            context,
            "pex",
            "read",
            "extract",
        )
    except ValueError as exc:
        return subprocess.CompletedProcess([], 2, "", str(exc))

    source_root = default_plugin_root()
    script = plugin_script_path(extract_entrypoint)
    if not script.is_file():
        return subprocess.CompletedProcess(
            [],
            2,
            "",
            f"missing PEX adapter entrypoint: {extract_entrypoint}",
        )
    output_path = resolve_project_path(root, output_rel, must_exist=False)
    report_path = resolve_project_path(root, report_rel, must_exist=False)
    require_under(output_path, root / "source" / "pex_exports", "PEX export output")
    if not (is_under(report_path, root / "qa") or is_under(report_path, root / "out")):
        raise ValueError(f"PEX export report must be under qa/ or out/: {report_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "Export",
            "--game",
            context.game_id,
            "--input-pex-path",
            str(pex_path),
            "--report-path",
            str(report_path),
            "--output-jsonl-path",
            str(output_path),
        ],
        cwd=str(root),
        env={
            **os.environ,
            "SKYRIM_CHS_WORKSPACE_ROOT": str(root),
            "SKYRIM_CHS_PLUGIN_ROOT": str(source_root),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


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
            value(row, "game_id"),
            value(row, "plugin"),
            value(row, "record_type"),
            value(row, "form_id"),
            value(row, "editor_id"),
            value(row, "field_path"),
            value(row, "subrecord_type"),
            value(row, "subrecord_index"),
            value(row, "occurrence_index"),
        ]
    )


def plugin_logical_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            value(row, "game_id"),
            value(row, "plugin"),
            value(row, "record_type"),
            value(row, "form_id"),
            value(row, "editor_id"),
            value(row, "field_path"),
            value(row, "subrecord_type"),
        ]
    )


def pex_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            value(row, "game_id"),
            value(row, "ModName"),
            value(row, "object_name"),
            value(row, "state_name"),
            value(row, "function_name"),
            value(row, "opcode"),
            value(row, "opcode_form"),
            value(row, "instruction_index"),
            value(row, "argument_index"),
            value(row, "callee"),
            value(row, "semantic_argument_index"),
            value(row, "semantic_argument_role"),
            value(row, "visibility_basis"),
            value(row, "classification"),
            value(row, "Source"),
        ]
    )


def pex_location_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            value(row, "game_id"),
            value(row, "ModName"),
            value(row, "object_name"),
            value(row, "state_name"),
            value(row, "function_name"),
            value(row, "opcode"),
            value(row, "opcode_form"),
            value(row, "instruction_index"),
            value(row, "argument_index"),
            value(row, "callee"),
            value(row, "semantic_argument_index"),
            value(row, "semantic_argument_role"),
            value(row, "visibility_basis"),
            value(row, "classification"),
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


def approved_pex_translation_targets(root: Path, mod_name: str, pex: Path) -> dict[str, tuple[str, str]]:
    translation = root / "work" / "normalized" / mod_name / "pex_apply" / f"{pex.stem}.translation.jsonl"
    if not translation.is_file():
        return {}
    approved: dict[str, tuple[str, str]] = {}
    for row in read_jsonl_objects(translation):
        if pex_translation_skip_reason(row):
            continue
        source = pex_row_value(row, "Source", "source")
        target = pex_row_value(row, "Result", "result", "Target", "target", "translation")
        if source.strip() and target.strip():
            approved[pex_location_identity(row)] = (source, target)
    return approved



def protected_binary_value(text: str, context: str) -> bool:
    trimmed = text.strip()
    normalized_context = context.lower()
    normalized_text = trimmed.lower()
    pex_row = {
        "Source": trimmed,
        "Context": context,
        "opcode": "",
        "risk": "",
    }
    opcode_match = re.search(r"\bopcode=([^;\s]+)", context, re.IGNORECASE)
    if opcode_match:
        pex_row["opcode"] = opcode_match.group(1)
    if pex_logic_protection_reason(pex_row):
        return True
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
        "_" in trimmed or re.search(r"[A-Z]", trimmed[1:]) or "opcode=cmp_eq" in normalized_context
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


def collect_plugin_items(
    root: Path,
    workspace: Path,
    final_mod: Path,
    mod_name: str,
    allowed_words: set[str],
    context: GameContext,
) -> tuple[int, list[ReviewItem], list[ExportFailure]]:
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

        original_run = run_esp_export(root, original_plugin, mod_name, original_export, original_report, context.game_id)
        if original_run.returncode != 0:
            failures.append(ExportFailure("plugin", relative_plugin, "export-original", process_failure_message(original_run)))
            continue
        final_run = run_esp_export(root, plugin, mod_name, final_export, final_report, context.game_id)
        if final_run.returncode != 0:
            failures.append(ExportFailure("plugin", relative_plugin, "export-final", process_failure_message(final_run)))
            continue

        try:
            original_rows = read_jsonl_objects(root / original_export, strict=True)
            final_rows = read_jsonl_objects(root / final_export, strict=True)
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
            item_context = (
                f"record={value(original_row, 'record_type')}; "
                f"form_id={value(original_row, 'form_id')}; "
                f"subrecord={value(original_row, 'subrecord_type')}; "
                f"editor_id={value(original_row, 'editor_id')}"
            )
            add_review_item(
                items,
                relative_plugin,
                "plugin-binary",
                item_context,
                source_text,
                final_text,
                risk,
                identity,
                allowed_words,
            )
    return len(plugin_files), items, failures


def collect_pex_items(
    root: Path,
    workspace: Path,
    final_mod: Path,
    mod_name: str,
    allowed_words: set[str],
    context: GameContext,
) -> tuple[int, list[ReviewItem], list[ExportFailure]]:
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

        original_run = run_pex_export(
            root,
            original_pex,
            original_export,
            original_report,
            context,
        )
        if original_run.returncode != 0:
            failures.append(ExportFailure("pex", relative_pex, "export-original", process_failure_message(original_run)))
            continue
        final_run = run_pex_export(
            root,
            pex,
            final_export,
            final_report,
            context,
        )
        if final_run.returncode != 0:
            failures.append(ExportFailure("pex", relative_pex, "export-final", process_failure_message(final_run)))
            continue

        try:
            original_rows = read_jsonl_objects(root / original_export, strict=True)
            final_rows = read_jsonl_objects(root / final_export, strict=True)
        except json.JSONDecodeError as exc:
            failures.append(ExportFailure("pex", relative_pex, "read-export", str(exc)))
            continue

        final_by_key: dict[str, dict[str, Any]] = {}
        for row in final_rows:
            final_by_key.setdefault(pex_location_identity(row), row)
        approved_targets = approved_pex_translation_targets(root, mod_name, pex)
        for original_row in original_rows:
            identity = pex_identity(original_row)
            final_row = final_by_key.get(pex_location_identity(original_row))
            if final_row is None:
                continue
            source_text = value(original_row, "Source")
            final_text = value(final_row, "Source")
            row_context = (
                f"object={value(original_row, 'object_name')}; "
                f"function={value(original_row, 'function_name')}; "
                f"opcode={value(original_row, 'opcode')}; "
                f"callee={value(original_row, 'callee')}; "
                f"instruction={value(original_row, 'instruction_index')}; "
                f"argument={value(original_row, 'argument_index')}; "
                f"semantic_argument={value(original_row, 'semantic_argument_index')}; "
                f"semantic_role={value(original_row, 'semantic_argument_role')}; "
                f"classification={value(original_row, 'classification')}; "
                f"visibility_basis={value(original_row, 'visibility_basis')}"
            )
            safety_row = dict(original_row)
            safety_row.setdefault("Source", source_text)
            safety_row.setdefault("Context", row_context)
            approved = approved_targets.get(pex_location_identity(original_row))
            exact_approved_change = approved == (source_text, final_text)
            safety_reason = "" if exact_approved_change else pex_logic_protection_reason(safety_row)
            risk = "protected-review" if safety_reason else (
                "review" if exact_approved_change else review_risk(pex_row_value(original_row, "risk", "Risk"))
            )
            add_review_item(
                items,
                relative_pex,
                "pex-binary",
                row_context,
                source_text,
                final_text,
                risk,
                identity,
                allowed_words,
            )
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
    context: GameContext,
) -> str:
    sorted_items = sorted(review_items, key=lambda item: (item.File, item.Kind, item.Identity, item.Context, item.Source, item.Final))
    item_lines = [json.dumps(asdict(item), ensure_ascii=False, separators=(",", ":")) for item in sorted_items]
    item_text = "\n".join(item_lines) + ("\n" if item_lines else "")
    items_hash = string_sha256(item_text)
    write_text_if_changed(items_path, item_lines)

    protected_count = sum(1 for item in sorted_items if item.Risk == "protected-review")
    manual_count = sum(1 for item in sorted_items if item.Risk == "manual-review")
    grouped_rows = aggregate_review_rows(
        [
            {
                "File": item.File,
                "Line": 0,
                "Type": item.Kind,
                "Risk": item.Risk,
                "Context": f"{item.Context}; identity={item.Identity}",
                "Source": item.Source,
                "Target": item.Final,
            }
            for item in sorted_items
        ]
    )
    translation_context_path = root / "qa" / f"{mod_name}.translation_context.json"
    translation_context, _context_issues = validated_translation_context(root, mod_name, context)
    plugin_text = context.require_capability("plugin_text")
    pex = context.require_capability("pex")

    lines: list[str] = [
        "# Final Binary Review Packet",
        "",
        *(f"- {key}: {value}" for key, value in context_metadata(context).items()),
        f"- plugin_text_adapter_id: {plugin_text.adapter_id}",
        "- plugin_text_adapter_contract_version: "
        f"{context.capability_option_positive_int('plugin_text', 'adapter_contract_version')}",
        f"- pex_adapter_id: {pex.adapter_id}",
        f"- pex_capability_level: {pex.level}",
        f"- pex_category: {context.capability_option_text('pex', 'pex_category')}",
        "- archive_readable_formats: "
        f"{', '.join(sorted(context.archive_extensions_at_least('read_only'))) or 'none'}",
        "- archive_write_formats: "
        f"{', '.join(sorted(context.archive_extensions_at_least('experimental_write'))) or 'none'}",
        f"- ModName: {mod_name}",
        f"- Workspace: {relative_path(root, workspace)}",
        f"- FinalModDir: {relative_path(root, final_mod)}",
        f"- Items JSONL: {relative_path(root, items_path)}",
        f"- Items SHA256: {items_hash}",
        f"- Plugin files checked: {plugin_count}",
        f"- PEX files checked: {pex_count}",
        f"- Review items: {len(sorted_items)}",
        f"- Aggregated review groups: {len(grouped_rows)}",
        f"- Manual review items: {manual_count}",
        f"- Protected review items: {protected_count}",
        f"- Export failures: {len(failures)}",
        f"- Mod context: {relative_path(root, translation_context_path)}",
        f"- Mod context status: {translation_context.get('status', 'missing')}",
        f"- Mod summary: {translation_context.get('summary', '')}",
        "",
        "## Review Instructions",
        "",
        "- This packet compares original workspace ESP/PEX strings with strings re-read from `final_mod` binaries.",
        "- Review the `Final` column as the actual delivered text, not as an intermediate translation table.",
        "- Use the current Game Profile and evidence-bound Mod summary to judge terminology, tone, actions, objects, control ownership, and functional relationships.",
        "- Give targeted semantic review to short labels, long help text, and conflicting-target groups instead of accepting word-by-word Chinese.",
        "- `protected-review` means a protected or logic-like original string changed in final_mod and must be treated as blocking until explained.",
        "- `untranslated-review` means an English string was unchanged in final_mod and must be translated or explicitly justified before delivery.",
        "- The final model review must explicitly mention every final_mod ESP/PEX file listed in the JSONL packet.",
        "- This script is read-only for ESP/PEX binaries and writes only source/pex_exports, source/plugin_exports, and qa reports.",
        "",
        "The model review output must mention this packet, the JSONL path, the Items SHA256, every reviewed file, and these exact passing claims:",
        "",
        *model_claim_lines(code=True),
        "",
        "## Aggregated Binary Text",
        "",
        "Only rows with the same source, final text, kind, risk, and context are compressed. The raw Items JSONL remains complete occurrence-level evidence. A conclusion for a group ID covers all listed occurrences unless the finding names an exception.",
        "",
    ]
    if not grouped_rows:
        lines.append("No changed ESP/PEX text rows were detected.")
    else:
        append_review_groups_table(
            lines,
            grouped_rows,
            kind_heading="Kind",
            target_heading="Final",
            include_line=False,
            cell=markdown_cell,
        )

    append_review_group_sections(lines, grouped_rows)

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
            "- Real game installations, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    write_text_if_changed(packet_path, lines)
    return items_hash


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an agent model review packet from actual final_mod ESP/PEX text differences.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--packet-output-path", default="")
    parser.add_argument("--items-jsonl-path", default="")
    parser.add_argument("--cache-path", default="")
    parser.add_argument("--reuse-current-if-unchanged", action="store_true")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    args = parser.parse_args()

    root = project_root()
    context = resolve_workspace_game_context(root, args.game)
    mod_name = args.mod_name
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{mod_name}", must_exist=True)
    workspace = find_data_root(workspace, context=context).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_path(root, default_final_mod_dir(root, mod_name)), must_exist=True)
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
    original_fingerprints = binary_fingerprints(workspace)
    cache_game_metadata = game_context_metadata(context)
    if args.reuse_current_if_unchanged and cached_packet_is_current(
        cache_path,
        packet_path,
        items_path,
        fingerprints,
        original_fingerprints,
        cache_game_metadata,
    ):
        print(f"Final binary review packet written to: {packet_path}")
        print(f"Final binary review items written to: {items_path}")
        print(f"Review items: {read_report_metric(packet_path, 'Review items') or count_jsonl_rows(items_path)}")
        print(f"Protected review items: {read_report_metric(packet_path, 'Protected review items') or 0}")
        print(f"Export failures: {read_report_metric(packet_path, 'Export failures') or 0}")
        print("Reused current final binary review packet cache.")
        return 0

    if not fingerprints:
        write_reports(root, mod_name, workspace, final_mod, packet_path, items_path, 0, 0, [], [], context)
        write_cache(
            cache_path,
            packet_path,
            items_path,
            fingerprints,
            original_fingerprints,
            cache_game_metadata,
        )
        print(f"Final binary review packet written to: {packet_path}")
        print(f"Final binary review items written to: {items_path}")
        print("Review items: 0")
        print("Protected review items: 0")
        print("Export failures: 0")
        return 0

    allowed_words = load_allowed_words(root)
    plugin_count, plugin_items, plugin_failures = collect_plugin_items(root, workspace, final_mod, mod_name, allowed_words, context)
    pex_count, pex_items, pex_failures = collect_pex_items(
        root,
        workspace,
        final_mod,
        mod_name,
        allowed_words,
        context,
    )
    review_items = plugin_items + pex_items
    failures = plugin_failures + pex_failures
    write_reports(root, mod_name, workspace, final_mod, packet_path, items_path, plugin_count, pex_count, review_items, failures, context)
    write_cache(
        cache_path,
        packet_path,
        items_path,
        fingerprints,
        original_fingerprints,
        cache_game_metadata,
    )
    protected_count = sum(1 for item in review_items if item.Risk == "protected-review")

    print(f"Final binary review packet written to: {packet_path}")
    print(f"Final binary review items written to: {items_path}")
    print(f"Review items: {len(review_items)}")
    print(f"Protected review items: {protected_count}")
    print(f"Export failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

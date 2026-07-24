"""Run the controlled Bethesda STRINGS-family adapter inside one workspace."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from adapter_registry import require_adapter
from adapter_result_io import (
    build_result,
    prepare_adapter_result_path,
    read_adapter_result,
    require_translation_input_lane,
    write_adapter_result_if_requested,
)
from capability_resolver import CapabilityDecision, resolve_capability
from file_utils import create_regular_directory_under
from file_utils import sha256_file, validate_regular_path_under
from game_context import resolve_workspace_game_context, supported_game_ids
from managed_tool_resolver import (
    adapter_uses_managed_binding,
    leased_payload_path,
    load_workspace_tool_config,
)
from project_paths import is_under, project_root
from project_paths import plugin_root as _plugin_root
from project_paths import relative_path, require_under_any, resolve_project_path


ADAPTER_ID = "bethesda-string-tables"
TABLE_EXTENSIONS = frozenset({".strings", ".dlstrings", ".ilstrings"})
EXPERIMENTAL_WARNING = (
    "Experimental string-table writeback capability was used; independent in-game "
    "validation is still required."
)
_CAPABILITY_LINE = re.compile(r"(?mi)^-\s*capability_level:\s*(\S+)\s*$")
_GAME_LINE = re.compile(r"(?mi)^-\s*game_id:\s*(\S+)\s*$")


def plugin_root() -> Path:
    """Compatibility seam retained for wrapper tests and downstream adapters."""

    return _plugin_root()


def configured_dotnet_path(
    root: Path,
    config: dict[str, object],
    leases: ExitStack,
) -> Path:
    """Resolve .NET while retaining any managed runtime lease."""

    resolution = leases.enter_context(
        leased_payload_path(
            root,
            config,
            "DotNetSdkPath",
            command="run string-table adapter",
            managed_only=adapter_uses_managed_binding(
                root,
                config,
                "BethesdaStringTableToolPath",
            ),
        )
    )
    if resolution.path is None:
        raise FileNotFoundError("managed .NET binding is unavailable")
    return resolution.path


def ensure_adapter_dll(
    root: Path,
    config: dict[str, object],
    leases: ExitStack,
) -> Path:
    """Resolve the string-table adapter without workspace-local rebuilding."""

    resolution = leases.enter_context(
        leased_payload_path(
            root,
            config,
            "BethesdaStringTableToolPath",
            command="run string-table adapter",
        )
    )
    if resolution.path is None:
        raise FileNotFoundError("managed string-table adapter binding is unavailable")
    return resolution.path


def _option_text(options: Mapping[str, object], name: str) -> str:
    value = options.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"string_tables adapter option '{name}' must be non-empty text")
    return value.strip()


def _option_positive_int(options: Mapping[str, object], name: str) -> int:
    value = options.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"string_tables adapter option '{name}' must be a positive integer")
    return value


def _require_table_path(path: Path, label: str) -> Path:
    if path.suffix.lower() not in TABLE_EXTENSIONS:
        supported = ", ".join(sorted(TABLE_EXTENSIONS))
        raise ValueError(f"{label} must use one of: {supported}")
    return path


def _source_mod_lane(root: Path, path: Path) -> str:
    resolved = path.resolve(strict=True)
    for base in (
        root / "work" / "extracted_mods",
        root / "work" / "archive_extracts",
    ):
        base_resolved = base.resolve(strict=False)
        if not is_under(resolved, base_resolved):
            continue
        relative = resolved.relative_to(base_resolved)
        if len(relative.parts) >= 2:
            return relative.parts[0]
    raise ValueError(
        "String-table input must identify a Mod lane under work/extracted_mods "
        f"or work/archive_extracts: {path}"
    )


def _resolve_report(root: Path, value: str) -> Path:
    report = resolve_project_path(root, value, must_exist=False)
    require_under_any(report, [root / "qa", root / "out"], "ReportPath")
    if report.suffix.lower() != ".md":
        raise ValueError("ReportPath must be a Markdown (.md) file")
    return report


def _resolve_inventory_json(root: Path, value: str) -> Path | None:
    if not value:
        return None
    output = resolve_project_path(root, value, must_exist=False)
    require_under_any(output, [root / "qa", root / "out"], "OutputJsonPath")
    if output.suffix.lower() != ".json":
        raise ValueError("OutputJsonPath must be a JSON (.json) file")
    return output


def _resolve_export_jsonl(root: Path, value: str, mod_name: str) -> Path:
    if not value:
        raise ValueError("--output-jsonl-path is required for Export")
    output = resolve_project_path(root, value, must_exist=False)
    require_under_any(
        output,
        [
            root / "source" / "string_tables" / mod_name,
            root / "source" / "localized_delivery" / mod_name,
            root / "work" / "normalized" / mod_name,
        ],
        "OutputJsonlPath",
    )
    if output.suffix.lower() != ".jsonl":
        raise ValueError("OutputJsonlPath must be a JSONL (.jsonl) file")
    return output


def _resolve_output_table(root: Path, value: str, mod_name: str) -> Path:
    if not value:
        raise ValueError("--output-table-path is required for Apply or Verify")
    output = _require_table_path(
        resolve_project_path(root, value, must_exist=False),
        "OutputTablePath",
    )
    require_under_any(
        output,
        [
            root / "out" / mod_name / "tool_outputs",
            root / "translated" / "tool_outputs" / mod_name,
        ],
        "OutputTablePath",
    )
    return output


def _read_apply_evidence(
    root: Path,
    raw_path: str,
    *,
    output_table: Path,
    input_table: Path,
    translation_jsonl: Path,
    mod_name: str,
    game_id: str,
    adapter_id: str,
) -> tuple[str, Path]:
    if not raw_path:
        raise ValueError("Verify requires --apply-adapter-result-path capability evidence")
    receipt_path = resolve_project_path(root, raw_path, must_exist=True)
    require_under_any(receipt_path, [root / "qa", root / "out"], "ApplyAdapterResultPath")
    receipt_path = validate_regular_path_under(
        receipt_path,
        root,
        kind="file",
        label="ApplyAdapterResultPath",
    )
    result = read_adapter_result(receipt_path)
    if result.status != "success" or result.operation != "apply":
        raise ValueError("Verify requires a successful string-table Apply adapter result")
    if result.adapter_id != adapter_id:
        raise ValueError("Apply adapter result adapter_id does not match the string-table adapter")
    if result.mod_name != mod_name:
        raise ValueError("Apply adapter result Mod lane does not match Verify inputs")

    expected_output = relative_path(root, output_table).replace("\\", "/")
    expected_output_hash = sha256_file(output_table)
    artifacts = result.artifacts
    if not any(
        item.path == expected_output and item.sha256 == expected_output_hash
        for item in artifacts
    ):
        raise ValueError("Apply adapter result does not bind the verified string-table hash")

    expected_inputs = {
        relative_path(root, input_table).replace("\\", "/"): sha256_file(input_table),
        relative_path(root, translation_jsonl).replace("\\", "/"): sha256_file(
            translation_jsonl
        ),
    }
    actual_inputs = {item.path: item.sha256 for item in result.inputs}
    if actual_inputs != expected_inputs:
        raise ValueError("Apply adapter result input lineage does not match Verify inputs")

    if not result.evidence_files:
        raise ValueError("Apply adapter result has no capability evidence file")
    levels: set[str] = set()
    games: set[str] = set()
    for value in result.evidence_files:
        evidence = resolve_project_path(root, value, must_exist=True)
        require_under_any(evidence, [root / "qa", root / "out"], "ApplyEvidencePath")
        evidence = validate_regular_path_under(
            evidence,
            root,
            kind="file",
            label="ApplyEvidencePath",
        )
        artifact_hash = next(
            (item.sha256 for item in artifacts if item.path == value),
            None,
        )
        if artifact_hash != sha256_file(evidence):
            raise ValueError("Apply evidence hash does not match the adapter result")
        text = evidence.read_text(encoding="utf-8-sig")
        levels.update(match.group(1) for match in _CAPABILITY_LINE.finditer(text))
        games.update(match.group(1) for match in _GAME_LINE.finditer(text))
    if games != {game_id}:
        raise ValueError("Apply capability evidence game_id does not match the workspace")
    if len(levels) != 1:
        raise ValueError("Apply capability evidence must contain one capability_level")
    level = next(iter(levels))
    if level not in {"experimental_write", "stable"}:
        raise ValueError("Apply capability evidence is not write-capable")
    if level == "experimental_write" and not any(item.strip() for item in result.warnings):
        raise ValueError("Experimental Apply adapter result must include a warning")
    return level, receipt_path


def _capability_decision(context, mode: str) -> CapabilityDecision:
    operation = {
        "Inventory": "inventory",
        "Export": "read",
        "Apply": "write",
        "Verify": "read",
    }[mode]
    return resolve_capability(context, "string_tables", operation)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the project-local Bethesda STRINGS-family adapter."
    )
    parser.add_argument("--mode", choices=("Inventory", "Export", "Apply", "Verify"), required=True)
    parser.add_argument("--input-table-path", required=True)
    parser.add_argument("--output-json-path", default="")
    parser.add_argument("--output-jsonl-path", default="")
    parser.add_argument("--translation-jsonl-path", default="")
    parser.add_argument("--output-table-path", default="")
    parser.add_argument("--report-path", default="qa/bethesda_string_table_tool_report.md")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    parser.add_argument("--adapter-result-path", default="")
    parser.add_argument("--apply-adapter-result-path", default="")
    parser.add_argument("--allow-experimental-writeback", action="store_true")
    args = parser.parse_args()

    root = project_root()
    result_path = prepare_adapter_result_path(root, args.adapter_result_path)
    adapter_operation = {
        "Inventory": "inventory",
        "Export": "extract",
        "Apply": "apply",
        "Verify": "verify",
    }[args.mode]
    adapter_id = ADAPTER_ID
    report: Path | None = None
    input_table: Path | None = None
    mod_name = ""
    generated_artifacts: list[Path] = []
    adapter_invoked = False

    try:
        context = resolve_workspace_game_context(root, args.game)
        decision = _capability_decision(context, args.mode)
        adapter_id = decision.adapter_id or ADAPTER_ID
        if not decision.supported:
            write_adapter_result_if_requested(
                result_path,
                lambda: build_result(
                    root=root,
                    status="blocked",
                    error_code=decision.error_code or "capability_unsupported",
                    operation=adapter_operation,
                    adapter_id=adapter_id,
                    blockers=(decision.reason,),
                ),
            )
            return 2
        require_adapter(adapter_id, adapter_operation)

        options = decision.adapter_options
        source_encoding = _option_text(options, "source_encoding")
        target_encoding = _option_text(options, "target_encoding")
        source_language = _option_text(options, "source_language")
        target_language = _option_text(options, "target_language")
        max_entries = _option_positive_int(options, "max_entries")
        max_file_bytes = _option_positive_int(options, "max_file_bytes")

        input_table = _require_table_path(
            resolve_project_path(root, args.input_table_path, must_exist=True),
            "InputTablePath",
        )
        mod_name = _source_mod_lane(root, input_table)
        input_table = validate_regular_path_under(
            input_table,
            root,
            kind="file",
            label="InputTablePath",
        )
        report = _resolve_report(root, args.report_path)
        inventory_json: Path | None = None
        output_jsonl: Path | None = None
        translation_jsonl: Path | None = None
        output_table: Path | None = None
        apply_receipt: Path | None = None

        if args.mode == "Inventory":
            inventory_json = _resolve_inventory_json(root, args.output_json_path)
            if inventory_json is not None:
                generated_artifacts.append(inventory_json)
        elif args.mode == "Export":
            output_jsonl = _resolve_export_jsonl(root, args.output_jsonl_path, mod_name)
            generated_artifacts.append(output_jsonl)
        else:
            if not args.translation_jsonl_path:
                raise ValueError("--translation-jsonl-path is required for Apply or Verify")
            translation_jsonl = resolve_project_path(
                root,
                args.translation_jsonl_path,
                must_exist=True,
            )
            require_translation_input_lane(root, translation_jsonl, mod_name)
            translation_jsonl = validate_regular_path_under(
                translation_jsonl,
                root / "translated",
                kind="file",
                label="TranslationJsonlPath",
            )
            output_table = _resolve_output_table(root, args.output_table_path, mod_name)
            if args.mode == "Apply":
                generated_artifacts.append(output_table)
                if decision.level == "experimental_write" and not args.allow_experimental_writeback:
                    message = (
                        f"String-table writeback for '{context.game_id}' is experimental; pass "
                        "--allow-experimental-writeback for an explicit project-local attempt."
                    )
                    write_adapter_result_if_requested(
                        result_path,
                        lambda: build_result(
                            root=root,
                            status="blocked",
                            error_code="experimental_confirmation_required",
                            operation=adapter_operation,
                            adapter_id=adapter_id,
                            blockers=(message,),
                        ),
                    )
                    return 2
            else:
                if not output_table.is_file():
                    raise FileNotFoundError(f"OutputTablePath for Verify does not exist: {output_table}")
                output_table = validate_regular_path_under(
                    output_table,
                    root,
                    kind="file",
                    label="OutputTablePath",
                )
                capability_level, apply_receipt = _read_apply_evidence(
                    root,
                    args.apply_adapter_result_path,
                    output_table=output_table,
                    input_table=input_table,
                    translation_jsonl=translation_jsonl,
                    mod_name=mod_name,
                    game_id=context.game_id,
                    adapter_id=adapter_id,
                )
                write_decision = resolve_capability(context, "string_tables", "write")
                if (
                    not write_decision.supported
                    or write_decision.adapter_id != adapter_id
                    or write_decision.level != capability_level
                ):
                    raise ValueError(
                        "Apply capability evidence does not match the current string-table write capability"
                    )
                decision = write_decision

        create_regular_directory_under(
            report.parent,
            root,
            label="ReportPath directory",
        )
        if report.exists():
            validate_regular_path_under(
                report,
                root,
                kind="file",
                label="ReportPath",
            ).unlink()
        for path in generated_artifacts:
            create_regular_directory_under(
                path.parent,
                root,
                label="Generated string-table artifact directory",
            )
            if path.exists():
                validate_regular_path_under(
                    path,
                    root,
                    kind="file",
                    label="Generated string-table artifact",
                ).unlink()

        config = resolve_project_path(root, args.config_path, must_exist=True)
        config = validate_regular_path_under(
            config,
            root,
            kind="file",
            label="ConfigPath",
        )
        tool_config = load_workspace_tool_config(root, config)
        input_hash = sha256_file(input_table)
        adapter_invoked = True
        with ExitStack() as leases:
            adapter_dll = ensure_adapter_dll(root, tool_config, leases)
            resolved_dotnet = configured_dotnet_path(root, tool_config, leases)
            command = [
                str(resolved_dotnet),
                str(adapter_dll),
                args.mode.lower(),
                "--game",
                context.game_id,
                "--capability-level",
                decision.level,
                "--project-root",
                str(root),
                "--input-table",
                str(input_table),
                "--source-encoding",
                source_encoding,
                "--source-language",
                source_language,
                "--report",
                str(report),
                "--max-entries",
                str(max_entries),
                "--max-file-bytes",
                str(max_file_bytes),
            ]
            if inventory_json is not None:
                command.extend(("--output-json", str(inventory_json)))
            if output_jsonl is not None:
                command.extend(("--output-jsonl", str(output_jsonl)))
            if args.mode in {"Apply", "Verify"}:
                assert translation_jsonl is not None and output_table is not None
                command.extend(
                    (
                        "--target-encoding",
                        target_encoding,
                        "--target-language",
                        target_language,
                        "--translation-jsonl",
                        str(translation_jsonl),
                        "--output-table",
                        str(output_table),
                    )
                )
            return_code = subprocess.run(command, cwd=str(root), check=False).returncode
        if sha256_file(input_table) != input_hash:
            raise RuntimeError("Input string table changed during controlled adapter invocation")
        success = return_code == 0 and report.is_file()
        success = success and all(path.is_file() for path in generated_artifacts)
        if not success:
            raise RuntimeError(f"String-table adapter failed with exit code {return_code}")

        if args.mode == "Inventory":
            artifacts = tuple(path for path in (inventory_json, report) if path is not None)
        elif args.mode == "Export":
            artifacts = (output_jsonl, report)
        else:
            artifacts = (output_table, report)
        warnings = (
            (EXPERIMENTAL_WARNING,)
            if args.mode in {"Apply", "Verify"} and decision.level == "experimental_write"
            else ()
        )
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="success",
                error_code=None,
                operation=adapter_operation,
                adapter_id=adapter_id,
                artifact_paths=tuple(path for path in artifacts if path is not None),
                evidence_paths=(report,),
                warnings=warnings,
                mod_name=mod_name,
                input_paths=(input_table, translation_jsonl)
                if args.mode == "Apply" and translation_jsonl is not None
                else (
                    (input_table, translation_jsonl, apply_receipt)
                    if args.mode == "Verify"
                    and translation_jsonl is not None
                    and apply_receipt is not None
                    else (input_table,)
                ),
            ),
        )
        return 0
    except Exception as exc:
        failure_reason = str(exc)
        for path in generated_artifacts:
            path.unlink(missing_ok=True)
        evidence = (report,) if report is not None and report.is_file() else ()
        failure_inputs = (
            (input_table,)
            if input_table is not None and input_table.is_file() and mod_name
            else ()
        )
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="error",
                error_code="adapter_failed" if adapter_invoked else "adapter_preflight_failed",
                operation=adapter_operation,
                adapter_id=adapter_id,
                evidence_paths=evidence,
                blockers=(failure_reason,),
                mod_name=mod_name if failure_inputs else "",
                input_paths=failure_inputs,
            ),
        )
        if result_path is None:
            raise
        print(f"Bethesda string-table tool failed: {failure_reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Bethesda string-table tool failed: {exc}", file=sys.stderr)
        sys.exit(1)

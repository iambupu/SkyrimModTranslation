"""Launch the project-local Mutagen plugin text adapter with strict path guards.

This wrapper is the only Python entry that may request ESP/ESM/ESL writeback
or independent controlled-adapter verification. Apply writes under out/;
Verify is read-only and may inspect an existing final_mod plugin under out/.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from adapter_registry import require_adapter
from adapter_result_io import (
    build_result,
    mod_lane_for_workspace_input,
    prepare_adapter_result_path,
    require_translation_input_lane,
    write_adapter_result_if_requested,
)
from capability_resolver import resolve_resource_capability
from dotnet_adapter_cache import configured_dotnet_path, ensure_adapter_dll
from game_context import resolve_workspace_game_context, supported_game_ids
from project_paths import plugin_root, project_root
from project_paths import resolve_project_path
from plugin_resource_evidence import (
    plugin_report_error_code,
    read_plugin_translation_target_light_state,
    validate_plugin_master_style_context,
    validate_regular_evidence_path_under,
)
from resource_model import classify_resource
from file_utils import create_regular_directory_under
from file_utils import read_json_unchecked as read_json
from file_utils import validate_regular_path_under
from project_paths import require_under_any as require_under


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}
TES4_RECORD_HEADER_SIZE = 24
TES4_LOCALIZED_FLAG = 0x00000080
TES4_LIGHT_FLAG = 0x00000200
ADAPTER_ID_FALLBACK = "mutagen-bethesda-plugin"
EXPERIMENTAL_WARNING = (
    "Experimental plugin writeback succeeded; the output remains experimental "
    "and requires independent in-game validation."
)
DRY_RUN_WARNING = "Dry run completed; no output plugin binary was generated."


def validate_translation_schema(path: Path) -> None:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"translation JSONL line {line_number} is invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(row, dict) or row.get("schema_version") != 2:
                actual = row.get("schema_version") if isinstance(row, dict) else None
                raise ValueError(
                    f"translation JSONL line {line_number} has unsupported "
                    f"schema_version={actual}; expected 2"
                )


def inspect_plugin_header_traits(path: Path) -> frozenset[str]:
    with path.open("rb") as handle:
        header = handle.read(TES4_RECORD_HEADER_SIZE)
    if len(header) != TES4_RECORD_HEADER_SIZE or header[:4] != b"TES4":
        raise ValueError(f"Input plugin does not start with a complete TES4 header: {path}")
    flags = int.from_bytes(header[8:12], byteorder="little", signed=False)
    traits: set[str] = set()
    if flags & TES4_LOCALIZED_FLAG:
        traits.add("localized")
    if flags & TES4_LIGHT_FLAG:
        traits.add("light")
    return frozenset(traits)


def dotnet_path(root: Path, config_path: Path, source_root: Path) -> Path:
    return configured_dotnet_path(
        root,
        read_json(config_path),
        source_root=source_root,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the project-local Mutagen ESP/ESM/ESL text adapter.")
    parser.add_argument("--mode", choices=("Apply", "Verify"), default="Apply")
    parser.add_argument("--input-plugin-path", required=True)
    parser.add_argument("--translation-jsonl-path", required=True)
    parser.add_argument("--output-plugin-path", required=True)
    parser.add_argument("--report-path", default="qa/mutagen_plugin_text_tool_report.md")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    parser.add_argument("--adapter-result-path", default="")
    parser.add_argument("--master-style-manifest", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_root()
    adapter_operation = "apply" if args.mode == "Apply" else "verify"
    result_path = prepare_adapter_result_path(root, args.adapter_result_path)
    adapter_id = ADAPTER_ID_FALLBACK
    output_plugin: Path | None = None
    report: Path | None = None
    output_guarded = False
    adapter_invoked = False
    input_paths: tuple[Path, ...] = ()
    master_style_manifest: Path | None = None
    mod_name = ""

    try:
        output_plugin = resolve_project_path(root, args.output_plugin_path, must_exist=False)
        report = resolve_project_path(root, args.report_path, must_exist=False)
        require_under(output_plugin, [root / "out"], "OutputPluginPath")
        require_under(report, [root / "qa"], "ReportPath")
        if output_plugin.suffix.lower() not in PLUGIN_EXTENSIONS:
            raise ValueError("OutputPluginPath must be .esp, .esm, or .esl.")
        if report.suffix.lower() != ".md":
            raise ValueError("ReportPath must be a Markdown (.md) file.")
        source_root = plugin_root()
        context = resolve_workspace_game_context(root, args.game)
        input_plugin = resolve_project_path(root, args.input_plugin_path, must_exist=True)
        translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
        config = resolve_project_path(root, args.config_path, must_exist=True)
        if args.master_style_manifest.strip():
            candidate = resolve_project_path(
                root,
                args.master_style_manifest,
                must_exist=True,
            )
            if candidate.suffix.casefold() != ".json":
                raise ValueError("MasterStyleManifest must be a JSON file.")
            master_style_manifest = validate_regular_evidence_path_under(
                candidate,
                root / "work" / "plugin_context",
                kind="file",
                label="MasterStyleManifest",
            )

        require_under(
            input_plugin,
            [root / "work" / "extracted_mods"],
            "InputPluginPath",
        )
        require_under(translation_jsonl, [root / "translated"], "TranslationJsonlPath")
        input_plugin = validate_regular_path_under(
            input_plugin,
            root / "work" / "extracted_mods",
            kind="file",
            label="InputPluginPath",
        )
        translation_jsonl = validate_regular_path_under(
            translation_jsonl,
            root / "translated",
            kind="file",
            label="TranslationJsonlPath",
        )
        config = validate_regular_path_under(
            config,
            root,
            kind="file",
            label="ConfigPath",
        )
        mod_name = mod_lane_for_workspace_input(root, input_plugin)
        require_translation_input_lane(root, translation_jsonl, mod_name)
        output_roots = (
            [root / "out" / mod_name / "tool_outputs"]
            if args.mode == "Apply"
            else [root / "out" / mod_name]
        )
        require_under(output_plugin, output_roots, "OutputPluginPath")
        if input_plugin.suffix.lower() not in PLUGIN_EXTENSIONS:
            raise ValueError("InputPluginPath must be .esp, .esm, or .esl.")
        if translation_jsonl.suffix.lower() != ".jsonl":
            raise ValueError("TranslationJsonlPath must be a JSONL (.jsonl) file.")
        validate_translation_schema(translation_jsonl)
        input_paths = tuple(
            path
            for path in (input_plugin, translation_jsonl, master_style_manifest)
            if path is not None
        )

        target_light_state = read_plugin_translation_target_light_state(
            translation_jsonl
        )
        if target_light_state is None:
            write_adapter_result_if_requested(
                result_path,
                lambda: build_result(
                    root=root,
                    status="blocked",
                    error_code="master_style_unknown",
                    operation=adapter_operation,
                    adapter_id=adapter_id,
                    blockers=(
                        "Plugin translation contains an actual write target with "
                        "unknown master style; regenerate target-scoped canonical "
                        "evidence before invoking writeback.",
                    ),
                    mod_name=mod_name,
                    input_paths=input_paths,
                ),
            )
            return 2

        capability_operation = "write" if args.mode == "Apply" else "read"
        resource_traits = set(inspect_plugin_header_traits(input_plugin))
        if target_light_state is True:
            resource_traits.add("light")
        resource = classify_resource(
            context,
            input_plugin.relative_to(root),
            traits=frozenset(resource_traits),
        )
        decision = resolve_resource_capability(context, resource, capability_operation)
        adapter_id = decision.adapter_id or ADAPTER_ID_FALLBACK
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
                    mod_name=mod_name,
                    input_paths=input_paths,
                ),
            )
            return 2

        try:
            require_adapter(adapter_id, adapter_operation)
        except ValueError as exc:
            blocker = str(exc)
            write_adapter_result_if_requested(
                result_path,
                lambda: build_result(
                    root=root,
                    status="blocked",
                    error_code="adapter_unavailable",
                    operation=adapter_operation,
                    adapter_id=adapter_id,
                    blockers=(blocker,),
                    mod_name=mod_name,
                    input_paths=input_paths,
                ),
            )
            return 2

        release = str(decision.adapter_options.get("mutagen_release") or "").strip()
        if not release:
            raise ValueError("plugin_text adapter option 'mutagen_release' must be non-empty")

        output_guarded = True
        create_regular_directory_under(
            output_plugin.parent,
            root,
            label="OutputPluginPath directory",
        )
        create_regular_directory_under(
            report.parent,
            root,
            label="ReportPath directory",
        )
        if report.exists():
            validate_regular_path_under(
                report,
                root / "qa",
                kind="file",
                label="ReportPath",
            ).unlink()
        if args.mode == "Apply":
            if output_plugin.exists():
                validate_regular_path_under(
                    output_plugin,
                    output_roots[0],
                    kind="file",
                    label="OutputPluginPath",
                ).unlink()
        elif not output_plugin.is_file():
            raise FileNotFoundError(f"Verify output plugin does not exist: {output_plugin}")
        else:
            output_plugin = validate_regular_path_under(
                output_plugin,
                output_roots[0],
                kind="file",
                label="OutputPluginPath",
            )

        dotnet = dotnet_path(root, config, source_root)
        adapter_dll = ensure_adapter_dll(root, source_root, dotnet, "SkyrimPluginTextTool")

        command = [
            str(dotnet),
            str(adapter_dll),
            args.mode.lower(),
            "--game",
            context.game_id,
            "--mutagen-release",
            release,
            "--capability-level",
            decision.level,
            "--project-root",
            str(root),
            "--input-plugin",
            str(input_plugin),
            "--translation-jsonl",
            str(translation_jsonl),
            "--output-plugin",
            str(output_plugin),
            "--report",
            str(report),
        ]
        if master_style_manifest is not None:
            command.extend(["--master-style-manifest", str(master_style_manifest)])
        if args.dry_run and args.mode == "Apply":
            command.append("--dry-run")

        adapter_invoked = True
        return_code = subprocess.run(command, cwd=str(root), check=False).returncode
        valid_success = return_code == 0 and report.is_file()
        if args.mode == "Apply":
            valid_success = valid_success and (args.dry_run or output_plugin.is_file())
        if not valid_success:
            if args.mode == "Apply":
                output_plugin.unlink(missing_ok=True)
            evidence_paths = (report,) if report.is_file() else ()
            error_code = (
                plugin_report_error_code(report)
                if report.is_file()
                else ""
            )
            write_adapter_result_if_requested(
                result_path,
                lambda: build_result(
                    root=root,
                    status="error",
                    error_code=error_code or "adapter_failed",
                    operation=adapter_operation,
                    adapter_id=adapter_id,
                    evidence_paths=evidence_paths,
                    mod_name=mod_name,
                    input_paths=input_paths,
                ),
            )
            return return_code or 1

        master_style_context = validate_plugin_master_style_context(
            report,
            project_root=root,
            expected_input=input_plugin,
            expected_game=context.game_id,
        )

        artifacts = tuple(
            path
            for path in (
                output_plugin if output_plugin.is_file() else None,
                report,
                master_style_context.path,
            )
            if path is not None and path.is_file()
        )
        evidence_paths = tuple(
            path
            for path in (report, master_style_context.path)
            if path is not None and path.is_file()
        )
        warnings: list[str] = []
        if decision.level == "experimental_write":
            warnings.append(EXPERIMENTAL_WARNING)
        if args.dry_run:
            warnings.append(DRY_RUN_WARNING)
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="success",
                error_code=None,
                operation=adapter_operation,
                adapter_id=adapter_id,
                artifact_paths=artifacts,
                evidence_paths=evidence_paths,
                warnings=tuple(warnings),
                mod_name=mod_name,
                input_paths=input_paths,
            ),
        )
        return 0
    except Exception as exc:
        blocker = str(exc)
        if args.mode == "Apply" and output_guarded and output_plugin is not None:
            output_plugin.unlink(missing_ok=True)
        evidence_paths = (report,) if report is not None and report.is_file() else ()
        report_error_code = (
            plugin_report_error_code(report)
            if report is not None and report.is_file()
            else ""
        )
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="error",
                error_code=(
                    report_error_code
                    or ("adapter_failed" if adapter_invoked else "adapter_preflight_failed")
                ),
                operation=adapter_operation,
                adapter_id=adapter_id,
                evidence_paths=evidence_paths,
                blockers=(blocker,),
                mod_name=mod_name,
                input_paths=input_paths if mod_name else (),
            ),
        )
        print(f"Mutagen plugin text tool failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Mutagen plugin text tool failed: {exc}", file=sys.stderr)
        sys.exit(1)

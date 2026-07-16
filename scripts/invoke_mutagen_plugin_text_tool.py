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
from dotnet_adapter_cache import ensure_adapter_dll
from game_context import resolve_workspace_game_context, supported_game_ids
from project_paths import plugin_root, project_root
from project_paths import is_under, resolve_project_path
from resource_model import classify_resource
from file_utils import read_json_unchecked as read_json
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
    config = read_json(config_path)
    decoder_tools = config.get("DecoderTools")
    configured = ""
    if isinstance(decoder_tools, dict):
        configured = str(decoder_tools.get("DotNetSdkPath") or "")
    candidate = Path(configured) if configured else root / "tools" / "dotnet-sdk" / "dotnet.exe"
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    if not (is_under(resolved, root) or is_under(resolved, source_root)):
        raise ValueError(f"DotNetSdkPath must be under the workspace or plugin source: {resolved}")
    return resolved


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

        require_under(
            input_plugin,
            [root / "work" / "extracted_mods"],
            "InputPluginPath",
        )
        require_under(translation_jsonl, [root / "translated"], "TranslationJsonlPath")
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

        capability_operation = "write" if args.mode == "Apply" else "read"
        resource = classify_resource(
            context,
            input_plugin.relative_to(root),
            traits=inspect_plugin_header_traits(input_plugin),
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
                    input_paths=(input_plugin, translation_jsonl),
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
                    input_paths=(input_plugin, translation_jsonl),
                ),
            )
            return 2

        release = str(decision.adapter_options.get("mutagen_release") or "").strip()
        if not release:
            raise ValueError("plugin_text adapter option 'mutagen_release' must be non-empty")

        output_guarded = True
        output_plugin.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        if args.mode == "Apply":
            output_plugin.unlink(missing_ok=True)
        elif not output_plugin.is_file():
            raise FileNotFoundError(f"Verify output plugin does not exist: {output_plugin}")

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
            write_adapter_result_if_requested(
                result_path,
                lambda: build_result(
                    root=root,
                    status="error",
                    error_code="adapter_failed",
                    operation=adapter_operation,
                    adapter_id=adapter_id,
                    evidence_paths=evidence_paths,
                ),
            )
            return return_code or 1

        artifacts = tuple(
            path
            for path in (output_plugin if output_plugin.is_file() else None, report)
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
                evidence_paths=(report,),
                warnings=tuple(warnings),
                mod_name=mod_name,
                input_paths=(input_plugin, translation_jsonl),
            ),
        )
        return 0
    except Exception as exc:
        blocker = str(exc)
        if args.mode == "Apply" and output_guarded and output_plugin is not None:
            output_plugin.unlink(missing_ok=True)
        evidence_paths = (report,) if report is not None and report.is_file() else ()
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="error",
                error_code="adapter_failed" if adapter_invoked else "adapter_preflight_failed",
                operation=adapter_operation,
                adapter_id=adapter_id,
                evidence_paths=evidence_paths,
                blockers=(blocker,),
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

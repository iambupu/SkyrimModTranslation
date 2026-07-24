"""Launch the project-local Mutagen PEX visible-string adapter."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from adapter_registry import require_adapter
from adapter_result_io import (
    build_result,
    mod_lane_for_adapter_input,
    mod_lane_for_workspace_input,
    prepare_adapter_result_path,
    read_adapter_result,
    require_translation_input_lane,
    write_adapter_result_if_requested,
)
from capability_resolver import resolve_capability
from file_utils import sha256_file
from game_context import (
    GameContext,
    resolve_workspace_game_context as resolve_game_context,
    supported_game_ids,
)
from project_paths import plugin_root, project_root
from project_paths import require_under_any as require_under
from project_paths import relative_path, resolve_project_path
from managed_tool_resolver import (
    adapter_uses_managed_binding,
    leased_payload_path,
    load_workspace_tool_config,
)


ADAPTER_ID_FALLBACK = "mutagen-pex"
EXPERIMENTAL_WARNING = (
    "Experimental PEX writeback capability was used; independent in-game "
    "validation is still required."
)
DRY_RUN_WARNING = "Dry run completed; no output PEX binary was generated."
_CAPABILITY_LINE = re.compile(r"(?mi)^-\s*capability_level:\s*(\S+)\s*$")
_GAME_LINE = re.compile(r"(?mi)^-\s*game_id:\s*(\S+)\s*$")
_PEX_CALL_OPCODES = {"CALLMETHOD", "CALLPARENT", "CALLSTATIC"}
_PEX_SEMANTIC_CLASSIFICATIONS = {"visible", "protected", "manual_review"}


def dotnet_path(root: Path, config: dict[str, object], leases: ExitStack) -> Path:
    """Resolve .NET while retaining any managed runtime lease."""

    resolution = leases.enter_context(
        leased_payload_path(
            root,
            config,
            "DotNetSdkPath",
            command="run PEX adapter",
            managed_only=adapter_uses_managed_binding(
                root,
                config,
                "PexStringToolPath",
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
    """Resolve the PEX adapter payload without workspace-local rebuilding."""

    resolution = leases.enter_context(
        leased_payload_path(
            root,
            config,
            "PexStringToolPath",
            command="run PEX adapter",
        )
    )
    if resolution.path is None:
        raise FileNotFoundError("managed PEX adapter binding is unavailable")
    return resolution.path


def _row_text(row: dict[str, object], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def validate_semantic_pex_jsonl(path: Path, *, expected_game_id: str) -> None:
    """Fail closed on stale or wording-authorized semantic PEX rows."""
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_number}: invalid PEX translation JSONL") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line_number}: PEX translation row must be an object")
        if row.get("schema_version") != 2 or row.get("game_id") != expected_game_id:
            raise ValueError(
                f"line {line_number}: semantic PEX rows require schema_version=2 "
                f"and game_id={expected_game_id}"
            )
        classification = _row_text(row, "classification", "Classification")
        if classification not in _PEX_SEMANTIC_CLASSIFICATIONS:
            raise ValueError(
                f"line {line_number}: missing or invalid Fallout 4 PEX semantic classification"
            )
        direct_literal = row.get("is_direct_literal", row.get("IsDirectLiteral"))
        if not isinstance(direct_literal, bool):
            raise ValueError(f"line {line_number}: is_direct_literal must be boolean")
        opcode = _row_text(row, "opcode", "Opcode")
        opcode_form = _row_text(row, "opcode_form", "OpcodeForm")
        if opcode_form != opcode:
            raise ValueError(f"line {line_number}: opcode_form does not match opcode")
        source = _row_text(row, "Source", "source", "original", "text")
        target = _row_text(row, "Result", "result", "Target", "target", "translation")
        requests_write = bool(source and target and source != target)
        if classification == "visible":
            if not direct_literal or opcode not in _PEX_CALL_OPCODES:
                raise ValueError(
                    f"line {line_number}: visible PEX authorization requires a direct supported call literal"
                )
            for field, aliases in {
                "callee": ("callee", "Callee"),
                "semantic_argument_role": (
                    "semantic_argument_role",
                    "SemanticArgumentRole",
                ),
                "visibility_basis": ("visibility_basis", "VisibilityBasis"),
            }.items():
                if not _row_text(row, *aliases):
                    raise ValueError(
                        f"line {line_number}: visible PEX authorization is missing {field}"
                    )
            semantic_index = row.get(
                "semantic_argument_index",
                row.get("SemanticArgumentIndex"),
            )
            if isinstance(semantic_index, bool) or not isinstance(semantic_index, int) or semantic_index < 0:
                raise ValueError(
                    f"line {line_number}: visible PEX authorization has invalid semantic_argument_index"
                )
        elif requests_write:
            raise ValueError(
                f"line {line_number}: {classification} Fallout 4 PEX rows are not writable"
            )


def validate_apply_output_path(root: Path, value: str) -> Path:
    output_pex = resolve_project_path(root, value, must_exist=False)
    require_under(
        output_pex,
        [root / "out", root / "translated" / "tool_outputs"],
        "OutputPexPath",
    )
    if output_pex.suffix.lower() != ".pex":
        raise ValueError("OutputPexPath must be .pex.")
    return output_pex


def _read_apply_evidence(
    root: Path,
    raw_path: str,
    *,
    output_pex: Path,
    input_pex: Path,
    translation_jsonl: Path,
    mod_name: str,
    game_id: str,
    adapter_id: str,
) -> str:
    receipt_path = resolve_project_path(root, raw_path, must_exist=True)
    require_under(receipt_path, [root / "qa", root / "out"], "ApplyAdapterResultPath")
    result = read_adapter_result(receipt_path)
    if result.status != "success" or result.operation != "apply":
        raise ValueError("Verify requires a successful Apply adapter result.")
    if result.adapter_id != adapter_id:
        raise ValueError("Apply adapter result adapter_id does not match the PEX adapter.")

    expected_path = relative_path(root, output_pex).replace("\\", "/")
    expected_hash = sha256_file(output_pex)
    artifacts = result.artifacts
    if not any(
        item.path == expected_path and item.sha256 == expected_hash
        for item in artifacts
    ):
        raise ValueError("Apply adapter result does not bind the verified output PEX hash.")

    if result.mod_name:
        if result.mod_name != mod_name:
            raise ValueError("Apply adapter result Mod lane does not match Verify inputs.")
        expected_inputs = {
            relative_path(root, input_pex).replace("\\", "/"): sha256_file(input_pex),
            relative_path(root, translation_jsonl).replace("\\", "/"): sha256_file(
                translation_jsonl
            ),
        }
        actual_inputs = {item.path: item.sha256 for item in result.inputs}
        if actual_inputs != expected_inputs:
            raise ValueError("Apply adapter result input lineage does not match Verify inputs.")

    evidence_files = result.evidence_files
    if not evidence_files:
        raise ValueError("Apply adapter result has no capability evidence file.")
    levels: set[str] = set()
    games: set[str] = set()
    for value in evidence_files:
        evidence = resolve_project_path(root, value, must_exist=True)
        require_under(evidence, [root / "qa", root / "out"], "ApplyEvidencePath")
        evidence_hash = next(
            (
                item.sha256
                for item in artifacts
                if item.path == value
            ),
            None,
        )
        if evidence_hash != sha256_file(evidence):
            raise ValueError("Apply evidence hash does not match the adapter result.")
        text = evidence.read_text(encoding="utf-8-sig")
        levels.update(match.group(1) for match in _CAPABILITY_LINE.finditer(text))
        games.update(match.group(1) for match in _GAME_LINE.finditer(text))
    if games != {game_id}:
        raise ValueError("Apply capability evidence game_id does not match the workspace.")
    if len(levels) != 1:
        raise ValueError("Apply capability evidence must contain one capability_level.")
    level = next(iter(levels))
    if level not in {"experimental_write", "stable"}:
        raise ValueError("Apply capability evidence is not write-capable.")
    warnings = result.warnings
    if level == "experimental_write" and (
        not any(item.strip() for item in warnings)
    ):
        raise ValueError("Experimental Apply adapter result must include a warning.")
    return level


def build_command(
    root: Path,
    dotnet: Path,
    adapter_dll: Path,
    args: argparse.Namespace,
    context: GameContext,
    *,
    pex_category: str,
    capability_level: str,
    visible_api_registry: Path | None,
) -> tuple[list[str], Path, Path | None, Path | None]:
    input_pex = resolve_project_path(root, args.input_pex_path, must_exist=True)
    report = resolve_project_path(root, args.report_path, must_exist=False)
    if input_pex.suffix.lower() != ".pex":
        raise ValueError("InputPexPath must be .pex.")
    require_under(report, [root / "qa", root / "out"], "ReportPath")
    if report.suffix.lower() != ".md":
        raise ValueError("ReportPath must be a Markdown (.md) file.")

    command = [
        str(dotnet),
        str(adapter_dll),
        args.mode.lower(),
        "--game",
        context.game_id,
        "--pex-category",
        pex_category,
        "--capability-level",
        capability_level,
        "--project-root",
        str(root),
        "--input-pex",
        str(input_pex),
        "--report",
        str(report),
    ]
    if visible_api_registry is not None:
        command.extend(["--visible-api-registry", str(visible_api_registry)])
    output_jsonl: Path | None = None
    output_pex: Path | None = None
    if args.mode == "Export":
        require_under(
            input_pex,
            [
                root / "work" / "extracted_mods",
                root / "out",
                root / "translated" / "tool_outputs",
            ],
            "InputPexPath for Export",
        )
        output_jsonl = resolve_project_path(root, args.output_jsonl_path, must_exist=False)
        require_under(
            output_jsonl,
            [root / "source" / "pex_exports", root / "work" / "normalized"],
            "OutputJsonlPath",
        )
        if output_jsonl.suffix.lower() != ".jsonl":
            raise ValueError("OutputJsonlPath must be .jsonl.")
        command.extend(["--output-jsonl", str(output_jsonl)])
    else:
        require_under(input_pex, [root / "work" / "extracted_mods"], f"InputPexPath for {args.mode}")
        translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
        output_pex = validate_apply_output_path(root, args.output_pex_path)
        require_under(
            translation_jsonl,
            [root / "translated", root / "work" / "normalized"],
            "TranslationJsonlPath",
        )
        if translation_jsonl.suffix.lower() != ".jsonl":
            raise ValueError("TranslationJsonlPath must be .jsonl.")
        if args.mode == "Verify" and not output_pex.is_file():
            raise ValueError(f"OutputPexPath for Verify must exist: {args.output_pex_path}")
        command.extend(
            [
                "--translation-jsonl",
                str(translation_jsonl),
                "--output-pex",
                str(output_pex),
            ]
        )
        if args.mode == "Apply" and args.dry_run:
            command.append("--dry-run")
        if (
            args.mode == "Apply"
            and capability_level == "experimental_write"
            and args.allow_experimental_writeback
        ):
            command.append("--allow-experimental-writeback")
    return command, input_pex, output_jsonl, output_pex


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the project-local Mutagen PEX visible string adapter."
    )
    parser.add_argument("--mode", choices=("Export", "Apply", "Verify"), required=True)
    parser.add_argument("--input-pex-path", required=True)
    parser.add_argument("--translation-jsonl-path", default="")
    parser.add_argument("--output-pex-path", default="")
    parser.add_argument("--output-jsonl-path", default="")
    parser.add_argument("--report-path", default="qa/mutagen_pex_string_tool_report.md")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    parser.add_argument("--adapter-result-path", default="")
    parser.add_argument("--apply-adapter-result-path", default="")
    parser.add_argument("--allow-experimental-writeback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_root()
    result_path = prepare_adapter_result_path(root, args.adapter_result_path)
    adapter_operation = {
        "Export": "extract",
        "Apply": "apply",
        "Verify": "verify",
    }[args.mode]
    adapter_id = ADAPTER_ID_FALLBACK
    report: Path | None = None
    output_jsonl: Path | None = None
    output_pex: Path | None = None
    generated_artifacts: list[Path] = []
    adapter_invoked = False
    leases = ExitStack()

    try:
        if args.mode == "Export" and not args.output_jsonl_path:
            raise ValueError("--output-jsonl-path is required for Export.")
        if args.mode in {"Apply", "Verify"} and (
            not args.translation_jsonl_path or not args.output_pex_path
        ):
            raise ValueError(
                f"--translation-jsonl-path and --output-pex-path are required for {args.mode}."
            )

        context = resolve_game_context(root, args.game)
        capability_operation = "write" if args.mode == "Apply" else "read"
        decision = resolve_capability(context, "pex", capability_operation)
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
                ),
            )
            return 2

        pex_category = str(decision.adapter_options.get("pex_category") or "").strip()
        if not pex_category:
            raise ValueError("pex adapter option 'pex_category' must be non-empty")
        registry_option = str(
            decision.adapter_options.get("visible_api_registry") or ""
        ).strip()
        visible_api_registry: Path | None = None
        if registry_option:
            registry_root = plugin_root() / "config" / "pex_visible_apis"
            visible_api_registry = resolve_project_path(
                plugin_root(),
                registry_option,
                must_exist=True,
            )
            require_under(
                visible_api_registry,
                [registry_root],
                "PexVisibleApiRegistry",
            )
            if visible_api_registry.suffix.lower() != ".json":
                raise ValueError("PexVisibleApiRegistry must be a JSON file")
        capability_level = decision.level

        if args.mode == "Apply" and decision.level == "experimental_write" and not args.allow_experimental_writeback:
            message = (
                f"PEX writeback capability for '{context.game_id}' is experimental; "
                "pass --allow-experimental-writeback for an explicit project-local attempt."
            )
            if result_path is None:
                raise ValueError(message)
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

        if args.mode == "Verify" and args.apply_adapter_result_path:
            verify_input = resolve_project_path(root, args.input_pex_path, must_exist=True)
            verify_mod_name = mod_lane_for_workspace_input(root, verify_input)
            verify_translation = resolve_project_path(
                root,
                args.translation_jsonl_path,
                must_exist=True,
            )
            require_translation_input_lane(root, verify_translation, verify_mod_name)
            candidate_output = validate_apply_output_path(root, args.output_pex_path)
            require_under(
                candidate_output,
                [
                    root / "out" / verify_mod_name,
                    root / "translated" / "tool_outputs" / verify_mod_name,
                ],
                "OutputPexPath",
            )
            if not candidate_output.is_file():
                raise FileNotFoundError(f"OutputPexPath for Verify must exist: {candidate_output}")
            capability_level = _read_apply_evidence(
                root,
                args.apply_adapter_result_path,
                output_pex=candidate_output,
                input_pex=verify_input,
                translation_jsonl=verify_translation,
                mod_name=verify_mod_name,
                game_id=context.game_id,
                adapter_id=adapter_id,
            )
            write_decision = resolve_capability(context, "pex", "write")
            if (
                not write_decision.supported
                or write_decision.adapter_id != adapter_id
                or write_decision.level != capability_level
            ):
                raise ValueError(
                    "Apply capability evidence does not match the current PEX write capability."
                )
        elif args.mode == "Verify":
            raise ValueError(
                "PEX Verify requires --apply-adapter-result-path capability evidence."
            )

        config = resolve_project_path(root, args.config_path, must_exist=True)
        tool_config = load_workspace_tool_config(root, config)
        adapter_dll = ensure_adapter_dll(root, tool_config, leases)
        resolved_dotnet = dotnet_path(root, tool_config, leases)
        command, input_pex, output_jsonl, output_pex = build_command(
            root,
            resolved_dotnet,
            adapter_dll,
            args,
            context,
            pex_category=pex_category,
            capability_level=capability_level,
            visible_api_registry=visible_api_registry,
        )
        report = resolve_project_path(root, args.report_path, must_exist=False)
        mod_name = ""
        translation_jsonl: Path | None = None
        if args.mode == "Export":
            mod_name = mod_lane_for_adapter_input(root, input_pex)
            if output_jsonl is None:
                raise ValueError("PEX Export did not resolve an output path")
            require_under(
                output_jsonl,
                [
                    root / "source" / "pex_exports" / mod_name,
                    root / "work" / "normalized" / mod_name,
                ],
                "OutputJsonlPath",
            )
        elif args.mode in {"Apply", "Verify"}:
            mod_name = mod_lane_for_workspace_input(root, input_pex)
            translation_jsonl = resolve_project_path(
                root,
                args.translation_jsonl_path,
                must_exist=True,
            )
            require_translation_input_lane(root, translation_jsonl, mod_name)
            if visible_api_registry is not None:
                validate_semantic_pex_jsonl(
                    translation_jsonl,
                    expected_game_id=context.game_id,
                )
            if output_pex is None:
                raise ValueError("PEX write operation did not resolve an output path")
            output_roots = (
                [
                    root / "out" / mod_name / "tool_outputs",
                    root / "translated" / "tool_outputs" / mod_name,
                ]
                if args.mode == "Apply"
                else [root / "out" / mod_name, root / "translated" / "tool_outputs" / mod_name]
            )
            require_under(output_pex, output_roots, "OutputPexPath")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.unlink(missing_ok=True)
        if args.mode == "Export" and output_jsonl is not None:
            generated_artifacts.append(output_jsonl)
        elif args.mode == "Apply" and output_pex is not None:
            generated_artifacts.append(output_pex)
        for path in generated_artifacts:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)

        input_hash = sha256_file(input_pex)
        adapter_invoked = True
        return_code = subprocess.run(command, cwd=str(root), check=False).returncode
        if sha256_file(input_pex) != input_hash:
            raise RuntimeError("Input PEX changed during controlled adapter invocation.")
        success = return_code == 0 and report.is_file()
        if args.mode == "Export":
            success = success and output_jsonl is not None and output_jsonl.is_file()
            if success and visible_api_registry is not None and output_jsonl is not None:
                validate_semantic_pex_jsonl(
                    output_jsonl,
                    expected_game_id=context.game_id,
                )
        if args.mode == "Apply":
            success = success and (
                args.dry_run or (output_pex is not None and output_pex.is_file())
            )
        if not success:
            raise RuntimeError(f"PEX adapter failed with exit code {return_code}")

        artifacts: tuple[Path, ...]
        if args.mode == "Export":
            artifacts = (output_jsonl,) if output_jsonl is not None else ()
        elif args.mode == "Apply" and not args.dry_run:
            artifacts = (output_pex,) if output_pex is not None else ()
        elif args.mode == "Apply":
            artifacts = (report,)
        else:
            artifacts = (output_pex,) if output_pex is not None else ()
        warnings: list[str] = []
        if capability_level == "experimental_write":
            warnings.append(EXPERIMENTAL_WARNING)
        if args.dry_run:
            warnings.append(DRY_RUN_WARNING)
        receipt_artifacts = artifacts
        if args.mode == "Apply" and report not in receipt_artifacts:
            receipt_artifacts = (*receipt_artifacts, report)
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="success",
                error_code=None,
                operation=adapter_operation,
                adapter_id=adapter_id,
                artifact_paths=receipt_artifacts,
                evidence_paths=(report,),
                warnings=tuple(warnings),
                mod_name=mod_name if args.mode == "Apply" else "",
                input_paths=(input_pex, translation_jsonl)
                if args.mode == "Apply" and translation_jsonl is not None
                else (),
            ),
        )
        return 0
    except Exception as exc:
        blocker = str(exc)
        for path in generated_artifacts:
            path.unlink(missing_ok=True)
        evidence = (report,) if report is not None and report.is_file() else ()
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="error",
                error_code="adapter_failed" if adapter_invoked else "adapter_preflight_failed",
                operation=adapter_operation,
                adapter_id=adapter_id,
                evidence_paths=evidence,
                blockers=(blocker,),
            ),
        )
        if result_path is None:
            raise
        print(f"Mutagen PEX string tool failed: {exc}", file=sys.stderr)
        return 1
    finally:
        leases.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Mutagen PEX string tool failed: {exc}", file=sys.stderr)
        sys.exit(1)

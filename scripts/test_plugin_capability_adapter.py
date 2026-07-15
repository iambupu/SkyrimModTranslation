from __future__ import annotations

import ast
import hashlib
import importlib
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import invoke_mutagen_plugin_text_tool as invoke_tool  # noqa: E402
import dotnet_adapter_cache  # noqa: E402
import export_esp_strings as esp_exporter  # noqa: E402
import run_plugin_translation_stage as plugin_stage  # noqa: E402
from capability_resolver import resolve_capability  # noqa: E402
from game_context import load_game_profile  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dotnet_adapter_cache_serializes_shared_build(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source_root = tmp_path / "source"
    adapter_name = "FixtureAdapter"
    project = source_root / "adapters" / adapter_name / f"{adapter_name}.csproj"
    project.parent.mkdir(parents=True)
    project.write_text("<Project />", encoding="utf-8")
    dotnet = root / "tools" / "dotnet-sdk" / "dotnet.exe"
    dotnet.parent.mkdir(parents=True)
    dotnet.write_bytes(b"")
    active = 0
    max_active = 0
    builds = 0
    guard = threading.Lock()

    def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal active, max_active, builds
        with guard:
            active += 1
            builds += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        output = root / "tools" / "dotnet-adapters" / adapter_name / f"{adapter_name}.dll"
        output.write_bytes(b"fixture-dll")
        with guard:
            active -= 1
        return subprocess.CompletedProcess([], 0, stdout="")

    with mock.patch.object(dotnet_adapter_cache.subprocess, "run", side_effect=fake_run):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _index: dotnet_adapter_cache.ensure_adapter_dll(
                        root,
                        source_root,
                        dotnet,
                        adapter_name,
                    ),
                    range(2),
                )
            )

    assert results[0] == results[1]
    assert builds == 1
    assert max_active == 1


def _prepare_workspace(tmp_path: Path, game_id: str) -> SimpleNamespace:
    for relative in (
        "work/extracted_mods/TestMod",
        "translated/plugin_exports/TestMod",
        "out/TestMod/tool_outputs",
        "qa",
        "config",
        "tools/dotnet-sdk",
        "tools/cache",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    (tmp_path / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "plugin_name": "skyrim-mod-chs-translation",
                "game_id": game_id,
                "game_profile": game_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    input_plugin = tmp_path / "work/extracted_mods/TestMod/Test.esp"
    translation = tmp_path / "translated/plugin_exports/TestMod/Test.zh.jsonl"
    output = tmp_path / "out/TestMod/tool_outputs/Test.esp"
    report = tmp_path / "qa/Test.write.md"
    receipt = tmp_path / "qa/Test.adapter_result.json"
    config = tmp_path / "config/tools.local.json"
    dotnet = tmp_path / "tools/dotnet-sdk/dotnet.exe"
    dll = tmp_path / "tools/cache/SkyrimPluginTextTool.dll"
    input_plugin.write_bytes(b"TES4-input-fixture")
    translation.write_text("{}\n", encoding="utf-8")
    config.write_text("{}\n", encoding="utf-8")
    dotnet.write_bytes(b"")
    dll.write_bytes(b"")
    return SimpleNamespace(
        input=input_plugin,
        translation=translation,
        output=output,
        report=report,
        receipt=receipt,
        config=config,
        dotnet=dotnet,
        dll=dll,
    )


def _invoke(
    tmp_path: Path,
    game_id: str,
    *,
    return_code: int = 0,
    capability_decision: object | None = None,
    dry_run: bool = False,
    include_receipt: bool = True,
) -> tuple[int, list[str], SimpleNamespace]:
    paths = _prepare_workspace(tmp_path, game_id)
    argv = [
        "invoke_mutagen_plugin_text_tool.py",
        "--input-plugin-path",
        str(paths.input),
        "--translation-jsonl-path",
        str(paths.translation),
        "--output-plugin-path",
        str(paths.output),
        "--report-path",
        str(paths.report),
    ]
    if include_receipt:
        argv.extend(("--adapter-result-path", str(paths.receipt)))
    if dry_run:
        argv.append("--dry-run")

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if not dry_run:
            paths.output.write_bytes(b"partial" if return_code else b"translated-plugin")
        paths.report.write_text("# Adapter report\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, return_code)

    patches = [
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(invoke_tool, "project_root", return_value=tmp_path),
        mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
        mock.patch.object(invoke_tool, "dotnet_path", return_value=paths.dotnet),
        mock.patch.object(invoke_tool, "ensure_adapter_dll", return_value=paths.dll),
        mock.patch.object(invoke_tool.subprocess, "run", side_effect=fake_run),
    ]
    if capability_decision is not None:
        patches.append(mock.patch.object(invoke_tool, "resolve_capability", return_value=capability_decision))

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as run:
        if len(patches) == 7:
            with patches[6]:
                code = invoke_tool.main()
        else:
            code = invoke_tool.main()
    command = list(run.call_args.args[0]) if run.called else []
    return code, command, paths


def test_profiles_share_plugin_adapter_and_supply_distinct_mutagen_releases() -> None:
    skyrim = resolve_capability(load_game_profile("skyrim-se"), "plugin_text", "write")
    fallout = resolve_capability(load_game_profile("fallout4"), "plugin_text", "write")

    assert skyrim.adapter_id == fallout.adapter_id == "mutagen-bethesda-plugin"
    assert skyrim.adapter_options["mutagen_release"] == "SkyrimSE"
    assert fallout.adapter_options["mutagen_release"] == "Fallout4"
    assert skyrim.level == "stable"
    assert fallout.level == "experimental_write"


def test_plugin_stage_resolves_read_and_write_through_capability_registry() -> None:
    read, write = plugin_stage.resolve_plugin_text_access(load_game_profile("fallout4"))

    assert read.supported is True
    assert write.supported is True
    assert read.adapter_id == write.adapter_id == "mutagen-bethesda-plugin"
    assert write.level == "experimental_write"


def test_plugin_stage_uses_registry_entrypoints_for_runtime_dispatch() -> None:
    read, write, extract_entrypoint, apply_entrypoint, verify_entrypoint = (
        plugin_stage.resolve_plugin_text_entrypoints(load_game_profile("fallout4"))
    )

    assert read.adapter_id == write.adapter_id == "mutagen-bethesda-plugin"
    assert extract_entrypoint == "export_esp_strings.py"
    assert apply_entrypoint == "invoke_mutagen_plugin_text_tool.py"
    assert verify_entrypoint == "invoke_mutagen_plugin_text_tool.py"


def test_export_fails_closed_when_plugin_text_read_is_unsupported(tmp_path: Path) -> None:
    paths = _prepare_workspace(tmp_path, "skyrim-se")
    output = tmp_path / "source/plugin_exports/TestMod/Test.jsonl"
    output.parent.mkdir(parents=True)
    paths.input.write_bytes(b"TES4" + (b"\x00" * 20))
    decision = SimpleNamespace(
        supported=False,
        adapter_id="mutagen-bethesda-plugin",
        adapter_options={},
        level="unsupported",
        error_code="capability_unsupported",
        reason="read is unsupported",
    )
    argv = [
        "export_esp_strings.py",
        "--project-root",
        str(tmp_path),
        "--plugin-path",
        str(paths.input),
        "--output-path",
        str(output),
        "--report-path",
        str(paths.report),
    ]
    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(esp_exporter, "has_risky_path_marker", return_value=False),
        mock.patch.object(esp_exporter, "resolve_capability", return_value=decision, create=True),
        mock.patch.object(esp_exporter, "require_adapter", create=True) as registry,
    ):
        code = esp_exporter.main()

    assert code == 2
    assert not output.exists()
    assert registry.call_count == 0
    assert "read is unsupported" in paths.report.read_text(encoding="utf-8")


def test_export_fails_closed_for_unknown_profile_dispatch_option(tmp_path: Path) -> None:
    paths = _prepare_workspace(tmp_path, "skyrim-se")
    output = tmp_path / "source/plugin_exports/TestMod/Test.jsonl"
    output.parent.mkdir(parents=True)
    paths.input.write_bytes(b"TES4" + (b"\x00" * 20))
    decision = SimpleNamespace(
        supported=True,
        adapter_id="mutagen-bethesda-plugin",
        adapter_options={
            "mutagen_release": "SkyrimSE",
            "extract_backend": "unknown-backend",
            "localized_plugin_policy": "allow",
        },
        level="stable",
        error_code=None,
        reason="supported",
    )
    argv = [
        "export_esp_strings.py",
        "--project-root",
        str(tmp_path),
        "--plugin-path",
        str(paths.input),
        "--output-path",
        str(output),
        "--report-path",
        str(paths.report),
    ]
    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(esp_exporter, "has_risky_path_marker", return_value=False),
        mock.patch.object(esp_exporter, "resolve_capability", return_value=decision),
    ):
        code = esp_exporter.main()

    assert code == 2
    assert not output.exists()
    assert "supported extract_backend" in paths.report.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("game_id", "release", "level", "experimental_warning"),
    (
        ("skyrim-se", "SkyrimSE", "stable", False),
        ("fallout4", "Fallout4", "experimental_write", True),
    ),
)
def test_apply_passes_profile_format_parameters_and_writes_standard_receipt(
    tmp_path: Path,
    game_id: str,
    release: str,
    level: str,
    experimental_warning: bool,
) -> None:
    code, command, paths = _invoke(tmp_path, game_id)

    assert code == 0
    assert command[command.index("--game") + 1] == game_id
    assert command[command.index("--mutagen-release") + 1] == release
    assert command[command.index("--capability-level") + 1] == level
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["status"] == "success"
    assert receipt["operation"] == "apply"
    assert receipt["adapter_id"] == "mutagen-bethesda-plugin"
    assert receipt["artifacts"] == [
        {
            "path": "out/TestMod/tool_outputs/Test.esp",
            "sha256": _sha256(paths.output),
        },
        {
            "path": "qa/Test.write.md",
            "sha256": _sha256(paths.report),
        },
    ]
    assert receipt["evidence_files"] == ["qa/Test.write.md"]
    assert any("experimental" in item.lower() for item in receipt["warnings"]) is experimental_warning
    assert paths.receipt.read_bytes().endswith(b"\n")


def test_apply_failure_cleans_partial_output_and_writes_stable_error_receipt(tmp_path: Path) -> None:
    code, _command, paths = _invoke(tmp_path, "fallout4", return_code=2)

    assert code == 2
    assert not paths.output.exists()
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["status"] == "error"
    assert receipt["error_code"] == "adapter_failed"
    assert receipt["artifacts"] == []
    assert receipt["evidence_files"] == ["qa/Test.write.md"]


def test_apply_dry_run_writes_success_receipt_without_plugin_output(tmp_path: Path) -> None:
    code, command, paths = _invoke(tmp_path, "skyrim-se", dry_run=True)

    assert code == 0
    assert "--dry-run" in command
    assert not paths.output.exists()
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["status"] == "success"
    assert receipt["artifacts"] == [
        {"path": "qa/Test.write.md", "sha256": _sha256(paths.report)}
    ]
    assert receipt["evidence_files"] == ["qa/Test.write.md"]
    assert any("dry run" in warning.lower() for warning in receipt["warnings"])


def test_adapter_result_path_is_optional_and_omission_writes_no_json(tmp_path: Path) -> None:
    with (
        mock.patch.object(invoke_tool, "build_result") as build_result,
        mock.patch("adapter_result_io.write_adapter_result") as write_result,
    ):
        code, _command, paths = _invoke(tmp_path, "skyrim-se", include_receipt=False)

    assert code == 0
    assert not paths.receipt.exists()
    assert not paths.report.with_suffix(".adapter_result.json").exists()
    build_result.assert_not_called()
    write_result.assert_not_called()


def test_explicit_receipt_replaces_stale_success_when_verify_output_is_missing(
    tmp_path: Path,
) -> None:
    paths = _prepare_workspace(tmp_path, "skyrim-se")
    paths.receipt.write_text('{"status":"success"}\n', encoding="utf-8")
    argv = [
        "invoke_mutagen_plugin_text_tool.py",
        "--mode",
        "Verify",
        "--input-plugin-path",
        str(paths.input),
        "--translation-jsonl-path",
        str(paths.translation),
        "--output-plugin-path",
        str(paths.output),
        "--report-path",
        str(paths.report),
        "--adapter-result-path",
        str(paths.receipt),
    ]

    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(invoke_tool, "project_root", return_value=tmp_path),
    ):
        code = invoke_tool.main()

    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert code != 0
    assert receipt["status"] == "error"
    assert receipt["error_code"] == "adapter_preflight_failed"
    assert receipt["operation"] == "verify"


def test_explicit_receipt_replaces_stale_success_on_marker_game_conflict(
    tmp_path: Path,
) -> None:
    paths = _prepare_workspace(tmp_path, "fallout4")
    paths.receipt.write_text('{"status":"success"}\n', encoding="utf-8")
    argv = [
        "invoke_mutagen_plugin_text_tool.py",
        "--input-plugin-path",
        str(paths.input),
        "--translation-jsonl-path",
        str(paths.translation),
        "--output-plugin-path",
        str(paths.output),
        "--report-path",
        str(paths.report),
        "--adapter-result-path",
        str(paths.receipt),
        "--game",
        "skyrim-se",
    ]

    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(invoke_tool, "project_root", return_value=tmp_path),
    ):
        code = invoke_tool.main()

    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert code != 0
    assert receipt["status"] == "error"
    assert receipt["error_code"] == "adapter_preflight_failed"
    assert "conflict" in " ".join(receipt["blockers"]).lower()


def test_stage_commands_route_adapter_receipt_only_to_write(tmp_path: Path) -> None:
    plugin = tmp_path / "work/extracted_mods/TestMod/Test.esp"
    export = tmp_path / "source/plugin_exports/TestMod/Test.jsonl"
    export_report = tmp_path / "qa/Test.export.md"
    translation = tmp_path / "translated/plugin_exports/TestMod/Test.zh.jsonl"
    output = tmp_path / "out/TestMod/tool_outputs/Test.esp"
    write_report = tmp_path / "qa/Test.write.md"
    receipt = tmp_path / "qa/Test.adapter_result.json"

    export_args = plugin_stage.build_export_command_args(
        plugin=plugin,
        mod_name="TestMod",
        output_path=export,
        report_path=export_report,
        game_id="skyrim-se",
    )
    write_args = plugin_stage.build_write_command_args(
        input_plugin=plugin,
        translation_jsonl=translation,
        output_plugin=output,
        report_path=write_report,
        adapter_result_path=receipt,
        game_id="skyrim-se",
    )

    assert "--adapter-result-path" not in export_args
    assert write_args[write_args.index("--adapter-result-path") + 1] == str(receipt)


def test_write_capability_failure_closes_before_adapter_invocation(tmp_path: Path) -> None:
    decision = SimpleNamespace(
        supported=False,
        adapter_id="mutagen-bethesda-plugin",
        adapter_options={},
        level="read_only",
        error_code="capability_unsupported",
        reason="write is unsupported",
    )

    code, command, paths = _invoke(tmp_path, "skyrim-se", capability_decision=decision)

    assert code != 0
    assert command == []
    assert not paths.output.exists()
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked"
    assert receipt["error_code"] == "capability_unsupported"
    assert receipt["blockers"] == ["write is unsupported"]


def test_adapter_result_io_is_deterministic_and_uses_workspace_relative_paths(tmp_path: Path) -> None:
    result_io = importlib.import_module("adapter_result_io")
    artifact_path = tmp_path / "out/TestMod/tool_outputs/Test.esp"
    evidence_path = tmp_path / "qa/Test.report.md"
    receipt_path = tmp_path / "qa/Test.adapter_result.json"
    artifact_path.parent.mkdir(parents=True)
    evidence_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"artifact")
    evidence_path.write_text("evidence\n", encoding="utf-8")
    result = result_io.build_result(
        root=tmp_path,
        status="success",
        error_code=None,
        operation="apply",
        adapter_id="mutagen-bethesda-plugin",
        artifact_paths=(artifact_path,),
        evidence_paths=(evidence_path,),
    )

    result_io.write_adapter_result(receipt_path, result)
    first = receipt_path.read_bytes()
    result_io.write_adapter_result(receipt_path, result)

    assert receipt_path.read_bytes() == first
    payload = json.loads(first)
    assert payload["artifacts"][0]["path"] == "out/TestMod/tool_outputs/Test.esp"
    assert payload["artifacts"][0]["sha256"] == _sha256(artifact_path)
    assert payload["evidence_files"] == ["qa/Test.report.md"]


def test_adapter_result_path_contract_is_shared_and_fail_closed(tmp_path: Path) -> None:
    result_io = importlib.import_module("adapter_result_io")

    assert result_io.prepare_adapter_result_path(tmp_path, "") is None
    for relative in ("qa/Test.adapter_result.json", "out/Test/adapter_result.json"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"stale":true}\n', encoding="utf-8")
        resolved = result_io.prepare_adapter_result_path(tmp_path, relative)
        assert resolved == target.resolve()
        assert not target.exists()

    invalid_paths = (
        "mod/Test.adapter_result.json",
        "work/Test.adapter_result.json",
        "qa/Test.adapter_result.txt",
        str(tmp_path.parent / "outside.adapter_result.json"),
    )
    for value in invalid_paths:
        with pytest.raises(ValueError):
            result_io.prepare_adapter_result_path(tmp_path, value)


def test_adapter_wrappers_do_not_redefine_shared_result_contract() -> None:
    wrapper_names = (
        "invoke_ba2_extractor_safe.py",
        "invoke_bsa_file_extractor_safe.py",
        "invoke_mutagen_pex_string_tool.py",
        "invoke_mutagen_plugin_text_tool.py",
    )
    forbidden = {"prepare_result_path", "write_result_if_requested"}
    for wrapper_name in wrapper_names:
        path = SCRIPTS / wrapper_name
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert defined.isdisjoint(forbidden), wrapper_name

    ba2_manifest = SCRIPTS / "new_ba2_archive_manifest.py"
    tree = ast.parse(ba2_manifest.read_text(encoding="utf-8-sig"), filename=str(ba2_manifest))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_is_reparse_point" not in defined


def test_stage_report_uses_plugin_capability_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "work/extracted_mods/TestMod"
    report = tmp_path / "qa/TestMod.plugin_translation_stage.md"
    payload = tmp_path / "qa/TestMod.plugin_translation_stage.json"
    workspace.mkdir(parents=True)

    plugin_stage.write_reports(
        tmp_path,
        "TestMod",
        workspace,
        report,
        payload,
        [],
        [],
        load_game_profile("fallout4"),
    )

    report_text = report.read_text(encoding="utf-8")
    report_json = json.loads(payload.read_text(encoding="utf-8"))
    assert "plugin_adapter: mutagen-bethesda-plugin" in report_text
    assert "plugin_text_capability_level: experimental_write" in report_text
    assert report_json["plugin_adapter"] == "mutagen-bethesda-plugin"
    assert report_json["plugin_text_capability_level"] == "experimental_write"

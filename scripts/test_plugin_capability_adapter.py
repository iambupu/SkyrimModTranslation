from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import subprocess
import sys
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
import plugin_resource_evidence  # noqa: E402
import run_plugin_translation_stage as plugin_stage  # noqa: E402
import verify_plugin_output as plugin_output_verifier  # noqa: E402
from capability_resolver import resolve_capability, resolve_resource_capability  # noqa: E402
from game_context import load_game_profile  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_plugin_output_verifier_resolves_light_resource_capability(tmp_path: Path) -> None:
    plugin = tmp_path / "work" / "extracted_mods" / "TestMod" / "Test.esp"
    plugin.parent.mkdir(parents=True)
    header = bytearray(b"TES4" + (b"\x00" * 20))
    header[8:12] = (0x00000200).to_bytes(4, "little")
    plugin.write_bytes(header)
    context_path = (
        tmp_path
        / "work"
        / "plugin_context"
        / "TestMod"
        / "Test.esp.resolved-master-styles.json"
    )
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "game_id": "skyrim-se",
                "plugin": "Test.esp",
                "input_path": "work/extracted_mods/TestMod/Test.esp",
                "input_sha256": _sha256(plugin),
                "current_style": "light",
                "current_evidence_source": "fixture:small-header",
                "current_inspected_path": "work/extracted_mods/TestMod/Test.esp",
                "current_inspected_sha256": _sha256(plugin),
                "current_small_flag": True,
                "masters": [],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "qa" / "Test.apply.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: false",
                "- light_by_header: true",
                "- current_plugin_light: true",
                "- references_light_master: false",
                "- targets_light_owner: false",
                "- light_context: true",
                "- contains_unsupported_light_formids: false",
                "- Master-style context: "
                "work/plugin_context/TestMod/Test.esp.resolved-master-styles.json",
                f"- Master-style context SHA256: {_sha256(context_path)}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    decision = plugin_output_verifier.resolve_report_write_decision(
        load_game_profile("skyrim-se"),
        tmp_path,
        plugin,
        report,
    )

    assert decision.level == "experimental_write"
    assert decision.adapter_id == "mutagen-bethesda-plugin"


def test_full_master_style_context_does_not_mark_plugin_as_light(tmp_path: Path) -> None:
    plugin = tmp_path / "work" / "extracted_mods" / "TestMod" / "Test.esp"
    master = plugin.with_name("Master.esm")
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"TES4" + (b"\x00" * 20))
    master.write_bytes(b"TES4" + (b"\x00" * 20))
    context_path = (
        tmp_path
        / "work"
        / "plugin_context"
        / "TestMod"
        / "Test.esp.resolved-master-styles.json"
    )
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "game_id": "skyrim-se",
                "plugin": "Test.esp",
                "input_path": "work/extracted_mods/TestMod/Test.esp",
                "input_sha256": _sha256(plugin),
                "current_style": "full",
                "current_evidence_source": "workspace-header:Test.esp",
                "current_inspected_path": "work/extracted_mods/TestMod/Test.esp",
                "current_inspected_sha256": _sha256(plugin),
                "current_small_flag": False,
                "masters": [
                    {
                        "mod_key": "Master.esm",
                        "master_style": "full",
                        "evidence_source": "workspace-header:Master.esm",
                        "inspected_path": "work/extracted_mods/TestMod/Master.esm",
                        "inspected_sha256": _sha256(master),
                        "small_flag": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_context_sha256 = _sha256(context_path)
    report = tmp_path / "qa" / "Test.apply.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: false",
                "- light_by_header: false",
                "- current_plugin_light: false",
                "- references_light_master: false",
                "- targets_light_owner: false",
                "- light_context: false",
                "- contains_unsupported_light_formids: false",
                "- Master-style context: "
                "work/plugin_context/TestMod/Test.esp.resolved-master-styles.json",
                f"- Master-style context SHA256: {original_context_sha256}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    decision = plugin_output_verifier.resolve_report_write_decision(
        load_game_profile("skyrim-se"),
        tmp_path,
        plugin,
        report,
    )

    assert decision.level == "stable"
    assert decision.adapter_id == "mutagen-bethesda-plugin"

    evidence = plugin_resource_evidence.validate_plugin_master_style_context(
        report,
        project_root=tmp_path,
        expected_input=plugin,
        expected_game="skyrim-se",
    )
    manifest = plugin_resource_evidence.materialize_master_style_manifest(
        evidence,
        project_root=tmp_path,
        destination=(
            tmp_path
            / "work"
            / "plugin_context"
            / "TestMod"
            / "Test.esp.output-master-styles.json"
        ),
        expected_game="skyrim-se",
        expected_plugin="Test.esp",
    )

    assert manifest is not None
    assert json.loads(manifest.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "game_id": "skyrim-se",
        "plugin": "Test.esp",
        "masters": [
            {
                "mod_key": "Master.esm",
                "master_style": "full",
                "inspected_path": "work/extracted_mods/TestMod/Master.esm",
                "inspected_sha256": _sha256(master),
                "small_flag": False,
            }
        ],
    }


def test_light_target_without_master_style_context_is_rejected(tmp_path: Path) -> None:
    plugin = tmp_path / "work" / "extracted_mods" / "TestMod" / "Test.esp"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"TES4" + (b"\x00" * 20))
    report = tmp_path / "qa" / "Test.apply.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: false",
                "- light_by_header: false",
                "- current_plugin_light: false",
                "- references_light_master: true",
                "- targets_light_owner: true",
                "- light_context: true",
                "- contains_unsupported_light_formids: false",
                "- Master-style context: <none>",
                "- Master-style context SHA256: <none>",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required master-style context"):
        plugin_resource_evidence.validate_plugin_master_style_context(
            report,
            project_root=tmp_path,
            expected_input=plugin,
            expected_game="skyrim-se",
        )


def test_full_plugin_reference_to_light_master_keeps_own_target_stable() -> None:
    context = load_game_profile("skyrim-se")
    traits = plugin_resource_evidence.PluginReportTraits(
        localized=False,
        light_by_extension=False,
        light_by_header=False,
        current_plugin_light=False,
        references_light_master=True,
        targets_light_owner=False,
        light_context=False,
        contains_unsupported_light_formids=False,
    )
    resource = plugin_resource_evidence.plugin_resource_descriptor(
        context,
        Path("OrdinaryPatch.esp"),
        traits,
    )

    decision = resolve_resource_capability(context, resource, "write")

    assert "light" not in resource.traits
    assert decision.level == "stable"


def test_known_full_master_context_does_not_require_inspected_game_file(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "work" / "extracted_mods" / "TestMod" / "Test.esp"
    plugin.parent.mkdir(parents=True)
    master_payload = b"Fallout4.esm\0"
    header_data = b"MAST" + len(master_payload).to_bytes(2, "little") + master_payload
    plugin.write_bytes(
        b"TES4"
        + len(header_data).to_bytes(4, "little")
        + (0x00000200).to_bytes(4, "little")
        + (b"\0" * 12)
        + header_data
    )
    context_path = (
        tmp_path
        / "work"
        / "plugin_context"
        / "TestMod"
        / "Test.esp.resolved-master-styles.json"
    )
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "game_id": "fallout4",
                "plugin": "Test.esp",
                "input_path": "work/extracted_mods/TestMod/Test.esp",
                "input_sha256": _sha256(plugin),
                "current_style": "light",
                "current_evidence_source": "workspace-header:Test.esp",
                "current_inspected_path": "work/extracted_mods/TestMod/Test.esp",
                "current_inspected_sha256": _sha256(plugin),
                "current_small_flag": True,
                "masters": [
                    {
                        "mod_key": "Fallout4.esm",
                        "master_style": "full",
                        "evidence_source": "game-profile:known-full",
                        "inspected_path": None,
                        "inspected_sha256": None,
                        "small_flag": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "qa" / "Test.apply.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: false",
                "- light_by_header: true",
                "- current_plugin_light: true",
                "- references_light_master: false",
                "- targets_light_owner: false",
                "- light_context: true",
                "- contains_unsupported_light_formids: false",
                "- Master-style context: "
                "work/plugin_context/TestMod/Test.esp.resolved-master-styles.json",
                f"- Master-style context SHA256: {_sha256(context_path)}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    evidence = plugin_resource_evidence.validate_plugin_master_style_context(
        report,
        project_root=tmp_path,
        expected_input=plugin,
        expected_game="fallout4",
    )
    manifest = plugin_resource_evidence.materialize_master_style_manifest(
        evidence,
        project_root=tmp_path,
        destination=(
            tmp_path
            / "work"
            / "plugin_context"
            / "TestMod"
            / "Test.esp.output-master-styles.json"
        ),
        expected_game="fallout4",
        expected_plugin="Test.esp",
    )

    assert evidence.light_context is True
    assert manifest is None

    original_context_sha256 = _sha256(context_path)
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    payload["masters"][0]["mod_key"] = "CustomMaster.esm"
    context_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after validation"):
        plugin_resource_evidence.materialize_master_style_manifest(
            evidence,
            project_root=tmp_path,
            destination=(
                tmp_path
                / "work"
                / "plugin_context"
                / "TestMod"
                / "Test.esp.changed-master-styles.json"
            ),
            expected_game="fallout4",
            expected_plugin="Test.esp",
        )
    report.write_text(
        report.read_text(encoding="utf-8").replace(
            original_context_sha256,
            _sha256(context_path),
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing for CustomMaster.esm"):
        plugin_resource_evidence.validate_plugin_master_style_context(
            report,
            project_root=tmp_path,
            expected_input=plugin,
            expected_game="fallout4",
        )


def test_master_style_context_rejects_small_flag_header_conflict(tmp_path: Path) -> None:
    plugin = tmp_path / "work" / "extracted_mods" / "TestMod" / "Test.esp"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"TES4" + (b"\x00" * 20))
    context_path = (
        tmp_path
        / "work"
        / "plugin_context"
        / "TestMod"
        / "Test.esp.resolved-master-styles.json"
    )
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "game_id": "skyrim-se",
                "plugin": "Test.esp",
                "input_path": "work/extracted_mods/TestMod/Test.esp",
                "input_sha256": _sha256(plugin),
                "current_style": "full",
                "current_evidence_source": "workspace-header:Test.esp",
                "current_inspected_path": "work/extracted_mods/TestMod/Test.esp",
                "current_inspected_sha256": _sha256(plugin),
                "current_small_flag": True,
                "masters": [],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "qa" / "Test.apply.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "\n".join(
            [
                "- localized: false",
                "- light_by_extension: false",
                "- light_by_header: false",
                "- current_plugin_light: false",
                "- references_light_master: false",
                "- targets_light_owner: false",
                "- light_context: false",
                "- contains_unsupported_light_formids: false",
                "- Master-style context: "
                "work/plugin_context/TestMod/Test.esp.resolved-master-styles.json",
                f"- Master-style context SHA256: {_sha256(context_path)}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="small_flag conflicts"):
        plugin_resource_evidence.validate_plugin_master_style_context(
            report,
            project_root=tmp_path,
            expected_input=plugin,
            expected_game="skyrim-se",
        )


def test_dotnet_adapter_cache_hashes_project_declared_external_resource(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    project = source_root / "adapters" / "FixtureAdapter" / "FixtureAdapter.csproj"
    resource = source_root / "config" / "policy.json"
    project.parent.mkdir(parents=True)
    resource.parent.mkdir(parents=True)
    project.write_text(
        """<Project><ItemGroup><EmbeddedResource """
        'Include="../../config/policy.json" /></ItemGroup></Project>',
        encoding="utf-8",
    )
    resource.write_text('{"version": 1}\n', encoding="utf-8")
    initial = dotnet_adapter_cache.adapter_source_hash(
        project,
        source_root=source_root,
    )

    resource.write_text('{"version": 2}\n', encoding="utf-8")

    assert dotnet_adapter_cache.adapter_source_hash(
        project,
        source_root=source_root,
    ) != initial


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
    manifest = tmp_path / "work/plugin_context/TestMod/Test.esp.master-styles.json"
    config = tmp_path / "config/tools.local.json"
    dotnet = tmp_path / "tools/dotnet-sdk/dotnet.exe"
    dll = tmp_path / "tools/cache/SkyrimPluginTextTool.dll"
    input_plugin.write_bytes(b"TES4" + (b"\x00" * 20))
    translation.write_text('{"schema_version":2}\n', encoding="utf-8")
    config.write_text("{}\n", encoding="utf-8")
    dotnet.write_bytes(b"")
    dll.write_bytes(b"")
    return SimpleNamespace(
        input=input_plugin,
        translation=translation,
        output=output,
        report=report,
        receipt=receipt,
        manifest=manifest,
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
    master_style_context: bool = False,
    master_style_manifest: bool = False,
    report_error_code: str = "",
    hardlink_input: bool = False,
    translation_rows: list[dict[str, object]] | None = None,
    reported_target_state: bool | None = False,
) -> tuple[int, list[str], SimpleNamespace]:
    paths = _prepare_workspace(tmp_path, game_id)
    if translation_rows is not None:
        paths.translation.write_text(
            "".join(json.dumps(row) + "\n" for row in translation_rows),
            encoding="utf-8",
        )
    if hardlink_input:
        outside = tmp_path / "outside-plugin.esp"
        outside.write_bytes(paths.input.read_bytes())
        paths.input.unlink()
        os.link(outside, paths.input)
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
    if master_style_manifest:
        paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "game_id": game_id,
                    "plugin": "Test.esp",
                    "masters": [
                        {
                            "mod_key": "Master.esm",
                            "master_style": "full",
                            "inspected_path": "work/extracted_mods/TestMod/Master.esm",
                            "inspected_sha256": "0" * 64,
                            "small_flag": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        argv.extend(("--master-style-manifest", str(paths.manifest)))

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if not dry_run:
            paths.output.write_bytes(b"partial" if return_code else b"translated-plugin")
        context_path = (
            tmp_path
            / "work"
            / "plugin_context"
            / "TestMod"
            / "Test.esp.resolved-master-styles.json"
        )
        if master_style_context:
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "game_id": game_id,
                        "plugin": "Test.esp",
                        "input_path": "work/extracted_mods/TestMod/Test.esp",
                        "input_sha256": _sha256(paths.input),
                        "current_style": "full",
                        "current_evidence_source": "workspace-header:Test.esp",
                        "current_inspected_path": "work/extracted_mods/TestMod/Test.esp",
                        "current_inspected_sha256": _sha256(paths.input),
                        "current_small_flag": False,
                        "masters": [
                            {
                                "mod_key": "LightMaster.esl",
                                "master_style": "light",
                                "evidence_source": "extension:.esl",
                                "inspected_path": None,
                                "inspected_sha256": None,
                                "small_flag": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        context_value = (
            context_path.relative_to(tmp_path).as_posix()
            if master_style_context
            else "<none>"
        )
        context_hash = _sha256(context_path) if master_style_context else "<none>"
        paths.report.write_text(
            "# Adapter report\n\n"
            "- localized: false\n"
            "- light_by_extension: false\n"
            "- light_by_header: false\n"
            "- current_plugin_light: false\n"
            f"- references_light_master: {'true' if master_style_context else 'false'}\n"
            f"- targets_light_owner: {plugin_resource_evidence._format_trait(reported_target_state)}\n"
            f"- light_context: {plugin_resource_evidence._format_trait(reported_target_state)}\n"
            "- contains_unsupported_light_formids: false\n"
            f"- Master-style context: {context_value}\n"
            f"- Master-style context SHA256: {context_hash}\n"
            + (
                f"\n## Unsupported\n\n- {report_error_code}: fixture failure\n"
                if report_error_code
                else ""
            ),
            encoding="utf-8",
        )
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
        patches.append(
            mock.patch.object(
                invoke_tool,
                "resolve_resource_capability",
                return_value=capability_decision,
            )
        )

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


@pytest.mark.parametrize(
    ("rows", "expected"),
    (
        ([{"schema_version": 2, "source": "Name", "target": "名称"}], False),
        (
            [
                {
                    "schema_version": 2,
                    "risk": "candidate",
                    "source": "Name",
                    "target": "名称",
                    "owner_mod_key": "Dependency.esl",
                    "local_id": 0x800,
                    "master_style": "light",
                    "master_style_evidence": "extension:.esl",
                }
            ],
            True,
        ),
        (
            [
                {
                    "schema_version": 2,
                    "risk": "candidate",
                    "source": "Name",
                    "target": "名称",
                    "owner_mod_key": "Dependency.esp",
                    "local_id": 0x800,
                    "master_style": "unknown",
                    "master_style_evidence": "unresolved:unseparated-master-order",
                }
            ],
            None,
        ),
        ([{"schema_version": 2, "source": "Name", "target": ""}], False),
        ([{"schema_version": 2, "source": "Name", "target": "Name"}], False),
        (
            [
                {
                    "schema_version": 2,
                    "source": "Name",
                    "target": "名称",
                    "owner_mod_key": "LightMaster.esl",
                    "local_id": 0x800,
                    "master_style": "light",
                    "master_style_evidence": "extension:.esl",
                },
                {
                    "schema_version": 2,
                    "risk": "",
                    "source": "Name",
                    "target": "名称",
                    "owner_mod_key": "UnknownMaster.esp",
                    "local_id": 0x800,
                    "master_style": "unknown",
                    "master_style_evidence": "unresolved:unseparated-master-order",
                },
            ],
            False,
        ),
        (
            [
                {
                    "schema_version": 2,
                    "risk": "candidate",
                    "source": "Light name",
                    "target": "轻量名称",
                    "owner_mod_key": "Dependency.esl",
                    "local_id": 0x800,
                    "master_style": "light",
                    "master_style_evidence": "extension:.esl",
                },
                {
                    "schema_version": 2,
                    "risk": "candidate",
                    "source": "Unknown name",
                    "target": "未知名称",
                    "owner_mod_key": "UnknownMaster.esp",
                    "local_id": 0x801,
                    "master_style": "unknown",
                    "master_style_evidence": "unresolved:unseparated-master-order",
                },
            ],
            None,
        ),
        (
            [
                {
                    "schema_version": 2,
                    "risk": "candidate",
                    "source": "Unknown name",
                    "target": "未知名称",
                    "owner_mod_key": "UnknownMaster.esp",
                    "local_id": 0x801,
                    "master_style": "unknown",
                    "master_style_evidence": "unresolved:unseparated-master-order",
                },
                {
                    "schema_version": 2,
                    "risk": "candidate",
                    "source": "Light name",
                    "target": "轻量名称",
                    "owner_mod_key": "Dependency.esl",
                    "local_id": 0x800,
                    "master_style": "light",
                    "master_style_evidence": "extension:.esl",
                },
            ],
            None,
        ),
        (
            [
                {
                    "schema_version": 2,
                    "risk": "review",
                    "source": "Name",
                    "target": "名称",
                    "owner_mod_key": "LightMaster.esl",
                    "local_id": 0x800,
                    "master_style": "light",
                    "master_style_evidence": "extension:.esl",
                }
            ],
            False,
        ),
    ),
)
def test_translation_target_light_state_uses_only_changed_rows(
    tmp_path: Path,
    rows: list[dict[str, object]],
    expected: bool | None,
) -> None:
    translation = tmp_path / "translations.jsonl"
    translation.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    assert (
        plugin_resource_evidence.read_plugin_translation_target_light_state(
            translation
        )
        is expected
    )


def test_translation_target_manifest_owners_use_only_changed_candidate_rows(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "schema_version": 2,
            "risk": "candidate",
            "source": "Target name",
            "target": "目标名称",
            "owner_mod_key": "TargetMaster.esm",
            "local_id": 0x12345,
            "master_style": "full",
            "master_style_evidence": "manifest-header:work/master_context/TargetMaster.esm",
        },
        {
            "schema_version": 2,
            "risk": "candidate",
            "source": "Unchanged",
            "target": "Unchanged",
            "owner_mod_key": "UnchangedMaster.esm",
            "local_id": 0x23456,
            "master_style": "full",
            "master_style_evidence": "manifest-header:work/master_context/UnchangedMaster.esm",
        },
        {
            "schema_version": 2,
            "risk": "review",
            "source": "Review",
            "target": "复核",
            "owner_mod_key": "ReviewMaster.esm",
            "local_id": 0x34567,
            "master_style": "full",
            "master_style_evidence": "manifest-header:work/master_context/ReviewMaster.esm",
        },
        {
            "schema_version": 2,
            "risk": "candidate",
            "source": "Own record",
            "target": "自有记录",
            "owner_mod_key": "Patch.esp",
            "local_id": 0x45678,
            "master_style": "full",
            "master_style_evidence": "workspace-header:work/extracted_mods/Example/Patch.esp",
        },
    ]
    translation = tmp_path / "translations.jsonl"
    translation.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    assert plugin_resource_evidence.read_plugin_translation_target_manifest_owners(
        translation
    ) == ("TargetMaster.esm",)


def test_translation_target_context_rejects_unrecognized_master_evidence(
    tmp_path: Path,
) -> None:
    translation = tmp_path / "translations.jsonl"
    translation.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "risk": "candidate",
                "source": "Name",
                "target": "名称",
                "owner_mod_key": "CustomMaster.esm",
                "local_id": 0x12345,
                "master_style": "full",
                "master_style_evidence": "claimed-without-bound-evidence",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="master_style_evidence"):
        plugin_resource_evidence.read_plugin_translation_target_manifest_owners(
            translation
        )


def test_apply_uses_actual_light_target_for_prewrite_capability(tmp_path: Path) -> None:
    row = {
        "schema_version": 2,
        "risk": "candidate",
        "source": "Name",
        "target": "名称",
        "owner_mod_key": "LightMaster.esl",
        "local_id": 0x800,
        "master_style": "light",
        "master_style_evidence": "extension:.esl",
    }

    code, command, _paths = _invoke(
        tmp_path,
        "skyrim-se",
        master_style_context=True,
        translation_rows=[row],
        reported_target_state=True,
    )

    assert code == 0
    assert command[command.index("--capability-level") + 1] == "experimental_write"


def test_apply_blocks_unknown_actual_target_before_adapter_invocation(
    tmp_path: Path,
) -> None:
    row = {
        "schema_version": 2,
        "risk": "candidate",
        "source": "Name",
        "target": "名称",
        "owner_mod_key": "UnknownMaster.esp",
        "local_id": 0x800,
        "master_style": "unknown",
        "master_style_evidence": "unresolved:unseparated-master-order",
    }

    code, command, paths = _invoke(
        tmp_path,
        "skyrim-se",
        translation_rows=[row],
        reported_target_state=None,
    )

    assert code == 2
    assert command == []
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["error_code"] == "master_style_unknown"


def test_apply_does_not_downgrade_noop_light_row(tmp_path: Path) -> None:
    row = {
        "schema_version": 2,
        "risk": "candidate",
        "source": "Name",
        "target": "Name",
        "owner_mod_key": "LightMaster.esl",
        "local_id": 0x800,
        "master_style": "light",
        "master_style_evidence": "extension:.esl",
    }

    code, command, _paths = _invoke(
        tmp_path,
        "skyrim-se",
        translation_rows=[row],
    )

    assert code == 0
    assert command[command.index("--capability-level") + 1] == "stable"


def test_plugin_wrapper_rejects_hardlinked_input_before_adapter_invocation(tmp_path: Path) -> None:
    code, command, receipt_paths = _invoke(
        tmp_path,
        "skyrim-se",
        hardlink_input=True,
    )

    assert code == 1
    assert command == []
    payload = json.loads(receipt_paths.receipt.read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert "hardlink" in " ".join(payload["blockers"]).casefold()


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


def test_light_master_context_is_hash_bound_in_adapter_result(tmp_path: Path) -> None:
    code, _command, paths = _invoke(
        tmp_path,
        "fallout4",
        master_style_context=True,
    )

    assert code == 0
    context = (
        tmp_path
        / "work"
        / "plugin_context"
        / "TestMod"
        / "Test.esp.resolved-master-styles.json"
    )
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["artifacts"][-1] == {
        "path": "work/plugin_context/TestMod/Test.esp.resolved-master-styles.json",
        "sha256": _sha256(context),
    }
    assert receipt["evidence_files"] == [
        "qa/Test.write.md",
        "work/plugin_context/TestMod/Test.esp.resolved-master-styles.json",
    ]


def test_apply_forwards_master_style_manifest_and_hash_binds_it_as_input(
    tmp_path: Path,
) -> None:
    code, command, paths = _invoke(
        tmp_path,
        "fallout4",
        master_style_manifest=True,
    )

    assert code == 0
    assert command[command.index("--master-style-manifest") + 1] == str(paths.manifest)
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["inputs"][-1] == {
        "path": "work/plugin_context/TestMod/Test.esp.master-styles.json",
        "sha256": _sha256(paths.manifest),
    }


def test_apply_failure_cleans_partial_output_and_writes_stable_error_receipt(tmp_path: Path) -> None:
    code, _command, paths = _invoke(tmp_path, "fallout4", return_code=2)

    assert code == 2
    assert not paths.output.exists()
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["status"] == "error"
    assert receipt["error_code"] == "adapter_failed"
    assert receipt["artifacts"] == []
    assert receipt["evidence_files"] == ["qa/Test.write.md"]


@pytest.mark.parametrize(
    "error_code",
    (
        "master_style_unknown",
        "master_style_evidence_stale",
        "master_style_conflict",
    ),
)
def test_apply_failure_preserves_master_style_root_error_code(
    tmp_path: Path,
    error_code: str,
) -> None:
    code, _command, paths = _invoke(
        tmp_path,
        "fallout4",
        return_code=2,
        report_error_code=error_code,
    )

    assert code == 2
    receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
    assert receipt["error_code"] == error_code


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
    manifest = tmp_path / "work/plugin_context/TestMod/Test.esp.master-styles.json"

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
        master_style_manifest=manifest,
    )

    assert "--adapter-result-path" not in export_args
    assert write_args[write_args.index("--adapter-result-path") + 1] == str(receipt)
    assert write_args[write_args.index("--master-style-manifest") + 1] == str(manifest)


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

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PROJECT = ROOT / "adapters" / "SkyrimPluginTextTool" / "SkyrimPluginTextTool.csproj"
DLL = ROOT / "adapters" / "SkyrimPluginTextTool" / "bin" / "Debug" / "net8.0" / "SkyrimPluginTextTool.dll"
sys.path.insert(0, str(SCRIPTS))

import invoke_mutagen_plugin_text_tool as invoke_tool  # noqa: E402
import export_esp_strings as esp_exporter  # noqa: E402
import new_final_binary_review_packet as binary_review  # noqa: E402
import route_translation_task  # noqa: E402
import run_non_gui_qa_gates as qa_gates  # noqa: E402
import run_plugin_translation_stage as plugin_stage  # noqa: E402
from game_context import load_game_context, load_game_profile  # noqa: E402


def sdk_list_has_net8_or_newer(output: str) -> bool:
    for line in output.splitlines():
        version = line.strip().split(maxsplit=1)[0] if line.strip() else ""
        major_text = version.split(".", maxsplit=1)[0]
        try:
            if int(major_text) >= 8:
                return True
        except ValueError:
            continue
    return False


def discover_dotnet_host() -> Path | None:
    def has_sdk(candidate: Path) -> bool:
        try:
            result = subprocess.run(
                [str(candidate), "--list-sdks"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and sdk_list_has_net8_or_newer(result.stdout)

    configured_host = os.environ.get("DOTNET_HOST_PATH", "").strip()
    if configured_host and Path(configured_host).is_file() and has_sdk(Path(configured_host)):
        return Path(configured_host)

    path_host = shutil.which("dotnet")
    if path_host and has_sdk(Path(path_host)):
        return Path(path_host)

    roots: list[Path] = []
    workspace_root = os.environ.get("SKYRIM_CHS_WORKSPACE_ROOT", "").strip()
    if workspace_root:
        roots.append(Path(workspace_root))
    roots.append(ROOT)
    for root in roots:
        config_path = root / "config" / "tools.local.json"
        if not config_path.is_file():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            value = str(config.get("DecoderTools", {}).get("DotNetSdkPath") or "").strip()
        except (OSError, ValueError, AttributeError):
            continue
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file() and has_sdk(candidate):
            return candidate
    return None


DOTNET = discover_dotnet_host()


def marker(game_id: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2 if game_id else 1,
        "kind": "bethesda-mod-chs-translation-workspace",
        "plugin_name": "skyrim-mod-chs-translation",
    }
    if game_id:
        payload["game_id"] = game_id
        payload["game_profile"] = game_id
    return payload


def subrecord(signature: str, payload: bytes) -> bytes:
    return signature.encode("ascii") + struct.pack("<H", len(payload)) + payload


def record(signature: str, form_id: int, payload: bytes, *, flags: int = 0) -> bytes:
    return (
        signature.encode("ascii")
        + struct.pack("<I", len(payload))
        + struct.pack("<I", flags)
        + struct.pack("<I", form_id)
        + (b"\x00" * 8)
        + payload
    )


def tes4_plugin(*records: bytes, localized: bool = False) -> bytes:
    header = record("TES4", 0, b"", flags=0x00000080 if localized else 0)
    return header + b"".join(records)


class Fallout4PluginAdapterRegressionTests(unittest.TestCase):
    _adapter_build: subprocess.CompletedProcess[str] | None = None

    def test_dotnet_sdk_list_requires_major_eight_or_newer(self) -> None:
        self.assertFalse(sdk_list_has_net8_or_newer("6.0.428 [C:\\dotnet\\sdk]\n7.0.410 [C:\\dotnet\\sdk]\n"))
        self.assertFalse(sdk_list_has_net8_or_newer("not-an-sdk\n"))
        self.assertTrue(sdk_list_has_net8_or_newer("8.0.422 [C:\\dotnet\\sdk]\n"))
        self.assertTrue(sdk_list_has_net8_or_newer("7.0.410 [x]\n10.0.100 [x]\n"))

    def setUp(self) -> None:
        temp_root = ROOT / ".tmp" / "task-3-tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name)
        for relative in (
            "work/extracted_mods/TestMod",
            "work/plugin_translation_maps/TestMod",
            "translated/plugin_exports/TestMod",
            "out/TestMod/tool_outputs",
            "source/plugin_exports/TestMod",
            "qa",
            "config",
        ):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)

    def write_marker(self, game_id: str | None) -> None:
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker(game_id), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def require_dotnet(self) -> Path:
        if DOTNET is None:
            self.skipTest("portable dotnet host was not found; covered by the independent dotnet test command")
        return DOTNET

    def ensure_adapter_built(self) -> Path:
        dotnet = self.require_dotnet()
        if type(self)._adapter_build is None:
            type(self)._adapter_build = subprocess.run(
                [str(dotnet), "build", str(PROJECT)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        build = type(self)._adapter_build
        assert build is not None
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        return dotnet

    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=str(self.workspace),
            env={
                **os.environ,
                "SKYRIM_CHS_WORKSPACE_ROOT": str(self.workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def invoke_wrapper(
        self,
        game_id: str | None,
        explicit_game: str = "",
        *,
        marker_present: bool = True,
        plugin_name: str = "Test.esp",
        plugin_flags: int = 0,
        adapter_result_path: str = "",
    ) -> tuple[int, list[str]]:
        if marker_present:
            self.write_marker(game_id)
        plugin = self.workspace / "work/extracted_mods/TestMod" / plugin_name
        translation = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        plugin.write_bytes(record("TES4", 0, b"", flags=plugin_flags))
        translation.write_text("", encoding="utf-8")
        config = self.workspace / "config/tools.local.json"
        config.write_text("{}\n", encoding="utf-8")
        fake_dotnet = self.workspace / "tools/dotnet-sdk/dotnet.exe"
        fake_dll = self.workspace / "tools/cache/SkyrimPluginTextTool.dll"
        fake_dotnet.parent.mkdir(parents=True)
        fake_dll.parent.mkdir(parents=True)
        fake_dotnet.write_bytes(b"")
        fake_dll.write_bytes(b"")
        argv = [
            "invoke_mutagen_plugin_text_tool.py",
            "--input-plugin-path",
            str(plugin),
            "--translation-jsonl-path",
            str(translation),
            "--output-plugin-path",
            str(self.workspace / "out/TestMod/tool_outputs" / plugin_name),
            "--report-path",
            str(self.workspace / "qa/Test.write.md"),
        ]
        if explicit_game:
            argv.extend(["--game", explicit_game])
        if adapter_result_path:
            argv.extend(["--adapter-result-path", adapter_result_path])

        def completed(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = self.workspace / "out/TestMod/tool_outputs" / plugin_name
            report = self.workspace / "qa/Test.write.md"
            output.write_bytes(b"translated-plugin")
            light_by_extension = plugin.suffix.casefold() == ".esl"
            light_by_header = bool(plugin_flags & 0x00000200)
            context: Path | None = None
            if light_by_extension or light_by_header:
                context = (
                    self.workspace
                    / "work"
                    / "plugin_context"
                    / "TestMod"
                    / f"{plugin_name}.resolved-master-styles.json"
                )
                context.parent.mkdir(parents=True, exist_ok=True)
                input_relative = plugin.relative_to(self.workspace).as_posix()
                input_sha256 = hashlib.sha256(plugin.read_bytes()).hexdigest()
                context.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "game_id": explicit_game or game_id,
                            "plugin": plugin.name,
                            "input_path": input_relative,
                            "input_sha256": input_sha256,
                            "current_style": "light",
                            "current_evidence_source": "fixture:light-plugin",
                            "current_inspected_path": input_relative,
                            "current_inspected_sha256": input_sha256,
                            "current_small_flag": bool(plugin_flags & 0x00000200),
                            "masters": [],
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            context_relative = (
                context.relative_to(self.workspace).as_posix()
                if context is not None
                else "<none>"
            )
            context_sha256 = (
                hashlib.sha256(context.read_bytes()).hexdigest()
                if context is not None
                else "<none>"
            )
            report.write_text(
                "# Mutagen report\n\n"
                f"- localized: {str(bool(plugin_flags & 0x00000080)).lower()}\n"
                f"- light_by_extension: {str(light_by_extension).lower()}\n"
                f"- light_by_header: {str(light_by_header).lower()}\n"
                "- contains_unsupported_light_formids: false\n"
                f"- Master-style context: {context_relative}\n"
                f"- Master-style context SHA256: {context_sha256}\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path", return_value=fake_dotnet),
            mock.patch.object(invoke_tool, "ensure_adapter_dll", return_value=fake_dll),
            mock.patch.object(invoke_tool.subprocess, "run", side_effect=completed) as run,
        ):
            code = invoke_tool.main()
        command = list(run.call_args.args[0]) if run.called else []
        return code, command

    def test_wrapper_uses_fallout4_marker_and_passes_required_game(self) -> None:
        code, command = self.invoke_wrapper("fallout4")
        self.assertEqual(code, 0)
        self.assertEqual(command[command.index("--game") + 1], "fallout4")

    def test_wrapper_rejects_explicit_game_conflicting_with_marker(self) -> None:
        code, command = self.invoke_wrapper("fallout4", "skyrim-se")
        self.assertEqual(code, 1)
        self.assertEqual(command, [])

    def test_wrapper_rejects_marker_without_game_id(self) -> None:
        code, command = self.invoke_wrapper(None)
        self.assertEqual(code, 1)
        self.assertEqual(command, [])

    def test_wrapper_requires_marker_or_explicit_game(self) -> None:
        code, command = self.invoke_wrapper(None, marker_present=False)
        self.assertEqual(code, 1)
        self.assertEqual(command, [])

    def test_wrapper_accepts_explicit_game_without_marker(self) -> None:
        code, command = self.invoke_wrapper(
            None,
            "fallout4",
            marker_present=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(command[command.index("--game") + 1], "fallout4")

    def test_wrapper_runs_fallout4_esl_with_hash_bound_context(self) -> None:
        result_path = "qa/Test.light.adapter-result.json"
        code, command = self.invoke_wrapper(
            "fallout4",
            plugin_name="Test.esl",
            adapter_result_path=result_path,
        )
        self.assertEqual(code, 0)
        self.assertNotEqual(command, [])
        result = json.loads((self.workspace / result_path).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["error_code"])
        self.assertTrue(
            any(path.startswith("work/plugin_context/") for path in result["evidence_files"])
        )

    def test_wrapper_runs_light_flagged_esp_with_hash_bound_context(self) -> None:
        result_path = "qa/Test.light-flagged.adapter-result.json"
        code, command = self.invoke_wrapper(
            "fallout4",
            plugin_flags=0x00000200,
            adapter_result_path=result_path,
        )
        self.assertEqual(code, 0)
        self.assertNotEqual(command, [])
        result = json.loads((self.workspace / result_path).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["error_code"])
        self.assertTrue(
            any(path.startswith("work/plugin_context/") for path in result["evidence_files"])
        )

    def test_wrapper_blocks_localized_fallout4_esp_before_invoking_adapter(self) -> None:
        result_path = "qa/Test.localized.adapter-result.json"
        code, command = self.invoke_wrapper(
            "fallout4",
            plugin_flags=0x00000080,
            adapter_result_path=result_path,
        )
        self.assertEqual(code, 2)
        self.assertEqual(command, [])
        result = json.loads((self.workspace / result_path).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "experimental_limit")

    def test_plugin_stage_requires_marker_or_explicit_game(self) -> None:
        argv = [
            "run_plugin_translation_stage.py",
            "--mod-name",
            "TestMod",
            "--workspace-path",
            "work/extracted_mods/TestMod",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(plugin_stage, "project_root", return_value=self.workspace),
        ):
            with self.assertRaisesRegex(ValueError, "Workspace marker is required"):
                plugin_stage.main()

    def test_plugin_stage_rejects_raw_mod_directory(self) -> None:
        self.write_marker("fallout4")
        raw_workspace = self.workspace / "mod" / "TestMod"
        raw_workspace.mkdir(parents=True)
        argv = [
            "run_plugin_translation_stage.py",
            "--mod-name",
            "TestMod",
            "--workspace-path",
            str(raw_workspace),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(plugin_stage, "project_root", return_value=self.workspace),
        ):
            with self.assertRaisesRegex(ValueError, "prepared workspace"):
                plugin_stage.main()

    def test_wrapper_build_failure_removes_stale_output(self) -> None:
        self.write_marker("fallout4")
        plugin = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        translation = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        output = self.workspace / "out/TestMod/tool_outputs/Test.esp"
        config = self.workspace / "config/tools.local.json"
        fake_dotnet = self.workspace / "tools/dotnet-sdk/dotnet.exe"
        plugin.write_bytes(tes4_plugin())
        translation.write_text("", encoding="utf-8")
        output.write_bytes(b"stale-output")
        config.write_text("{}\n", encoding="utf-8")
        fake_dotnet.parent.mkdir(parents=True)
        fake_dotnet.write_bytes(b"")
        argv = [
            "invoke_mutagen_plugin_text_tool.py",
            "--input-plugin-path",
            str(plugin),
            "--translation-jsonl-path",
            str(translation),
            "--output-plugin-path",
            str(output),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path", return_value=fake_dotnet),
            mock.patch.object(invoke_tool, "ensure_adapter_dll", side_effect=RuntimeError("build failed")),
        ):
            code = invoke_tool.main()
        self.assertEqual(code, 1)
        self.assertFalse(output.exists())

    def test_wrapper_preflight_failure_preserves_existing_output(self) -> None:
        self.write_marker("fallout4")
        plugin = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        translation = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        output = self.workspace / "out/TestMod/tool_outputs/Test.esp"
        config = self.workspace / "config/tools.local.json"
        argv = [
            "invoke_mutagen_plugin_text_tool.py",
            "--input-plugin-path",
            str(plugin),
            "--translation-jsonl-path",
            str(translation),
            "--output-plugin-path",
            str(output),
            "--config-path",
            str(config),
        ]
        for missing in (plugin, translation, config):
            with self.subTest(missing=missing.name):
                plugin.write_bytes(tes4_plugin())
                translation.write_text("", encoding="utf-8")
                config.write_text("{}\n", encoding="utf-8")
                output.write_bytes(b"stale-output")
                missing.unlink()
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
                    mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
                ):
                    code = invoke_tool.main()
                self.assertEqual(code, 1)
                self.assertEqual(output.read_bytes(), b"stale-output")

    def test_wrapper_cross_mod_output_rejection_preserves_target(self) -> None:
        self.write_marker("fallout4")
        plugin = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        translation = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        output = self.workspace / "out/OtherMod/tool_outputs/Test.esp"
        config = self.workspace / "config/tools.local.json"
        plugin.write_bytes(tes4_plugin())
        translation.write_text("", encoding="utf-8")
        output.parent.mkdir(parents=True)
        output.write_bytes(b"other-mod-output")
        config.write_text("{}\n", encoding="utf-8")
        argv = [
            "invoke_mutagen_plugin_text_tool.py",
            "--input-plugin-path",
            str(plugin),
            "--translation-jsonl-path",
            str(translation),
            "--output-plugin-path",
            str(output),
            "--config-path",
            str(config),
        ]

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
        ):
            code = invoke_tool.main()

        self.assertEqual(code, 1)
        self.assertEqual(output.read_bytes(), b"other-mod-output")

    def test_exporter_routes_fallout4_to_controlled_mutagen_export(self) -> None:
        self.write_marker("fallout4")
        plugin = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        plugin.write_bytes(tes4_plugin())
        output = self.workspace / "source/plugin_exports/TestMod/Test.jsonl"
        report = self.workspace / "qa/Test.export.md"
        manifest = self.workspace / "work/plugin_context/TestMod/Test.esp.master-styles.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}\n", encoding="utf-8")
        argv = [
            "export_esp_strings.py",
            "--project-root",
            str(self.workspace),
            "--plugin-path",
            str(plugin),
            "--output-path",
            str(output),
            "--report-path",
            str(report),
            "--master-style-manifest",
            str(manifest),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(esp_exporter, "run_mutagen_export", return_value=0) as controlled_export,
        ):
            result = esp_exporter.main()
        self.assertEqual(result, 0)
        controlled_export.assert_called_once()
        export_args = controlled_export.call_args.args
        self.assertEqual(export_args[:4], (self.workspace, plugin, output, report))
        self.assertEqual(export_args[4].game_id, "fallout4")
        self.assertEqual(export_args[5].adapter_options["mutagen_release"], "Fallout4")
        self.assertEqual(export_args[5].level, "experimental_write")
        self.assertEqual(export_args[6], manifest)

    def test_fallout4_localized_header_is_blocked_without_candidate_jsonl(self) -> None:
        self.write_marker("fallout4")
        plugin = self.workspace / "work/extracted_mods/TestMod/Localized.esp"
        plugin.write_bytes(tes4_plugin(localized=True))
        output = self.workspace / "source/plugin_exports/TestMod/Localized.jsonl"
        report = self.workspace / "qa/Localized.export.md"
        result = self.run_script(
            "export_esp_strings.py",
            "--project-root",
            str(self.workspace),
            "--plugin-path",
            str(plugin),
            "--output-path",
            str(output),
            "--report-path",
            str(report),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("blocked", report_text.lower())
        self.assertIn("localized", report_text.lower())
        self.assertIn("plugin_text_capability_level: experimental_write", report_text)
        self.assertNotIn("support_level:", report_text)

    def test_translation_map_keeps_same_source_rows_separate_by_identity(self) -> None:
        self.write_marker("fallout4")
        export = self.workspace / "source/plugin_exports/TestMod/Test.jsonl"
        base = {
            "schema_version": 2,
            "game_id": "fallout4",
            "plugin": "Test.esp",
            "record_type": "WEAP",
            "editor_id": "",
            "field_path": "Name",
            "subrecord_type": "FULL",
            "subrecord_index": 1,
            "source": "Common Name",
            "target": "",
            "risk": "candidate",
            "writeback": "supported",
        }
        rows = [{**base, "form_id": "00000100"}, {**base, "form_id": "00000200"}]
        export.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        translation_map = self.workspace / "work/plugin_translation_maps/TestMod/Test.translation_map.json"
        translation_map.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "game_id": "fallout4",
                    "translations": [
                        {**rows[0], "target": "名称甲"},
                        {**rows[1], "target": "名称乙"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        output = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        result = self.run_script(
            "apply_plugin_translation_map.py",
            "--export-path",
            str(export),
            "--translation-map-path",
            str(translation_map),
            "--output-path",
            str(output),
            "--report-path",
            str(self.workspace / "qa/Test.map.md"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        translated = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["target"] for row in translated], ["名称甲", "名称乙"])
        self.assertTrue(all(row["schema_version"] == 2 for row in translated))
        self.assertTrue(all(row["field_path"] == "Name" for row in translated))

    def test_translation_map_rejects_source_drift_with_same_structural_identity(self) -> None:
        self.write_marker("fallout4")
        export = self.workspace / "source/plugin_exports/TestMod/Test.jsonl"
        row = {
            "schema_version": 2,
            "game_id": "fallout4",
            "plugin": "Test.esp",
            "record_type": "WEAP",
            "form_id": "00000100",
            "editor_id": "TestWeapon",
            "field_path": "Name",
            "subrecord_type": "FULL",
            "subrecord_index": 1,
            "source": "Current Source",
            "target": "",
            "risk": "candidate",
            "writeback": "supported",
        }
        export.write_text(json.dumps(row) + "\n", encoding="utf-8")
        translation_map = self.workspace / "work/plugin_translation_maps/TestMod/Test.translation_map.json"
        translation_map.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "game_id": "fallout4",
                    "translations": [{**row, "source": "Stale Source", "target": "译文"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        output = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        result = self.run_script(
            "apply_plugin_translation_map.py",
            "--export-path",
            str(export),
            "--translation-map-path",
            str(translation_map),
            "--output-path",
            str(output),
            "--report-path",
            str(self.workspace / "qa/Test.map.md"),
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        translated = json.loads(output.read_text(encoding="utf-8").strip())
        self.assertEqual(translated["target"], "")

    def test_stage_uses_strict_verification_without_warn_only(self) -> None:
        source = (SCRIPTS / "run_plugin_translation_stage.py").read_text(encoding="utf-8")
        verify_block = source[source.index('"verify_plugin_output.py"') :]
        self.assertNotIn('"--warn-only"', verify_block)
        self.assertIn('"--require-translation-evidence"', verify_block)
        self.assertIn('"--writeback-report-path"', verify_block)

    def test_strict_gate_cannot_reuse_a_stale_passing_verifier_report(self) -> None:
        source = (SCRIPTS / "run_non_gui_qa_gates.py").read_text(encoding="utf-8")
        verify_block = source[source.index('verify_path = root / verify_report') : source.index('coverage_complete =')]
        self.assertIn("verify_path.unlink()", verify_block)
        self.assertIn("if verify.returncode != 0:", verify_block)
        self.assertNotIn("if verify.returncode != 0 and", verify_block)

    def test_strict_candidate_probe_uses_current_plugin_and_independent_outputs(self) -> None:
        plugin = self.workspace / "out/TestMod/汉化产出/final_mod/Test.esp"
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(tes4_plugin())
        export_path = self.workspace / "source/plugin_exports/TestMod/Test.esp.strict_candidate_probe.jsonl"
        report_path = self.workspace / "qa/Test.esp.strict_candidate_probe.md"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text("", encoding="utf-8")
        report_path.write_text("# stale\n", encoding="utf-8")

        def successful_export(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            self.assertFalse(export_path.exists())
            self.assertFalse(report_path.exists())
            self.assertEqual(script_name, "export_esp_strings.py")
            self.assertEqual(args[args.index("--plugin-path") + 1], str(plugin))
            self.assertEqual(args[args.index("--game") + 1], "fallout4")
            self.assertIn("--allow-generated-plugin", args)
            self.assertEqual(
                args[args.index("--output-path") + 1],
                str(Path("source/plugin_exports/TestMod/Test.esp.strict_candidate_probe.jsonl")),
            )
            self.assertEqual(
                args[args.index("--report-path") + 1],
                str(Path("qa/Test.esp.strict_candidate_probe.md")),
            )
            export_path.write_text(json.dumps({"risk": "candidate"}) + "\n", encoding="utf-8")
            report_path.write_text("# current\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(qa_gates, "run_python_script", side_effect=successful_export) as run_export:
            count = qa_gates.get_plugin_candidate_count(
                self.workspace,
                "TestMod",
                plugin,
                "Test.esp",
                "fallout4",
            )

        self.assertEqual(count, 1)
        run_export.assert_called_once()

    def test_strict_candidate_probe_fails_closed_when_current_export_fails(self) -> None:
        plugin = self.workspace / "out/TestMod/汉化产出/final_mod/Test.esp"
        failed_export = subprocess.CompletedProcess([], 1, "", "current export failed")

        with mock.patch.object(qa_gates, "run_python_script", return_value=failed_export) as run_export:
            count = qa_gates.get_plugin_candidate_count(
                self.workspace,
                "TestMod",
                plugin,
                "Test.esp",
                "fallout4",
            )

        self.assertIsNone(count)
        run_export.assert_called_once()

    def test_strict_candidate_probe_fails_closed_without_current_report(self) -> None:
        plugin = self.workspace / "out/TestMod/汉化产出/final_mod/Test.esp"
        export_path = self.workspace / "source/plugin_exports/TestMod/Test.esp.strict_candidate_probe.jsonl"

        def export_without_report(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            export_path.write_text(json.dumps({"risk": "candidate"}) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(qa_gates, "run_python_script", side_effect=export_without_report):
            count = qa_gates.get_plugin_candidate_count(
                self.workspace,
                "TestMod",
                plugin,
                "Test.esp",
                "fallout4",
            )

        self.assertIsNone(count)

    def test_strict_candidate_probe_fails_closed_for_invalid_jsonl(self) -> None:
        plugin = self.workspace / "out/TestMod/汉化产出/final_mod/Test.esp"
        export_path = self.workspace / "source/plugin_exports/TestMod/Test.esp.strict_candidate_probe.jsonl"
        report_path = self.workspace / "qa/Test.esp.strict_candidate_probe.md"

        for content in ("{not-json}\n", "42\n", "[]\n", "{}\n"):
            with self.subTest(content=content):
                def invalid_export(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
                    export_path.write_text(content, encoding="utf-8")
                    report_path.write_text("# current\n", encoding="utf-8")
                    return subprocess.CompletedProcess([], 0, "", "")

                with mock.patch.object(qa_gates, "run_python_script", side_effect=invalid_export):
                    count = qa_gates.get_plugin_candidate_count(
                        self.workspace,
                        "TestMod",
                        plugin,
                        "Test.esp",
                        "fallout4",
                    )

                self.assertIsNone(count)

    def test_strict_gate_probes_the_current_final_plugin(self) -> None:
        source = (SCRIPTS / "run_non_gui_qa_gates.py").read_text(encoding="utf-8")
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_plugin_candidate_count"
        ]

        self.assertTrue(calls)
        self.assertTrue(
            all(len(call.args) >= 3 and isinstance(call.args[2], ast.Name) for call in calls)
        )
        self.assertEqual({call.args[2].id for call in calls}, {"plugin"})

    def test_plugin_stage_routes_batch_with_one_loaded_game_context(self) -> None:
        self.write_marker("fallout4")
        workspace = self.workspace / "work" / "extracted_mods" / "TestMod"
        for name in ("One.esp", "Two.esl"):
            (workspace / name).write_bytes(tes4_plugin())

        failed_export = subprocess.CompletedProcess([], 1, "", "synthetic export failure")
        real_resolve = plugin_stage.resolve_workspace_game_context
        route_counter = mock.Mock(
            side_effect=lambda root, path, context=None: route_translation_task.route_for(root, path, context)
        )
        with (
            mock.patch.object(plugin_stage, "project_root", return_value=self.workspace),
            mock.patch.object(plugin_stage, "find_data_root", return_value=workspace),
            mock.patch.object(
                plugin_stage,
                "resolve_workspace_game_context",
                wraps=real_resolve,
            ) as load_counter,
            mock.patch.object(plugin_stage, "route_for", route_counter, create=True),
            mock.patch.object(plugin_stage, "run_python_script", return_value=failed_export) as subprocess_counter,
            mock.patch.object(
                sys,
                "argv",
                [
                    "run_plugin_translation_stage.py",
                    "--mod-name",
                    "TestMod",
                    "--workspace-path",
                    str(workspace),
                ],
            ),
            mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(ROOT)}, clear=False),
        ):
            result = plugin_stage.main()

        self.assertEqual(result, 1)
        self.assertEqual(load_counter.call_count, 1)
        self.assertEqual(route_counter.call_count, 2)
        routed_contexts = [call.args[2] for call in route_counter.call_args_list]
        self.assertIs(routed_contexts[0], routed_contexts[1])
        self.assertEqual(routed_contexts[0].game_id, "fallout4")
        self.assertNotIn(
            "route_translation_task.py",
            [call.args[1] for call in subprocess_counter.call_args_list],
        )

    def test_strict_verification_rejects_hash_only_output(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        output = self.workspace / "out/TestMod/tool_outputs/Test.esp"
        original.write_bytes(tes4_plugin())
        output.write_bytes(tes4_plugin(record("MISC", 1, b"")))
        report = self.workspace / "qa/Test.verify.md"
        writeback_report = self.workspace / "qa/Test.write.md"
        writeback_report.write_text(
            "- Reparse succeeded: True\n- Reparse record count: 1\n",
            encoding="utf-8",
        )
        result = self.run_script(
            "verify_plugin_output.py",
            "--original-plugin-path",
            str(original),
            "--output-plugin-path",
            str(output),
            "--report-output-path",
            str(report),
            "--writeback-report-path",
            str(writeback_report),
            "--require-translation-evidence",
        )
        self.assertNotEqual(result.returncode, 0)
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("Round-trip verified: False", report_text)
        self.assertIn("Translation rows verified: 0", report_text)

    def test_strict_verification_rejects_cross_game_or_wrong_path_writeback_report(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        output = self.workspace / "out/TestMod/tool_outputs/Test.esp"
        translation = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        original.write_bytes(tes4_plugin())
        output.write_bytes(tes4_plugin(record("MISC", 1, b"")))
        translation.write_text("{}\n", encoding="utf-8")
        report = self.workspace / "qa/Test.verify.md"
        writeback_report = self.workspace / "qa/Test.write.md"
        writeback_report.write_text(
            "\n".join(
                [
                    "- game_id: skyrim-se",
                    "- Input plugin: work/extracted_mods/Other.esp",
                    f"- Input SHA256: {'0' * 64}",
                    "- Translation JSONL: translated/plugin_exports/TestMod/Test.zh.jsonl",
                    f"- Translation SHA256: {'0' * 64}",
                    "- Output plugin: out/TestMod/tool_outputs/Other.esp",
                    f"- Output SHA256: {'0' * 64}",
                    "- Reparse succeeded: True",
                    "- Structural validation succeeded: True",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        result = self.run_script(
            "verify_plugin_output.py",
            "--original-plugin-path",
            str(original),
            "--output-plugin-path",
            str(output),
            "--translation-jsonl-path",
            str(translation),
            "--report-output-path",
            str(report),
            "--writeback-report-path",
            str(writeback_report),
            "--require-translation-evidence",
        )
        self.assertNotEqual(result.returncode, 0)
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("writeback report game_id mismatch", report_text)
        self.assertIn("writeback report input path mismatch", report_text)
        self.assertIn("writeback report input hash mismatch", report_text)
        self.assertIn("writeback report translation hash mismatch", report_text)
        self.assertIn("writeback report output path mismatch", report_text)
        self.assertIn("writeback report output hash mismatch", report_text)

    def test_strict_verification_rejects_writeback_report_without_output_hash(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        output = self.workspace / "out/TestMod/tool_outputs/Test.esp"
        original.write_bytes(tes4_plugin())
        output.write_bytes(tes4_plugin(record("MISC", 1, b"")))
        report = self.workspace / "qa/Test.verify.md"
        writeback_report = self.workspace / "qa/Test.write.md"
        writeback_report.write_text(
            "\n".join(
                [
                    "- game_id: fallout4",
                    "- Input plugin: work/extracted_mods/TestMod/Test.esp",
                    "- Output plugin: out/TestMod/tool_outputs/Test.esp",
                    "- Reparse succeeded: True",
                    "- Structural validation succeeded: True",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        result = self.run_script(
            "verify_plugin_output.py",
            "--original-plugin-path",
            str(original),
            "--output-plugin-path",
            str(output),
            "--report-output-path",
            str(report),
            "--writeback-report-path",
            str(writeback_report),
            "--require-translation-evidence",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writeback report output hash missing", report.read_text(encoding="utf-8"))

        writeback_report.write_text(
            writeback_report.read_text(encoding="utf-8").replace(
                "- Reparse succeeded: True",
                "- Output SHA256: NOT-A-HASH\n- Reparse succeeded: True",
            ),
            encoding="utf-8",
        )
        malformed = self.run_script(
            "verify_plugin_output.py",
            "--original-plugin-path",
            str(original),
            "--output-plugin-path",
            str(output),
            "--report-output-path",
            str(report),
            "--writeback-report-path",
            str(writeback_report),
            "--require-translation-evidence",
        )
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("writeback report output hash malformed", report.read_text(encoding="utf-8"))

    def test_binary_review_cache_isolated_by_game_context(self) -> None:
        cache = self.workspace / "qa/cache.json"
        packet = self.workspace / "qa/packet.md"
        items = self.workspace / "qa/items.jsonl"
        packet.write_text("packet\n", encoding="utf-8")
        items.write_text("", encoding="utf-8")
        final_fingerprints = {"Fixture.esp": "ABC"}
        original_fingerprints = {"Fixture.esp": "DEF"}
        fallout_context = {
            "game_id": "fallout4",
            "game_profile_version": 1,
            "plugin_adapter": "fallout4-mutagen",
            "plugin_adapter_version": 1,
            "support_level": "experimental",
        }
        skyrim_context = {**fallout_context, "game_id": "skyrim-se", "plugin_adapter": "skyrim-mutagen", "support_level": "stable"}
        binary_review.write_cache(
            cache,
            packet,
            items,
            final_fingerprints,
            original_fingerprints,
            fallout_context,
        )
        self.assertTrue(
            binary_review.cached_packet_is_current(
                cache,
                packet,
                items,
                final_fingerprints,
                original_fingerprints,
                fallout_context,
            )
        )
        self.assertFalse(
            binary_review.cached_packet_is_current(
                cache,
                packet,
                items,
                final_fingerprints,
                original_fingerprints,
                skyrim_context,
            )
        )

    def test_final_binary_review_collects_two_plugins_without_losing_game_context(self) -> None:
        source_root = self.workspace / "work/extracted_mods/TestMod"
        final_mod = self.workspace / "out/TestMod/汉化产出/final_mod"
        final_mod.mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(("First.esp", "Second.esp"), start=1):
            payload = (
                subrecord("EDID", f"FixtureWeapon{index}".encode("ascii") + b"\x00")
                + subrecord("FULL", f"Visible Weapon {index}".encode("ascii") + b"\x00")
            )
            plugin_bytes = tes4_plugin(record("WEAP", 0x800 + index, payload))
            (source_root / name).write_bytes(plugin_bytes)
            (final_mod / name).write_bytes(plugin_bytes)

        game_context = load_game_profile("fallout4")
        export_games: list[str] = []

        def fake_export(
            root: Path,
            plugin_path: Path,
            mod_name: str,
            output_rel: str,
            report_rel: str,
            game_id: str,
        ) -> subprocess.CompletedProcess[str]:
            export_games.append(game_id)
            index = 1 if plugin_path.name == "First.esp" else 2
            row = {
                "schema_version": 2,
                "game_id": game_id,
                "plugin": plugin_path.name,
                "record_type": "WEAP",
                "form_id": f"{0x800 + index:08X}",
                "editor_id": f"FixtureWeapon{index}",
                "field_path": "Name",
                "subrecord_type": "FULL",
                "subrecord_index": 1,
                "source": f"Visible Weapon {index}",
                "target": "",
                "risk": "candidate",
                "writeback": "supported",
            }
            output_path = root / output_rel
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(binary_review, "run_esp_export", side_effect=fake_export):
            plugin_count, items, failures = binary_review.collect_plugin_items(
                self.workspace,
                source_root,
                final_mod,
                "TestMod",
                set(),
                game_context,
            )

        self.assertEqual(plugin_count, 2)
        self.assertEqual(failures, [])
        self.assertEqual(export_games, ["fallout4"] * 4)
        self.assertEqual({item.File for item in items}, {"First.esp", "Second.esp"})
        packet = self.workspace / "qa/TestMod.plugin.final_binary_review_packet.md"
        review_items = self.workspace / "qa/TestMod.plugin.final_binary_review_items.jsonl"
        binary_review.write_reports(
            self.workspace,
            "TestMod",
            source_root,
            final_mod,
            packet,
            review_items,
            plugin_count,
            0,
            items,
            failures,
            game_context,
        )
        packet_text = packet.read_text(encoding="utf-8")
        self.assertIn("First.esp", packet_text)
        self.assertIn("Second.esp", packet_text)

    def test_skyrim_export_remains_supported_and_stable(self) -> None:
        self.write_marker("skyrim-se")
        context = load_game_context(self.workspace)
        capability = context.require_capability("plugin_text")

        self.assertEqual(capability.level, "stable")
        self.assertEqual(capability.adapter_id, "mutagen-bethesda-plugin")
        self.assertEqual(capability.options["extract_backend"], "mutagen-adapter")

    def test_verification_report_marks_fallout4_experimental(self) -> None:
        self.write_marker("fallout4")
        original = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        output = self.workspace / "out/TestMod/tool_outputs/Test.esp"
        original.write_bytes(tes4_plugin())
        output.write_bytes(tes4_plugin(record("MISC", 1, b"")))
        report = self.workspace / "qa/Test.verify.md"
        result = self.run_script(
            "verify_plugin_output.py",
            "--original-plugin-path",
            str(original),
            "--output-plugin-path",
            str(output),
            "--report-output-path",
            str(report),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = report.read_text(encoding="utf-8")
        self.assertIn("game_id: fallout4", text)
        self.assertIn("plugin_adapter: mutagen-bethesda-plugin", text)
        self.assertIn("plugin_adapter_version: 1", text)
        self.assertIn("support_level: experimental", text)
        self.assertNotIn("game verified", text.lower())

    def test_csharp_contract_and_real_sdk_build(self) -> None:
        self.ensure_adapter_built()
        project_text = PROJECT.read_text(encoding="utf-8")
        self.assertIn('Include="Mutagen.Bethesda.Skyrim" Version="0.53.1"', project_text)
        self.assertIn('Include="Mutagen.Bethesda.Fallout4" Version="0.53.1"', project_text)
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((PROJECT.parent).rglob("*.cs"))
            if "obj" not in path.parts and "bin" not in path.parts
        )
        for required in (
            "schema_version",
            "game_id",
            "field_path",
            "source",
            "Fallout4Mod.CreateFromBinary",
            "Fallout 4\\\\Data",
        ):
            self.assertIn(required, sources)

    def test_csharp_fixture_uses_portable_dotnet_host_and_ignored_build_outputs(self) -> None:
        source = (ROOT / "adapters/SkyrimPluginTextTool.Tests/PluginWritebackTests.cs").read_text(encoding="utf-8")
        self.assertIn("DOTNET_HOST_PATH", source)
        self.assertIn("File.Exists(configured)", source)
        self.assertIn('? configured : "dotnet";', source)
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("**/bin/", gitignore)
        self.assertIn("**/obj/", gitignore)
        python_source = Path(__file__).read_text(encoding="utf-8")
        forbidden_sdk_fragment = "SkyrimModTranslationWork" + "FO4SDK"
        self.assertNotIn(forbidden_sdk_fragment, python_source)
        self.assertIn("DOTNET_HOST_PATH", python_source)
        self.assertIn('shutil.which("dotnet")', python_source)
        self.assertIn('config" / "tools.local.json', python_source)

    def test_adapter_cli_rejects_unknown_mutagen_release(self) -> None:
        dotnet = self.ensure_adapter_built()
        result = subprocess.run(
            [
                str(dotnet),
                str(DLL),
                "apply",
                "--game",
                "skyrim-se",
                "--mutagen-release",
                "UnknownRelease",
                "--capability-level",
                "stable",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown Mutagen release", result.stdout + result.stderr)

    def test_adapter_cli_rejects_unknown_game(self) -> None:
        dotnet = self.ensure_adapter_built()
        result = subprocess.run(
            [
                str(dotnet),
                str(DLL),
                "apply",
                "--game",
                "oblivion",
                "--mutagen-release",
                "SkyrimSE",
                "--capability-level",
                "stable",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", (result.stdout + result.stderr).lower())

    def test_adapter_blocks_fallout4_localized_plugin_before_mutagen_write(self) -> None:
        dotnet = self.ensure_adapter_built()
        plugin = self.workspace / "work/extracted_mods/TestMod/Localized.esp"
        translations = self.workspace / "translated/plugin_exports/TestMod/Localized.zh.jsonl"
        output = self.workspace / "out/TestMod/tool_outputs/Localized.esp"
        report = self.workspace / "qa/Localized.write.md"
        plugin.write_bytes(tes4_plugin(localized=True))
        translations.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "game_id": "fallout4",
                    "plugin": "Localized.esp",
                    "record_type": "WEAP",
                    "form_id": "00000100",
                    "editor_id": "TestWeapon",
                    "field_path": "Name",
                    "subrecord_type": "FULL",
                    "subrecord_index": 1,
                    "source": "Laser Rifle",
                    "target": "激光步枪",
                    "risk": "candidate",
                    "writeback": "supported",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(dotnet),
                str(DLL),
                "apply",
                "--game",
                "fallout4",
                "--mutagen-release",
                "Fallout4",
                "--capability-level",
                "experimental_write",
                "--project-root",
                str(self.workspace),
                "--input-plugin",
                str(plugin),
                "--translation-jsonl",
                str(translations),
                "--output-plugin",
                str(output),
                "--report",
                str(report),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(output.exists())
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("localized flag", report_text)
        self.assertIn("support_level: experimental", report_text)


if __name__ == "__main__":
    unittest.main()

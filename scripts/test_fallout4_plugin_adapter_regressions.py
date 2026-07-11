from __future__ import annotations

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
import new_final_binary_review_packet as binary_review  # noqa: E402


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

    def invoke_wrapper(self, game_id: str | None, explicit_game: str = "") -> tuple[int, list[str]]:
        self.write_marker(game_id)
        plugin = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        translation = self.workspace / "translated/plugin_exports/TestMod/Test.zh.jsonl"
        plugin.write_bytes(tes4_plugin())
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
            str(self.workspace / "out/TestMod/tool_outputs/Test.esp"),
            "--report-path",
            str(self.workspace / "qa/Test.write.md"),
        ]
        if explicit_game:
            argv.extend(["--game", explicit_game])
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(invoke_tool, "project_root", return_value=self.workspace),
            mock.patch.object(invoke_tool, "plugin_root", return_value=ROOT),
            mock.patch.object(invoke_tool, "dotnet_path", return_value=fake_dotnet),
            mock.patch.object(invoke_tool, "ensure_adapter_dll", return_value=fake_dll),
            mock.patch.object(invoke_tool.subprocess, "run", return_value=completed) as run,
        ):
            code = invoke_tool.main()
        command = list(run.call_args.args[0]) if run.called else []
        return code, command

    def test_wrapper_uses_fallout4_marker_and_passes_required_game(self) -> None:
        code, command = self.invoke_wrapper("fallout4")
        self.assertEqual(code, 0)
        self.assertEqual(command[command.index("--game") + 1], "fallout4")

    def test_wrapper_rejects_explicit_game_conflicting_with_marker(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflict|mismatch"):
            self.invoke_wrapper("fallout4", "skyrim-se")

    def test_wrapper_legacy_marker_defaults_to_skyrim(self) -> None:
        code, command = self.invoke_wrapper(None)
        self.assertEqual(code, 0)
        self.assertEqual(command[command.index("--game") + 1], "skyrim-se")

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
            with self.assertRaisesRegex(RuntimeError, "build failed"):
                invoke_tool.main()
        self.assertFalse(output.exists())

    def test_wrapper_missing_dependencies_remove_stale_output(self) -> None:
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
                    with self.assertRaises(FileNotFoundError):
                        invoke_tool.main()
                self.assertFalse(output.exists())

    def test_exporter_writes_v2_identity_and_fallout_metadata(self) -> None:
        self.write_marker("fallout4")
        plugin = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        payload = subrecord("EDID", b"TestWeapon\x00") + subrecord("FULL", b"Laser Rifle\x00")
        plugin.write_bytes(tes4_plugin(record("WEAP", 0x1234, payload)))
        output = self.workspace / "source/plugin_exports/TestMod/Test.jsonl"
        report = self.workspace / "qa/Test.export.md"
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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        full = next(row for row in rows if row["subrecord_type"] == "FULL")
        self.assertEqual(full["schema_version"], 2)
        self.assertEqual(full["game_id"], "fallout4")
        self.assertEqual(full["field_path"], "Name")
        self.assertEqual(full["writeback"], "supported")
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("game_id: fallout4", report_text)
        self.assertIn("support_level: experimental", report_text)

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
        self.assertIn("support_level: experimental", report_text)

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
        original.write_bytes(tes4_plugin())
        output.write_bytes(tes4_plugin(record("MISC", 1, b"")))
        report = self.workspace / "qa/Test.verify.md"
        writeback_report = self.workspace / "qa/Test.write.md"
        writeback_report.write_text(
            "\n".join(
                [
                    "- game_id: skyrim-se",
                    "- Input plugin: work/extracted_mods/Other.esp",
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
        fingerprints = {"Fixture.esp": "ABC"}
        fallout_context = {
            "game_id": "fallout4",
            "game_profile_version": 1,
            "plugin_adapter": "fallout4-mutagen",
            "plugin_adapter_version": 1,
            "support_level": "experimental",
        }
        skyrim_context = {**fallout_context, "game_id": "skyrim-se", "plugin_adapter": "skyrim-mutagen", "support_level": "stable"}
        binary_review.write_cache(cache, fingerprints, "HASH", fallout_context)
        self.assertTrue(binary_review.cached_packet_is_current(cache, packet, items, fingerprints, fallout_context))
        self.assertFalse(binary_review.cached_packet_is_current(cache, packet, items, fingerprints, skyrim_context))

    def test_skyrim_export_remains_supported_and_stable(self) -> None:
        self.write_marker("skyrim-se")
        plugin = self.workspace / "work/extracted_mods/TestMod/Test.esp"
        payload = subrecord("EDID", b"TestWeapon\x00") + subrecord("FULL", b"Steel Sword\x00")
        plugin.write_bytes(tes4_plugin(record("WEAP", 0x1234, payload)))
        output = self.workspace / "source/plugin_exports/TestMod/Test.jsonl"
        report = self.workspace / "qa/Test.export.md"
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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("game_id: skyrim-se", report.read_text(encoding="utf-8"))
        self.assertIn("support_level: stable", report.read_text(encoding="utf-8"))

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
        self.assertIn("plugin_adapter: fallout4-mutagen", text)
        self.assertIn("plugin_adapter_version: 1", text)
        self.assertIn("support_level: experimental", text)
        self.assertNotIn("game verified", text.lower())

    def test_csharp_contract_and_real_sdk_build(self) -> None:
        dotnet = self.require_dotnet()
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
        build = subprocess.run(
            [str(dotnet), "build", str(PROJECT)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)

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

    def test_skyrim_writer_reparses_temporary_output_only_once(self) -> None:
        source = (ROOT / "adapters/SkyrimPluginTextTool/Program.cs").read_text(encoding="utf-8")
        block = source[source.index("private static void WriteValidateAndCommitSkyrim") : source.index("private static void WriteReport")]
        self.assertEqual(block.count("SkyrimMod.CreateFromBinary"), 1)
        self.assertIn("SkyrimMod.CreateFromBinary(temporaryPlugin", block)
        self.assertNotIn("SkyrimMod.CreateFromBinary(outputPlugin", block)

    def test_adapter_cli_rejects_unknown_game(self) -> None:
        dotnet = self.require_dotnet()
        build = subprocess.run(
            [str(dotnet), "build", str(PROJECT)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        result = subprocess.run(
            [str(dotnet), str(DLL), "apply", "--game", "oblivion"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported game", result.stdout + result.stderr)

    def test_adapter_blocks_fallout4_localized_plugin_before_mutagen_write(self) -> None:
        dotnet = self.require_dotnet()
        build = subprocess.run(
            [str(dotnet), "build", str(PROJECT)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
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

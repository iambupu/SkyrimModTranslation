from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from test_fallout4_plugin_adapter_regressions import DOTNET, record, subrecord, tes4_plugin  # noqa: E402
from dotnet_adapter_cache import ensure_adapter_dll  # noqa: E402
from build_final_mod import source_hash as final_source_hash  # noqa: E402

MOD_NAME = "Classic Holstered Weapons - v1.09-46101-1-09-1779912557"
GAME_KEYS = {
    "game_id",
    "game_profile_version",
    "game_display_name",
    "support_level",
    "plugin_adapter",
    "plugin_adapter_version",
    "pex_category",
    "pex_writeback_status",
    "archive_delivery",
    "archive_allow_repack",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_group(signature: str, *records: bytes) -> bytes:
    payload = b"".join(records)
    return b"GRUP" + struct.pack("<I4sIHHHH", 24 + len(payload), signature.encode("ascii"), 0, 0, 0, 0, 0) + payload


class Fallout4WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_parent = ROOT / ".tmp" / "test-fallout4-workflow-integration"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.tempdir.cleanup)
        self.reset_workspace("workspace")

    def reset_workspace(self, name: str) -> None:
        self.workspace = Path(self.tempdir.name) / name
        self.workspace.mkdir()
        for name in ("mod", "work", "qa", "out", "source", "translated", "glossary", ".workflow", "traces", "config"):
            (self.workspace / name).mkdir(parents=True, exist_ok=True)

    def write_marker(self, game_id: str | None) -> None:
        marker: dict[str, object] = {
            "schema_version": 2 if game_id else 1,
            "kind": "bethesda-mod-chs-translation-workspace" if game_id else "skyrim-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "plugin_root": str(ROOT),
        }
        if game_id:
            marker["game_id"] = game_id
            marker["game_profile"] = game_id
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["SKYRIM_CHS_WORKSPACE_ROOT"] = str(self.workspace)
        env["SKYRIM_CHS_PLUGIN_ROOT"] = str(ROOT)
        env.update(getattr(self, "extra_env", {}))
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            cwd=self.workspace,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def read_json(self, relative: str) -> dict[str, object]:
        return json.loads((self.workspace / relative).read_text(encoding="utf-8-sig"))

    def assert_game_metadata(self, payload: dict[str, object], game_id: str) -> None:
        self.assertTrue(GAME_KEYS.issubset(payload), sorted(GAME_KEYS - set(payload)))
        self.assertEqual(payload["game_id"], game_id)
        if game_id == "fallout4":
            self.assertEqual(payload["game_display_name"], "Fallout 4")
            self.assertEqual(payload["support_level"], "experimental")
            self.assertEqual(payload["plugin_adapter"], "fallout4-mutagen")
            self.assertEqual(payload["pex_category"], "Fallout4")
            self.assertEqual(payload["pex_writeback_status"], "experimental")
            self.assertEqual(payload["archive_delivery"], "loose_override")
            self.assertIs(payload["archive_allow_repack"], False)

    def run_state_chain(self) -> None:
        commands = (
            ("audit_translation_readiness.py",),
            ("write_workflow_state.py",),
            ("write_workflow_tasks.py",),
            ("write_codex_handoff.py",),
            ("write_agent_handoff.py",),
        )
        for command in commands:
            result = self.run_script(*command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write_dictionary(self, mod_name: str = MOD_NAME) -> None:
        dictionary = self.workspace / "out" / mod_name / "lex_dictionary" / "entries.jsonl"
        dictionary.parent.mkdir(parents=True, exist_ok=True)
        dictionary.write_text(
            json.dumps({"source": "Visible text", "target": "可见文本"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def test_marker_identity_flows_through_state_handoff_and_progress_chain(self) -> None:
        cases = (
            (None, "skyrim-se"),
            ("skyrim-se", "skyrim-se"),
            ("fallout4", "fallout4"),
        )
        for marker_game, expected_game in cases:
            with self.subTest(marker_game=marker_game):
                for relative in ("qa", ".workflow", "traces"):
                    directory = self.workspace / relative
                    shutil.rmtree(directory)
                    directory.mkdir()
                self.write_marker(marker_game)
                self.run_state_chain()
                for relative in (
                    "qa/translation_readiness.json",
                    "qa/workflow_state.json",
                    "qa/workflow_tasks.json",
                    "qa/codex_handoff.json",
                    "qa/agent_handoff.json",
                    ".workflow/workflow_state.json",
                    ".workflow/progress_card.json",
                ):
                    self.assert_game_metadata(self.read_json(relative), expected_game)

        card = (self.workspace / ".workflow" / "progress_card.md").read_text(encoding="utf-8")
        self.assertRegex(card, r"^## \[SMT (?:进度|阻断|完成)\]")
        self.assertIn("Fallout 4 (Experimental)", card)

    def test_mismatched_downstream_game_evidence_blocks_state_chain(self) -> None:
        self.write_marker("fallout4")
        stale_report = self.workspace / "qa" / f"{MOD_NAME}.final_binary_review_packet.md"
        stale_report.write_text(
            "\n".join(
                (
                    "# Final Binary Review Packet",
                    "",
                    "- game_id: skyrim-se",
                    "- game_profile_version: 1",
                    "- plugin_adapter: skyrim-mutagen",
                    "- plugin_adapter_version: 1",
                    "- pex_category: Skyrim",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        result = self.run_script("audit_translation_readiness.py", "--mod-name", MOD_NAME)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        readiness = self.read_json("qa/translation_readiness.json")
        self.assertEqual(readiness["OverallStatus"], "blocked")
        issues = readiness.get("Issues", [])
        self.assertTrue(any("game" in str(row).lower() and "mismatch" in str(row).lower() for row in issues), issues)

    def test_progress_from_state_rejects_marker_mismatch_without_overwriting_completion(self) -> None:
        self.write_marker("fallout4")
        self.run_state_chain()
        state_path = self.workspace / "qa" / "workflow_state.json"
        state = self.read_json("qa/workflow_state.json")
        state.update(
            {
                "game_id": "skyrim-se",
                "game_display_name": "Skyrim Special Edition",
                "support_level": "stable",
                "plugin_adapter": "skyrim-mutagen",
                "pex_category": "Skyrim",
                "pex_writeback_status": "stable",
            }
        )
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        progress_state = self.workspace / ".workflow" / "workflow_state.json"
        progress_card = self.workspace / ".workflow" / "progress_card.json"
        before_state = progress_state.read_bytes()
        before_card = progress_card.read_bytes()

        result = self.run_script("workflow_progress.py", "from-state")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(progress_state.read_bytes(), before_state)
        self.assertEqual(progress_card.read_bytes(), before_card)

    def test_agent_handoff_freshness_rejects_marker_change_without_rewrite(self) -> None:
        self.write_marker("fallout4")
        self.run_state_chain()
        handoff_path = self.workspace / "qa" / "agent_handoff.json"
        before = handoff_path.read_bytes()
        self.write_marker("skyrim-se")

        result = self.run_script("write_agent_handoff.py", "--check-freshness")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        freshness = json.loads(result.stdout)
        self.assertFalse(freshness["fresh"])
        self.assertTrue(any(row["reason"] == "game_metadata_mismatch" for row in freshness["reasons"]))
        self.assertEqual(handoff_path.read_bytes(), before)

    def test_classic_name_route_uses_marker_not_filename_guessing(self) -> None:
        path = self.workspace / "mod" / MOD_NAME / "F4SE" / "Plugins" / "ClassicHolsteredWeapons.dll"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"synthetic-dll")
        relative = str(path.relative_to(self.workspace))
        for game_id in ("fallout4", "skyrim-se"):
            with self.subTest(game_id=game_id):
                self.write_marker(game_id)
                result = self.run_script("route_translation_task.py", "--file-path", relative, "--as-json")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["game_id"], game_id)

    def test_fallout4_final_mod_uses_profile_and_binds_metadata_and_hashes(self) -> None:
        self.write_marker("fallout4")
        source = self.workspace / "mod" / MOD_NAME / "Data"
        dll = source / "F4SE" / "Plugins" / "ClassicHolsteredWeapons.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b"synthetic-f4se-dll-not-a-real-mod-binary\x00\x01")
        (source / "Materials").mkdir()
        (source / "Materials" / "classic.bgsm").write_bytes(b"synthetic-material")
        (source / "MCM" / "Config" / "ClassicHolsteredWeapons").mkdir(parents=True)
        (source / "MCM" / "Config" / "ClassicHolsteredWeapons" / "config.json").write_text(
            '{"modName":"Classic Holstered Weapons"}\n', encoding="utf-8"
        )
        self.write_dictionary()
        attempted_dll_overlay = (
            self.workspace
            / "translated"
            / "tool_outputs"
            / MOD_NAME
            / "F4SE"
            / "Plugins"
            / "ClassicHolsteredWeapons.dll"
        )
        attempted_dll_overlay.parent.mkdir(parents=True)
        attempted_dll_overlay.write_bytes(b"attempted-modified-dll")

        original_hash = sha256(dll)
        built = self.run_script(
            "build_final_mod.py",
            "--mod-name",
            MOD_NAME,
            "--source-mod-dir",
            f"mod/{MOD_NAME}",
            "--force",
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        final_dll = final_mod / "F4SE" / "Plugins" / "ClassicHolsteredWeapons.dll"
        self.assertTrue(final_dll.is_file())
        self.assertEqual(sha256(final_dll), original_hash)
        self.assertFalse((final_mod / "Data").exists())

        manifest = self.read_json(f"out/{MOD_NAME}/汉化产出/final_mod/meta/manifest.json")
        self.assert_game_metadata(manifest, "fallout4")
        provenance = [
            json.loads(line)
            for line in (final_mod / "meta" / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(provenance)
        self.assertTrue(all(GAME_KEYS.issubset(row) for row in provenance))

        validated = self.run_script("validate_final_mod.py", "--final-mod-dir", f"out/{MOD_NAME}/汉化产出/final_mod")
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        report = (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8")
        self.assertIn("Fallout 4 (Experimental)", report)
        self.assertNotIn("No common Skyrim Data directories", report)

        manifest_path = final_mod / "meta" / "manifest.json"
        for key, value in (
            ("game_id", "skyrim-se"),
            ("game_profile_version", 999),
            ("plugin_adapter", "skyrim-mutagen"),
            ("pex_category", "Skyrim"),
        ):
            with self.subTest(manifest_metadata=key):
                tampered_manifest = {**manifest, key: value}
                manifest_path.write_text(
                    json.dumps(tampered_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                tampered = self.run_script(
                    "validate_final_mod.py", "--final-mod-dir", f"out/{MOD_NAME}/汉化产出/final_mod"
                )
                self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)
                self.assertIn("Manifest game metadata mismatch", (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        provenance_path = final_mod / "meta" / "provenance.jsonl"
        original_provenance = provenance_path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in original_provenance.splitlines() if line.strip()]
        rows[0]["plugin_adapter"] = "skyrim-mutagen"
        provenance_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
        tampered = self.run_script("validate_final_mod.py", "--final-mod-dir", f"out/{MOD_NAME}/汉化产出/final_mod")
        self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)
        self.assertIn("Provenance game metadata mismatch", (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8"))
        provenance_path.write_text(original_provenance, encoding="utf-8")

    def test_zip_sources_bind_member_hash_for_skyrim_and_fallout4(self) -> None:
        cases = (
            ("skyrim-se", "Meshes/protected.nif", "SKSE/Plugins/Example.dll"),
            ("fallout4", "Materials/protected.bgsm", "F4SE/Plugins/Example.dll"),
        )
        for game_id, protected_entry, dll_entry in cases:
            with self.subTest(game_id=game_id):
                self.reset_workspace(f"zip-{game_id}")
                self.write_marker(game_id)
                archive_path = self.workspace / "mod" / f"{game_id}.zip"
                protected_bytes = f"{game_id}-protected".encode()
                dll_bytes = f"{game_id}-dll".encode()
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(protected_entry, protected_bytes)
                    archive.writestr(dll_entry, dll_bytes)
                self.assertEqual(
                    final_source_hash(self.workspace, f"mod/{game_id}.zip::{protected_entry}"),
                    hashlib.sha256(protected_bytes).hexdigest(),
                )
                self.write_dictionary("ZipMod")

                built = self.run_script(
                    "build_final_mod.py",
                    "--mod-name",
                    "ZipMod",
                    "--source-mod-dir",
                    f"mod/{game_id}.zip",
                    "--force",
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                final_mod = self.workspace / "out" / "ZipMod" / "汉化产出" / "final_mod"
                validated = self.run_script(
                    "validate_final_mod.py", "--final-mod-dir", "out/ZipMod/汉化产出/final_mod"
                )
                self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
                rows = [
                    json.loads(line)
                    for line in (final_mod / "meta" / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                by_file = {row["file"]: row for row in rows}
                for entry, content in ((protected_entry, protected_bytes), (dll_entry, dll_bytes)):
                    row = by_file[f"final_mod/{entry}"]
                    self.assertEqual(row["source_sha256"], hashlib.sha256(content).hexdigest())
                    self.assertEqual(row["source_archive_sha256"], sha256(archive_path))
                delivered_dll = final_mod / Path(dll_entry)
                delivered_dll.write_bytes(b"tampered")
                tampered = self.run_script(
                    "validate_final_mod.py", "--final-mod-dir", "out/ZipMod/汉化产出/final_mod"
                )
                self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)

    def test_strict_qa_summary_carries_fallout4_identity(self) -> None:
        self.write_marker("fallout4")
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        (workspace / "F4SE").mkdir(parents=True)
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        final_mod.mkdir(parents=True)
        result = self.run_script(
            "run_non_gui_qa_gates.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--strict-complete",
        )
        self.assertNotEqual(result.returncode, 0)
        report = self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md"
        self.assertTrue(report.is_file(), result.stdout + result.stderr)
        text = report.read_text(encoding="utf-8")
        self.assertIn("- game_id: fallout4", text)
        self.assertIn("- plugin_adapter: fallout4-mutagen", text)
        self.assertIn("- pex_category: Fallout4", text)
        self.assertIn("- pex_writeback_status: experimental", text)

    @unittest.skipIf(DOTNET is None, "a .NET 8 SDK is required for strict plugin production evidence")
    def test_strict_chain_consumes_plugin_pex_and_ba2_production_evidence(self) -> None:
        self.write_marker("fallout4")
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        workspace.mkdir(parents=True)
        final_mod.mkdir(parents=True)

        plugin_name = "ClassicHolsteredWeapons.esp"
        plugin_payload = subrecord("EDID", b"ClassicWeapon\x00") + subrecord("FULL", b"Classic Weapon\x00")
        weapon_record = record("WEAP", 0x1234, plugin_payload)
        (workspace / plugin_name).write_bytes(tes4_plugin(record_group("WEAP", weapon_record)))
        plugin_translation = self.workspace / "translated" / "plugin_exports" / MOD_NAME / f"{plugin_name}_strings.zh.jsonl"
        plugin_translation.parent.mkdir(parents=True)
        plugin_translation.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "game_id": "fallout4",
                    "plugin": plugin_name,
                    "record_type": "WEAP",
                    "form_id": "00001234",
                    "editor_id": "ClassicWeapon",
                    "field_path": "Name",
                    "subrecord_type": "FULL",
                    "subrecord_index": 1,
                    "source": "Classic Weapon",
                    "target": "经典武器",
                    "risk": "candidate",
                    "writeback": "supported",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        adapter_dll = ensure_adapter_dll(self.workspace, ROOT, DOTNET, "SkyrimPluginTextTool")
        applied = subprocess.run(
            [
                str(DOTNET),
                str(adapter_dll),
                "apply",
                "--game",
                "fallout4",
                "--project-root",
                str(self.workspace),
                "--input-plugin",
                str(workspace / plugin_name),
                "--translation-jsonl",
                str(plugin_translation),
                "--output-plugin",
                str(final_mod / plugin_name),
                "--report",
                str(self.workspace / "qa" / f"{plugin_name}.integration_apply.md"),
            ],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

        original_pex = workspace / "Scripts" / "ClassicHolsteredWeapons.pex"
        original_pex.parent.mkdir(parents=True)
        original_pex.write_bytes(b"synthetic-pex-original")
        final_pex = final_mod / "Scripts" / original_pex.name
        final_pex.parent.mkdir(parents=True)
        final_pex.write_bytes(original_pex.read_bytes())
        pex_translation = self.workspace / "work" / "normalized" / MOD_NAME / "pex_apply" / "ClassicHolsteredWeapons.translation.jsonl"
        pex_translation.parent.mkdir(parents=True)
        pex_translation.write_text(
            json.dumps({"Source": "Visible", "Target": "可见", "ModName": original_pex.name}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        (workspace / "ClassicHolsteredWeapons - Main.ba2").write_bytes(b"BTDX-synthetic-without-manifest")

        result = self.run_script(
            "run_non_gui_qa_gates.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--strict-complete",
        )
        self.assertNotEqual(result.returncode, 0)
        strict_report = (self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md").read_text(encoding="utf-8")
        plugin_report = self.workspace / "qa" / f"{plugin_name}.gate_plugin_output_verification.md"
        pex_report = self.workspace / "qa" / f"{MOD_NAME}.pex_delivery_post_build.md"
        archive_report = self.workspace / "qa" / f"{MOD_NAME}.archive_coverage.md"
        self.assertTrue(plugin_report.is_file(), result.stdout + result.stderr)
        self.assertIn("No blocking issues.", plugin_report.read_text(encoding="utf-8"))
        self.assertTrue(pex_report.is_file())
        self.assertRegex(pex_report.read_text(encoding="utf-8"), r"- Blocking issues: [1-9]")
        self.assertTrue(archive_report.is_file())
        self.assertRegex(archive_report.read_text(encoding="utf-8"), r"- Archives (?:missing|invalid) evidence: [1-9]")
        self.assertIn("- Final plugins checked: 1", strict_report)
        self.assertIn("- Final PEX files checked: 1", strict_report)
        self.assertNotIn("| error | plugin-output |", strict_report)
        self.assertIn("| error | pex-delivery |", strict_report)
        self.assertIn("| error | archive-coverage |", strict_report)

    def test_localized_fallout4_strings_are_blocked_by_strict_chain(self) -> None:
        self.write_marker("fallout4")
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        strings = workspace / "Strings" / "ClassicHolsteredWeapons_en.strings"
        strings.parent.mkdir(parents=True)
        strings.write_bytes(b"synthetic-localized-string-table")
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        final_mod.mkdir(parents=True)

        result = self.run_script(
            "run_non_gui_qa_gates.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--strict-complete",
        )
        self.assertNotEqual(result.returncode, 0)
        report = self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md"
        text = report.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?i)(localized|STRINGS).*(blocked|unsupported)|blocked.*(localized|STRINGS)")


if __name__ == "__main__":
    unittest.main()

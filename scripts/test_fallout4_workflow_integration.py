from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
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


class Fallout4WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_parent = ROOT / ".tmp" / "test-fallout4-workflow-integration"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
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
        dictionary = self.workspace / "out" / MOD_NAME / "lex_dictionary" / "entries.jsonl"
        dictionary.parent.mkdir(parents=True)
        dictionary.write_text(
            json.dumps({"source": "Visible text", "target": "可见文本"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
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

        manifest["game_id"] = "skyrim-se"
        (final_mod / "meta" / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tampered = self.run_script("validate_final_mod.py", "--final-mod-dir", f"out/{MOD_NAME}/汉化产出/final_mod")
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

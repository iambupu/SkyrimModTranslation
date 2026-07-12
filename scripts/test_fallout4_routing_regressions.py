from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_non_gui_coverage  # noqa: E402
import build_external_glossary_matches  # noqa: E402
import extract_mcm_text  # noqa: E402
import extract_non_gui_candidates  # noqa: E402
import init_workspace  # noqa: E402
import detect_mod_files  # noqa: E402
import route_translation_task  # noqa: E402
import run_translation_queue  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class Fallout4RoutingRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "mod").mkdir()
        (self.root / "work" / "extracted_mods").mkdir(parents=True)
        (self.root / "out").mkdir()
        (self.root / "qa").mkdir()
        (self.root / "glossary" / "lextranslator_dynamic_dictionaries").mkdir(parents=True)

    def write_workspace_marker(self, game_id: str) -> None:
        marker = {
            "schema_version": 2,
            "kind": "bethesda-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "game_id": game_id,
            "game_profile": game_id,
        }
        (self.root / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def env(self, *, plugin_root: Path = ROOT) -> mock._patch_dict:
        return mock.patch.dict(
            os.environ,
            {
                "SKYRIM_CHS_WORKSPACE_ROOT": str(self.root),
                "SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root),
            },
            clear=False,
        )

    def create_plugin_fixture(self) -> Path:
        plugin_root = self.root / "plugin-root"
        (plugin_root / "config" / "game_profiles").mkdir(parents=True, exist_ok=True)
        (plugin_root / "config" / "mcm_schemas").mkdir(parents=True, exist_ok=True)
        for relative in (
            "config/game_profiles/skyrim-se.json",
            "config/game_profiles/fallout4.json",
            "config/mcm_schemas/skyrim-se.json",
            "config/mcm_schemas/fallout4.json",
        ):
            target = plugin_root / relative
            target.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
        return plugin_root

    def run_init_workspace(self, workspace: Path, *args: str) -> None:
        argv = ["init_workspace.py", str(workspace), "--tool-setup", "skip", "--skip-initial-state", *args]
        with self.env(), mock.patch.object(sys, "argv", argv):
            exit_code = init_workspace.main()
        self.assertEqual(exit_code, 0)

    def test_route_payload_includes_game_metadata_for_plugin_and_pex_files(self) -> None:
        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                self.write_workspace_marker(game_id)
                esp_path = self.root / "mod" / "Example.esp"
                pex_path = self.root / "mod" / "Example.pex"
                esp_path.write_bytes(b"")
                pex_path.write_bytes(b"")
                with self.env():
                    esp_payload = route_translation_task.route_payload(route_translation_task.route_for(self.root, esp_path))
                    pex_payload = route_translation_task.route_payload(route_translation_task.route_for(self.root, pex_path))
                self.assertEqual(esp_payload["game_id"], game_id)
                self.assertEqual(pex_payload["game_id"], game_id)

    def test_strings_routes_differ_between_skyrim_and_fallout4(self) -> None:
        cases = {
            "skyrim-se": ("tool-mediated", ""),
            "fallout4": ("blocked", "missing string-table adapter"),
        }
        extensions = (".strings", ".dlstrings", ".ilstrings")
        for game_id, (status, blocked_reason) in cases.items():
            with self.subTest(game_id=game_id):
                self.write_workspace_marker(game_id)
                with self.env():
                    for extension in extensions:
                        path = self.root / "mod" / f"dialog{extension}"
                        path.write_bytes(b"placeholder")
                        payload = route_translation_task.route_payload(route_translation_task.route_for(self.root, path))
                        self.assertEqual(payload["skill"], "localized-string-table-translation")
                        self.assertEqual(payload["status"], status)
                        self.assertEqual(payload["blocked_reason"], blocked_reason)
                for extension in extensions:
                    (self.root / "mod" / f"dialog{extension}").unlink()

    def test_fallout4_routes_ba2_and_protected_binaries(self) -> None:
        self.write_workspace_marker("fallout4")
        cases = {
            "archive.ba2": "skills/ba2-archive-audit",
            "menu.swf": "manual-review",
            "scaleform.gfx": "manual-review",
            "plugin.dll": "manual-review",
        }
        with self.env():
            for name, skill in cases.items():
                path = self.root / "mod" / name
                path.write_bytes(b"placeholder")
                payload = route_translation_task.route_payload(route_translation_task.route_for(self.root, path))
                self.assertEqual(payload["skill"], skill)
                if path.suffix.lower() == ".ba2":
                    self.assertEqual(payload["status"], "ready")
                    self.assertEqual(payload["blocked_reason"], "")
                    self.assertIn("read-only", payload["auxiliary_tool"].lower())

    def test_queue_ba2_uses_dedicated_skill_and_blocks_only_without_ready_adapter(self) -> None:
        self.write_workspace_marker("fallout4")
        archive = self.root / "mod" / "Example.ba2"
        archive.write_bytes(b"synthetic-ba2")
        row = {"LikelyModName": "Example", "Path": "mod/Example.ba2"}

        with self.env():
            blocked, issue = run_translation_queue.run_prepare(self.root, row, False)
        self.assertEqual(blocked.Skill, "skills/ba2-archive-audit")
        self.assertEqual(blocked.Status, "blocked")
        self.assertIsNotNone(issue)
        self.assertIn("controlled BA2 adapter", " ".join(blocked.Output))
        self.assertNotIn("future", " ".join(blocked.Output).lower())

        adapter = self.root / "tools" / "ba2_adapter.py"
        adapter.parent.mkdir()
        adapter.write_text("# controlled adapter fixture\n", encoding="utf-8")
        config = self.root / "config" / "tools.local.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps(
                {
                    "DecoderTools": {
                        "Ba2ExtractorPath": "tools/ba2_adapter.py",
                        "Ba2ExtractorProtocol": route_translation_task.ADAPTER_PROTOCOL,
                    }
                }
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, list[str]]] = []

        def fake_run(_root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append((script_name, args))
            return subprocess.CompletedProcess([script_name, *args], 0, "verified", "")

        with self.env(), mock.patch.object(run_translation_queue, "run_python_script", side_effect=fake_run):
            ready, issue = run_translation_queue.run_prepare(self.root, row, False)
        self.assertEqual(ready.Skill, "skills/ba2-archive-audit")
        self.assertEqual(ready.Status, "passed")
        self.assertIsNone(issue)
        self.assertEqual(calls[0][0], "invoke_ba2_extractor_safe.py")
        self.assertIn("--archive-path", calls[0][1])

    def test_detect_ba2_reports_profile_route_and_materialization_readiness(self) -> None:
        self.write_workspace_marker("fallout4")
        archive = self.root / "mod" / "Example.ba2"
        archive.write_bytes(b"synthetic-ba2")
        report = self.root / "qa" / "inventory.md"

        with self.env():
            detect_mod_files.write_inventory(self.root, self.root / "mod", report, [archive])
        blocked_text = report.read_text(encoding="utf-8")
        self.assertIn("skills/ba2-archive-audit", blocked_text)
        self.assertIn("BA2 materialization adapter: blocked", blocked_text)
        self.assertNotIn("future", blocked_text.lower())

        adapter = self.root / "tools" / "ba2_adapter.py"
        adapter.parent.mkdir()
        adapter.write_text("# controlled adapter fixture\n", encoding="utf-8")
        config = self.root / "config" / "tools.local.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps(
                {
                    "DecoderTools": {
                        "Ba2ExtractorPath": "tools/ba2_adapter.py",
                        "Ba2ExtractorProtocol": route_translation_task.ADAPTER_PROTOCOL,
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.env():
            detect_mod_files.write_inventory(self.root, self.root / "mod", report, [archive])
        ready_text = report.read_text(encoding="utf-8")
        self.assertIn("BA2 materialization adapter: ready", ready_text)

    def test_extract_non_gui_candidates_blocks_fallout4_string_tables_without_text_decoding(self) -> None:
        self.write_workspace_marker("fallout4")
        mod_name = "Fallout4Sample"
        workspace_dir = self.root / "work" / "extracted_mods" / mod_name / "Strings"
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "Fallout4_en.strings").write_bytes(b"Hello from string table")
        with self.env(), mock.patch.object(
            sys,
            "argv",
            ["extract_non_gui_candidates.py", "--mod-name", mod_name],
        ):
            exit_code = extract_non_gui_candidates.main()
        self.assertEqual(exit_code, 0)
        all_rows = read_jsonl(self.root / "out" / mod_name / "non_gui_exports" / "all_string_observations.jsonl")
        candidate_rows = read_jsonl(self.root / "out" / mod_name / "non_gui_exports" / "translation_candidates.jsonl")
        self.assertTrue(any(row["kind"] == "localized-string-table-blocker" for row in all_rows))
        self.assertFalse(any("Hello from string table" in str(row.get("source", "")) for row in all_rows))
        self.assertTrue(any(row["kind"] == "localized-string-table-blocker" for row in candidate_rows))

    def test_extract_non_gui_candidates_skips_skyrim_string_table_payload_decode(self) -> None:
        self.write_workspace_marker("skyrim-se")
        mod_name = "SkyrimStrings"
        workspace_dir = self.root / "work" / "extracted_mods" / mod_name / "Strings"
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "Skyrim_english.strings").write_bytes(b"Dragonborn Whiterun payload")
        with self.env(), mock.patch.object(
            sys,
            "argv",
            ["extract_non_gui_candidates.py", "--mod-name", mod_name],
        ):
            exit_code = extract_non_gui_candidates.main()
        self.assertEqual(exit_code, 0)
        all_rows = read_jsonl(self.root / "out" / mod_name / "non_gui_exports" / "all_string_observations.jsonl")
        self.assertTrue(any(row["kind"] == "localized-string-table-tool-handoff" for row in all_rows))
        self.assertFalse(any("Dragonborn Whiterun payload" in str(row.get("source", "")) for row in all_rows))

    def test_audit_non_gui_coverage_counts_string_table_blockers_as_unverified(self) -> None:
        self.write_workspace_marker("fallout4")
        mod_name = "CoverageBlocker"
        export_dir = self.root / "out" / mod_name / "non_gui_exports"
        export_dir.mkdir(parents=True)
        blocker_row = {
            "file": f"work/extracted_mods/{mod_name}/Strings/Fallout4_en.strings",
            "source": "",
            "target": "",
            "kind": "localized-string-table-blocker",
            "risk": "blocking",
            "reason": "missing-string-table-adapter",
            "game_id": "fallout4",
        }
        (export_dir / "translation_candidates.jsonl").write_text(
            json.dumps(blocker_row, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        final_mod_dir = self.root / "out" / mod_name / "汉化产出" / "final_mod"
        final_mod_dir.mkdir(parents=True)
        with self.env(), mock.patch.object(
            sys,
            "argv",
            ["audit_non_gui_coverage.py", "--mod-name", mod_name],
        ):
            exit_code = audit_non_gui_coverage.main()
        self.assertEqual(exit_code, 0)
        unverified = read_jsonl(self.root / "out" / mod_name / "qa" / "non_gui_unverified_candidates.jsonl")
        self.assertEqual(len(unverified), 1)
        self.assertEqual(unverified[0]["coverage_reason"], "blocking-missing-string-table-adapter")
        report = (self.root / "out" / mod_name / "qa" / "non_gui_translation_coverage.md").read_text(encoding="utf-8")
        self.assertIn("- Unverified: 1", report)

    def test_fallout4_mcm_schema_extracts_only_whitelisted_fields(self) -> None:
        self.write_workspace_marker("fallout4")
        input_dir = self.root / "mod" / "Fo4Mod" / "MCM"
        input_dir.mkdir(parents=True)
        payload = {
            "title": "Main title",
            "text": "Main text",
            "label": "Visible label",
            "description": "Long description",
            "tooltip": "Hover help",
            "help": "Detailed help",
            "displayName": "Skyrim-only legacy field",
            "id": "MenuRoot",
            "key": "RootKey",
            "setting": "SettingName",
            "script": "QuestScript",
            "function": "HandleOpen",
            "plugin": "Fallout4.esm",
            "path": "Interface/Menu.swf",
            "type": "toggle",
            "default": "1",
        }
        (input_dir / "config.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.env(), mock.patch.object(
            sys,
            "argv",
            ["extract_mcm_text.py", "--input-path", "mod/Fo4Mod/MCM", "--mod-name", "Fo4Mod"],
        ):
            exit_code = extract_mcm_text.main()
        self.assertEqual(exit_code, 0)
        rows = read_jsonl(self.root / "work" / "normalized" / "Fo4Mod" / "mcm_text_candidates.jsonl")
        keys = {row["key"] for row in rows}
        self.assertEqual(keys, {"title", "text", "label", "description", "tooltip", "help"})
        self.assertEqual({row["game_id"] for row in rows}, {"fallout4"})

    def test_invalid_mcm_schema_fails_closed(self) -> None:
        self.write_workspace_marker("fallout4")
        plugin_root = self.create_plugin_fixture()
        (plugin_root / "config" / "mcm_schemas" / "fallout4.json").write_text(
            json.dumps({"schema_version": 2, "game_id": "fallout4"}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        input_dir = self.root / "mod" / "BrokenFo4" / "MCM"
        input_dir.mkdir(parents=True)
        (input_dir / "config.json").write_text('{"title":"Hello"}\n', encoding="utf-8")
        with self.env(plugin_root=plugin_root), mock.patch.object(
            sys,
            "argv",
            ["extract_mcm_text.py", "--input-path", "mod/BrokenFo4/MCM", "--mod-name", "BrokenFo4"],
        ):
            with self.assertRaises(ValueError):
                extract_mcm_text.main()

    def test_skyrim_mcm_output_remains_compatible(self) -> None:
        self.write_workspace_marker("skyrim-se")
        input_dir = self.root / "mod" / "SkyrimMod" / "MCM"
        input_dir.mkdir(parents=True)
        payload = {
            "displayName": "Legacy display name",
            "desc": "Legacy description",
            "title": "Menu title",
            "scriptName": "InternalScript",
        }
        (input_dir / "config.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.env(), mock.patch.object(
            sys,
            "argv",
            ["extract_mcm_text.py", "--input-path", "mod/SkyrimMod/MCM", "--mod-name", "SkyrimMod"],
        ):
            exit_code = extract_mcm_text.main()
        self.assertEqual(exit_code, 0)
        rows = read_jsonl(self.root / "work" / "normalized" / "SkyrimMod" / "mcm_text_candidates.jsonl")
        keys = {row["key"] for row in rows}
        self.assertEqual(keys, {"displayName", "desc", "title"})
        self.assertEqual({row["game_id"] for row in rows}, {"skyrim-se"})

    def test_real_fallout4_workspace_glossary_match_is_isolated_then_mod_terms_override(self) -> None:
        workspace = self.root / "fo4-workspace"
        self.run_init_workspace(workspace, "--game", "fallout4")
        input_dir = workspace / "work" / "normalized" / "Fo4Terms"
        input_dir.mkdir(parents=True)
        source_path = input_dir / "dialogue.txt"
        source_path.write_text("Whiterun and Institute and Commonwealth.\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            },
            clear=False,
        ), mock.patch.object(
            sys,
            "argv",
            [
                "build_external_glossary_matches.py",
                "--mod-name",
                "Fo4Terms",
                "--input-path",
                "work/normalized/Fo4Terms/dialogue.txt",
            ],
        ):
            exit_code = build_external_glossary_matches.main()
        self.assertEqual(exit_code, 0)
        rows = read_jsonl(workspace / "work" / "glossary_matches" / "Fo4Terms" / "external_glossary_matches.jsonl")
        by_source = {row["Source"]: row for row in rows}
        self.assertIn("Institute", by_source)
        self.assertNotIn("Whiterun", by_source)

        mod_terms = workspace / "glossary" / "mod_terms.md"
        mod_terms.write_text(
            mod_terms.read_text(encoding="utf-8").rstrip("\n")
            + "\n| Commonwealth | 联邦-模组优先 | 自定义优先 |\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {
                "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            },
            clear=False,
        ), mock.patch.object(
            sys,
            "argv",
            [
                "build_external_glossary_matches.py",
                "--mod-name",
                "Fo4Terms",
                "--input-path",
                "work/normalized/Fo4Terms/dialogue.txt",
                "--rebuild-index",
            ],
        ):
            exit_code = build_external_glossary_matches.main()
        self.assertEqual(exit_code, 0)
        rows = read_jsonl(workspace / "work" / "glossary_matches" / "Fo4Terms" / "external_glossary_matches.jsonl")
        by_source = {row["Source"]: row for row in rows}
        self.assertEqual(by_source["Commonwealth"]["Target"], "联邦-模组优先")

    def test_ba2_materialization_requires_profile_capability_even_with_ready_adapter(self) -> None:
        archive = self.root / "mod" / "Capability.ba2"
        archive.write_bytes(b"synthetic-ba2")
        adapter = self.root / "tools" / "ba2_adapter.py"
        adapter.parent.mkdir()
        adapter.write_text("# controlled adapter fixture\n", encoding="utf-8")
        config = self.root / "config" / "tools.local.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps(
                {
                    "DecoderTools": {
                        "Ba2ExtractorPath": "tools/ba2_adapter.py",
                        "Ba2ExtractorProtocol": route_translation_task.ADAPTER_PROTOCOL,
                    }
                }
            ),
            encoding="utf-8",
        )
        row = {"LikelyModName": "Capability", "Path": "mod/Capability.ba2"}

        self.write_workspace_marker("fallout4")
        with self.env():
            self.assertTrue(route_translation_task.ba2_adapter_ready(self.root))

        self.write_workspace_marker("skyrim-se")
        calls: list[tuple[str, list[str]]] = []

        def fake_run(_root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append((script_name, args))
            return subprocess.CompletedProcess([script_name, *args], 0, "unexpected", "")

        with self.env(), mock.patch.object(run_translation_queue, "run_python_script", side_effect=fake_run):
            self.assertFalse(route_translation_task.ba2_adapter_ready(self.root))
            route = route_translation_task.route_for(self.root, archive)
            item, issue = run_translation_queue.run_prepare(self.root, row, False)
        self.assertEqual(route.status, "ready")
        self.assertIn("inventory only", route.notes.lower())
        self.assertEqual(item.Status, "blocked")
        self.assertIsNotNone(issue)
        self.assertEqual(calls, [])

        report = self.root / "qa" / "inventory.md"
        with self.env():
            detect_mod_files.write_inventory(self.root, self.root / "mod", report, [archive])
        self.assertIn("BA2 materialization adapter: inventory-only", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

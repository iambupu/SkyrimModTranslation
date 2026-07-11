from __future__ import annotations

import json
import os
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
import route_translation_task  # noqa: E402


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

    def test_fallout4_routes_ba2_string_tables_and_protected_binaries(self) -> None:
        self.write_workspace_marker("fallout4")
        cases = {
            "archive.ba2": ("skills/bsa-archive-audit", None, None),
            "dialog.strings": ("localized-string-table-translation", "blocked", "missing string-table adapter"),
            "menu.swf": ("manual-review", None, None),
            "scaleform.gfx": ("manual-review", None, None),
            "plugin.dll": ("manual-review", None, None),
        }
        with self.env():
            for name, (skill, status, blocked_reason) in cases.items():
                path = self.root / "mod" / name
                path.write_bytes(b"placeholder")
                payload = route_translation_task.route_payload(route_translation_task.route_for(self.root, path))
                self.assertEqual(payload["skill"], skill)
                if status is not None:
                    self.assertEqual(payload["status"], status)
                if blocked_reason is not None:
                    self.assertEqual(payload["blocked_reason"], blocked_reason)

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

    def test_fallout4_glossary_priority_uses_mod_terms_then_current_game_base_glossary(self) -> None:
        self.write_workspace_marker("fallout4")
        (self.root / "glossary" / "mod_terms.md").write_text(
            "\n".join(
                [
                    "# Mod Terms",
                    "",
                    "| English | 简体中文 |",
                    "|---|---|",
                    "| Commonwealth | 联邦-模组优先 |",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / "glossary" / "fallout4_cn_glossary.md").write_text(
            "\n".join(
                [
                    "# Fallout 4 基础术语表",
                    "",
                    "| English | 简体中文 |",
                    "| --- | --- |",
                    "| Commonwealth | 联邦 |",
                    "| Institute | 学院 |",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / "glossary" / "skyrim_cn_glossary.md").write_text(
            "\n".join(
                [
                    "# Skyrim CN Glossary",
                    "",
                    "| English | 简体中文 |",
                    "| --- | --- |",
                    "| Dragonborn | 龙裔 |",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / "glossary" / "lextranslator_dynamic_dictionaries" / "seed.txt").write_text(
            "1|1|1|1|Commonwealth|联邦-动态\n",
            encoding="utf-8",
        )
        input_dir = self.root / "work" / "normalized" / "Fo4Terms"
        input_dir.mkdir(parents=True)
        (input_dir / "dialogue.txt").write_text(
            "The Commonwealth belongs to the Institute, not the Dragonborn.\n",
            encoding="utf-8",
        )
        with self.env(), mock.patch.object(
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
        rows = read_jsonl(self.root / "work" / "glossary_matches" / "Fo4Terms" / "external_glossary_matches.jsonl")
        by_source = {row["Source"]: row for row in rows}
        self.assertEqual(by_source["Commonwealth"]["Target"], "联邦-模组优先")
        self.assertEqual(by_source["Institute"]["Target"], "学院")
        self.assertNotIn("Dragonborn", by_source)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import init_workspace  # noqa: E402
import project_paths  # noqa: E402


def load_game_context_module():
    return importlib.import_module("game_context")


def profile_payload(
    *,
    game_id: str,
    display_name: str,
    mutagen_release: str,
    pex_category: str,
    glossary_path: str,
    archive_extensions: list[str],
    string_table_extensions: list[str],
    data_directories: list[str],
    protected_directories: list[str],
    risky_paths: list[str],
    supports_localized_plugins: bool,
    string_tables_enabled: bool,
    pex_writeback_status: str,
    support_level: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "game_id": game_id,
        "display_name": display_name,
        "mutagen_release": mutagen_release,
        "pex_category": pex_category,
        "plugin_extensions": [".esp", ".esm", ".esl"],
        "archive_extensions": archive_extensions,
        "string_table_extensions": string_table_extensions,
        "data_directories": data_directories,
        "protected_directories": protected_directories,
        "risky_paths": risky_paths,
        "glossary_path": glossary_path,
        "supports_localized_plugins": supports_localized_plugins,
        "string_tables_enabled": string_tables_enabled,
        "pex_export_supported": True,
        "pex_writeback_status": pex_writeback_status,
        "interface_translation_encoding": "utf-16-le-bom",
        "archive_default_delivery": "loose_override",
        "archive_allow_repack": False,
        "support_level": support_level,
    }


class GameProfileRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.temp_root = Path(self.tempdir.name)

    def create_plugin_fixture(self, *, fallout_glossary_name: str = "fallout4_cn_glossary.md") -> Path:
        plugin_root = self.temp_root / "plugin-root"
        (plugin_root / "config" / "game_profiles").mkdir(parents=True, exist_ok=True)
        (plugin_root / "glossary" / "lextranslator_dynamic_dictionaries").mkdir(parents=True, exist_ok=True)
        (plugin_root / "config" / "tools.example.json").write_text("{}\n", encoding="utf-8")
        (plugin_root / "glossary" / "lex_dictionary_notes.md").write_text("notes\n", encoding="utf-8")
        (plugin_root / "glossary" / "mod_terms.md").write_text("mod terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "mod_terms.template.md").write_text("template terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "skyrim_cn_glossary.md").write_text("skyrim terms\n", encoding="utf-8")
        (plugin_root / "glossary" / fallout_glossary_name).write_text("fallout terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "lextranslator_dynamic_dictionaries" / "seed.txt").write_text(
            "dynamic\n",
            encoding="utf-8",
        )

        skyrim_profile = profile_payload(
            game_id="skyrim-se",
            display_name="Skyrim Special Edition",
            mutagen_release="SkyrimSE",
            pex_category="Skyrim",
            glossary_path="glossary/skyrim_cn_glossary.md",
            archive_extensions=[".bsa", ".ba2"],
            string_table_extensions=[".strings", ".dlstrings", ".ilstrings"],
            data_directories=["interface", "scripts", "skse", "meshes", "textures", "sound", "seq", "mcm"],
            protected_directories=["meshes", "textures", "sound"],
            risky_paths=["Skyrim Special Edition\\Data", "SteamLibrary"],
            supports_localized_plugins=True,
            string_tables_enabled=True,
            pex_writeback_status="stable",
            support_level="stable",
        )
        fallout_profile = profile_payload(
            game_id="fallout4",
            display_name="Fallout 4",
            mutagen_release="Fallout4",
            pex_category="Fallout4",
            glossary_path=f"glossary/{fallout_glossary_name}",
            archive_extensions=[".ba2"],
            string_table_extensions=[".strings", ".dlstrings", ".ilstrings"],
            data_directories=[
                "interface",
                "scripts",
                "f4se",
                "mcm",
                "strings",
                "meshes",
                "textures",
                "materials",
                "sound",
                "video",
            ],
            protected_directories=["meshes", "textures", "materials", "sound", "video"],
            risky_paths=["Fallout 4\\Data", "SteamLibrary"],
            supports_localized_plugins=False,
            string_tables_enabled=False,
            pex_writeback_status="experimental",
            support_level="experimental",
        )
        (plugin_root / "config" / "game_profiles" / "skyrim-se.json").write_text(
            json.dumps(skyrim_profile, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (plugin_root / "config" / "game_profiles" / "fallout4.json").write_text(
            json.dumps(fallout_profile, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return plugin_root

    def run_init_workspace(self, plugin_root: Path, workspace: Path, *args: str) -> None:
        argv = ["init_workspace.py", str(workspace), "--tool-setup", "skip", "--skip-initial-state", *args]
        with (
            mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False),
            mock.patch.object(init_workspace, "PROJECT_ROOT", plugin_root),
            mock.patch.object(sys, "argv", argv),
        ):
            exit_code = init_workspace.main()
        self.assertEqual(exit_code, 0)

    def test_real_repo_profile_values_match_brief(self) -> None:
        game_context = load_game_context_module()
        skyrim = game_context.load_game_profile("skyrim-se")
        fallout4 = game_context.load_game_profile("fallout4")

        self.assertEqual(skyrim.archive_default_delivery, "loose_override")
        self.assertEqual(fallout4.archive_default_delivery, "loose_override")
        self.assertEqual(skyrim.support_level, "stable")
        self.assertEqual(fallout4.support_level, "experimental")
        self.assertEqual(
            skyrim.protected_directories,
            frozenset({"meshes", "textures", "sound"}),
        )
        self.assertEqual(
            fallout4.protected_directories,
            frozenset({"meshes", "textures", "materials", "sound", "video"}),
        )
        self.assertEqual(
            fallout4.string_table_extensions,
            frozenset({".strings", ".dlstrings", ".ilstrings"}),
        )
        self.assertFalse(fallout4.string_tables_enabled)
        self.assertEqual(skyrim.interface_translation_encoding, "utf-16-le-bom")
        self.assertEqual(fallout4.interface_translation_encoding, "utf-16-le-bom")

    def test_real_fallout4_glossary_contains_required_terms(self) -> None:
        glossary_path = ROOT / "glossary" / "fallout4_cn_glossary.md"
        self.assertTrue(glossary_path.is_file())
        text = glossary_path.read_text(encoding="utf-8")
        for expected in (
            "S.P.E.C.I.A.L.",
            "V.A.T.S.",
            "Pip-Boy",
            "Commonwealth",
            "Institute",
            "Railroad",
            "Minutemen",
            "Brotherhood of Steel",
            "Synth",
            "Power Armor",
            "Workshop",
            "Settlement",
            "Sole Survivor",
            "Fusion Core",
            "Stimpak",
            "RadAway",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_parse_args_defaults_to_skyrim_se(self) -> None:
        with mock.patch.object(sys, "argv", ["init_workspace.py"]):
            args = init_workspace.parse_args()
        self.assertEqual(args.game, "skyrim-se")

    def test_help_and_output_call_out_fallout4_experimental_support(self) -> None:
        help_output = io.StringIO()
        with mock.patch.object(sys, "argv", ["init_workspace.py", "--help"]):
            with redirect_stdout(help_output), self.assertRaises(SystemExit):
                init_workspace.parse_args()
        self.assertIn("Experimental Support", help_output.getvalue())
        plugin_root = self.create_plugin_fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            self.run_init_workspace(plugin_root, self.temp_root / "help-output", "--game", "fallout4")
        self.assertIn("Experimental Support", output.getvalue())

    def test_load_game_profile_returns_expected_skyrim_defaults(self) -> None:
        game_context = load_game_context_module()
        context = game_context.load_game_profile("skyrim-se")
        self.assertEqual(context.game_id, "skyrim-se")
        self.assertEqual(context.mutagen_release, "SkyrimSE")
        self.assertIn(".bsa", context.archive_extensions)
        self.assertEqual(context.pex_category, "Skyrim")
        self.assertEqual(context.support_level, "stable")
        with self.assertRaises(Exception):
            context.game_id = "fallout4"

    def test_unknown_interface_encoding_policy_is_rejected(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "fallout4.json"
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        payload["interface_translation_encoding"] = "unknown-runtime-encoding"
        profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "interface_translation_encoding"):
                game_context.load_game_profile("fallout4")

    def test_load_game_context_falls_back_to_legacy_skyrim_marker(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "legacy-workspace"
        workspace.mkdir()
        marker = {
            "schema_version": 1,
            "kind": "skyrim-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "plugin_root": str(plugin_root),
        }
        (workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            context = game_context.load_game_context(workspace)
        self.assertEqual(context.game_id, "skyrim-se")
        self.assertEqual(context.glossary_path, plugin_root / "glossary" / "skyrim_cn_glossary.md")

    def test_marker_game_identity_rejects_explicit_invalid_or_conflicting_values(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "invalid-marker-workspace"
        workspace.mkdir()
        game_context = load_game_context_module()
        invalid_markers = (
            {"game_id": None},
            {"game_id": ""},
            {"game_id": "   "},
            {"game_id": 4},
            {"game_id": "unknown-game"},
            {"game_id": "fallout4", "game_profile": None},
            {"game_id": "fallout4", "game_profile": ""},
            {"game_id": "fallout4", "game_profile": 4},
            {"game_id": "fallout4", "game_profile": "skyrim-se"},
            {"game_profile": "fallout4"},
        )
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            for marker in invalid_markers:
                with self.subTest(marker=marker):
                    (workspace / ".skyrim-chs-workspace.json").write_text(
                        json.dumps(marker, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        game_context.load_game_context(workspace)

            (workspace / ".skyrim-chs-workspace.json").write_text(
                json.dumps({"game_profile": "skyrim-se"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(game_context.load_game_context(workspace).game_id, "skyrim-se")

    def test_init_workspace_writes_fallout4_v2_marker(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "fo4-workspace"
        self.run_init_workspace(plugin_root, workspace, "--game", "fallout4")
        marker = json.loads((workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["schema_version"], 2)
        self.assertEqual(marker["kind"], "bethesda-mod-chs-translation-workspace")
        self.assertEqual(marker["game_id"], "fallout4")
        self.assertEqual(marker["game_profile"], "fallout4")

    def test_unknown_game_profile_is_rejected(self) -> None:
        game_context = load_game_context_module()
        with self.assertRaisesRegex(ValueError, "Unsupported game id"):
            game_context.load_game_profile("oblivion")

    def test_invalid_or_malicious_profile_is_rejected(self) -> None:
        plugin_root = self.create_plugin_fixture()
        malicious = profile_payload(
            game_id="skyrim-se",
            display_name="Skyrim Special Edition",
            mutagen_release="SkyrimSE",
            pex_category="Skyrim",
            glossary_path="../outside.md",
            archive_extensions=[".bsa", ".ba2"],
            string_table_extensions=[".strings"],
            data_directories=["interface", "scripts"],
            protected_directories=["meshes"],
            risky_paths=["Skyrim Special Edition\\Data"],
            supports_localized_plugins=True,
            string_tables_enabled=True,
            pex_writeback_status="stable",
            support_level="stable",
        )
        (plugin_root / "config" / "game_profiles" / "skyrim-se.json").write_text(
            json.dumps(malicious, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaises(ValueError):
                game_context.load_game_profile("skyrim-se")

    def test_has_data_root_markers_and_find_data_root_support_both_games(self) -> None:
        plugin_root = self.create_plugin_fixture()
        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            skyrim = game_context.load_game_profile("skyrim-se")
            fallout4 = game_context.load_game_profile("fallout4")

        skyrim_root = self.temp_root / "skyrim-mod" / "Data"
        (skyrim_root / "Scripts").mkdir(parents=True)
        fallout_root = self.temp_root / "fallout-mod" / "Data"
        (fallout_root / "F4SE").mkdir(parents=True)

        self.assertTrue(project_paths.has_data_root_markers(skyrim_root))
        self.assertEqual(project_paths.find_data_root(skyrim_root.parent), skyrim_root)
        self.assertFalse(project_paths.has_data_root_markers(fallout_root))
        self.assertTrue(project_paths.has_data_root_markers(fallout_root, context=fallout4))
        self.assertEqual(project_paths.find_data_root(fallout_root.parent, context=fallout4), fallout_root)

    def test_risky_marker_can_use_fallout4_profile(self) -> None:
        plugin_root = self.create_plugin_fixture()
        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            fallout4 = game_context.load_game_profile("fallout4")
        self.assertEqual(project_paths.risky_marker(r"C:\Games\Fallout 4\Data"), "")
        self.assertEqual(project_paths.risky_marker(r"C:\Games\Fallout 4\Data", context=fallout4), r"Fallout 4\Data")

    def test_fallout4_init_copies_only_fallout_glossary_seed(self) -> None:
        plugin_root = self.create_plugin_fixture()
        (plugin_root / "glossary" / "mod_terms.template.md").write_text("template terms\n", encoding="utf-8")
        workspace = self.temp_root / "fallout-seed"
        self.run_init_workspace(plugin_root, workspace, "--game", "fallout4")
        self.assertTrue((workspace / "glossary" / "mod_terms.md").is_file())
        self.assertTrue((workspace / "glossary" / "fallout4_cn_glossary.md").is_file())
        self.assertTrue((workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertFalse((workspace / "glossary" / "skyrim_cn_glossary.md").exists())
        self.assertTrue((workspace / "glossary" / "lextranslator_dynamic_dictionaries").is_dir())
        self.assertEqual(list((workspace / "glossary" / "lextranslator_dynamic_dictionaries").iterdir()), [])
        self.assertEqual((workspace / "glossary" / "mod_terms.md").read_text(encoding="utf-8"), "template terms\n")

    def test_default_skyrim_init_remains_compatible(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "skyrim-default"
        self.run_init_workspace(plugin_root, workspace)
        marker = json.loads((workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["plugin_name"], "skyrim-mod-chs-translation")
        self.assertEqual(marker["game_id"], "skyrim-se")
        self.assertTrue((workspace / "glossary" / "skyrim_cn_glossary.md").is_file())
        self.assertFalse((workspace / "glossary" / "fallout4_cn_glossary.md").exists())
        self.assertTrue((workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertTrue((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "seed.txt").is_file())

    def test_skyrim_init_excludes_other_game_glossary_derived_from_profiles(self) -> None:
        plugin_root = self.create_plugin_fixture(fallout_glossary_name="fo4_alt_terms.md")
        workspace = self.temp_root / "skyrim-isolation"
        self.run_init_workspace(plugin_root, workspace)
        self.assertTrue((workspace / "glossary" / "skyrim_cn_glossary.md").is_file())
        self.assertFalse((workspace / "glossary" / "fo4_alt_terms.md").exists())
        self.assertTrue((workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertTrue((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "seed.txt").is_file())

    def test_missing_current_game_glossary_fails_initialization(self) -> None:
        plugin_root = self.create_plugin_fixture()
        (plugin_root / "glossary" / "mod_terms.template.md").write_text("template terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "fallout4_cn_glossary.md").unlink()
        argv = [
            "init_workspace.py",
            str(self.temp_root / "missing-glossary"),
            "--tool-setup",
            "skip",
            "--skip-initial-state",
            "--game",
            "fallout4",
        ]
        with (
            mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False),
            mock.patch.object(init_workspace, "PROJECT_ROOT", plugin_root),
            mock.patch.object(sys, "argv", argv),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "fallout4_cn_glossary.md"):
                init_workspace.main()

    def test_missing_fallout4_mod_terms_template_fails_initialization(self) -> None:
        plugin_root = self.create_plugin_fixture()
        (plugin_root / "glossary" / "mod_terms.template.md").unlink()
        argv = [
            "init_workspace.py",
            str(self.temp_root / "missing-template"),
            "--tool-setup",
            "skip",
            "--skip-initial-state",
            "--game",
            "fallout4",
        ]
        with (
            mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False),
            mock.patch.object(init_workspace, "PROJECT_ROOT", plugin_root),
            mock.patch.object(sys, "argv", argv),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "mod_terms.template.md"):
                init_workspace.main()

    def test_replaced_plugin_root_uses_single_authoritative_root(self) -> None:
        plugin_root = self.create_plugin_fixture()
        (plugin_root / "glossary" / "mod_terms.template.md").write_text("template terms\n", encoding="utf-8")
        workspace = self.temp_root / "alternate-plugin-root"
        self.run_init_workspace(plugin_root, workspace, "--game", "fallout4")
        self.assertTrue((workspace / "glossary" / "fallout4_cn_glossary.md").is_file())

    def test_real_repo_init_smoke_for_both_games(self) -> None:
        skyrim_workspace = self.temp_root / "real-skyrim"
        fallout_workspace = self.temp_root / "real-fallout"
        self.run_init_workspace(ROOT, skyrim_workspace)
        self.run_init_workspace(ROOT, fallout_workspace, "--game", "fallout4")
        self.assertTrue((skyrim_workspace / "glossary" / "skyrim_cn_glossary.md").is_file())
        self.assertFalse((skyrim_workspace / "glossary" / "fallout4_cn_glossary.md").exists())
        self.assertTrue((skyrim_workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertTrue((skyrim_workspace / "glossary" / "lextranslator_dynamic_dictionaries").is_dir())
        self.assertTrue((skyrim_workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "重光SSE词库1.2.txt").is_file())
        self.assertTrue((fallout_workspace / "glossary" / "fallout4_cn_glossary.md").is_file())
        self.assertFalse((fallout_workspace / "glossary" / "skyrim_cn_glossary.md").exists())
        self.assertTrue((fallout_workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        fallout_mod_terms = (fallout_workspace / "glossary" / "mod_terms.md").read_text(encoding="utf-8")
        self.assertNotIn("Whiterun", fallout_mod_terms)
        self.assertNotIn("Dragonborn", fallout_mod_terms)
        self.assertNotIn("Skyrim", fallout_mod_terms)


if __name__ == "__main__":
    unittest.main()

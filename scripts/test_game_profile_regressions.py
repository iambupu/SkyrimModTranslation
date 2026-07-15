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
from subprocess import CompletedProcess
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import init_workspace  # noqa: E402
import audit_translation_readiness as readiness_audit  # noqa: E402
import build_external_glossary_matches as glossary_matches  # noqa: E402
import ci_validate_repo  # noqa: E402
import export_esp_strings as esp_exporter  # noqa: E402
import project_paths  # noqa: E402
import run_non_gui_translation_workflow as non_gui_workflow  # noqa: E402
import test_workflow_health as workflow_health  # noqa: E402


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
    glossary_game_dir = "skyrim" if game_id == "skyrim-se" else game_id
    glossary_sources: list[dict[str, object]] = [
        {
            "path": glossary_path,
            "format": "markdown",
            "consumers": ["rag"],
            "recommended": True,
        },
        {
            "path": f"glossary/lextranslator_dynamic_dictionaries/{glossary_game_dir}",
            "format": "lextranslator-text",
            "consumers": ["rag", "lextranslator"],
            "recommended": True,
        },
    ]
    if game_id == "fallout4":
        glossary_sources.extend(
            [
                {
                    "path": "glossary/eet/fallout4",
                    "format": "eet",
                    "consumers": ["rag", "esp-esm-translator"],
                    "recommended": True,
                },
                {
                    "path": "glossary/sst/fallout4",
                    "format": "sst",
                    "consumers": ["rag", "xtranslator"],
                    "recommended": True,
                },
            ]
        )
    archive_capabilities: dict[str, dict[str, object]] = {
        "archive.bsa": {
            "level": "read_only" if game_id == "skyrim-se" else "unsupported",
            "adapter": "bethesda-bsa",
        },
        "archive.ba2": {
            "level": (
                "read_only"
                if game_id == "fallout4"
                else "inventory_only"
                if game_id == "skyrim-se"
                else "unsupported"
            ),
            "adapter": "bethesda-ba2",
        },
    }
    for extension in archive_extensions:
        capability_name = f"archive{extension.lower()}"
        archive_capabilities.setdefault(
            capability_name,
            {"level": "inventory_only", "adapter": "bethesda-ba2"},
        )
    pex_level = {
        "stable": "stable",
        "experimental": "experimental_write",
        "blocked": "unsupported",
    }[pex_writeback_status]
    pex_spec: dict[str, object] = {
        "level": pex_level,
        "adapter": "mutagen-pex",
    }
    if pex_level != "unsupported":
        pex_spec["options"] = {"pex_category": pex_category}
    return {
        "schema_version": 2,
        "game_id": game_id,
        "display_name": display_name,
        "format_families": {
            "plugin": "creation-engine-plugin",
            "pex": f"papyrus-pex-{pex_category.casefold()}",
            "archive": "bsa" if ".bsa" in archive_extensions else "ba2-general",
            "loose_text": "creation-engine-loose-text",
            "string_tables": "bethesda-string-tables",
        },
        "capabilities": {
            "plugin_text": {
                "level": "stable" if support_level == "stable" else "experimental_write",
                "adapter": "mutagen-bethesda-plugin",
                "options": {
                    "adapter_contract_version": "1",
                    "mutagen_release": mutagen_release,
                    "extract_backend": (
                        "builtin-tes4-parser"
                        if supports_localized_plugins
                        else "mutagen-adapter"
                    ),
                    "localized_plugin_policy": (
                        "allow" if supports_localized_plugins else "block"
                    ),
                },
            },
            "pex": pex_spec,
            **archive_capabilities,
            "loose_text": {"level": "stable", "adapter": "loose-text"},
            "string_tables": {
                "level": "stable" if string_tables_enabled else "unsupported",
                "adapter": "bethesda-string-tables",
            },
        },
        "plugin_extensions": [".esp", ".esm", ".esl"],
        "string_table_extensions": string_table_extensions,
        "data_directories": data_directories,
        "protected_directories": protected_directories,
        "risky_paths": risky_paths,
        "glossary_path": glossary_path,
        "glossary_sources": glossary_sources,
        "interface_translation_encoding": "utf-16-le-bom",
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
        (plugin_root / "glossary" / "lextranslator_dynamic_dictionaries" / "skyrim").mkdir(parents=True, exist_ok=True)
        (plugin_root / "glossary" / "lextranslator_dynamic_dictionaries" / "fallout4").mkdir(parents=True, exist_ok=True)
        (plugin_root / "glossary" / "eet" / "fallout4").mkdir(parents=True, exist_ok=True)
        (plugin_root / "glossary" / "sst" / "fallout4").mkdir(parents=True, exist_ok=True)
        (plugin_root / "config" / "tools.example.json").write_text("{}\n", encoding="utf-8")
        (plugin_root / "glossary" / "lex_dictionary_notes.md").write_text("notes\n", encoding="utf-8")
        (plugin_root / "glossary" / "mod_terms.md").write_text("mod terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "mod_terms.template.md").write_text("template terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "skyrim_cn_glossary.md").write_text("skyrim terms\n", encoding="utf-8")
        (plugin_root / "glossary" / fallout_glossary_name).write_text("fallout terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "lextranslator_dynamic_dictionaries" / "skyrim" / "seed.txt").write_text(
            "skyrim dynamic\n",
            encoding="utf-8",
        )
        (plugin_root / "glossary" / "lextranslator_dynamic_dictionaries" / "fallout4" / "seed.txt").write_text(
            "fallout dynamic\n",
            encoding="utf-8",
        )
        (plugin_root / "glossary" / "eet" / "fallout4" / "seed.eet").write_bytes(b"fixture")
        (plugin_root / "glossary" / "sst" / "fallout4" / "seed.sst").write_bytes(b"fixture")

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

    def test_game_agnostic_core_guard_rejects_literal_and_direct_conditions(self) -> None:
        path = self.temp_root / "concrete_game_branch.py"
        path.write_text(
            "def choose(context):\n"
            "    if context.game_id == 'fallout4':\n"
            "        return 1\n"
            "    if context.game_id:\n"
            "        return 2\n"
            "    if context.game_id and ready:\n"
            "        return 3\n",
            encoding="utf-8",
        )

        findings = ci_validate_repo.game_specific_branch_findings(path)

        self.assertTrue(any("compared with a literal" in finding for finding in findings))
        direct_conditions = [
            finding for finding in findings if "used directly as a condition" in finding
        ]
        self.assertEqual(len(direct_conditions), 2)

    def test_game_agnostic_core_guard_rejects_match_tables_and_adapter_dispatch(self) -> None:
        path = self.temp_root / "hidden_game_dispatch.py"
        path.write_text(
            "def choose(context, localized_flag_mode, adapter_id):\n"
            "    table = {'skyrim-se': 1, 'fallout4': 2}\n"
            "    if localized_flag_mode == 'fallout4':\n"
            "        return table['fallout4']\n"
            "    if adapter_id == 'special-adapter':\n"
            "        return 3\n"
            "    match context.game_id:\n"
            "        case 'skyrim-se':\n"
            "            return 4\n",
            encoding="utf-8",
        )

        findings = ci_validate_repo.game_specific_branch_findings(path)

        self.assertTrue(any("dispatch-table key" in finding for finding in findings))
        self.assertTrue(any("concrete game id used for dispatch" in finding for finding in findings))
        self.assertTrue(any("adapter id compared" in finding for finding in findings))
        self.assertTrue(any("match dispatch" in finding for finding in findings))

    def test_game_agnostic_core_guard_tracks_aliases_and_container_tables(self) -> None:
        path = self.temp_root / "aliased_game_dispatch.py"
        path.write_text(
            "SPECIAL_GAMES = ('skyrim-se', 'fallout4')\n"
            "def choose(context, adapter_name):\n"
            "    gid = context.game_id\n"
            "    if gid in SPECIAL_GAMES:\n"
            "        return 1\n"
            "    if adapter_name == 'special-adapter':\n"
            "        return 2\n",
            encoding="utf-8",
        )

        findings = ci_validate_repo.game_specific_branch_findings(path)

        self.assertTrue(any("dispatch table" in finding for finding in findings))
        self.assertTrue(any("adapter id compared" in finding for finding in findings))

    def test_game_agnostic_core_guard_tracks_normalized_aliases_and_frozensets(self) -> None:
        path = self.temp_root / "normalized_selector_dispatch.py"
        path.write_text(
            "SPECIAL_GAMES = frozenset({'skyrim-se', 'fallout4'})\n"
            "def choose(context, adapter_id):\n"
            "    gid = context.game_id.casefold()\n"
            "    normalized_adapter = adapter_id.strip().casefold()\n"
            "    if gid in SPECIAL_GAMES:\n"
            "        return 1\n"
            "    if normalized_adapter == 'special-adapter':\n"
            "        return 2\n",
            encoding="utf-8",
        )

        findings = ci_validate_repo.game_specific_branch_findings(path)

        self.assertTrue(any("dispatch table" in finding for finding in findings))
        self.assertTrue(any("adapter id compared" in finding for finding in findings))

    def test_game_agnostic_core_guard_rejects_direct_game_alias_condition(self) -> None:
        path = self.temp_root / "direct_game_alias.py"
        path.write_text(
            "def choose(context):\n"
            "    gid = context.game_id\n"
            "    if gid:\n"
            "        return 1\n",
            encoding="utf-8",
        )

        findings = ci_validate_repo.game_specific_branch_findings(path)

        self.assertTrue(any("used directly as a condition" in finding for finding in findings))

    def test_game_agnostic_core_guard_allows_data_flow_and_marker_checks(self) -> None:
        path = self.temp_root / "capability_driven.py"
        path.write_text(
            "def route(context, marker_game, adapter):\n"
            "    if marker_game != context.game_id:\n"
            "        return None\n"
            "    return adapter(context.game_id)\n",
            encoding="utf-8",
        )

        self.assertEqual(ci_validate_repo.game_specific_branch_findings(path), [])

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

        self.assertEqual(skyrim.schema_version, 2)
        self.assertEqual(fallout4.schema_version, 2)
        self.assertEqual(skyrim.format_families["plugin"], "creation-engine-plugin")
        self.assertEqual(fallout4.format_families["archive"], "ba2-general")
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
        self.assertFalse(fallout4.capability_at_least("string_tables", "read_only"))
        self.assertEqual(skyrim.interface_translation_encoding, "utf-16-le-bom")
        self.assertEqual(fallout4.interface_translation_encoding, "utf-16-le-bom")
        self.assertTrue(skyrim.can_materialize_archive(".bsa"))
        self.assertFalse(skyrim.can_materialize_archive(".ba2"))
        self.assertTrue(fallout4.can_materialize_archive(".ba2"))
        self.assertFalse(fallout4.can_repack_archive(".ba2"))
        self.assertEqual(skyrim.capability_write_status("pex"), "stable")
        self.assertEqual(fallout4.capability_write_status("pex"), "experimental")
        self.assertEqual(
            skyrim.require_capability("plugin_text").adapter_id,
            skyrim.capabilities["plugin_text"].adapter_id,
        )
        self.assertEqual(
            fallout4.require_capability("plugin_text").adapter_id,
            fallout4.capabilities["plugin_text"].adapter_id,
        )
        self.assertEqual(
            skyrim.capabilities["string_tables"].adapter_id,
            "bethesda-string-tables",
        )
        self.assertEqual(
            fallout4.capabilities["string_tables"].adapter_id,
            "bethesda-string-tables",
        )

    def test_real_fallout4_glossary_contains_recommended_terms(self) -> None:
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

    def test_parse_args_leaves_game_unselected_when_omitted(self) -> None:
        with mock.patch.object(sys, "argv", ["init_workspace.py"]):
            args = init_workspace.parse_args()
        self.assertIsNone(args.game)

    def test_explicit_game_does_not_prompt(self) -> None:
        with mock.patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            selected = init_workspace.resolve_game_selection("skyrim-se")

        self.assertEqual(selected, "skyrim-se")

    def test_omitted_game_requires_interactive_selection_and_confirmation(self) -> None:
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        with (
            mock.patch.object(init_workspace.sys, "stdin", stdin),
            mock.patch("builtins.input", side_effect=["2", "yes"]) as prompt,
        ):
            selected = init_workspace.resolve_game_selection(None)

        self.assertEqual(selected, "fallout4")
        self.assertEqual(prompt.call_count, 2)

    def test_omitted_game_fails_before_initialization_when_noninteractive(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "noninteractive-no-game"
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        argv = [
            "init_workspace.py",
            str(workspace),
            "--tool-setup",
            "skip",
            "--skip-initial-state",
        ]
        with (
            mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False),
            mock.patch.object(init_workspace, "PROJECT_ROOT", plugin_root),
            mock.patch.object(init_workspace.sys, "stdin", stdin),
            mock.patch.object(sys, "argv", argv),
            self.assertRaisesRegex(SystemExit, "--game"),
        ):
            init_workspace.main()

        self.assertFalse(workspace.exists())

    def test_rejected_game_confirmation_is_cancelled(self) -> None:
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        with (
            mock.patch.object(init_workspace.sys, "stdin", stdin),
            mock.patch("builtins.input", side_effect=["1", "no"]) as prompt,
            self.assertRaisesRegex(SystemExit, "cancelled"),
        ):
            init_workspace.resolve_game_selection(None)

        self.assertEqual(prompt.call_count, 2)

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
        self.assertEqual(
            context.capability_option_text("plugin_text", "mutagen_release"),
            "SkyrimSE",
        )
        self.assertIn(".bsa", context.archive_extensions_at_least("inventory_only"))
        self.assertEqual(context.capability_option_text("pex", "pex_category"), "Skyrim")
        self.assertEqual(context.support_level, "stable")
        with self.assertRaises(Exception):
            context.game_id = "fallout4"

    def test_schema_v1_profiles_are_rejected(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        payload["schema_version"] = 1
        profile_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "schema_version.*2"):
                game_context.load_game_profile("skyrim-se")

    def test_schema_version_rejects_bool_non_integer_and_unknown_versions(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        game_context = load_game_context_module()

        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            for invalid_version in (True, False, 1.0, "1", 0, 3):
                with self.subTest(schema_version=invalid_version):
                    payload = json.loads(
                        (ROOT / "config" / "game_profiles" / "skyrim-se.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    payload["schema_version"] = invalid_version
                    profile_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "schema_version"):
                        game_context.load_game_profile("skyrim-se")

    def test_support_level_rejects_unknown_values(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        game_context = load_game_context_module()
        payload = json.loads(
            (ROOT / "config" / "game_profiles" / "skyrim-se.json").read_text(
                encoding="utf-8"
            )
        )

        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            payload["support_level"] = "blocked"
            profile_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "support_level"):
                game_context.load_game_profile("skyrim-se")

    def test_v2_rejects_noncanonical_archive_capability_name(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        payload = json.loads((ROOT / "config" / "game_profiles" / "skyrim-se.json").read_text(encoding="utf-8"))
        payload["capabilities"]["archive.BSA"] = payload["capabilities"].pop("archive.bsa")
        profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "archive.*canonical"):
                game_context.load_game_profile("skyrim-se")

    def test_v2_rejects_casefold_colliding_archive_capability_names(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        payload = json.loads((ROOT / "config" / "game_profiles" / "skyrim-se.json").read_text(encoding="utf-8"))
        payload["capabilities"]["archive.BSA"] = dict(payload["capabilities"]["archive.bsa"])
        profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "duplicate.*archive"):
                game_context.load_game_profile("skyrim-se")

    def test_v2_rejects_archive_capability_names_with_whitespace_aliases(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        game_context = load_game_context_module()

        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            for alias in (" archive.bsa ", " archive.BSA "):
                with self.subTest(alias=alias):
                    payload = json.loads(
                        (ROOT / "config" / "game_profiles" / "skyrim-se.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    payload["capabilities"][alias] = dict(
                        payload["capabilities"]["archive.bsa"]
                    )
                    profile_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "capability.*whitespace"):
                        game_context.load_game_profile("skyrim-se")

    def test_v2_rejects_non_archive_capability_name_with_whitespace(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        payload = json.loads(
            (ROOT / "config" / "game_profiles" / "skyrim-se.json").read_text(
                encoding="utf-8"
            )
        )
        payload["capabilities"][" loose_text "] = payload["capabilities"].pop("loose_text")
        profile_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "capability.*whitespace"):
                game_context.load_game_profile("skyrim-se")

    def test_schema_v2_rejects_invalid_capability_level(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        payload = json.loads((ROOT / "config" / "game_profiles" / "skyrim-se.json").read_text(encoding="utf-8"))
        payload["capabilities"]["plugin_text"]["level"] = "beta"
        profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "capability.*level"):
                game_context.load_game_profile("skyrim-se")

    def test_schema_v2_rejects_all_removed_top_level_fields(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "fallout4.json"
        source_profile = ROOT / "config" / "game_profiles" / "fallout4.json"
        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            for field in sorted(game_context.REMOVED_PROFILE_FIELDS):
                with self.subTest(field=field):
                    payload = json.loads(source_profile.read_text(encoding="utf-8"))
                    payload[field] = False
                    profile_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, f"removed.*{field}"):
                        game_context.load_game_profile("fallout4")

    def test_schema_v2_queries_capability_values_directly(self) -> None:
        game_context = load_game_context_module()
        skyrim = game_context.load_game_profile("skyrim-se")
        fallout4 = game_context.load_game_profile("fallout4")

        self.assertTrue(skyrim.capability_at_least("string_tables", "read_only"))
        self.assertFalse(fallout4.capability_at_least("string_tables", "read_only"))
        self.assertEqual(skyrim.archive_extensions_at_least("read_only"), frozenset({".bsa"}))
        self.assertEqual(fallout4.archive_extensions_at_least("read_only"), frozenset({".ba2"}))
        self.assertEqual(
            skyrim.capability_option_positive_int("plugin_text", "adapter_contract_version"),
            1,
        )
        self.assertEqual(
            fallout4.capability_option_positive_int("plugin_text", "adapter_contract_version"),
            1,
        )

    def test_profile_discovery_and_adapter_identity_do_not_fall_back_to_skyrim(self) -> None:
        plugin_root = self.create_plugin_fixture()
        future_glossary = plugin_root / "glossary" / "future_game_terms.md"
        future_glossary.write_text("future terms\n", encoding="utf-8")
        future_profile = profile_payload(
            game_id="future-game",
            display_name="Future Bethesda Game",
            mutagen_release="FutureRelease",
            pex_category="none",
            glossary_path="glossary/future_game_terms.md",
            archive_extensions=[".futurearchive"],
            string_table_extensions=[],
            data_directories=["interface"],
            protected_directories=[],
            risky_paths=["Future Game\\Data"],
            supports_localized_plugins=False,
            string_tables_enabled=False,
            pex_writeback_status="blocked",
            support_level="experimental",
        )
        (plugin_root / "config" / "game_profiles" / "future-game.json").write_text(
            json.dumps(future_profile, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            self.assertIn("future-game", game_context.supported_game_ids())
            context = game_context.load_game_profile("future-game")
            self.assertEqual(
                context.require_capability("plugin_text").adapter_id,
                "mutagen-bethesda-plugin",
            )
            self.assertFalse(context.capability_at_least("pex", "read_only"))
            self.assertFalse(context.can_materialize_archive(".futurearchive"))

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

    def test_removed_archive_extension_field_is_rejected(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "fallout4.json"
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        payload["archive_materialization_extensions"] = [".ba2"]
        profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "removed.*archive_materialization_extensions"):
                game_context.load_game_profile("fallout4")

    def test_load_game_context_rejects_marker_without_game_id(self) -> None:
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
            with self.assertRaisesRegex(ValueError, "missing required game_id"):
                game_context.load_game_context(workspace)

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

    def test_resolve_workspace_context_uses_marker_and_rejects_explicit_conflict(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "marked-fallout-workspace"
        workspace.mkdir()
        (workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps({"game_id": "fallout4", "game_profile": "fallout4"}) + "\n",
            encoding="utf-8",
        )
        game_context = load_game_context_module()

        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            self.assertEqual(
                game_context.resolve_workspace_game_context(workspace).game_id,
                "fallout4",
            )
            with self.assertRaisesRegex(ValueError, "conflicts with workspace marker"):
                game_context.resolve_workspace_game_context(workspace, "skyrim-se")

    def test_export_context_requires_marker_or_explicit_game(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "export-unmarked-workspace"
        workspace.mkdir()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(SystemExit, "Workspace marker is required"):
                esp_exporter.resolve_game_context(workspace, "")

    def test_export_context_accepts_explicit_fallout4_without_marker(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "export-explicit-workspace"
        workspace.mkdir()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            context = esp_exporter.resolve_game_context(workspace, "fallout4")

        self.assertEqual(context.game_id, "fallout4")

    def test_export_context_rejects_explicit_game_conflicting_with_marker(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "export-conflicting-workspace"
        workspace.mkdir()
        (workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps({"game_id": "fallout4", "game_profile": "fallout4"}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(SystemExit, "conflicts with workspace marker"):
                esp_exporter.resolve_game_context(workspace, "skyrim-se")

    def test_export_context_uses_fallout4_marker_without_explicit_game(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "export-marked-workspace"
        workspace.mkdir()
        (workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps({"game_id": "fallout4", "game_profile": "fallout4"}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            context = esp_exporter.resolve_game_context(workspace, "")

        self.assertEqual(context.game_id, "fallout4")

    def test_export_context_rejects_marker_without_game_id(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "export-invalid-marker-workspace"
        workspace.mkdir()
        (workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps({"game_profile": "fallout4"}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(SystemExit, "missing required game_id"):
                esp_exporter.resolve_game_context(workspace, "")

    def test_explicit_game_without_marker_loads_only_requested_profile(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "unmarked-workspace"
        workspace.mkdir()
        skyrim_profile = plugin_root / "config" / "game_profiles" / "skyrim-se.json"
        game_context = load_game_context_module()

        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            skyrim_profile.write_text("{ damaged json", encoding="utf-8")
            with self.subTest(default_profile="damaged"):
                self.assertEqual(
                    game_context.resolve_workspace_game_context(workspace, "fallout4").game_id,
                    "fallout4",
                )

            with self.assertRaisesRegex(ValueError, "Workspace marker is required"):
                game_context.resolve_workspace_game_context(workspace)

            skyrim_profile.unlink()
            with self.subTest(default_profile="missing"):
                self.assertEqual(
                    game_context.resolve_workspace_game_context(workspace, "fallout4").game_id,
                    "fallout4",
                )

    def test_legacy_required_glossary_field_is_rejected(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "fallout4.json"
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        source = payload["glossary_sources"][0]
        source.pop("recommended")
        source["required"] = True
        profile_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        game_context = load_game_context_module()
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            with self.assertRaisesRegex(ValueError, "required.*removed"):
                game_context.load_game_profile("fallout4")

    def test_recommended_glossary_sources_may_all_be_absent(self) -> None:
        plugin_root = self.create_plugin_fixture()
        profile_path = plugin_root / "config" / "game_profiles" / "fallout4.json"
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        for source in payload["glossary_sources"]:
            source_path = plugin_root / source["path"]
            if source_path.is_dir():
                for item in source_path.iterdir():
                    item.unlink()
                source_path.rmdir()
            else:
                source_path.unlink()

        workspace = self.temp_root / "workspace-without-dictionaries"
        self.run_init_workspace(plugin_root, workspace, "--game", "fallout4")

        marker = json.loads(
            (workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8")
        )
        self.assertTrue(all(source["recommended"] is True for source in marker["glossary_sources"]))
        self.assertTrue(all("required" not in source for source in marker["glossary_sources"]))
        self.assertEqual(
            glossary_matches.default_glossary_paths(workspace),
            ["glossary/mod_terms.md"],
        )

    def test_repository_profiles_mark_every_glossary_source_recommended(self) -> None:
        for profile_path in sorted((ROOT / "config" / "game_profiles").glob("*.json")):
            with self.subTest(profile=profile_path.name):
                payload = json.loads(profile_path.read_text(encoding="utf-8"))
                sources = payload["glossary_sources"]
                self.assertTrue(sources)
                self.assertTrue(all(source["recommended"] is True for source in sources))
                self.assertTrue(all("required" not in source for source in sources))

    def test_optional_dictionary_stage_failure_is_a_non_blocking_warning(self) -> None:
        steps = []
        issues = []
        failed = CompletedProcess([], 1, stdout="dictionary decode failed\n", stderr="")

        with mock.patch.object(non_gui_workflow, "run_python_script", return_value=failed):
            result = non_gui_workflow.run_stage(
                self.temp_root,
                steps,
                issues,
                "refresh-lextranslator-dictionary-rag-index",
                "build_lextranslator_dictionary_rag_index.py",
                [],
                "qa/lextranslator_dictionary_rag_index.md",
                required=False,
                failure_severity="warning",
            )

        self.assertTrue(result)
        self.assertEqual(steps[-1].Status, "failed")
        self.assertEqual(issues[-1].Severity, "warning")

    def test_game_metadata_is_required_for_every_workspace(self) -> None:
        game_context = load_game_context_module()
        skyrim = game_context.load_game_profile("skyrim-se")
        fallout4 = game_context.load_game_profile("fallout4")

        expected = [f"missing {key}" for key in game_context.GAME_METADATA_KEYS]
        self.assertEqual(game_context.game_metadata_mismatches({}, skyrim), expected)
        self.assertEqual(game_context.game_metadata_mismatches({}, fallout4), expected)

    def test_game_metadata_mismatches_are_type_sensitive(self) -> None:
        game_context = load_game_context_module()
        skyrim = game_context.load_game_profile("skyrim-se")
        fallout4 = game_context.load_game_profile("fallout4")

        skyrim_metadata = game_context.game_context_metadata(skyrim)
        skyrim_metadata["game_profile_version"] = True
        self.assertEqual(
            game_context.game_metadata_mismatches(skyrim_metadata, skyrim),
            ["game_profile_version: expected 2, found True"],
        )

        fallout4_metadata = game_context.game_context_metadata(fallout4)
        fallout4_metadata["support_level"] = 1
        self.assertEqual(
            game_context.game_metadata_mismatches(fallout4_metadata, fallout4),
            ["support_level: expected 'experimental', found 1"],
        )

    def test_fallout4_readiness_rejects_known_evidence_without_metadata(self) -> None:
        workspace = self.temp_root / "fallout4-metadata-less-evidence"
        qa = workspace / "qa"
        qa.mkdir(parents=True)
        (qa / "workflow_health.md").write_text("# Legacy workflow health\n", encoding="utf-8")
        context = load_game_context_module().load_game_profile("fallout4")

        issues = readiness_audit.collect_game_identity_issues(workspace, context)

        self.assertTrue(issues)
        self.assertTrue(any("missing game_id" in issue.Message for issue in issues), issues)

    def test_fallout4_readiness_rejects_nonempty_jsonl_without_object_metadata(self) -> None:
        workspace = self.temp_root / "fallout4-invalid-jsonl-evidence"
        evidence = workspace / "source" / "plugin_exports" / "TestMod" / "Test.esp.jsonl"
        evidence.parent.mkdir(parents=True)
        context = load_game_context_module().load_game_profile("fallout4")

        for content in ("{not-json}\n", "42\n", "[]\n", "{}\n"):
            with self.subTest(content=content):
                evidence.write_text(content, encoding="utf-8")

                issues = readiness_audit.collect_game_identity_issues(workspace, context)

                self.assertTrue(issues)
                self.assertTrue(any("missing game_id" in issue.Message for issue in issues), issues)

    def test_fallout4_readiness_rejects_invalid_rows_mixed_with_valid_metadata(self) -> None:
        workspace = self.temp_root / "fallout4-mixed-jsonl-evidence"
        evidence = workspace / "source" / "plugin_exports" / "TestMod" / "Test.esp.jsonl"
        evidence.parent.mkdir(parents=True)
        game_context = load_game_context_module()
        context = game_context.load_game_profile("fallout4")
        valid = json.dumps(game_context.game_context_metadata(context))

        for invalid in ("{not-json}", "42", "[]"):
            with self.subTest(invalid=invalid):
                evidence.write_text(f"{valid}\n{invalid}\n", encoding="utf-8")

                issues = readiness_audit.collect_game_identity_issues(workspace, context)

                self.assertTrue(any("missing game_id" in issue.Message for issue in issues), issues)

    def test_skyrim_readiness_rejects_metadata_less_jsonl(self) -> None:
        workspace = self.temp_root / "skyrim-invalid-jsonl-evidence"
        evidence = workspace / "source" / "plugin_exports" / "TestMod" / "Test.esp.jsonl"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("{not-json}\n42\n[]\n", encoding="utf-8")
        context = load_game_context_module().load_game_profile("skyrim-se")

        issues = readiness_audit.collect_game_identity_issues(workspace, context)

        self.assertTrue(issues)
        self.assertTrue(any("missing game_id" in issue.Message for issue in issues), issues)

    def test_workflow_health_writes_complete_fallout4_metadata(self) -> None:
        workspace = self.temp_root / "fallout4-health"
        (workspace / "qa").mkdir(parents=True)
        (workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps({"game_id": "fallout4", "game_profile": "fallout4"}) + "\n",
            encoding="utf-8",
        )
        report_path = workspace / "qa" / "workflow_health.md"
        json_path = workspace / "qa" / "workflow_health.json"
        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(ROOT)}, clear=False):
            workflow_health.write_reports(
                workspace,
                report_path,
                json_path,
                "",
                None,
                None,
                False,
                [],
                [],
                [],
                [],
                [],
                [],
                [],
                [],
            )

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        game_context = load_game_context_module()
        self.assertEqual(payload["game_id"], "fallout4")
        self.assertEqual(set(game_context.GAME_METADATA_KEYS) - set(payload), set())

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

    def test_other_game_glossary_paths_uses_discovered_profiles(self) -> None:
        plugin_root = self.create_plugin_fixture()
        game_context = load_game_context_module()

        with mock.patch.dict(os.environ, {"SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root)}, clear=False):
            self.assertEqual(
                game_context.other_game_glossary_paths("skyrim-se"),
                frozenset({plugin_root / "glossary" / "fallout4_cn_glossary.md"}),
            )
            with self.assertRaisesRegex(ValueError, "Unsupported game id"):
                game_context.other_game_glossary_paths("oblivion")

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
            game_context.load_game_profile("skyrim-se")
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
        self.assertTrue((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "fallout4" / "seed.txt").is_file())
        self.assertFalse((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "skyrim").exists())
        self.assertTrue((workspace / "glossary" / "eet" / "fallout4" / "seed.eet").is_file())
        self.assertTrue((workspace / "glossary" / "sst" / "fallout4" / "seed.sst").is_file())
        self.assertEqual((workspace / "glossary" / "mod_terms.md").read_text(encoding="utf-8"), "template terms\n")

    def test_explicit_skyrim_init_remains_compatible(self) -> None:
        plugin_root = self.create_plugin_fixture()
        workspace = self.temp_root / "skyrim-default"
        self.run_init_workspace(plugin_root, workspace, "--game", "skyrim-se")
        marker = json.loads((workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["plugin_name"], "skyrim-mod-chs-translation")
        self.assertEqual(marker["game_id"], "skyrim-se")
        self.assertTrue((workspace / "glossary" / "skyrim_cn_glossary.md").is_file())
        self.assertFalse((workspace / "glossary" / "fallout4_cn_glossary.md").exists())
        self.assertTrue((workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertTrue((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "skyrim" / "seed.txt").is_file())
        self.assertFalse((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "fallout4").exists())

    def test_skyrim_init_excludes_other_game_glossary_derived_from_profiles(self) -> None:
        plugin_root = self.create_plugin_fixture(fallout_glossary_name="fo4_alt_terms.md")
        workspace = self.temp_root / "skyrim-isolation"
        self.run_init_workspace(plugin_root, workspace, "--game", "skyrim-se")
        self.assertTrue((workspace / "glossary" / "skyrim_cn_glossary.md").is_file())
        self.assertFalse((workspace / "glossary" / "fo4_alt_terms.md").exists())
        self.assertTrue((workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertTrue((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "skyrim" / "seed.txt").is_file())
        self.assertFalse((workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "fallout4").exists())

    def test_missing_recommended_game_glossary_does_not_block_initialization(self) -> None:
        plugin_root = self.create_plugin_fixture()
        (plugin_root / "glossary" / "mod_terms.template.md").write_text("template terms\n", encoding="utf-8")
        (plugin_root / "glossary" / "fallout4_cn_glossary.md").unlink()
        workspace = self.temp_root / "missing-glossary"

        self.run_init_workspace(plugin_root, workspace, "--game", "fallout4")

        self.assertTrue((workspace / ".skyrim-chs-workspace.json").is_file())
        self.assertFalse((workspace / "glossary" / "fallout4_cn_glossary.md").exists())

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
        self.run_init_workspace(ROOT, skyrim_workspace, "--game", "skyrim-se")
        self.run_init_workspace(ROOT, fallout_workspace, "--game", "fallout4")
        self.assertTrue((skyrim_workspace / "glossary" / "skyrim_cn_glossary.md").is_file())
        self.assertFalse((skyrim_workspace / "glossary" / "fallout4_cn_glossary.md").exists())
        self.assertTrue((skyrim_workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertTrue((skyrim_workspace / "glossary" / "lextranslator_dynamic_dictionaries" / "skyrim" / "重光SSE词库1.2.txt").is_file())
        self.assertTrue((fallout_workspace / "glossary" / "fallout4_cn_glossary.md").is_file())
        self.assertFalse((fallout_workspace / "glossary" / "skyrim_cn_glossary.md").exists())
        self.assertTrue((fallout_workspace / "glossary" / "lex_dictionary_notes.md").is_file())
        self.assertTrue((fallout_workspace / "glossary" / "eet" / "fallout4" / "BDD_FO4_ANK.eet").is_file())
        self.assertTrue((fallout_workspace / "glossary" / "sst" / "fallout4" / "fallout4_en_cn.sst").is_file())
        fallout_mod_terms = (fallout_workspace / "glossary" / "mod_terms.md").read_text(encoding="utf-8")
        self.assertNotIn("Whiterun", fallout_mod_terms)
        self.assertNotIn("Dragonborn", fallout_mod_terms)
        self.assertNotIn("Skyrim", fallout_mod_terms)


if __name__ == "__main__":
    unittest.main()

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
import prepare_mod_workspace  # noqa: E402
import route_translation_task  # noqa: E402
import run_translation_queue  # noqa: E402
from resource_model import ResourceDescriptor  # noqa: E402


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

    def test_fallout4_plugin_routes_use_descriptor_capability_and_light_traits(self) -> None:
        self.write_workspace_marker("fallout4")
        ordinary = self.root / "mod" / "Ordinary.esp"
        light_by_extension = self.root / "mod" / "Light.esl"
        light_by_adapter = self.root / "mod" / "SmallFlagged.esp"
        for path in (ordinary, light_by_extension, light_by_adapter):
            path.write_bytes(b"TES4 fixture whose bytes must not be inspected by Python")

        with self.env():
            ordinary_route = route_translation_task.route_for(self.root, ordinary)
            esl_route = route_translation_task.route_for(self.root, light_by_extension)
            explicit_route = route_translation_task.route_for(
                self.root,
                light_by_adapter,
                traits=frozenset({"light"}),
            )
            evidence_route = route_translation_task.route_for(
                self.root,
                light_by_adapter,
                evidence={"traits": ["light"], "source": "adapter-enrichment"},
            )

        self.assertEqual(ordinary_route.capability, "plugin_text")
        self.assertEqual(ordinary_route.effective_capability, "experimental_write")
        self.assertEqual(ordinary_route.traits, ())
        self.assertIn("apply_plugin_translation_map.py", ordinary_route.notes)
        for route in (esl_route, explicit_route, evidence_route):
            self.assertEqual(route.category, "plugin")
            self.assertEqual(route.traits, ("light",))
            self.assertEqual(route.effective_capability, "read_only")
            self.assertNotIn("apply_plugin_translation_map.py", route.notes)
            self.assertIn("read-only", route.agent_allowed.lower())

    def test_skyrim_esl_uses_read_only_route(self) -> None:
        self.write_workspace_marker("skyrim-se")
        path = self.root / "mod" / "SkyrimLight.esl"
        path.write_bytes(b"fixture")

        with self.env():
            route = route_translation_task.route_for(self.root, path)

        self.assertEqual(route.traits, ("light",))
        self.assertEqual(route.effective_capability, "read_only")
        self.assertNotIn("apply_plugin_translation_map.py", route.notes)
        self.assertIn("read-only", route.agent_allowed.lower())

    def test_route_for_consumes_supplied_descriptor_without_reclassification(self) -> None:
        self.write_workspace_marker("fallout4")
        path = self.root / "mod" / "AdapterOwned.bin"
        path.write_bytes(b"fixture")
        descriptor = ResourceDescriptor(
            relative_path=Path("mod/AdapterOwned.bin"),
            category="plugin",
            subtype="adapter.plugin",
            container="adapter-container",
            extension=".bin",
            capability="plugin_text",
            traits=frozenset(),
        )

        with self.env():
            route = route_translation_task.route_for(
                self.root,
                path,
                descriptor=descriptor,
            )
            capped = route_translation_task.route_for(
                self.root,
                path,
                descriptor=descriptor,
                traits=frozenset({"light"}),
                evidence={"traits": ["localized"]},
            )

        self.assertEqual(route.category, "plugin")
        self.assertEqual(route.subtype, "adapter.plugin")
        self.assertEqual(route.container, "adapter-container")
        self.assertEqual(route.capability, "plugin_text")
        self.assertEqual(route.effective_capability, "experimental_write")
        self.assertEqual(capped.traits, ("light", "localized"))
        self.assertEqual(capped.effective_capability, "inventory_only")
        self.assertEqual(capped.status, "blocked")

    def test_route_for_rejects_inconsistent_or_invalid_descriptor(self) -> None:
        self.write_workspace_marker("fallout4")
        path = self.root / "mod" / "Expected.esp"
        path.write_bytes(b"fixture")
        mismatch = ResourceDescriptor(
            relative_path=Path("mod/Other.esp"),
            category="plugin",
            subtype="plugin",
            container="",
            extension=".esp",
            capability="plugin_text",
            traits=frozenset(),
        )
        bad_extension = ResourceDescriptor(
            relative_path=Path("mod/Expected.esp"),
            category="plugin",
            subtype="plugin",
            container="",
            extension=".esm",
            capability="plugin_text",
            traits=frozenset(),
        )

        with self.env():
            with self.assertRaisesRegex(ValueError, "descriptor.relative_path"):
                route_translation_task.route_for(
                    self.root,
                    path,
                    descriptor=mismatch,
                )
            with self.assertRaisesRegex(ValueError, "descriptor.extension"):
                route_translation_task.route_for(
                    self.root,
                    path,
                    descriptor=bad_extension,
                )
            with self.assertRaisesRegex(TypeError, "ResourceDescriptor"):
                route_translation_task.route_for(
                    self.root,
                    path,
                    descriptor=object(),  # type: ignore[arg-type]
                )

    def test_markdown_routes_to_text_resource_translation_for_both_profiles(self) -> None:
        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                self.write_workspace_marker(game_id)
                path = self.root / "mod" / f"Readme-{game_id}.md"
                path.write_text("Visible documentation text\n", encoding="utf-8")
                with self.env():
                    route = route_translation_task.route_for(self.root, path)

                self.assertEqual(route.category, "loose_text")
                self.assertEqual(route.subtype, "loose_text")
                self.assertEqual(route.capability, "loose_text")
                self.assertEqual(route.skill, "skills/text-resource-translation")

    def test_unmarked_external_workspace_has_no_implicit_game(self) -> None:
        with self.env():
            with self.assertRaisesRegex(FileNotFoundError, "workspace marker is required"):
                route_translation_task.current_game_context(self.root)

    def test_fallout4_rejects_bsa_absent_from_profile(self) -> None:
        self.write_workspace_marker("fallout4")
        archive = self.root / "mod" / "Unsupported.bsa"
        archive.write_bytes(b"fixture")
        with self.env():
            route = route_translation_task.route_for(self.root, archive)
        self.assertEqual(route.status, "blocked")
        self.assertEqual(route.skill, "manual-review")
        self.assertIn("not declared", route.blocked_reason)

    def test_bsa_wrapper_rejects_fallout4_before_tool_launch(self) -> None:
        self.write_workspace_marker("fallout4")
        archive = self.root / "mod" / "Unsupported.bsa"
        archive.write_bytes(b"fixture")
        tool = self.root / "tools" / "BSAFileExtractor" / "BSAFileExtractor.py"
        tool.parent.mkdir(parents=True)
        tool.write_text("raise SystemExit('tool must not run')\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUTF8": "1",
                "SKYRIM_CHS_WORKSPACE_ROOT": str(self.root),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "invoke_bsa_file_extractor_safe.py"),
                "--archive-path",
                "mod/Unsupported.bsa",
                "--output-dir",
                "work/archive_extracts/Unsupported",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not declare .bsa", result.stdout + result.stderr)
        self.assertFalse((self.root / "work" / "archive_extracts" / "Unsupported").exists())

    def test_bsa_wrapper_keeps_skyrim_materialization_path_working(self) -> None:
        self.write_workspace_marker("skyrim-se")
        archive = self.root / "mod" / "Supported.bsa"
        archive.write_bytes(b"fixture")
        tool = self.root / "tools" / "BSAFileExtractor" / "BSAFileExtractor.py"
        tool.parent.mkdir(parents=True)
        tool.write_text("print('fixture extractor called')\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUTF8": "1",
                "SKYRIM_CHS_WORKSPACE_ROOT": str(self.root),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "invoke_bsa_file_extractor_safe.py"),
                "--archive-path",
                "mod/Supported.bsa",
                "--output-dir",
                "work/archive_extracts/Supported",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture extractor called", result.stdout)
        self.assertTrue((self.root / "work" / "archive_extracts" / "Supported").is_dir())

    def test_strings_routes_differ_between_skyrim_and_fallout4(self) -> None:
        cases = {
            "skyrim-se": ("skills/xtranslator-gui-automation", "tool-mediated", ""),
            "fallout4": ("manual-review", "blocked", "missing string-table adapter"),
        }
        extensions = (".strings", ".dlstrings", ".ilstrings")
        for game_id, (skill, status, blocked_reason) in cases.items():
            with self.subTest(game_id=game_id):
                self.write_workspace_marker(game_id)
                with self.env():
                    for extension in extensions:
                        path = self.root / "mod" / f"dialog{extension}"
                        path.write_bytes(b"placeholder")
                        payload = route_translation_task.route_payload(route_translation_task.route_for(self.root, path))
                        self.assertEqual(payload["skill"], skill)
                        self.assertEqual(payload["status"], status)
                        self.assertEqual(payload["blocked_reason"], blocked_reason)
                        if payload["skill"].startswith("skills/"):
                            self.assertTrue((ROOT / payload["skill"] / "SKILL.md").is_file())
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

    def test_inventory_is_profile_driven_and_emits_resource_descriptor_columns(self) -> None:
        self.write_workspace_marker("fallout4")
        files = []
        for relative in (
            "Example.esl",
            "MCM/Config/config.json",
            "F4SE/Plugins/example.dll",
            "Package.7z",
        ):
            path = self.root / "mod" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            files.append(path)
        report = self.root / "qa" / "inventory.md"

        with self.env():
            detect_mod_files.write_inventory(
                self.root,
                self.root / "mod",
                report,
                files,
            )

        text = report.read_text(encoding="utf-8")
        self.assertFalse(hasattr(detect_mod_files, "TRACKED_EXTENSIONS"))
        self.assertIn("| Category | Subtype | Container | Traits | Capability |", text)
        self.assertIn("| plugin | plugin |", text)
        self.assertIn("| light | plugin_text", text)
        self.assertIn("| package | package |", text)

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

    def test_f4se_config_observations_require_structured_manual_review(self) -> None:
        self.write_workspace_marker("fallout4")
        mod_name = "F4SEConfigCandidates"
        config_dir = self.root / "work" / "extracted_mods" / mod_name / "F4SE" / "Plugins"
        config_dir.mkdir(parents=True)
        (config_dir / "settings.json").write_text(
            json.dumps({"title": "Do not auto translate this JSON value"}),
            encoding="utf-8",
        )
        (config_dir / "settings.ini").write_text(
            "; Explain this visible setting to the user\npath=Interface/Menu.swf\ntitle=Do not auto translate\n",
            encoding="utf-8",
        )
        (config_dir / "settings.toml").write_text(
            '# Explain this visible option to the user\nprotocol = "f4se"\ntitle = "Do not auto translate"\n',
            encoding="utf-8",
        )

        with self.env(), mock.patch.object(
            sys,
            "argv",
            ["extract_non_gui_candidates.py", "--mod-name", mod_name],
        ):
            self.assertEqual(extract_non_gui_candidates.main(), 0)

        rows = read_jsonl(
            self.root
            / "out"
            / mod_name
            / "non_gui_exports"
            / "all_string_observations.jsonl"
        )
        config_rows = [row for row in rows if row["kind"] == "config-manual-review"]
        self.assertEqual(len(config_rows), 3)
        self.assertEqual({row["descriptor"]["container"] for row in config_rows}, {"f4se"})
        self.assertEqual(
            {row["descriptor"]["extension"] for row in config_rows},
            {".json", ".ini", ".toml"},
        )
        self.assertTrue(all(row["risk"] == "review" for row in config_rows))
        self.assertTrue(all(row["status"] == "manual" for row in config_rows))
        self.assertFalse(any(row["kind"] == "json-string" for row in rows))

        candidates = read_jsonl(
            self.root
            / "out"
            / mod_name
            / "non_gui_exports"
            / "translation_candidates.jsonl"
        )
        self.assertEqual(
            {row["source"] for row in candidates},
            {
                "Explain this visible setting to the user",
                "Explain this visible option to the user",
            },
        )
        self.assertTrue(all(row["kind"] == "config-comment" for row in candidates))

    def test_protected_container_text_formats_never_emit_translation_candidates(self) -> None:
        self.write_workspace_marker("fallout4")
        mod_name = "ProtectedTextCandidates"
        workspace = self.root / "work" / "extracted_mods" / mod_name
        fixtures = {
            "Meshes/labels.json": json.dumps({"title": "Visible JSON candidate"}),
            "Textures/labels.xml": "<root><text>Visible XML candidate</text></root>",
            "Sound/readme.md": "Visible Markdown candidate\n",
            "Music/labels.txt": "Visible text candidate\n",
        }
        for relative, content in fixtures.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        with self.env(), mock.patch.object(
            sys,
            "argv",
            ["extract_non_gui_candidates.py", "--mod-name", mod_name],
        ):
            self.assertEqual(extract_non_gui_candidates.main(), 0)

        observations = read_jsonl(
            self.root
            / "out"
            / mod_name
            / "non_gui_exports"
            / "all_string_observations.jsonl"
        )
        self.assertEqual(len(observations), 4)
        self.assertEqual(
            {row["kind"] for row in observations},
            {"protected-container-manual-review"},
        )
        self.assertEqual({row["risk"] for row in observations}, {"protected"})
        self.assertEqual({row["status"] for row in observations}, {"manual"})
        self.assertEqual({row["descriptor"]["container"] for row in observations}, {"protected"})
        self.assertFalse(any(row["source"] for row in observations))

        candidates = read_jsonl(
            self.root
            / "out"
            / mod_name
            / "non_gui_exports"
            / "translation_candidates.jsonl"
        )
        self.assertEqual(candidates, [])

    def test_nested_protected_and_f4se_containers_override_later_mcm_or_scripts(self) -> None:
        self.write_workspace_marker("fallout4")
        mod_name = "NestedContainerPriority"
        workspace = self.root / "work" / "extracted_mods" / mod_name
        fixtures = {
            "Meshes/MCM/config.json": json.dumps({"title": "Must stay protected"}),
            "Materials/Scripts/foo.pex": "Visible binary-like payload must not be scanned",
            "F4SE/MCM/config.json": json.dumps({"title": "Must stay manual"}),
            "F4SE/Scripts/X.pex": "F4SE Papyrus-like payload must stay manual",
            "F4SE/Plugins/X.esp": "F4SE plugin-like payload must stay manual",
        }
        for relative, content in fixtures.items():
            extracted_path = workspace / relative
            extracted_path.parent.mkdir(parents=True, exist_ok=True)
            extracted_path.write_text(content, encoding="utf-8")

            mod_path = self.root / "mod" / relative
            mod_path.parent.mkdir(parents=True, exist_ok=True)
            mod_path.write_text(content, encoding="utf-8")

        with self.env():
            protected_json = route_translation_task.route_for(
                self.root,
                self.root / "mod" / "Meshes" / "MCM" / "config.json",
            )
            protected_pex = route_translation_task.route_for(
                self.root,
                self.root / "mod" / "Materials" / "Scripts" / "foo.pex",
            )
            f4se_json = route_translation_task.route_for(
                self.root,
                self.root / "mod" / "F4SE" / "MCM" / "config.json",
            )
            f4se_pex = route_translation_task.route_for(
                self.root,
                self.root / "mod" / "F4SE" / "Scripts" / "X.pex",
            )
            f4se_plugin = route_translation_task.route_for(
                self.root,
                self.root / "mod" / "F4SE" / "Plugins" / "X.esp",
            )

        for route in (protected_json, protected_pex):
            self.assertEqual(route.container, "protected")
            self.assertEqual(route.primary_tool, "Copy unchanged")
            self.assertEqual(route.auxiliary_tool, "final_mod provenance validation")
            self.assertEqual(route.status, "manual")
            self.assertEqual(
                route.agent_allowed,
                "No automatic translation or binary editing",
            )

        self.assertEqual(f4se_json.container, "f4se")
        self.assertEqual(f4se_json.primary_tool, "Structured configuration manual review")
        self.assertEqual(f4se_json.auxiliary_tool, "")
        self.assertEqual(f4se_json.status, "manual")
        self.assertEqual(
            f4se_json.agent_allowed,
            "No automatic extraction or translation; confirm player-visible values manually",
        )
        for route in (f4se_pex, f4se_plugin):
            self.assertEqual(route.container, "f4se")
            self.assertEqual(route.skill, "manual-review")
            self.assertEqual(route.primary_tool, "Copy unchanged")
            self.assertEqual(route.auxiliary_tool, "final_mod provenance validation")
            self.assertEqual(route.status, "manual")

        with self.env(), mock.patch.object(
            extract_non_gui_candidates,
            "extract_binary_scan",
            side_effect=AssertionError("protected PEX payload was scanned"),
        ), mock.patch.object(
            sys,
            "argv",
            ["extract_non_gui_candidates.py", "--mod-name", mod_name],
        ):
            self.assertEqual(extract_non_gui_candidates.main(), 0)

        observations = read_jsonl(
            self.root
            / "out"
            / mod_name
            / "non_gui_exports"
            / "all_string_observations.jsonl"
        )
        self.assertEqual(len(observations), 5)
        by_relative_path = {
            row["descriptor"]["relative_path"].replace("\\", "/"): row
            for row in observations
        }
        for relative in ("Meshes/MCM/config.json", "Materials/Scripts/foo.pex"):
            row = by_relative_path[relative]
            self.assertEqual(row["kind"], "protected-container-manual-review")
            self.assertEqual(row["risk"], "protected")
            self.assertEqual(row["status"], "manual")
            self.assertEqual(row["descriptor"]["container"], "protected")
            self.assertEqual(row["source"], "")

        f4se_row = by_relative_path["F4SE/MCM/config.json"]
        self.assertEqual(f4se_row["kind"], "config-manual-review")
        self.assertEqual(f4se_row["risk"], "review")
        self.assertEqual(f4se_row["status"], "manual")
        self.assertEqual(f4se_row["descriptor"]["container"], "f4se")
        self.assertEqual(f4se_row["source"], "")
        for relative in ("F4SE/Scripts/X.pex", "F4SE/Plugins/X.esp"):
            row = by_relative_path[relative]
            self.assertEqual(row["kind"], "f4se-manual-review")
            self.assertEqual(row["risk"], "review")
            self.assertEqual(row["status"], "manual")
            self.assertEqual(row["descriptor"]["container"], "f4se")
            self.assertEqual(row["source"], "")

        candidates = read_jsonl(
            self.root
            / "out"
            / mod_name
            / "non_gui_exports"
            / "translation_candidates.jsonl"
        )
        self.assertEqual(candidates, [])

    def test_plugin_candidate_scan_is_gated_by_effective_read_capability(self) -> None:
        self.write_workspace_marker("fallout4")
        workspace = self.root / "work" / "extracted_mods" / "CapabilityGate"
        workspace.mkdir(parents=True)
        plugin = workspace / "Localized.esp"
        plugin.write_bytes(b"Visible text must not be scanned")

        with self.env():
            context = route_translation_task.current_game_context(self.root)
            for kwargs in (
                {"traits": frozenset({"localized"})},
                {"evidence": {"traits": ["localized"]}},
            ):
                with self.subTest(kwargs=kwargs), mock.patch.object(
                    extract_non_gui_candidates,
                    "extract_binary_scan",
                    side_effect=AssertionError("plugin bytes were scanned"),
                ):
                    rows, skipped_xml = extract_non_gui_candidates.extract_file_observations(
                        self.root,
                        workspace,
                        plugin,
                        context,
                        target_interface_files=set(),
                        **kwargs,
                    )

                self.assertFalse(skipped_xml)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["kind"], "plugin-manual-review")
                self.assertEqual(rows[0]["risk"], "review")
                self.assertEqual(rows[0]["descriptor"]["traits"], ["localized"])

    def test_unsupported_plugin_candidate_emits_blocker_without_scan(self) -> None:
        self.write_workspace_marker("fallout4")
        workspace = self.root / "work" / "extracted_mods" / "UnsupportedGate"
        workspace.mkdir(parents=True)
        plugin = workspace / "Unsupported.bin"
        plugin.write_bytes(b"Visible text must not be scanned")
        descriptor = ResourceDescriptor(
            relative_path=Path("Unsupported.bin"),
            category="plugin",
            subtype="adapter.plugin",
            container="",
            extension=".bin",
            capability="missing",
            traits=frozenset(),
        )

        with self.env():
            context = route_translation_task.current_game_context(self.root)
            with mock.patch.object(
                extract_non_gui_candidates,
                "extract_binary_scan",
                side_effect=AssertionError("plugin bytes were scanned"),
            ):
                rows, _ = extract_non_gui_candidates.extract_file_observations(
                    self.root,
                    workspace,
                    plugin,
                    context,
                    target_interface_files=set(),
                    descriptor=descriptor,
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "plugin-capability-blocker")
        self.assertEqual(rows[0]["risk"], "blocking")

    def test_markdown_candidate_extraction_uses_profile_descriptor(self) -> None:
        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                self.write_workspace_marker(game_id)
                mod_name = f"Markdown-{game_id}"
                workspace = self.root / "work" / "extracted_mods" / mod_name
                workspace.mkdir(parents=True)
                (workspace / "Readme.md").write_text(
                    "Visible documentation sentence for players.\n",
                    encoding="utf-8",
                )

                with self.env(), mock.patch.object(
                    sys,
                    "argv",
                    ["extract_non_gui_candidates.py", "--mod-name", mod_name],
                ):
                    self.assertEqual(extract_non_gui_candidates.main(), 0)

                rows = read_jsonl(
                    self.root
                    / "out"
                    / mod_name
                    / "non_gui_exports"
                    / "all_string_observations.jsonl"
                )
                markdown_rows = [row for row in rows if row["kind"] == "markdown-line"]
                self.assertEqual(len(markdown_rows), 1)
                self.assertEqual(
                    markdown_rows[0]["source"],
                    "Visible documentation sentence for players.",
                )
                self.assertEqual(markdown_rows[0]["risk"], "candidate")
                self.assertEqual(
                    markdown_rows[0]["descriptor"]["capability"],
                    "loose_text",
                )

    def test_typical_fallout4_data_tree_has_stable_single_routes(self) -> None:
        self.write_workspace_marker("fallout4")
        cases = {
            "MCM/Config/config.json": ("loose_text", "mcm", "skills/mcm-translation"),
            "MCM/Config/settings.ini": ("loose_text", "mcm", "skills/mcm-translation"),
            "MCM/Config/settings.toml": ("loose_text", "mcm", "skills/mcm-translation"),
            "MCM/Config/help.txt": ("loose_text", "mcm", "skills/mcm-translation"),
            "F4SE/Plugins/example.dll": ("protected_binary", "f4se", "manual-review"),
            "F4SE/Plugins/settings.ini": ("loose_text", "f4se", "manual-review"),
            "F4SE/Plugins/settings.json": ("loose_text", "f4se", "manual-review"),
            "F4SE/Plugins/settings.toml": ("loose_text", "f4se", "manual-review"),
            "Interface/menu.swf": ("interface", "interface", "manual-review"),
            "Interface/menu.gfx": ("interface", "interface", "manual-review"),
            "Meshes/example.nif": ("protected_binary", "protected", "manual-review"),
            "Textures/example.dds": ("protected_binary", "protected", "manual-review"),
            "Materials/example.bgsm": ("protected_binary", "protected", "manual-review"),
            "Materials/example.bgem": ("protected_binary", "protected", "manual-review"),
            "Sound/example.xwm": ("unknown", "protected", "manual-review"),
            "Music/example.xwm": ("unknown", "protected", "manual-review"),
            "Video/example.bk2": ("unknown", "protected", "manual-review"),
            "Vis/example.vis": ("unknown", "protected", "manual-review"),
            "Seq/example.seq": ("unknown", "protected", "manual-review"),
            "settings.ini": ("loose_text", "", "manual-review"),
            "settings.toml": ("loose_text", "", "manual-review"),
        }
        with self.env():
            for relative, expected in cases.items():
                with self.subTest(relative=relative):
                    path = self.root / "mod" / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fixture")
                    first = route_translation_task.route_payload(
                        route_translation_task.route_for(self.root, path)
                    )
                    second = route_translation_task.route_payload(
                        route_translation_task.route_for(self.root, path)
                    )
                    self.assertEqual(first, second)
                    self.assertEqual(
                        (first["category"], first["container"], first["skill"]),
                        expected,
                    )

    def test_mcm_container_routes_each_text_format_with_explicit_tool_contract(self) -> None:
        self.write_workspace_marker("fallout4")
        cases = {
            "MCM/Config/settings.json": {
                "primary_tool": "Agent Structured MCM Extractor",
                "auxiliary_tool": "Codex-only LexTranslator fallback",
                "status": "ready",
                "agent_allowed": "Yes, extract and translate confirmed visible MCM values",
            },
            "MCM/Config/settings.ini": {
                "primary_tool": "Agent Structured MCM Extractor",
                "auxiliary_tool": "Codex-only LexTranslator fallback",
                "status": "ready",
                "agent_allowed": "Yes, extract and translate confirmed visible MCM values",
            },
            "MCM/Config/help.txt": {
                "primary_tool": "Agent Text Pipeline",
                "auxiliary_tool": "",
                "status": "ready",
                "agent_allowed": "Yes, translate visible MCM text while preserving structure",
            },
            "MCM/Config/settings.toml": {
                "primary_tool": "Structured TOML manual review",
                "auxiliary_tool": "",
                "status": "manual",
                "agent_allowed": "No automatic translation or writeback; manual review required",
            },
        }

        with self.env():
            for relative, expected in cases.items():
                with self.subTest(relative=relative):
                    path = self.root / "mod" / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture\n", encoding="utf-8")
                    payload = route_translation_task.route_payload(
                        route_translation_task.route_for(self.root, path)
                    )
                    self.assertEqual(payload["skill"], "skills/mcm-translation")
                    self.assertEqual(payload["container"], "mcm")
                    for field, value in expected.items():
                        self.assertEqual(payload[field], value)

    def test_f4se_config_routes_are_manual_before_generic_text_routing(self) -> None:
        self.write_workspace_marker("fallout4")
        with self.env():
            for extension in ("json", "ini", "toml"):
                with self.subTest(extension=extension):
                    path = self.root / "mod" / "F4SE" / "Plugins" / f"settings.{extension}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture\n", encoding="utf-8")
                    payload = route_translation_task.route_payload(
                        route_translation_task.route_for(self.root, path)
                    )
                    self.assertEqual(payload["skill"], "manual-review")
                    self.assertEqual(payload["primary_tool"], "Structured configuration manual review")
                    self.assertEqual(payload["auxiliary_tool"], "")
                    self.assertEqual(payload["status"], "manual")
                    self.assertEqual(
                        payload["agent_allowed"],
                        "No automatic extraction or translation; confirm player-visible values manually",
                    )

    def test_mcm_ini_route_precedes_manual_config_for_both_profiles(self) -> None:
        path = self.root / "mod" / "MCM" / "Config" / "settings.ini"
        path.parent.mkdir(parents=True)
        path.write_text("label=Visible setting\n", encoding="utf-8")

        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                self.write_workspace_marker(game_id)
                route = route_translation_task.route_payload(
                    route_translation_task.route_for(self.root, path)
                )
                self.assertEqual(route["category"], "loose_text")
                self.assertEqual(route["container"], "mcm")
                self.assertEqual(route["skill"], "skills/mcm-translation")
                self.assertEqual(route["status"], "ready")

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

    def test_mcm_ini_uses_profile_schema_for_natural_language_and_protected_keys(self) -> None:
        cases = {
            "skyrim-se": ({"title", "displayName"}, {"scriptName", "script"}),
            "fallout4": ({"title"}, {"script", "displayName"}),
        }
        for game_id, (expected_candidates, expected_protected) in cases.items():
            with self.subTest(game_id=game_id):
                self.write_workspace_marker(game_id)
                mod_name = f"Ini-{game_id}"
                input_dir = self.root / "mod" / mod_name / "MCM"
                input_dir.mkdir(parents=True, exist_ok=True)
                (input_dir / "settings.ini").write_text(
                    "\n".join(
                        [
                            "[General]",
                            "title=Main configuration menu",
                            "displayName=Visible legacy label",
                            "scriptName=Quest Script Name",
                            "script=Quest Script Name",
                            "path=Interface/MCM/config.json",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                with self.env(), mock.patch.object(
                    sys,
                    "argv",
                    ["extract_mcm_text.py", "--input-path", f"mod/{mod_name}/MCM", "--mod-name", mod_name],
                ):
                    exit_code = extract_mcm_text.main()
                self.assertEqual(exit_code, 0)
                rows = read_jsonl(self.root / "work" / "normalized" / mod_name / "mcm_text_candidates.jsonl")
                candidate_keys = {row["key"] for row in rows}
                self.assertEqual(candidate_keys, expected_candidates)
                self.assertTrue(expected_protected.isdisjoint(candidate_keys))
                self.assertEqual({row["game_id"] for row in rows}, {game_id})

    def test_inventory_reuses_one_game_context_for_all_routes(self) -> None:
        self.write_workspace_marker("skyrim-se")
        files = []
        for name in ("Example.esp", "Menu.txt", "Dialog.strings"):
            path = self.root / "mod" / name
            path.write_bytes(b"fixture")
            files.append(path)
        real_loader = route_translation_task.current_game_context
        calls = 0

        def counted_loader(root: Path):
            nonlocal calls
            calls += 1
            return real_loader(root)

        with self.env(), mock.patch.object(detect_mod_files, "current_game_context", side_effect=counted_loader):
            detect_mod_files.write_inventory(self.root, self.root / "mod", self.root / "qa" / "inventory.md", files)
        self.assertEqual(calls, 1)

    def test_route_for_accepts_explicit_context_without_reloading_profile(self) -> None:
        self.write_workspace_marker("skyrim-se")
        path = self.root / "mod" / "Example.esp"
        path.write_bytes(b"fixture")
        with self.env():
            context = route_translation_task.current_game_context(self.root)
            with mock.patch.object(route_translation_task, "current_game_context", side_effect=AssertionError("reloaded")):
                route = route_translation_task.route_for(self.root, path, context)
        self.assertEqual(route.game_id, "skyrim-se")
        self.assertEqual(route.skill, "skills/esp-esm-esl-translation")

    def test_prepare_report_reuses_one_game_context_for_file_loop(self) -> None:
        self.write_workspace_marker("skyrim-se")
        workspace = self.root / "work" / "extracted_mods" / "Example"
        workspace.mkdir(parents=True)
        files = []
        for name in ("Example.esp", "Menu.txt"):
            path = workspace / name
            path.write_bytes(b"fixture")
            files.append(path)
        real_loader = route_translation_task.current_game_context
        calls = 0

        def counted_loader(root: Path):
            nonlocal calls
            calls += 1
            return real_loader(root)

        with self.env(), mock.patch.object(prepare_mod_workspace, "current_game_context", side_effect=counted_loader):
            prepare_mod_workspace.write_workflow_report(
                self.root,
                self.root / "qa" / "workflow.md",
                "Example",
                self.root / "mod",
                workspace,
                files,
                [],
            )
        self.assertEqual(calls, 1)

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

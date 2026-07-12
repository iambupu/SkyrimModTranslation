from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import struct
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from test_fallout4_plugin_adapter_regressions import DOTNET, record, subrecord, tes4_plugin  # noqa: E402
from test_fallout4_pex_adapter_regressions import FIXTURE_PROJECT, FIXTURE_SOURCE  # noqa: E402
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
    "archive_materialization_enabled",
    "archive_allow_repack",
    "interface_translation_encoding",
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
            self.assertIs(payload["archive_materialization_enabled"], game_id == "fallout4")
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

    def build_pex_fixture(self, path: Path) -> Path:
        assert DOTNET is not None
        helper_root = ROOT / ".tmp" / "task-4-pex-fixture-builder"
        helper_root.mkdir(parents=True, exist_ok=True)
        (helper_root / "FixtureBuilder.csproj").write_text(FIXTURE_PROJECT, encoding="utf-8")
        (helper_root / "Program.cs").write_text(textwrap.dedent(FIXTURE_SOURCE), encoding="utf-8")
        helper_dll = helper_root / "bin" / "Debug" / "net8.0" / "FixtureBuilder.dll"
        if not helper_dll.is_file():
            built = subprocess.run(
                [str(DOTNET), "build", str(helper_root / "FixtureBuilder.csproj"), "--nologo"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        generated = subprocess.run(
            [str(DOTNET), str(helper_dll), str(path), "fallout4", "single"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        return path

    def build_plugin_fixture(self, path: Path, name: str) -> Path:
        assert DOTNET is not None
        helper_root = ROOT / ".tmp" / "task-3-plugin-fixture-builder"
        helper_root.mkdir(parents=True, exist_ok=True)
        (helper_root / "FixtureBuilder.csproj").write_text(
            textwrap.dedent(
                """
                <Project Sdk="Microsoft.NET.Sdk">
                  <PropertyGroup>
                    <OutputType>Exe</OutputType>
                    <TargetFramework>net8.0</TargetFramework>
                    <ImplicitUsings>enable</ImplicitUsings>
                  </PropertyGroup>
                  <ItemGroup>
                    <PackageReference Include="Mutagen.Bethesda.Fallout4" Version="0.53.1" />
                  </ItemGroup>
                </Project>
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (helper_root / "Program.cs").write_text(
            textwrap.dedent(
                """
                using Mutagen.Bethesda;
                using Mutagen.Bethesda.Fallout4;
                using Mutagen.Bethesda.Plugins;
                using Mutagen.Bethesda.Plugins.Binary.Parameters;
                using Mutagen.Bethesda.Plugins.Records;

                var output = Path.GetFullPath(args[0]);
                var mod = new Fallout4Mod(ModKey.FromNameAndExtension(Path.GetFileName(output)), Fallout4Release.Fallout4);
                var weapon = mod.Weapons.AddNew(new FormKey(mod.ModKey, 0x1234));
                weapon.EditorID = "ClassicWeapon";
                weapon.Name = args[1];
                Directory.CreateDirectory(Path.GetDirectoryName(output)!);
                mod.BeginWrite.ToPath(output).WithLoadOrderFromHeaderMasters().WithNoDataFolder().WithMastersListContent(MastersListContentOption.NoCheck).Write();
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        helper_dll = helper_root / "bin" / "Debug" / "net8.0" / "FixtureBuilder.dll"
        built = subprocess.run(
            [str(DOTNET), "build", str(helper_root / "FixtureBuilder.csproj"), "--nologo"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        generated = self.run_dotnet_adapter(helper_dll, str(path), name)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        return path

    def run_dotnet_adapter(self, adapter_dll: Path, *args: str) -> subprocess.CompletedProcess[str]:
        assert DOTNET is not None
        return subprocess.run(
            [str(DOTNET), str(adapter_dll), *args],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def write_ba2_adapter_config(self) -> None:
        adapter = self.workspace / "tools" / "fake_ba2_adapter.py"
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text(
            textwrap.dedent(
                """
                import argparse
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--archive-path", required=True)
                parser.add_argument("--output-dir", required=True)
                args = parser.parse_args()
                output = Path(args.output_dir)
                translation = output / "Interface" / "translations" / "Classic_en.txt"
                translation.parent.mkdir(parents=True, exist_ok=True)
                translation.write_text("$HELLO\\tHello", encoding="utf-8")
                material = output / "Materials" / "classic.bgsm"
                material.parent.mkdir(parents=True, exist_ok=True)
                material.write_bytes(b"synthetic-material")
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (self.workspace / "config" / "tools.local.json").write_text(
            json.dumps(
                {
                    "DecoderTools": {
                        "Ba2ExtractorPath": "tools/fake_ba2_adapter.py",
                        "Ba2ExtractorProtocol": "skyrim-mod-chs.ba2-extractor.v1",
                    }
                },
                indent=2,
            )
            + "\n",
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
                state = self.read_json("qa/workflow_state.json")
                schema = json.loads((ROOT / "config" / "workflow_state.schema.json").read_text(encoding="utf-8"))
                self.assertTrue(set(schema["required"]).issubset(state), sorted(set(schema["required"]) - set(state)))
                self.assertTrue(set(state).issubset(schema["properties"]), sorted(set(state) - set(schema["properties"])))
                self.assertIn("interface_translation_encoding", schema["required"])
                self.assertEqual(schema["properties"]["interface_translation_encoding"], {"type": "string"})
                self.assertIn("archive_materialization_enabled", schema["required"])
                state_items = schema["properties"]["states"]["items"]
                self.assertIn("next_actions", state_items["required"])
                next_actions = state_items["properties"]["next_actions"]
                self.assertEqual(next_actions["type"], "array")
                self.assertTrue(
                    {
                        "type",
                        "source",
                        "command",
                        "risk",
                        "reason",
                        "evidence",
                        "allowed",
                        "refresh_after",
                    }.issubset(next_actions["items"]["required"])
                )

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

    def test_fallout4_missing_game_metadata_blocks_state_tasks_and_handoff(self) -> None:
        self.write_marker("fallout4")
        self.run_state_chain()

        readiness_path = self.workspace / "qa" / "translation_readiness.json"
        readiness = self.read_json("qa/translation_readiness.json")
        damaged_readiness = dict(readiness)
        damaged_readiness.pop("archive_allow_repack")
        readiness_path.write_text(json.dumps(damaged_readiness, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state_result = self.run_script("write_workflow_state.py")
        self.assertNotEqual(state_result.returncode, 0, state_result.stdout + state_result.stderr)
        state = self.read_json("qa/workflow_state.json")
        self.assertTrue(any("missing archive_allow_repack" in str(row) for row in state["issues"]), state["issues"])

        readiness_path.write_text(json.dumps(readiness, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.run_script("write_workflow_state.py").returncode, 0)
        state_path = self.workspace / "qa" / "workflow_state.json"
        state = self.read_json("qa/workflow_state.json")
        damaged_state = dict(state)
        damaged_state.pop("pex_category")
        state_path.write_text(json.dumps(damaged_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tasks_result = self.run_script("write_workflow_tasks.py")
        self.assertNotEqual(tasks_result.returncode, 0, tasks_result.stdout + tasks_result.stderr)
        tasks = self.read_json("qa/workflow_tasks.json")
        self.assertEqual(tasks["tasks"], [])
        self.assertTrue(any("missing pex_category" in str(row) for row in tasks["issues"]), tasks["issues"])

        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.run_script("write_workflow_tasks.py").returncode, 0)
        health = {key: state[key] for key in GAME_KEYS}
        health["Verdict"] = "PASS"
        health_path = self.workspace / "qa" / "workflow_health.json"
        health_path.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifact_paths = {
            "translation_readiness": readiness_path,
            "workflow_state": state_path,
            "workflow_tasks": self.workspace / "qa" / "workflow_tasks.json",
            "workflow_health": health_path,
        }
        for index, (label, path) in enumerate(artifact_paths.items()):
            with self.subTest(label=label):
                original = json.loads(path.read_text(encoding="utf-8-sig"))
                missing_key = sorted(GAME_KEYS)[index]
                damaged = dict(original)
                damaged.pop(missing_key)
                path.write_text(json.dumps(damaged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result = self.run_script("write_codex_handoff.py")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                handoff = self.read_json("qa/codex_handoff.json")
                self.assertTrue(
                    any(label in str(row) and f"missing {missing_key}" in str(row) for row in handoff["issues"]),
                    handoff["issues"],
                )
                path.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
        mod_root = self.workspace / "mod" / MOD_NAME
        dll = mod_root / "F4SE" / "Plugins" / "ClassicHolsteredWeapons.dll"
        material = mod_root / "Materials" / "classic.bgsm"
        dll.parent.mkdir(parents=True)
        material.parent.mkdir(parents=True)
        dll.write_bytes(b"same-synthetic-dll")
        material.write_bytes(b"same-synthetic-material")
        routes: dict[str, dict[str, dict[str, object]]] = {}
        inventories: dict[str, str] = {}
        for game_id in ("fallout4", "skyrim-se"):
            with self.subTest(game_id=game_id):
                self.write_marker(game_id)
                routes[game_id] = {}
                for label, path in (("dll", dll), ("material", material)):
                    result = self.run_script(
                        "route_translation_task.py",
                        "--file-path",
                        str(path.relative_to(self.workspace)),
                        "--as-json",
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    routes[game_id][label] = json.loads(result.stdout)
                inventory = self.run_script(
                    "detect_mod_files.py",
                    "--scan-path",
                    f"mod/{MOD_NAME}",
                    "--report-path",
                    f"qa/{game_id}.inventory.md",
                )
                self.assertEqual(inventory.returncode, 0, inventory.stdout + inventory.stderr)
                inventories[game_id] = (self.workspace / "qa" / f"{game_id}.inventory.md").read_text(encoding="utf-8")

        self.assertEqual(routes["fallout4"]["material"]["risk"], "Profile-protected resource")
        self.assertEqual(routes["fallout4"]["material"]["primary_tool"], "Copy unchanged")
        self.assertIn("fallout4", str(routes["fallout4"]["material"]["notes"]).lower())
        self.assertIn("materials", str(routes["fallout4"]["material"]["notes"]).lower())
        self.assertEqual(routes["skyrim-se"]["material"]["risk"], "Unknown")
        self.assertNotEqual(routes["fallout4"]["material"]["notes"], routes["skyrim-se"]["material"]["notes"])
        self.assertIn("recognized data directory 'f4se'", str(routes["fallout4"]["dll"]["notes"]).lower())
        self.assertIn("not a recognized data directory", str(routes["skyrim-se"]["dll"]["notes"]).lower())
        self.assertIn("- GameId: fallout4", inventories["fallout4"])
        self.assertIn("Profile-protected resource", inventories["fallout4"])
        self.assertIn("- GameId: skyrim-se", inventories["skyrim-se"])
        self.assertNotIn("Profile-protected resource", inventories["skyrim-se"])

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
                    self.assertEqual(row["source_archive"], f"mod/{game_id}.zip")
                    self.assertEqual(row["source_archive_entry"], entry)
                    self.assertEqual(row["source_archive_sha256"], sha256(archive_path))
                provenance_path = final_mod / "meta" / "provenance.jsonl"
                original_provenance = provenance_path.read_text(encoding="utf-8")
                for mutation, field, replacement in (
                    ("missing-path", "source_archive", None),
                    ("missing-entry", "source_archive_entry", None),
                    ("missing-hash", "source_archive_sha256", None),
                    ("wrong-path", "source_archive", "mod/wrong.zip"),
                    ("wrong-entry", "source_archive_entry", "wrong/entry.bin"),
                    ("wrong-hash", "source_archive_sha256", "0" * 64),
                ):
                    with self.subTest(game_id=game_id, provenance_mutation=mutation):
                        tampered_rows = [json.loads(line) for line in original_provenance.splitlines() if line]
                        target = next(row for row in tampered_rows if row["file"] == f"final_mod/{protected_entry}")
                        if replacement is None:
                            target.pop(field)
                        else:
                            target[field] = replacement
                        provenance_path.write_text(
                            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in tampered_rows),
                            encoding="utf-8",
                        )
                        rejected = self.run_script(
                            "validate_final_mod.py", "--final-mod-dir", "out/ZipMod/汉化产出/final_mod"
                        )
                        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                        self.assertIn(field, (self.workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8"))
                        provenance_path.write_text(original_provenance, encoding="utf-8")
                delivered_dll = final_mod / Path(dll_entry)
                delivered_dll.write_bytes(b"tampered")
                tampered = self.run_script(
                    "validate_final_mod.py", "--final-mod-dir", "out/ZipMod/汉化产出/final_mod"
                )
                self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)

    def test_generated_meta_paths_are_reserved_for_directory_and_zip_rebuilds(self) -> None:
        for source_kind in ("directory", "zip"):
            with self.subTest(source_kind=source_kind):
                self.reset_workspace(f"reserved-meta-{source_kind}")
                self.write_marker("fallout4")
                sentinel_files = {
                    "meta/provenance.jsonl": "input-provenance-sentinel\n",
                    "meta/manifest.json": '{"input":"manifest-sentinel"}\n',
                    "meta/redistribution_notes.md": "input-redistribution-sentinel\n",
                    "meta/build_report.md": "input-build-report-sentinel\n",
                }
                if source_kind == "directory":
                    source = self.workspace / "mod" / "ReservedMetaMod"
                    for relative, content in sentinel_files.items():
                        path = source / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8")
                    (source / "Interface").mkdir(exist_ok=True)
                    (source / "Interface" / "payload.txt").write_text("payload", encoding="utf-8")
                    source_arg = "mod/ReservedMetaMod"
                else:
                    source = self.workspace / "mod" / "ReservedMetaMod.zip"
                    with zipfile.ZipFile(source, "w") as archive:
                        for relative, content in sentinel_files.items():
                            archive.writestr(relative, content)
                        archive.writestr("Interface/payload.txt", "payload")
                    source_arg = "mod/ReservedMetaMod.zip"
                self.write_dictionary("ReservedMetaMod")

                for _attempt in range(2):
                    built = self.run_script(
                        "build_final_mod.py",
                        "--mod-name",
                        "ReservedMetaMod",
                        "--source-mod-dir",
                        source_arg,
                        "--force",
                    )
                    self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                    validated = self.run_script(
                        "validate_final_mod.py", "--final-mod-dir", "out/ReservedMetaMod/汉化产出/final_mod"
                    )
                    self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

                final_meta = self.workspace / "out" / "ReservedMetaMod" / "汉化产出" / "final_mod" / "meta"
                for relative, sentinel in sentinel_files.items():
                    self.assertNotEqual((final_meta.parent / relative).read_text(encoding="utf-8"), sentinel)
                rows = [
                    json.loads(line)
                    for line in (final_meta / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
                    if line
                ]
                files = [row["file"].lower() for row in rows]
                self.assertEqual(files.count("final_mod/meta/provenance.jsonl"), 1)
                self.assertEqual(len(files), len(set(files)))
                for reserved in sentinel_files:
                    row = next(item for item in rows if item["file"].lower() == f"final_mod/{reserved}".lower())
                    self.assertTrue(
                        row["source"].startswith("generated:"),
                        f"reserved path reused input provenance: {row}",
                    )

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
    def test_strict_chain_uses_production_plugin_and_pex_evidence_then_blocks_experimental_gate(self) -> None:
        self.write_marker("fallout4")
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        workspace.mkdir(parents=True)
        final_mod.mkdir(parents=True)

        plugin_name = "ClassicHolsteredWeapons.esp"
        original_plugin = self.build_plugin_fixture(workspace / plugin_name, "Classic Weapon")
        original_export = self.workspace / "source" / "plugin_exports" / MOD_NAME / f"{plugin_name}_strings.jsonl"
        exported_plugin = self.run_script(
            "export_esp_strings.py",
            "--plugin-path",
            str(original_plugin),
            "--mod-name",
            MOD_NAME,
            "--output-path",
            str(original_export),
            "--report-path",
            f"qa/{plugin_name}.integration_source_export.md",
            "--game",
            "fallout4",
        )
        self.assertEqual(exported_plugin.returncode, 0, exported_plugin.stdout + exported_plugin.stderr)
        source_rows = [json.loads(line) for line in original_export.read_text(encoding="utf-8").splitlines() if line]
        source_row = next(row for row in source_rows if row.get("record_type") == "WEAP" and row.get("subrecord_type") == "FULL")
        plugin_translation = self.workspace / "translated" / "plugin_exports" / MOD_NAME / f"{plugin_name}_strings.zh.jsonl"
        plugin_translation.parent.mkdir(parents=True)
        plugin_translation.write_text(
            json.dumps(
                {
                    **source_row,
                    "target": "经典武器",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        adapter_dll = ensure_adapter_dll(self.workspace, ROOT, DOTNET, "SkyrimPluginTextTool")
        plugin_writeback_report = self.workspace / "qa" / f"{plugin_name}.plugin_stage_mutagen_write.md"
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
                str(plugin_writeback_report),
            ],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

        pex_adapter = ensure_adapter_dll(self.workspace, ROOT, DOTNET, "SkyrimPexStringTool")
        original_pex = self.build_pex_fixture(workspace / "Scripts" / "ClassicHolsteredWeapons.pex")
        exported_pex = self.workspace / "source" / "pex_exports" / MOD_NAME / "ClassicHolsteredWeapons.pex_strings.jsonl"
        exported_pex.parent.mkdir(parents=True)
        pex_export_report = self.workspace / "qa" / "ClassicHolsteredWeapons.production_export.md"
        exported = self.run_dotnet_adapter(
            pex_adapter,
            "export",
            "--game",
            "fallout4",
            "--project-root",
            str(self.workspace),
            "--input-pex",
            str(original_pex),
            "--output-jsonl",
            str(exported_pex),
            "--report",
            str(pex_export_report),
        )
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        pex_translation = self.workspace / "work" / "normalized" / MOD_NAME / "pex_apply" / "ClassicHolsteredWeapons.translation.jsonl"
        pex_translation.parent.mkdir(parents=True)
        export_rows = [json.loads(line) for line in exported_pex.read_text(encoding="utf-8").splitlines() if line]
        translated_rows = [
            {
                **row,
                "Result": "共享可见文本",
                "risk": "candidate",
                "notes": "confirmed visible text for synthetic integration fixture",
            }
            for row in export_rows
            if row.get("Source") == "Shared visible text"
        ]
        self.assertTrue(translated_rows, export_rows)
        pex_translation.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in translated_rows), encoding="utf-8"
        )
        tool_pex = self.workspace / "out" / MOD_NAME / "tool_outputs" / "Scripts" / original_pex.name
        tool_pex.parent.mkdir(parents=True)
        pex_apply_report = self.workspace / "qa" / "ClassicHolsteredWeapons.production_apply.md"
        pex_applied = self.run_dotnet_adapter(
            pex_adapter,
            "apply",
            "--game",
            "fallout4",
            "--project-root",
            str(self.workspace),
            "--input-pex",
            str(original_pex),
            "--translation-jsonl",
            str(pex_translation),
            "--output-pex",
            str(tool_pex),
            "--report",
            str(pex_apply_report),
            "--allow-experimental-writeback",
        )
        self.assertEqual(pex_applied.returncode, 0, pex_applied.stdout + pex_applied.stderr)
        pex_verify_report = self.workspace / "qa" / "ClassicHolsteredWeapons.production_verify.md"
        pex_verified = self.run_dotnet_adapter(
            pex_adapter,
            "verify",
            "--game",
            "fallout4",
            "--project-root",
            str(self.workspace),
            "--input-pex",
            str(original_pex),
            "--translation-jsonl",
            str(pex_translation),
            "--output-pex",
            str(tool_pex),
            "--report",
            str(pex_verify_report),
        )
        self.assertEqual(pex_verified.returncode, 0, pex_verified.stdout + pex_verified.stderr)
        final_pex = final_mod / "Scripts" / original_pex.name
        final_pex.parent.mkdir(parents=True)
        shutil.copy2(tool_pex, final_pex)

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
        plugin_output_export = self.workspace / "source" / "plugin_exports" / MOD_NAME / f"{plugin_name}.gate_final_mod_strings.jsonl"
        pex_report = self.workspace / "qa" / f"{MOD_NAME}.pex_delivery_post_build.md"
        pex_gate_report = self.workspace / "qa" / f"{MOD_NAME}.ClassicHolsteredWeapons.pex_experimental_gate.md"
        self.assertTrue(plugin_report.is_file(), result.stdout + result.stderr)
        self.assertIn("No blocking issues.", plugin_report.read_text(encoding="utf-8"))
        self.assertTrue(plugin_output_export.is_file(), result.stdout + result.stderr)
        self.assertIn("Binary invariant verified: True", plugin_writeback_report.read_text(encoding="utf-8"))
        self.assertTrue(pex_report.is_file())
        self.assertIn("- Blocking issues: 0", pex_report.read_text(encoding="utf-8"))
        self.assertNotIn("missing", pex_report.read_text(encoding="utf-8").lower())
        self.assertIn("- experimental_opt_in: True", pex_apply_report.read_text(encoding="utf-8"))
        self.assertIn("- Verification passed: True", pex_verify_report.read_text(encoding="utf-8"))
        self.assertTrue(pex_gate_report.is_file(), strict_report)
        gate_text = pex_gate_report.read_text(encoding="utf-8")
        self.assertIn("game_id: fallout4", gate_text)
        self.assertIn("pex_writeback_status: experimental", gate_text)
        self.assertIn("not eligible for strict completion", gate_text)
        self.assertIn("- Final plugins checked: 1", strict_report)
        self.assertIn("- Final PEX files checked: 1", strict_report)
        self.assertNotIn("| error | plugin-output |", strict_report)
        self.assertNotIn("| error | pex-delivery |", strict_report)
        self.assertIn("| error | pex-experimental-gate |", strict_report)
        self.assertNotIn("tool output PEX is missing", strict_report)

        plugin_writeback_report.unlink()
        missing_evidence = self.run_script(
            "run_non_gui_qa_gates.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--strict-complete",
        )
        self.assertNotEqual(missing_evidence.returncode, 0)
        missing_report = (self.workspace / "qa" / f"{MOD_NAME}.non_gui_qa_gates.md").read_text(encoding="utf-8")
        self.assertIn("| error | plugin-output |", missing_report)
        self.assertIn("writeback", missing_report.lower())

    def test_ba2_verified_safe_production_evidence_is_accepted_by_strict_coverage(self) -> None:
        self.write_marker("fallout4")
        self.write_ba2_adapter_config()
        archive = self.workspace / "mod" / "ClassicHolsteredWeapons - Main.ba2"
        archive.write_bytes(b"BTDX-synthetic-integration-fixture")
        extracted = self.run_script(
            "invoke_ba2_extractor_safe.py",
            "--mod-name",
            MOD_NAME,
            "--archive-path",
            "mod/ClassicHolsteredWeapons - Main.ba2",
            "--output-dir",
            f"work/archive_extracts/{MOD_NAME}/ClassicHolsteredWeapons - Main",
            "--config-path",
            "config/tools.local.json",
        )
        self.assertEqual(extracted.returncode, 0, extracted.stdout + extracted.stderr)
        manifest_path = (
            self.workspace
            / "out"
            / MOD_NAME
            / "archive_audits"
            / "ClassicHolsteredWeapons - Main"
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["game_id"], "fallout4")
        self.assertEqual(manifest["AuditMode"], "verified-safe-extraction")
        workspace = self.workspace / "work" / "extracted_mods" / MOD_NAME
        workspace.mkdir(parents=True)
        shutil.copy2(archive, workspace / archive.name)
        final_mod = self.workspace / "out" / MOD_NAME / "汉化产出" / "final_mod"
        for row in manifest["Files"]:
            destination = final_mod / Path(row["RelativePath"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = self.workspace / row["ProjectPath"]
            shutil.copy2(source, destination)

        audited = self.run_script(
            "audit_archive_coverage.py",
            "--mod-name",
            MOD_NAME,
            "--workspace-path",
            f"work/extracted_mods/{MOD_NAME}",
            "--final-mod-dir",
            f"out/{MOD_NAME}/汉化产出/final_mod",
            "--config-path",
            "config/tools.local.json",
            "--strict-complete",
            "--as-json",
        )
        self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
        payload = json.loads(audited.stdout)
        evidence = next(row for row in payload["Archives"] if row["Extension"] == ".ba2")
        self.assertTrue(evidence["EvidenceValid"])
        self.assertTrue(evidence["MaterializationReady"])
        self.assertEqual(evidence["EvidenceMode"], "verified-safe-extraction")
        self.assertEqual(payload["BlockingIssues"], 0)

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

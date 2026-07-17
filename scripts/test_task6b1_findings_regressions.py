from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import export_agent_context  # noqa: E402
import write_agent_handoff  # noqa: E402
from agent_capabilities import capability_config_fingerprint, load_agent_capabilities  # noqa: E402
from audit_translation_readiness import translation_dictionary_status  # noqa: E402
from file_utils import sha256_file  # noqa: E402
from game_context import game_context_metadata, load_game_profile  # noqa: E402
from plugin_resource_evidence import plugin_artifact_key  # noqa: E402


class Task6B1FindingsProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_parent = ROOT / ".tmp" / "test-task6b1-findings"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.tempdir.cleanup)

    def workspace(self, name: str, game_id: str) -> Path:
        root = Path(self.tempdir.name) / name
        for relative in ("mod", "work", "qa", "out", "source", "translated", "glossary", ".workflow", "traces", "config"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        marker = {
            "schema_version": 2,
            "kind": "bethesda-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "plugin_root": str(ROOT),
            "game_id": game_id,
            "game_profile": game_id,
        }
        (root / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return root

    def run_script(
        self,
        workspace: Path,
        script_name: str,
        *args: str,
        plugin_root: Path = ROOT,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
            "SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root),
        }
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script_name), *args],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def write_dictionary(self, workspace: Path, mod_name: str) -> None:
        path = workspace / "out" / mod_name / "lex_dictionary" / "entries.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"source": "Visible", "target": "可见"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def write_plugin_apply_report(
        self,
        workspace: Path,
        mod_name: str,
        relative_plugin: str,
        *,
        localized: str = "false",
        light_by_extension: str = "false",
        light_by_header: str = "false",
        contains_unsupported_light_formids: str = "false",
        status: str = "ready",
        game_id: str = "fallout4",
        report_input: Path | None = None,
        report_output: Path | None = None,
    ) -> Path:
        relative = Path(relative_plugin)
        original = workspace / "mod" / mod_name / relative
        output = workspace / "out" / mod_name / "tool_outputs" / relative
        bound_input = report_input or original
        bound_output = report_output or output
        report = workspace / "qa" / f"{plugin_artifact_key(mod_name, relative)}.apply.md"
        report.write_text(
            "\n".join(
                [
                    "# Plugin Apply Report",
                    "",
                    f"- game_id: {game_id}",
                    "- Operation: apply",
                    f"- localized: {localized}",
                    f"- light_by_extension: {light_by_extension}",
                    f"- light_by_header: {light_by_header}",
                    f"- contains_unsupported_light_formids: {contains_unsupported_light_formids}",
                    f"- Status: {status}",
                    f"- Input plugin: {bound_input.relative_to(workspace).as_posix()}",
                    f"- Input SHA256: {sha256_file(bound_input)}",
                    f"- Output plugin: {bound_output.relative_to(workspace).as_posix()}",
                    f"- Output SHA256: {sha256_file(bound_output)}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return report

    def test_handoff_file_fingerprint_includes_content_sha256(self) -> None:
        workspace = self.workspace("handoff-file-fingerprint", "skyrim-se")
        watched = workspace / "qa" / "workflow_state.json"
        watched.write_text("one", encoding="utf-8")
        original_stat = watched.stat()
        before = write_agent_handoff.path_snapshot(workspace, "qa/workflow_state.json")

        watched.write_text("two", encoding="utf-8")
        os.utime(watched, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        after = write_agent_handoff.path_snapshot(workspace, "qa/workflow_state.json")

        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertNotEqual(before["content_sha256"], after["content_sha256"])

    def test_handoff_directory_fingerprint_is_content_sensitive_and_deterministic(self) -> None:
        workspace = self.workspace("handoff-directory-fingerprint", "skyrim-se")
        watched = workspace / "mod" / "sample.txt"
        watched.write_text("one", encoding="utf-8")
        original_stat = watched.stat()
        before = write_agent_handoff.path_snapshot(workspace, "mod")
        repeated = write_agent_handoff.path_snapshot(workspace, "mod")

        watched.write_text("two", encoding="utf-8")
        os.utime(watched, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        after = write_agent_handoff.path_snapshot(workspace, "mod")

        self.assertEqual(before["fingerprint"], repeated["fingerprint"])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_handoff_snapshot_rejects_junction_without_reading_external_target(self) -> None:
        workspace = self.workspace("handoff-junction", "skyrim-se")
        external = Path(self.tempdir.name) / "outside-workspace"
        external.mkdir()
        sentinel = external / "must-not-read.txt"
        sentinel.write_text("outside", encoding="utf-8")
        junction = workspace / "mod" / "external"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(created.stdout + created.stderr)
        self.addCleanup(os.rmdir, junction)

        original_open = Path.open
        sentinel_target = sentinel.resolve(strict=True)

        def guarded_open(path: Path, *args: object, **kwargs: object):
            if path.resolve(strict=False) == sentinel_target:
                raise AssertionError("path_snapshot followed a junction outside the workspace")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            snapshot = write_agent_handoff.path_snapshot(workspace, "mod")

        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["kind"], "unsafe_entry")
        self.assertEqual(snapshot["unsafe_entry"], "external")

        checkpoint = write_agent_handoff.build_resume_checkpoint(
            workspace,
            {
                "project_state": "ready",
                "readiness_overall_status": "ready",
                "source_reports": {},
                "task_summary": {},
                "blocking_mods": [],
            },
        )
        result = write_agent_handoff.evaluate_resume_checkpoint(workspace, checkpoint)
        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "stored_snapshot_truncated" for row in result["reasons"])
        )

    def test_unchanged_dictionary_entry_is_not_reported_as_translated(self) -> None:
        workspace = self.workspace("unchanged-dictionary", "skyrim-se")
        dictionary_dir = (
            workspace / "out" / "ExampleMod" / "汉化产出" / "intermediate" / "translation_text_dictionary"
        )
        dictionary_dir.mkdir(parents=True)
        (dictionary_dir / "manifest.json").write_text(
            json.dumps({"TranslatedEntryCount": 1, "SourceFileCount": 1}),
            encoding="utf-8",
        )
        (dictionary_dir / "translation_dictionary.jsonl").write_text(
            json.dumps({"source": "Unchanged", "target": "Unchanged"}) + "\n",
            encoding="utf-8",
        )

        _, status, entries = translation_dictionary_status(workspace, "ExampleMod")

        self.assertEqual(status, "empty-jsonl")
        self.assertEqual(entries, "0")

    def test_protected_runtime_files_copy_unchanged_from_directory_and_zip(self) -> None:
        protected = {
            "Interface/Menu.swf": b"source-swf",
            "Interface/Menu.gfx": b"source-gfx",
            "F4SE/Plugins/Runtime.dll": b"source-dll",
            "F4SE/Runtime.exe": b"source-exe",
            "MCM/Materials/config.json": b'{"title":"source-protected"}',
        }
        for source_kind in ("directory", "zip"):
            with self.subTest(source_kind=source_kind):
                workspace = self.workspace(f"protected-{source_kind}", "fallout4")
                mod_name = "ProtectedRuntime"
                if source_kind == "directory":
                    source = workspace / "mod" / mod_name
                    for relative, content in protected.items():
                        path = source / Path(relative)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                    source_arg = f"mod/{mod_name}"
                else:
                    source = workspace / "mod" / f"{mod_name}.zip"
                    with zipfile.ZipFile(source, "w") as archive:
                        for relative, content in protected.items():
                            archive.writestr(relative, content)
                    source_arg = f"mod/{mod_name}.zip"

                for overlay_root in (
                    workspace / "translated" / "final_mod" / mod_name,
                    workspace / "out" / mod_name / "tool_outputs",
                ):
                    for relative in protected:
                        path = overlay_root / Path(relative)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"overlay-must-not-win")
                self.write_dictionary(workspace, mod_name)

                built = self.run_script(
                    workspace,
                    "build_final_mod.py",
                    "--mod-name",
                    mod_name,
                    "--source-mod-dir",
                    source_arg,
                    "--force",
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
                for relative, content in protected.items():
                    self.assertEqual((final_mod / Path(relative)).read_bytes(), content)

                manifest = json.loads((final_mod / "meta" / "manifest.json").read_text(encoding="utf-8"))
                copied = {Path(item).suffix.lower() for item in manifest["BinaryFilesCopiedUnmodified"]}
                self.assertTrue({".swf", ".gfx", ".dll", ".exe"}.issubset(copied))
                self.assertFalse(
                    any(Path(item).suffix.lower() in {".swf", ".gfx", ".dll", ".exe"} for item in manifest["OverlayFiles"])
                )

                validated = self.run_script(
                    workspace,
                    "validate_final_mod.py",
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

                provenance_path = final_mod / "meta" / "provenance.jsonl"
                original = provenance_path.read_text(encoding="utf-8")
                rows = [json.loads(line) for line in original.splitlines() if line]
                nested_protected_row = next(
                    row for row in rows if row["file"].endswith("MCM/Materials/config.json")
                )
                nested_protected_row["transform"] = "translated-overlay"
                provenance_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                rejected = self.run_script(
                    workspace,
                    "validate_final_mod.py",
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                report = (workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8")
                self.assertIn("Protected file was not copied unchanged", report)

                provenance_path.write_text(original, encoding="utf-8")
                (final_mod / "Interface" / "Menu.swf").write_bytes(b"tampered")
                rejected = self.run_script(
                    workspace,
                    "validate_final_mod.py",
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                report = (workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8")
                self.assertIn("Provenance file_sha256 mismatch", report)

    def test_string_table_overlays_cannot_bypass_controlled_binary_outputs(self) -> None:
        workspace = self.workspace("protected-string-tables", "skyrim-se")
        mod_name = "LocalizedStrings"
        relatives = (
            "Strings/Example_english.strings",
            "Strings/Example_english.dlstrings",
            "Strings/Example_english.ilstrings",
        )
        for relative in relatives:
            source = workspace / "mod" / mod_name / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"source:{relative}".encode("utf-8"))
            for overlay_root in (
                workspace / "translated" / "final_mod" / mod_name,
                workspace / "out" / mod_name / "tool_outputs",
            ):
                overlay = overlay_root / relative
                overlay.parent.mkdir(parents=True, exist_ok=True)
                overlay.write_bytes(b"unverified-string-table-output")
        self.write_dictionary(workspace, mod_name)

        built = self.run_script(
            workspace,
            "build_final_mod.py",
            "--mod-name",
            mod_name,
            "--source-mod-dir",
            f"mod/{mod_name}",
            "--force",
        )

        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
        for relative in relatives:
            self.assertEqual(
                (final_mod / relative).read_bytes(),
                (workspace / "mod" / mod_name / relative).read_bytes(),
            )
        manifest = json.loads(
            (final_mod / "meta" / "manifest.json").read_text(encoding="utf-8")
        )
        warnings = "\n".join(manifest["Warnings"]).replace("\\", "/")
        self.assertIn("Protected binary overlay skipped outside tool_outputs", warnings)
        self.assertIn("is not an allowed plugin or Papyrus binary", warnings)
        self.assertTrue(
            {".strings", ".dlstrings", ".ilstrings"}.issubset(
                {Path(item).suffix.lower() for item in manifest["BinaryFilesCopiedUnmodified"]}
            )
        )

    def test_tool_outputs_only_apply_profile_writable_plugin_and_pex_replacements(self) -> None:
        workspace = self.workspace("tool-output-boundary", "fallout4")
        mod_name = "ToolOutputBoundary"
        source_root = workspace / "mod" / mod_name
        source_files = {
            "Example.esp": b"source-esp",
            "Scripts/Example.pex": b"source-pex",
            "ReadOnly.esl": b"source-esl",
            "Interface/Menu.swf": b"source-swf",
            "F4SE/Plugins/Runtime.dll": b"source-dll",
            "Meshes/Example.nif": b"source-nif",
        }
        for relative, content in source_files.items():
            path = source_root / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        tool_root = workspace / "out" / mod_name / "tool_outputs"
        tool_outputs = {
            "Example.esp": b"translated-esp",
            "Scripts/Example.pex": b"translated-pex",
            "ReadOnly.esl": b"translated-esl-must-not-win",
            "Interface/Menu.swf": b"translated-swf-must-not-win",
            "F4SE/Plugins/Runtime.dll": b"translated-dll-must-not-win",
            "Meshes/Example.nif": b"translated-nif-must-not-win",
            "Config/Added.json": b'{}',
            "Docs/Added.txt": b"must-not-ship",
        }
        for relative, content in tool_outputs.items():
            path = tool_root / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.write_plugin_apply_report(workspace, mod_name, "Example.esp")
        self.write_plugin_apply_report(
            workspace,
            mod_name,
            "ReadOnly.esl",
            light_by_extension="true",
        )
        self.write_dictionary(workspace, mod_name)

        built = self.run_script(
            workspace,
            "build_final_mod.py",
            "--mod-name",
            mod_name,
            "--source-mod-dir",
            f"mod/{mod_name}",
            "--force",
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

        final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
        self.assertEqual((final_mod / "Example.esp").read_bytes(), b"translated-esp")
        self.assertEqual((final_mod / "Scripts/Example.pex").read_bytes(), b"translated-pex")
        for relative in (
            "ReadOnly.esl",
            "Interface/Menu.swf",
            "F4SE/Plugins/Runtime.dll",
            "Meshes/Example.nif",
        ):
            self.assertEqual((final_mod / Path(relative)).read_bytes(), source_files[relative])
        self.assertFalse((final_mod / "Config/Added.json").exists())
        self.assertFalse((final_mod / "Docs/Added.txt").exists())

        manifest = json.loads((final_mod / "meta" / "manifest.json").read_text(encoding="utf-8"))
        applied = {
            (workspace / Path(item)).relative_to(final_mod)
            for item in manifest["BinaryToolOutputsApplied"]
        }
        self.assertEqual(applied, {Path("Example.esp"), Path("Scripts/Example.pex")})
        warnings = "\n".join(manifest["Warnings"]).replace("\\", "/")
        self.assertIn("Config/Added.json", warnings)
        self.assertIn("Docs/Added.txt", warnings)
        self.assertIn("Interface/Menu.swf", warnings)
        self.assertIn("F4SE/Plugins/Runtime.dll", warnings)
        self.assertIn("Meshes/Example.nif", warnings)
        self.assertIn("not an allowed plugin or Papyrus binary", warnings)
        self.assertIn("ReadOnly.esl", warnings)
        self.assertIn("write rejected", warnings)
        self.assertIn("effective level 'read_only'", warnings)

    def test_skyrim_plugin_and_pex_tool_outputs_use_game_bound_apply_evidence(self) -> None:
        workspace = self.workspace("skyrim-tool-output-boundary", "skyrim-se")
        mod_name = "SkyrimToolOutputs"
        source_root = workspace / "mod" / mod_name
        tool_root = workspace / "out" / mod_name / "tool_outputs"
        source_files = {
            "Example.esp": b"source-esp",
            "Scripts/Example.pex": b"source-pex",
            "MCM/Meshes/config.json": b'{"title":"source"}',
        }
        tool_outputs = {
            "Example.esp": b"translated-esp",
            "Scripts/Example.pex": b"translated-pex",
        }
        for relative, content in source_files.items():
            path = source_root / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for relative, content in tool_outputs.items():
            path = tool_root / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        apply_report = self.write_plugin_apply_report(
            workspace,
            mod_name,
            "Example.esp",
            game_id="skyrim-se",
        )

        nested_overlay = workspace / "translated" / "final_mod" / mod_name / "MCM" / "Meshes" / "config.json"
        nested_overlay.parent.mkdir(parents=True, exist_ok=True)
        nested_overlay.write_bytes(b'{"title":"must-not-replace-protected"}')
        self.write_dictionary(workspace, mod_name)

        built = self.run_script(
            workspace,
            "build_final_mod.py",
            "--mod-name",
            mod_name,
            "--source-mod-dir",
            f"mod/{mod_name}",
            "--force",
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

        final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
        self.assertEqual((final_mod / "Example.esp").read_bytes(), b"translated-esp")
        self.assertEqual((final_mod / "Scripts" / "Example.pex").read_bytes(), b"translated-pex")
        self.assertEqual(
            (final_mod / "MCM" / "Meshes" / "config.json").read_bytes(),
            source_files["MCM/Meshes/config.json"],
        )
        self.assertTrue(apply_report.is_file())

    def test_plugin_tool_outputs_require_bound_known_trait_evidence(self) -> None:
        workspace = self.workspace("plugin-tool-output-evidence", "fallout4")
        mod_name = "PluginEvidence"
        source_root = workspace / "mod" / mod_name
        tool_root = workspace / "out" / mod_name / "tool_outputs"
        plugins = (
            "LightHeader.esp",
            "Localized.esm",
            "MissingReport.esp",
            "WrongIdentity.esp",
            "WrongOutput.esp",
            "UnknownTraits.esp",
            "Blocked.esp",
        )
        source_root.mkdir(parents=True)
        for name in plugins:
            (source_root / name).write_bytes(f"source-{name}".encode("ascii"))
            (tool_root / name).parent.mkdir(parents=True, exist_ok=True)
            (tool_root / name).write_bytes(f"translated-{name}".encode("ascii"))

        self.write_plugin_apply_report(
            workspace,
            mod_name,
            "LightHeader.esp",
            light_by_header="true",
        )
        self.write_plugin_apply_report(
            workspace,
            mod_name,
            "Localized.esm",
            localized="true",
        )
        self.write_plugin_apply_report(
            workspace,
            mod_name,
            "WrongIdentity.esp",
            report_input=source_root / "LightHeader.esp",
        )
        self.write_plugin_apply_report(
            workspace,
            mod_name,
            "WrongOutput.esp",
            report_output=tool_root / "LightHeader.esp",
        )
        self.write_plugin_apply_report(
            workspace,
            mod_name,
            "UnknownTraits.esp",
            light_by_header="unknown",
        )
        self.write_plugin_apply_report(
            workspace,
            mod_name,
            "Blocked.esp",
            status="blocked",
        )
        self.write_dictionary(workspace, mod_name)

        built = self.run_script(
            workspace,
            "build_final_mod.py",
            "--mod-name",
            mod_name,
            "--source-mod-dir",
            f"mod/{mod_name}",
            "--force",
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

        final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
        for name in plugins:
            self.assertEqual(
                (final_mod / name).read_bytes(),
                (source_root / name).read_bytes(),
            )
        manifest = json.loads((final_mod / "meta" / "manifest.json").read_text(encoding="utf-8"))
        warnings = "\n".join(manifest["Warnings"]).replace("\\", "/")
        self.assertIn("LightHeader.esp", warnings)
        self.assertIn("effective level 'read_only'", warnings)
        self.assertIn("Localized.esm", warnings)
        self.assertIn("effective level 'inventory_only'", warnings)
        self.assertIn("MissingReport.esp", warnings)
        self.assertIn("Plugin apply report", warnings)
        self.assertIn("WrongIdentity.esp", warnings)
        self.assertIn("Input plugin mismatch", warnings)
        self.assertIn("WrongOutput.esp", warnings)
        self.assertIn("Output plugin mismatch", warnings)
        self.assertIn("UnknownTraits.esp", warnings)
        self.assertIn("unknown write traits", warnings)
        self.assertIn("Blocked.esp", warnings)
        self.assertIn("Status 'blocked'", warnings)

    def test_profile_policy_drives_production_interface_normalize_and_audit(self) -> None:
        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                workspace = self.workspace(f"interface-{game_id}", game_id)
                mod_name = "InterfacePolicy"
                source = workspace / "mod" / mod_name / "Interface" / "translations" / "Example_english.txt"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("$HELLO Hello\n", encoding="utf-8")

                built = self.run_script(
                    workspace,
                    "build_final_mod.py",
                    "--mod-name",
                    mod_name,
                    "--source-mod-dir",
                    f"mod/{mod_name}",
                    "--overlay-translated-files",
                    "false",
                    "--force",
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
                delivered = final_mod / "Interface" / "translations" / "Example_english.txt"
                self.assertTrue(delivered.read_bytes().startswith(b"\xff\xfe"))
                first_bytes = delivered.read_bytes()

                rebuilt = self.run_script(
                    workspace,
                    "build_final_mod.py",
                    "--mod-name",
                    mod_name,
                    "--source-mod-dir",
                    f"mod/{mod_name}",
                    "--overlay-translated-files",
                    "false",
                    "--force",
                )
                self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
                self.assertEqual(delivered.read_bytes(), first_bytes)
                self.assertEqual(first_bytes.count(b"\xff\xfe"), 1)

                audited = self.run_script(
                    workspace,
                    "audit_final_interface_translations.py",
                    "--mod-name",
                    mod_name,
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
                report = (workspace / "qa" / f"{mod_name}.final_interface_runtime.md").read_text(encoding="utf-8")
                self.assertIn(f"GameId: {game_id}", report)
                self.assertIn("Encoding policy: utf-16-le-bom", report)

    def test_missing_or_unknown_interface_policy_fails_closed_in_production_audit(self) -> None:
        for mutation in ("missing", "unknown"):
            with self.subTest(mutation=mutation):
                workspace = self.workspace(f"invalid-policy-{mutation}", "fallout4")
                final_mod = workspace / "out" / "InvalidPolicy" / "汉化产出" / "final_mod"
                translation = final_mod / "Interface" / "translations" / "Example_english.txt"
                translation.parent.mkdir(parents=True, exist_ok=True)
                translation.write_bytes("$HELLO\t你好\r\n".encode("utf-16"))
                plugin_root = Path(self.tempdir.name) / f"plugin-{mutation}"
                profile_dir = plugin_root / "config" / "game_profiles"
                profile_dir.mkdir(parents=True)
                payload = json.loads((ROOT / "config" / "game_profiles" / "fallout4.json").read_text(encoding="utf-8"))
                if mutation == "missing":
                    payload.pop("interface_translation_encoding", None)
                else:
                    payload["interface_translation_encoding"] = "private-unknown-policy"
                (profile_dir / "fallout4.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

                audited = self.run_script(
                    workspace,
                    "audit_final_interface_translations.py",
                    "--mod-name",
                    "InvalidPolicy",
                    "--final-mod-dir",
                    "out/InvalidPolicy/汉化产出/final_mod",
                    plugin_root=plugin_root,
                )
                self.assertNotEqual(audited.returncode, 0, audited.stdout + audited.stderr)
                self.assertIn("interface_translation_encoding", audited.stdout + audited.stderr)

    def test_export_agent_context_main_uses_strict_bounded_allowlist(self) -> None:
        workspace = self.workspace("agent-packet", "fallout4")
        sentinel = "PRIVATE_SENTINEL_6B1"
        actions = [
            {
                "mod": f"Mod{index}",
                "task_id": f"task-{index}",
                "command": f"python scripts/safe_{index}.py",
                "type": "python",
                "risk": "low",
                "can_run_parallel": True,
                "resource_locks": [f"mod:Mod{index}"],
                "must_read_evidence": [f"qa/mod-{index}.md"],
                "private": sentinel,
            }
            for index in range(export_agent_context.MAX_NEXT_ACTIONS + 5)
        ]
        handoff = {
            **game_context_metadata(load_game_profile("fallout4")),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(
                load_agent_capabilities()
            ),
            "project_state": "blocked",
            "readiness_overall_status": "blocked",
            "workflow_health": {"verdict": "blocked", "blocking_issues": 3, "private": sentinel},
            "task_summary": {"pending_executable": 4, "pending_total": 9, "parallel_safe": 2, "private": sentinel},
            "blocking_mods": [
                {
                    "mod": "Example",
                    "state": "blocked",
                    "primary_blocker": "adapter_not_ready",
                    "task_id": "task-0",
                    "can_run_parallel": False,
                    "resource_locks": ["mod:Example"],
                    "safe_next_action": actions[0],
                    "private_payload": sentinel * 1000,
                }
            ],
            "private_large_payload": sentinel * 5000,
        }
        (workspace / "qa" / "codex_handoff.json").write_text(
            json.dumps({"private_codex_payload": sentinel * 5000}),
            encoding="utf-8",
        )
        handoff["resume_checkpoint"] = write_agent_handoff.build_resume_checkpoint(
            workspace,
            handoff,
        )
        handoff["resume_checkpoint"]["next_actions"] = actions
        handoff["resume_checkpoint"]["private_payload"] = sentinel * 2000
        handoff["resume_checkpoint"]["checkpoint_id"] = write_agent_handoff.checkpoint_id_for(
            handoff["resume_checkpoint"]
        )
        checkpoint_id = handoff["resume_checkpoint"]["checkpoint_id"]
        (workspace / "qa" / "agent_handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        exported = self.run_script(
            workspace,
            "export_agent_context.py",
            "--agent",
            "opencode",
            "--output",
            "qa/agent_context_prompts/latest.opencode.context.md",
        )
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        packet = workspace / "qa" / "agent_context_prompts" / "latest.opencode.context.md"
        data = packet.read_bytes()
        text = data.decode("utf-8")
        self.assertLessEqual(len(data), export_agent_context.MAX_PACKET_BYTES)
        self.assertNotIn(sentinel, text)
        self.assertNotIn("Agent Handoff", text)
        self.assertNotIn("Codex Handoff Fallback", text)
        self.assertIn("## Workflow Status", text)
        self.assertIn("## Next Actions", text)
        self.assertIn(checkpoint_id, text)
        self.assertLessEqual(text.count('"command"'), export_agent_context.MAX_NEXT_ACTIONS)

        original_packet = packet.read_bytes()
        for field, polluted in (
            ("game_id", {"private": sentinel}),
            ("task_summary", {"pending_total": {"private": sentinel}}),
        ):
            poisoned = dict(handoff)
            poisoned[field] = polluted
            (workspace / "qa" / "agent_handoff.json").write_text(
                json.dumps(poisoned, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            rejected = self.run_script(
                workspace,
                "export_agent_context.py",
                "--agent",
                "opencode",
                "--output",
                "qa/agent_context_prompts/latest.opencode.context.md",
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
            self.assertEqual(packet.read_bytes(), original_packet)
            self.assertNotIn(sentinel.encode("utf-8"), packet.read_bytes())

        (workspace / "qa" / "agent_handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {
                "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            },
            clear=False,
        ), mock.patch.object(export_agent_context, "MAX_PACKET_BYTES", 1), mock.patch.object(
            sys,
            "argv",
            [
                "export_agent_context.py",
                "--agent",
                "opencode",
                "--output",
                "qa/agent_context_prompts/latest.opencode.context.md",
            ],
        ):
            self.assertEqual(export_agent_context.main(), 1)
        self.assertEqual(packet.read_bytes(), original_packet)

    def test_init_opencode_game_cli_initializes_and_rejects_marker_conflicts(self) -> None:
        init_temp = tempfile.TemporaryDirectory()
        self.addCleanup(init_temp.cleanup)
        init_root = Path(init_temp.name)
        fallout_workspace = init_root / "opencode-fallout"
        created = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(fallout_workspace),
                "--game",
                "fallout4",
                "--tool-setup",
                "skip",
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        marker_path = fallout_workspace / ".skyrim-chs-workspace.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["game_id"], "fallout4")

        unchanged = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(fallout_workspace),
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(unchanged.returncode, 0, unchanged.stdout + unchanged.stderr)

        conflict = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(fallout_workspace),
                "--game",
                "skyrim-se",
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(conflict.returncode, 0, conflict.stdout + conflict.stderr)
        self.assertIn("conflicts", conflict.stdout + conflict.stderr)
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["game_id"], "fallout4")

        skyrim_workspace = init_root / "opencode-explicit-skyrim"
        omitted = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(skyrim_workspace),
                "--tool-setup",
                "skip",
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(omitted.returncode, 0, omitted.stdout + omitted.stderr)
        self.assertIn("--game", omitted.stdout + omitted.stderr)
        self.assertFalse(skyrim_workspace.exists())


if __name__ == "__main__":
    unittest.main()

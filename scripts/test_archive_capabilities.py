from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_resolver import resolve_capability  # noqa: E402
import audit_archive_coverage as archive_audit  # noqa: E402
from game_context import load_game_profile  # noqa: E402
from route_translation_task import route_for  # noqa: E402


def workspace_marker(game_id: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "bethesda-mod-chs-translation-workspace",
        "plugin_name": "skyrim-mod-chs-translation",
        "game_id": game_id,
        "game_profile": game_id,
    }


class ArchiveCapabilityResolutionTests(unittest.TestCase):
    def test_profiles_keep_bsa_and_ba2_capabilities_distinct(self) -> None:
        skyrim = load_game_profile("skyrim-se")
        fallout4 = load_game_profile("fallout4")

        cases = (
            (skyrim, "archive.bsa", "inventory", True, "bethesda-bsa"),
            (skyrim, "archive.bsa", "read", True, "bethesda-bsa"),
            (skyrim, "archive.ba2", "inventory", True, "bethesda-ba2"),
            (skyrim, "archive.ba2", "read", False, "bethesda-ba2"),
            (fallout4, "archive.bsa", "inventory", False, "bethesda-bsa"),
            (fallout4, "archive.bsa", "read", False, "bethesda-bsa"),
            (fallout4, "archive.ba2", "inventory", True, "bethesda-ba2"),
            (fallout4, "archive.ba2", "read", True, "bethesda-ba2"),
        )
        for context, capability, operation, supported, adapter_id in cases:
            with self.subTest(
                game=context.game_id,
                capability=capability,
                operation=operation,
            ):
                decision = resolve_capability(context, capability, operation)
                self.assertEqual(decision.supported, supported)
                self.assertEqual(decision.adapter_id, adapter_id)

    def test_router_uses_archive_and_loose_text_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mod").mkdir()
            files = {
                extension: root / "mod" / f"Example{extension}"
                for extension in (".bsa", ".ba2", ".txt")
            }
            for path in files.values():
                path.write_bytes(b"fixture")

            skyrim = load_game_profile("skyrim-se")
            fallout4 = load_game_profile("fallout4")
            self.assertEqual(route_for(root, files[".bsa"], skyrim).status, "ready")
            self.assertIn("inventory-only", route_for(root, files[".ba2"], skyrim).notes)
            self.assertEqual(route_for(root, files[".bsa"], fallout4).status, "blocked")
            self.assertEqual(route_for(root, files[".ba2"], fallout4).status, "ready")

            for context in (skyrim, fallout4):
                route = route_for(root, files[".txt"], context)
                self.assertEqual(route.status, "ready")
                self.assertEqual(route.skill, "skills/text-resource-translation")
                self.assertIn("loose_text=stable", route.notes)


class BsaCapabilityWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name)
        for relative in ("mod", "work/archive_extracts", "qa", "tools"):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)
        self.archive = self.workspace / "mod" / "Example.bsa"
        self.archive.write_bytes(b"BSA-fixture")
        self.tool = self.workspace / "tools" / "fake_bsa.py"
        self.tool.write_text(
            textwrap.dedent(
                """
                import argparse
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("archive")
                parser.add_argument("-i")
                parser.add_argument("-o")
                parser.add_argument("filters", nargs="*")
                args = parser.parse_args()
                target = Path(args.o) / "Interface" / "translations" / "Example_en.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("$HELLO\\tHello", encoding="utf-8")
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def run_wrapper(self, game_id: str, *extra: str) -> subprocess.CompletedProcess[str]:
        (self.workspace / ".skyrim-chs-workspace.json").write_text(
            json.dumps(workspace_marker(game_id), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "SKYRIM_CHS_WORKSPACE_ROOT": str(self.workspace),
            "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
        }
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "invoke_bsa_file_extractor_safe.py"),
                "--archive-path",
                "mod/Example.bsa",
                "--output-dir",
                "work/archive_extracts/TestMod/Example",
                "--tool-path",
                "tools/fake_bsa.py",
                *extra,
            ],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def test_skyrim_materialization_writes_standard_result_when_requested(self) -> None:
        result = self.run_wrapper(
            "skyrim-se",
            "--adapter-result-path",
            "qa/Example.bsa.adapter_result.json",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt_path = self.workspace / "qa" / "Example.bsa.adapter_result.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["operation"], "extract")
        self.assertEqual(receipt["adapter_id"], "bethesda-bsa")
        self.assertEqual(receipt["mod_name"], "TestMod")
        self.assertEqual(
            receipt["inputs"],
            [
                {
                    "path": "mod/Example.bsa",
                    "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                }
            ],
        )
        self.assertTrue(receipt["evidence_files"])
        self.assertTrue(receipt["artifacts"])
        artifact = receipt["artifacts"][0]
        artifact_path = self.workspace / artifact["path"]
        self.assertEqual(
            artifact["sha256"],
            hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        )
        manifest_value = "out/TestMod/archive_audits/Example/manifest.json"
        self.assertIn(manifest_value, receipt["evidence_files"])
        manifest = json.loads((self.workspace / manifest_value).read_text(encoding="utf-8"))
        self.assertEqual(manifest["ArchivePath"], "mod/Example.bsa")
        self.assertEqual(manifest["ArchiveSha256"], receipt["inputs"][0]["sha256"])
        self.assertEqual(manifest["Files"][0]["ProjectPath"], artifact["path"])

    def test_fallout4_bsa_is_blocked_and_invalidates_old_result(self) -> None:
        receipt_path = self.workspace / "qa" / "Example.bsa.adapter_result.json"
        receipt_path.write_text('{"status":"success"}\n', encoding="utf-8")
        result = self.run_wrapper(
            "fallout4",
            "--adapter-result-path",
            "qa/Example.bsa.adapter_result.json",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["error_code"], "capability_unsupported")
        self.assertFalse((self.workspace / "work/archive_extracts/TestMod/Example").exists())

    def test_bsa_failure_cleans_reused_empty_output_and_writes_error_result(self) -> None:
        output_dir = self.workspace / "work/archive_extracts/TestMod/Example"
        output_dir.mkdir(parents=True)
        self.tool.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "output = Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "(output / 'partial.txt').write_text('partial', encoding='utf-8')\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        result = self.run_wrapper(
            "skyrim-se",
            "--adapter-result-path",
            "qa/Example.bsa.adapter_result.json",
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(output_dir.is_dir())
        self.assertEqual(list(output_dir.iterdir()), [])
        receipt = json.loads(
            (self.workspace / "qa/Example.bsa.adapter_result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_code"], "adapter_failed")

    def test_bsa_rejects_hardlinked_output_with_and_without_adapter_result(self) -> None:
        source = self.workspace / "tools/hardlink-source.txt"
        source.write_text("outside-output", encoding="utf-8")
        archive_hash = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.tool.write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "output = Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "target = output / 'Interface' / 'Hardlinked.txt'\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            f"os.link({str(source)!r}, target)\n",
            encoding="utf-8",
        )
        for extra in (
            (),
            ("--adapter-result-path", "qa/Example.bsa.adapter_result.json"),
        ):
            with self.subTest(adapter_result=bool(extra)):
                result = self.run_wrapper("skyrim-se", *extra)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(
                    (self.workspace / "work/archive_extracts/TestMod/Example").exists()
                )
                self.assertEqual(
                    hashlib.sha256(self.archive.read_bytes()).hexdigest(), archive_hash
                )
                self.assertEqual(source.read_text(encoding="utf-8"), "outside-output")

    def test_bsa_rejects_symlinked_output_when_windows_allows_symlinks(self) -> None:
        source = self.workspace / "tools/symlink-source.txt"
        source.write_text("outside-output", encoding="utf-8")
        probe = self.workspace / "tools/symlink-probe.txt"
        try:
            os.symlink(source, probe)
        except OSError as exc:
            self.skipTest(f"Windows symlink creation unavailable: {exc}")
        else:
            probe.unlink()
        self.tool.write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "output = Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "target = output / 'Interface' / 'Symlinked.txt'\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            f"os.symlink({str(source)!r}, target)\n",
            encoding="utf-8",
        )
        result = self.run_wrapper("skyrim-se")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.workspace / "work/archive_extracts/TestMod/Example").exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "outside-output")

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_bsa_rejects_junction_output_when_windows_allows_junctions(self) -> None:
        source = self.workspace / "tools/junction-source"
        source.mkdir()
        sentinel = source / "sentinel.txt"
        sentinel.write_text("outside-output", encoding="utf-8")
        probe = self.workspace / "tools/junction-probe"
        probe_result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(probe), str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if probe_result.returncode != 0:
            self.skipTest(f"Windows junction creation unavailable: {probe_result.stderr}")
        probe.rmdir()
        self.tool.write_text(
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            "output = Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "target = output / 'Junctioned'\n"
            f"result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), {str(source)!r}])\n"
            "raise SystemExit(result.returncode)\n",
            encoding="utf-8",
        )
        result = self.run_wrapper("skyrim-se")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.workspace / "work/archive_extracts/TestMod/Example").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside-output")

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_bsa_cleanup_never_follows_replaced_reused_output_root(self) -> None:
        output = self.workspace / "work/archive_extracts/TestMod/Example"
        output.mkdir(parents=True)
        sentinel = self.workspace / "mod/sentinel.txt"
        sentinel.write_bytes(b"must-survive")
        archive_bytes = self.archive.read_bytes()
        probe = self.workspace / "tools/reused-root-junction-probe"
        probe_result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(probe), str(self.workspace / "mod")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if probe_result.returncode != 0:
            self.skipTest(f"Windows junction creation unavailable: {probe_result.stderr}")
        probe.rmdir()

        cases = (
            (7, ()),
            (0, ("--adapter-result-path", "qa/Example.bsa.adapter_result.json")),
        )
        for exit_code, extra in cases:
            with self.subTest(exit_code=exit_code, adapter_result=bool(extra)):
                self.tool.write_text(
                    "import os, subprocess, sys\n"
                    "from pathlib import Path\n"
                    "output = Path(sys.argv[sys.argv.index('-o') + 1])\n"
                    "os.rmdir(output)\n"
                    f"result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(output), {str(self.workspace / 'mod')!r}])\n"
                    f"raise SystemExit({exit_code} if result.returncode == 0 else result.returncode)\n",
                    encoding="utf-8",
                )
                result = self.run_wrapper("skyrim-se", *extra)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.archive.read_bytes(), archive_bytes)
                self.assertEqual(sentinel.read_bytes(), b"must-survive")
                output_stat = output.lstat()
                self.assertFalse(output.is_symlink())
                self.assertFalse(
                    output_stat.st_file_attributes
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                self.assertTrue(output.is_dir())
                self.assertEqual(list(output.iterdir()), [])


class ArchiveAuditPathSafetyTests(unittest.TestCase):
    def test_external_archive_symlink_is_error_evidence_without_hashing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            root = Path(temporary)
            workspace = root / "work/extracted_mods/TestMod"
            final_mod = root / "out/TestMod/final_mod"
            workspace.mkdir(parents=True)
            final_mod.mkdir(parents=True)
            target = Path(external) / "External.ba2"
            target.write_bytes(b"external-archive")
            linked = workspace / "Linked.ba2"
            try:
                os.symlink(target, linked)
            except OSError as exc:
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            manifest = archive_audit.evidence_path(root, "TestMod", linked)
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "ModName": "TestMod",
                        "ArchivePath": "Linked.ba2",
                        "ArchiveSha256": "0" * 64,
                        "ArchiveSize": target.stat().st_size,
                        "ExtractedDir": "work/archive_extracts/TestMod/Linked",
                        "FilesScanned": 0,
                        "ByKind": {},
                        "ByRisk": {},
                        "Files": [],
                        "Safety": {
                            "ProjectLocalOnly": True,
                            "ArchiveModified": False,
                            "ExtractedContentModified": False,
                            "RealGameDirectoriesAccessed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                archive_audit,
                "sha256_file",
                side_effect=AssertionError("external archive target was hashed"),
            ) as hash_file:
                rows = archive_audit.collect_archives(
                    root,
                    "TestMod",
                    workspace,
                    final_mod,
                    load_game_profile("fallout4"),
                )
            hash_file.assert_not_called()
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0].EvidenceValid)
            self.assertTrue(
                any("unsafe-archive-path" in issue for issue in rows[0].EvidenceIssues)
            )


if __name__ == "__main__":
    unittest.main()

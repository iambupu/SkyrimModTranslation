from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import prepare_mod_workspace  # noqa: E402


class PrepareModWorkspaceTests(unittest.TestCase):
    def create_workspace_source(self, root: Path) -> Path:
        (root / ".skyrim-chs-workspace.json").write_text(
            json.dumps({"game_id": "skyrim-se"}),
            encoding="utf-8",
        )
        source = root / "mod" / "Fixture"
        text_file = source / "Interface" / "translations" / "fixture_english.txt"
        text_file.parent.mkdir(parents=True)
        text_file.write_text("$HELLO\tHello\n", encoding="utf-8")
        return source

    def run_prepare_main(self, root: Path) -> int:
        environment = {
            "SKYRIM_CHS_WORKSPACE_ROOT": str(root),
            "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
        }
        argv = [
            "prepare_mod_workspace.py",
            "--mod-name",
            "Fixture",
            "--source-path",
            "mod/Fixture",
        ]
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            sys,
            "argv",
            argv,
        ):
            return prepare_mod_workspace.main()

    def test_directory_source_is_copied_to_extracted_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self.create_workspace_source(root)
            binary_file = source / "Scripts" / "Example.pex"
            binary_file.parent.mkdir(parents=True)
            binary_file.write_bytes(b"fake pex")

            exit_code = self.run_prepare_main(root)

            output_dir = root / "work" / "extracted_mods" / "Fixture"
            self.assertEqual(exit_code, 0)
            self.assertTrue(
                (output_dir / "Interface" / "translations" / "fixture_english.txt").is_file()
            )
            self.assertTrue((output_dir / "Scripts" / "Example.pex").is_file())

            report = root / "qa" / "archive_extraction_report.md"
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("# Input Preparation Report", report_text)
            self.assertIn("Directory source: mod", report_text)
            self.assertIn("Binary files copied unmodified: 1", report_text)

    def test_main_writes_advisory_scale_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_workspace_source(root)

            exit_code = self.run_prepare_main(root)

            report_path = root / "qa" / "Fixture.scale_assessment.json"
            self.assertEqual(exit_code, 0)
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["scale_level"], "L0")
            self.assertEqual(report["risk_level"], "R0")
            self.assertEqual(report["recommendations_status"], "advisory-not-enforced")
            self.assertFalse(report["execution_behavior_changed"])

    def test_scale_assessment_failure_blocks_unbounded_input_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_workspace_source(root)

            with mock.patch.object(
                prepare_mod_workspace,
                "assess_source",
                side_effect=RuntimeError("fixture assessment failure"),
            ):
                with self.assertRaisesRegex(ValueError, "bounded materialization is blocked"):
                    self.run_prepare_main(root)

            self.assertFalse((root / "work" / "extracted_mods" / "Fixture").exists())
            workflow_report = (root / "qa" / "workflow_report.md").read_text(encoding="utf-8")
            self.assertIn("Status: blocked", workflow_report)
            self.assertIn("bounded materialization is blocked", workflow_report)
            execution = json.loads(
                (root / "qa" / "Fixture.scale_execution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(execution["status"], "blocked")


if __name__ == "__main__":
    unittest.main()

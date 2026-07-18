from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_non_gui_translation_workflow as workflow


class NonGuiWorkflowCoverageTests(unittest.TestCase):
    def run_stage(self, *, missing: int, unverified: int, blocking: int) -> tuple[bool, list, list]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod_name = "TestMod"
            workspace = root / "work" / "extracted_mods" / mod_name
            final_mod = root / "out" / mod_name / "汉化产出" / "final_mod"
            report = root / "out" / mod_name / "qa" / "non_gui_translation_coverage.md"
            workspace.mkdir(parents=True)
            final_mod.mkdir(parents=True)
            report.parent.mkdir(parents=True)
            report.write_text(
                "\n".join(
                    [
                        "# Non-GUI Translation Coverage Audit",
                        "",
                        f"- Missing: {missing}",
                        f"- Unverified: {unverified}",
                        f"- Blocking: {blocking}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(["python"], 0, stdout="ok\n", stderr="")
            steps: list = []
            issues: list = []
            with mock.patch.object(workflow, "run_python_script", return_value=completed):
                result = workflow.run_quick_coverage_stage(
                    root, steps, issues, mod_name, workspace, final_mod
                )
            return result, steps, issues

    def test_nonblocking_unverified_rows_do_not_fail_quick_gate(self) -> None:
        result, steps, issues = self.run_stage(missing=0, unverified=144, blocking=0)

        self.assertTrue(result)
        self.assertEqual(steps[-1].Status, "passed")
        self.assertEqual(issues, [])

    def test_confirmed_missing_or_blocking_rows_fail_quick_gate(self) -> None:
        for missing, blocking in ((1, 0), (0, 1)):
            with self.subTest(missing=missing, blocking=blocking):
                result, steps, issues = self.run_stage(
                    missing=missing, unverified=2, blocking=blocking
                )
                self.assertFalse(result)
                self.assertEqual(steps[-1].Status, "failed")
                self.assertEqual(len(issues), 1)


if __name__ == "__main__":
    unittest.main()

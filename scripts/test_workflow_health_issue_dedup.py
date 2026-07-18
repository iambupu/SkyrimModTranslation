from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from test_workflow_health import Issue, summarize_readiness_blockers  # noqa: E402


class WorkflowHealthIssueDedupTests(unittest.TestCase):
    def test_readiness_summary_is_note_when_direct_root_cause_exists(self) -> None:
        issues = [Issue("error", "strict-gate", "Strict gate is not clean.", "qa/Example.non_gui_qa_gates.md")]
        notes: list[str] = []

        summarize_readiness_blockers(issues, notes, "2")

        self.assertEqual(len(issues), 1)
        self.assertTrue(any("2 blocking issue" in note for note in notes))

    def test_readiness_summary_remains_fallback_without_direct_root_cause(self) -> None:
        issues: list[Issue] = []
        notes: list[str] = []

        summarize_readiness_blockers(issues, notes, "1")

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].Area, "readiness")


if __name__ == "__main__":
    unittest.main()

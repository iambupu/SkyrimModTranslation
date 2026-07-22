from __future__ import annotations

import unittest

from run_non_gui_qa_gates import coverage_is_complete


class NonGuiStrictCoverageTests(unittest.TestCase):
    def test_nonblocking_unverified_rows_do_not_change_completion_state(self) -> None:
        self.assertTrue(coverage_is_complete(missing=0, blocking=0))

    def test_missing_or_explicit_blocker_prevents_completion(self) -> None:
        self.assertFalse(coverage_is_complete(missing=1, blocking=0))
        self.assertFalse(coverage_is_complete(missing=0, blocking=1))


if __name__ == "__main__":
    unittest.main()

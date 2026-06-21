from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from audit_final_review_quality import audit_row  # noqa: E402


class FinalReviewQualityTests(unittest.TestCase):
    def test_untranslated_review_blocks_unchanged_english(self) -> None:
        findings = []
        audit_row(
            ROOT,
            ROOT / "qa" / "items.jsonl",
            1,
            {
                "File": "Example.esp",
                "Source": "Clumsy bitch!",
                "Final": "Clumsy bitch!",
                "Risk": "untranslated-review",
                "Context": "record=INFO; subrecord=NAM1",
            },
            findings,
            set(),
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].Severity, "error")
        self.assertEqual(findings[0].Code, "unchanged-english")


if __name__ == "__main__":
    unittest.main()

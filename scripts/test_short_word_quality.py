from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from audit_final_review_quality import audit_row
from proofread_translation import load_allowed_words, remove_allowed_ascii_tokens


class ShortWordQualityTests(unittest.TestCase):
    def test_mcm_untranslated_review_blocks_unchanged_two_letter_label(self) -> None:
        findings = []
        audit_row(
            Path.cwd(),
            Path.cwd() / "qa" / "items.jsonl",
            1,
            {
                "File": "MCM/config.json",
                "Source": "On",
                "Final": "On",
                "Risk": "untranslated-review",
                "Context": "field=label",
            },
            findings,
            set(),
        )

        self.assertEqual([(item.Severity, item.Code) for item in findings], [("error", "unchanged-english")])

    def test_final_review_reports_two_letter_residual_english(self) -> None:
        findings = []
        audit_row(
            Path.cwd(),
            Path.cwd() / "qa" / "items.jsonl",
            1,
            {
                "File": "MCM/config.json",
                "Source": "Confirm",
                "Final": "确认 OK",
                "Risk": "changed",
                "Context": "field=label",
            },
            findings,
            set(),
        )

        self.assertIn(("warning", "residual-english"), [(item.Severity, item.Code) for item in findings])

    def test_explicit_glossary_can_approve_word_excluded_from_auto_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            auto_source = root / "mod" / "Classic" / "Config.json"
            auto_source.parent.mkdir(parents=True)
            auto_source.write_text("{}\n", encoding="utf-8")

            auto_words = load_allowed_words(root)
            self.assertRegex(remove_allowed_ascii_tokens("classic", auto_words), r"\bclassic\b")

            glossary = root / "glossary" / "mod_terms.md"
            glossary.parent.mkdir(parents=True)
            glossary.write_text(
                "| English | 简体中文 |\n| --- | --- |\n| classic | 经典版 |\n",
                encoding="utf-8",
            )

            explicit_words = load_allowed_words(root)
            self.assertNotRegex(remove_allowed_ascii_tokens("classic", explicit_words), r"\bclassic\b")


if __name__ == "__main__":
    unittest.main()

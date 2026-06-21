from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from export_esp_strings import classify_string  # noqa: E402


class ExportEspStringsTests(unittest.TestCase):
    def test_dialog_response_text_is_candidate(self) -> None:
        risk, reason = classify_string("INFO", "NAM1", "Clumsy bitch!")
        self.assertEqual(risk, "candidate")
        self.assertEqual(reason, "dialog-response-text")

    def test_dialog_editor_ids_stay_protected(self) -> None:
        risk, reason = classify_string("DIAL", "EDID", "Arial_VS_DialogueGuards")
        self.assertEqual(risk, "protected")
        self.assertEqual(reason, "protected-subrecord-EDID")

    def test_identifier_like_full_values_stay_protected(self) -> None:
        risk, reason = classify_string("QUST", "FULL", "Arial_VS_DialogueGuards")
        self.assertEqual(risk, "protected")
        self.assertEqual(reason, "identifier-like")

    def test_plain_single_word_full_values_remain_candidates(self) -> None:
        risk, reason = classify_string("CELL", "FULL", "Breezehome")
        self.assertEqual(risk, "candidate")
        self.assertEqual(reason, "visible-subrecord-FULL")


if __name__ == "__main__":
    unittest.main()

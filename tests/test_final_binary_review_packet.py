from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from new_final_binary_review_packet import likely_untranslated_candidate  # noqa: E402


class FinalBinaryReviewPacketTests(unittest.TestCase):
    def test_pex_cmp_strings_are_not_untranslated_candidates(self) -> None:
        self.assertFalse(
            likely_untranslated_candidate(
                "Vanilla Sexism Configuration",
                "review",
                "object=Arial_VS_MCMScript; function=OnPageReset; opcode=CMP_EQ; instruction=0; argument=2",
                set(),
            )
        )

    def test_pex_diagnostic_logs_are_not_untranslated_candidates(self) -> None:
        self.assertFalse(
            likely_untranslated_candidate(
                "Arial_VS spouse controller: spouse alias mismatch, restarting spouse dialogue quests",
                "review",
                "object=Arial_VS_SpouseScript; function=CheckSpouseAliases; opcode=CALLSTATIC; instruction=31; argument=4",
                set(),
            )
        )

    def test_pex_diagnostic_log_fragments_are_not_untranslated_candidates(self) -> None:
        self.assertFalse(
            likely_untranslated_candidate(
                " local=",
                "review",
                "object=Arial_VS_SpouseScript; function=CheckSpouseAliases; opcode=STRCAT; instruction=24; argument=2",
                set(),
            )
        )

    def test_pex_visible_mcm_text_remains_untranslated_candidate(self) -> None:
        self.assertTrue(
            likely_untranslated_candidate(
                "Enable or disable all dialogue introduced by this mod.",
                "review",
                "object=Arial_VS_MCMScript; function=OnOptionHighlight; opcode=CALLMETHOD; instruction=2; argument=4",
                set(),
            )
        )


if __name__ == "__main__":
    unittest.main()

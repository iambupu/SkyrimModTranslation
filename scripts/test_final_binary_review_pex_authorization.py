from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from new_final_binary_review_packet import approved_pex_translation_targets, pex_location_identity


class FinalBinaryReviewPexAuthorizationTests(unittest.TestCase):
    def write_rows(self, rows: list[dict]) -> tuple[Path, Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        pex = root / "out" / "TestMod" / "汉化产出" / "final_mod" / "Scripts" / "Test.pex"
        pex.parent.mkdir(parents=True)
        pex.write_bytes(b"fixture")
        translation = root / "work" / "normalized" / "TestMod" / "pex_apply" / "Test.translation.jsonl"
        translation.parent.mkdir(parents=True)
        translation.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return root, pex

    def test_exact_confirmed_visible_row_is_authorized_for_review_reclassification(self) -> None:
        row = {
            "ModName": "Test.pex",
            "Source": "Master on/off for the whole mod.",
            "Result": "控制整个 Mod 的总开关。",
            "risk": "candidate",
            "object_name": "Test",
            "state_name": "",
            "function_name": "OnOptionHighlight",
            "opcode": "CALLMETHOD",
            "instruction_index": 10,
            "argument_index": 4,
            "notes": "Agent model confirmed player-visible from PSC call context.",
        }
        root, pex = self.write_rows([row])

        approved = approved_pex_translation_targets(root, "TestMod", pex)

        self.assertEqual(approved[pex_location_identity(row)], (row["Source"], row["Result"]))

    def test_logic_compare_row_is_never_authorized_by_visible_note(self) -> None:
        row = {
            "ModName": "Test.pex",
            "Source": "Settings",
            "Result": "设置",
            "risk": "candidate",
            "object_name": "Test",
            "state_name": "",
            "function_name": "OnPageReset",
            "opcode": "CMP_EQ",
            "instruction_index": 3,
            "argument_index": 1,
            "notes": "Agent model confirmed player-visible from PSC call context.",
        }
        root, pex = self.write_rows([row])

        self.assertEqual(approved_pex_translation_targets(root, "TestMod", pex), {})


if __name__ == "__main__":
    unittest.main()

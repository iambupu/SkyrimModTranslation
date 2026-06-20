from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pex_translation_safety import (  # noqa: E402
    normalized_pex_translation_line,
    pex_row_matches,
    pex_translation_row_protects_source,
    pex_translation_skip_reason,
)


class PexTranslationSafetyTests(unittest.TestCase):
    def test_path_fields_match_pex(self) -> None:
        pex = Path("Scripts/Example.pex")
        self.assertTrue(pex_row_matches({"source_file": "Scripts/Example.pex"}, pex))
        self.assertTrue(pex_row_matches({"Path": "Interface/../Scripts/Example.pex"}, pex))

    def test_compare_opcode_is_protected(self) -> None:
        row = {"Source": "Menu Page", "Result": "菜单页面", "opcode": "CMP_EQ", "risk": "candidate"}
        self.assertEqual(pex_translation_skip_reason(row), "logic compare opcode: CMP_EQ")
        self.assertTrue(pex_translation_row_protects_source(row))

    def test_empty_target_skips_row_without_source_level_protection(self) -> None:
        row = {"Source": "Visible text", "Result": "", "risk": "candidate"}
        self.assertEqual(pex_translation_skip_reason(row), "missing target")
        self.assertFalse(pex_translation_row_protects_source(row))

    def test_csharp_field_aliases_are_supported(self) -> None:
        row = {"original": "Visible text", "translation": "可见文本", "risk": "candidate"}
        self.assertEqual(pex_translation_skip_reason(row), "")

    def test_normalization_overwrites_blank_canonical_fields(self) -> None:
        row = {
            "Source": "",
            "Result": "",
            "original": "Visible text",
            "translation": "可见文本",
            "source_file": "Scripts/Example.pex",
        }
        normalized = json.loads(normalized_pex_translation_line(row, Path("Scripts/Example.pex"), "{}"))
        self.assertEqual(normalized["ModName"], "Example.pex")
        self.assertEqual(normalized["Source"], "Visible text")
        self.assertEqual(normalized["Result"], "可见文本")


if __name__ == "__main__":
    unittest.main()

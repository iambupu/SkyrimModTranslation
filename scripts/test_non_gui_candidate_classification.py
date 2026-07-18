from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from extract_non_gui_candidates import classify_string, extract_config_comments
from extract_mcm_text import looks_like_path_or_identifier


class NonGuiCandidateClassificationTests(unittest.TestCase):
    def test_debug_brand_prefix_is_protected_even_in_visible_api(self) -> None:
        self.assertEqual(
            classify_string("[ExprDirector] ", 'Debug.Notification("[ExprDirector] " + msg)'),
            ("protected", "brand-or-debug-prefix"),
        )

    def test_slashes_in_visible_prose_are_not_paths(self) -> None:
        self.assertEqual(
            classify_string("Enable partner/NPC faces", "SetInfoText"),
            ("candidate", "visible-api-context"),
        )

    def test_data_paths_remain_protected(self) -> None:
        self.assertEqual(
            classify_string("Scripts/Source/Controller", ""),
            ("protected", "path-like"),
        )

    def test_single_word_visible_field_is_candidate(self) -> None:
        self.assertEqual(
            classify_string("Enabled", "label"),
            ("candidate", "visible-field-context"),
        )

    def test_single_word_without_visible_context_remains_protected(self) -> None:
        self.assertEqual(
            classify_string("InternalSettingName", ""),
            ("protected", "identifier-like"),
        )

    def test_mcm_plain_word_is_not_path_or_identifier(self) -> None:
        self.assertFalse(looks_like_path_or_identifier("Enabled"))
        self.assertTrue(looks_like_path_or_identifier("Scripts/Controller.pex"))

    def test_ini_and_toml_extract_only_full_line_comments(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ini = root / "Settings.ini"
            ini.write_text(
                "[Main]\n; Controls the visible holster.\nTitle=Do not extract this value\n# Second visible note\n; Rifles\n; InternalSettingName\n",
                encoding="utf-8",
            )
            toml = root / "Settings.toml"
            toml.write_text(
                '# Controls the visible menu.\ntitle = "Do not extract this value"\n',
                encoding="utf-8",
            )

            ini_rows = extract_config_comments(root, ini)
            toml_rows = extract_config_comments(root, toml)

            self.assertEqual([row["line"] for row in ini_rows], [2, 4, 5, 6])
            self.assertEqual([row["comment_prefix"] for row in ini_rows], [";", "#", ";", ";"])
            self.assertEqual(ini_rows[2]["risk"], "candidate")
            self.assertEqual(ini_rows[2]["reason"], "full-line-comment-config-comment-heading")
            self.assertEqual(ini_rows[3]["risk"], "protected")
            self.assertEqual([row["source"] for row in toml_rows], ["Controls the visible menu."])
            self.assertFalse(any("Do not extract this value" in row["source"] for row in ini_rows + toml_rows))


if __name__ == "__main__":
    unittest.main()

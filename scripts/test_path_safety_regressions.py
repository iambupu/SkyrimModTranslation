from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_final_mod  # noqa: E402
import project_paths  # noqa: E402


class PathSafetyRegressionTests(unittest.TestCase):
    def test_special_path_segments_do_not_survive(self) -> None:
        for value in ("", ".", "..", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    project_paths.safe_file_name(value)

    def test_windows_reserved_names_are_prefixed(self) -> None:
        for value in ("CON", "con.txt", "LPT1", "COM9.log", "NUL"):
            with self.subTest(value=value):
                result = project_paths.safe_file_name(value)
                basename = result.split(".", 1)[0].upper()
                self.assertNotIn(basename, project_paths.WINDOWS_RESERVED_FILE_NAMES)

    def test_trailing_spaces_and_dots_are_removed(self) -> None:
        result = project_paths.safe_file_name("example.  ")
        self.assertFalse(result.endswith((".", " ")))

    def test_all_callers_share_the_central_implementation(self) -> None:
        self.assertIs(build_final_mod.safe_file_name, project_paths.safe_file_name)
        definitions = []
        for path in SCRIPTS.glob("*.py"):
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if any(line.startswith("def safe_file_name(") for line in lines):
                definitions.append(path.name)
        self.assertEqual(definitions, ["project_paths.py"])


if __name__ == "__main__":
    unittest.main()

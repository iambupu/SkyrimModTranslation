from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import init_opencode
from game_context import load_game_profile
from new_manual_game_test_plan import GameTestRow, required_checks, write_reports
from new_manual_game_test_results_template import build_row, write_markdown


def write_marker(root: Path, game_id: str) -> None:
    (root / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "game_id": game_id,
                "game_profile": game_id,
            }
        ),
        encoding="utf-8",
    )


class GameProfilePromptTests(unittest.TestCase):
    def test_manual_plan_uses_workspace_game_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = load_game_profile("fallout4")
            checks = required_checks("Example", [], context)
            row = GameTestRow("Example", "out/Example.zip", "out/Example/final_mod", [], [], checks, "pending")
            report = root / "qa" / "manual_game_test_plan.md"

            write_reports(root, report, root / "qa" / "manual_game_test_plan.json", [row], context)

            text = report.read_text(encoding="utf-8")
            self.assertIn("Fallout 4 (Experimental)", text)
            self.assertNotIn("launches Skyrim", text)
            self.assertNotIn("real Skyrim", text)

    def test_manual_result_template_uses_workspace_game_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = root / "out" / "Example" / "Example_CHS.zip"
            final_mod = root / "out" / "Example" / "final_mod"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"package")
            (final_mod / "meta").mkdir(parents=True)
            (final_mod / "meta" / "manifest.json").write_text("{}\n", encoding="utf-8")
            context = load_game_profile("fallout4")
            source = {
                "ModName": "Example",
                "PackagePath": package.relative_to(root).as_posix(),
                "FinalModDir": final_mod.relative_to(root).as_posix(),
                "RequiredChecks": ["Check UI"],
            }

            row = build_row(root, source, context)
            report = root / "qa" / "manual_game_test_results_template.md"
            write_markdown(root, report, [row], context)

            self.assertEqual(row.TestEnvironment["Game"], "Fallout 4 (Experimental)")
            text = report.read_text(encoding="utf-8")
            self.assertIn("real Fallout 4 (Experimental) profile", text)
            self.assertNotIn("real Skyrim", text)

    def test_opencode_rules_use_marker_profile_instead_of_fixed_game_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            write_marker(workspace, "fallout4")

            rules = init_opencode.opencode_rules(workspace)
            agent = init_opencode.opencode_agent_markdown(workspace)

            self.assertIn("Current workspace Game Profile: Fallout 4 (Experimental)", rules)
            self.assertIn("Current workspace Game Profile: Fallout 4 (Experimental)", agent)
            self.assertNotIn("Skyrim SE/AE has stable", rules)
            self.assertNotIn('ask in natural language: "Skyrim SE/AE or Fallout 4?"', agent)


if __name__ == "__main__":
    unittest.main()

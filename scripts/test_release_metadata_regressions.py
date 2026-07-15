from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from package_project_release import git_tracked_files  # noqa: E402


class ReleaseMetadataRegressionTests(unittest.TestCase):
    def test_plugin_versions_match_project_version(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

        expected = project["project"]["version"]
        self.assertEqual(codex["version"], expected)
        self.assertEqual(claude["version"], expected)

    def test_claude_marketplace_uses_a_strict_non_gui_component_list(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        entry = marketplace["plugins"][0]
        skills = set(entry["skills"])

        self.assertIs(entry.get("strict"), True)
        self.assertNotIn("./skills/lextranslator-gui-automation", skills)
        self.assertNotIn("./skills/xtranslator-gui-automation", skills)
        self.assertIn("./skills/workflow-subagent-orchestration", skills)

    def test_shared_progress_text_is_agent_neutral(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8-sig", errors="replace")
            for path in (ROOT / "scripts").glob("*.py")
            if not path.name.startswith("test_")
        )
        self.assertNotIn("尚未由 Codex 完成", source)
        self.assertNotIn("SMT progress card for Codex", source)

    def test_empty_release_name_uses_default_package_name(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "package_project_release.py"),
                "--name",
                "",
                "--version",
                "0.0.0",
                "--dry-run",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SkyrimModTranslation-0.0.0.zip", result.stdout)

    def test_release_file_list_skips_tracked_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            retained = root / "retained.txt"
            deleted = root / "deleted.txt"
            retained.write_text("keep\n", encoding="utf-8")
            deleted.write_text("remove\n", encoding="utf-8")
            subprocess.run(["git", "add", "retained.txt", "deleted.txt"], cwd=root, check=True)
            deleted.unlink()

            self.assertEqual(git_tracked_files(root), [retained.resolve()])

    def test_parallel_subagent_protocol_has_a_dedicated_skill(self) -> None:
        skill_path = ROOT / "skills" / "workflow-subagent-orchestration" / "SKILL.md"
        self.assertTrue(skill_path.is_file())
        text = skill_path.read_text(encoding="utf-8-sig")
        description = text.split("---", 2)[1]
        self.assertIn("子智能体", description)
        self.assertIn("并发", description)

        recovery = (ROOT / "skills" / "workflow-agent-orchestration" / "SKILL.md").read_text(
            encoding="utf-8-sig"
        )
        self.assertNotIn("## Parallel Subagent Orchestration", recovery)

    def test_qa_skill_uses_progressive_disclosure_for_strict_details(self) -> None:
        skill_path = ROOT / "skills" / "qa-validation" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8-sig")
        reference = skill_path.parent / "references" / "strict-qa-contract.md"

        self.assertLess(len(text), 12000)
        self.assertTrue(reference.is_file())
        self.assertIn("references/strict-qa-contract.md", text)


if __name__ == "__main__":
    unittest.main()

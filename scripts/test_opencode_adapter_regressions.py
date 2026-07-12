from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import init_opencode  # noqa: E402
from list_agent_skills import skill_rows  # noqa: E402


class OpencodeAdapterRegressionTests(unittest.TestCase):
    def test_adapter_manifest_tracks_every_portable_skill_pointer(self) -> None:
        manifest = json.loads((ROOT / "agents" / "opencode" / "adapter.json").read_text(encoding="utf-8"))
        generated = set(manifest["generated_config_files"])
        expected = {
            f".opencode/skills/{row['skill_dir']}/SKILL.md"
            for row in skill_rows("opencode")
            if row["usable"]
        }
        self.assertTrue(expected.issubset(generated))

    def test_refresh_writes_codex_handoff_before_agent_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            commands: list[list[str]] = []

            with patch("init_opencode.run_checked", side_effect=lambda command, **_kwargs: commands.append(command) or 0):
                init_opencode.refresh_handoff_and_context(workspace)

            scripts = [Path(command[1]).name for command in commands]
            self.assertLess(scripts.index("write_codex_handoff.py"), scripts.index("write_agent_handoff.py"))
            self.assertLess(scripts.index("write_agent_handoff.py"), scripts.index("export_agent_context.py"))

    def test_refresh_prefers_workspace_python_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            executable = (
                workspace
                / "tools"
                / "python-venv"
                / ("Scripts" if init_opencode.os.name == "nt" else "bin")
                / ("python.exe" if init_opencode.os.name == "nt" else "python")
            )
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            commands: list[list[str]] = []

            with patch("init_opencode.run_checked", side_effect=lambda command, **_kwargs: commands.append(command) or 0):
                init_opencode.refresh_handoff_and_context(workspace)

            self.assertTrue(commands)
            self.assertTrue(all(command[0] == str(executable) for command in commands))

    def test_launch_uses_resolved_windows_command_shim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            resolved = r"C:\Users\Example\AppData\Roaming\npm\opencode.CMD"
            with (
                patch("init_opencode.shutil.which", return_value=resolved),
                patch("init_opencode.subprocess.call", return_value=0) as call,
            ):
                exit_code = init_opencode.launch_opencode(
                    workspace,
                    command="opencode",
                    mode="tui",
                    prompt="status",
                    auto=False,
                )

            self.assertEqual(exit_code, 0)
            argv = call.call_args.args[0]
            self.assertEqual(argv[0], resolved)

    def test_existing_opencode_config_and_rules_are_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            config_path = workspace / "opencode.json"
            rules_path = workspace / ".opencode" / "AGENTS.md"
            config_path.write_text(
                json.dumps(
                    {
                        "default_agent": "custom-agent",
                        "instructions": ["existing.md"],
                        "watcher": {"ignore": ["custom/**"]},
                        "mcp": {"existing": {"type": "local"}},
                    }
                ),
                encoding="utf-8",
            )
            rules_path.parent.mkdir(parents=True)
            rules_path.write_text("# User Rules\n\nKeep this text.\n", encoding="utf-8")

            init_opencode.write_opencode_config(workspace)

            merged = json.loads(config_path.read_text(encoding="utf-8"))
            rules = rules_path.read_text(encoding="utf-8")
            self.assertEqual(merged["default_agent"], "custom-agent")
            self.assertEqual(merged["mcp"], {"existing": {"type": "local"}})
            self.assertIn("existing.md", merged["instructions"])
            self.assertIn(init_opencode.LATEST_CONTEXT_PATH, merged["instructions"])
            self.assertIn("custom/**", merged["watcher"]["ignore"])
            self.assertIn("work/**", merged["watcher"]["ignore"])
            self.assertIn("Keep this text.", rules)
            self.assertIn("skyrim-chs:managed:start", rules)

    def test_generated_rules_use_marker_profile_without_guessing_game(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rules = init_opencode.opencode_rules(Path(temp_dir))

        self.assertIn("workspace marker", rules)
        self.assertIn("Skyrim SE/AE", rules)
        self.assertIn("Fallout 4 Experimental", rules)
        self.assertIn("Do not infer", rules)
        self.assertIn("Mod name", rules)
        self.assertIn("top-level adapter", rules)
        self.assertIn("must not directly claim", rules)
        self.assertIn("Codex-only", rules)

    def test_legacy_generated_rules_are_migrated_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            rules_path = workspace / ".opencode" / "AGENTS.md"
            rules_path.parent.mkdir(parents=True)
            rules_path.write_text(init_opencode.opencode_rules(workspace), encoding="utf-8")

            init_opencode.write_opencode_config(workspace)

            rules = rules_path.read_text(encoding="utf-8")
            self.assertEqual(
                rules.count("This workspace is controlled by the Skyrim CHS workflow core."),
                1,
            )
            self.assertEqual(rules.count(init_opencode.MANAGED_RULES_START), 1)
            self.assertEqual(rules.count(init_opencode.MANAGED_RULES_END), 1)

    def test_malformed_managed_rules_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            rules_path = workspace / ".opencode" / "AGENTS.md"
            rules_path.parent.mkdir(parents=True)
            rules_path.write_text(
                "# User Rules\n\n<!-- skyrim-chs:managed:start -->\nbroken\n",
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                init_opencode.write_opencode_config(workspace)

    def test_opencode_config_generates_native_pointer_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)

            init_opencode.write_opencode_config(workspace)

            pointer = workspace / ".opencode" / "skills" / "translation-task-router" / "SKILL.md"
            self.assertTrue(pointer.is_file())
            text = pointer.read_text(encoding="utf-8")
            self.assertIn("name: translation-task-router", text)
            self.assertIn(str(ROOT / "skills" / "translation-task-router" / "SKILL.md"), text)
            self.assertNotIn("## Routing Table", text)

    def test_stale_generated_pointer_is_removed_but_user_skill_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            stale = workspace / ".opencode" / "skills" / "removed-skill" / "SKILL.md"
            user = workspace / ".opencode" / "skills" / "user-skill" / "SKILL.md"
            stale.parent.mkdir(parents=True)
            user.parent.mkdir(parents=True)
            note = stale.parent / "notes.md"
            stale.write_text(
                "<!-- skyrim-chs:generated-skill-pointer -->\n# Removed\n",
                encoding="utf-8",
            )
            note.write_text("Keep this file.\n", encoding="utf-8")
            user.write_text("---\nname: user-skill\n---\n", encoding="utf-8")

            init_opencode.write_opencode_config(workspace)

            self.assertFalse(stale.exists())
            self.assertTrue(note.is_file())
            self.assertTrue(user.is_file())

    def test_opencode_agent_allows_read_only_plugin_source_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)

            init_opencode.write_opencode_config(workspace)

            agent = (workspace / ".opencode" / "agents" / "skyrim-chs.md").read_text(encoding="utf-8")
            plugin_pattern = ROOT.as_posix() + "/**"
            self.assertIn("external_directory:", agent)
            self.assertIn(f'"{plugin_pattern}": allow', agent)
            self.assertIn(f'"{plugin_pattern}": deny', agent)


if __name__ == "__main__":
    unittest.main()

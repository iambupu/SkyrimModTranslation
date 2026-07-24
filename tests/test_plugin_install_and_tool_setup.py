from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from install_codex_plugin import copy_plugin_source  # noqa: E402
from install_codex_plugin import marketplace_root  # noqa: E402
from install_codex_plugin import marketplace_source_path  # noqa: E402
from install_codex_plugin import plugin_install_path  # noqa: E402
from init_workspace import resolve_tool_setup_mode  # noqa: E402
from init_workspace import workspace_python as init_workspace_python  # noqa: E402
from managed_tool_provisioning import (  # noqa: E402
    DOTNET_SDK_SHA256,
    DOTNET_SDK_VERSION,
    GITHUB_ARCHIVES,
)
from package_project_release import git_untracked_files  # noqa: E402
from package_project_release import main as package_release_main  # noqa: E402
from package_project_release import source_root as package_source_root  # noqa: E402
from project_paths import python_script_command  # noqa: E402
import setup_workspace_tools  # noqa: E402
from setup_workspace_tools import run_command  # noqa: E402
from setup_workspace_tools import validate_workspace_root  # noqa: E402


class PluginInstallAndToolSetupTests(unittest.TestCase):
    def test_plugin_copy_excludes_local_tool_config_and_build_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "plugin-copy"

            copy_plugin_source(ROOT, target)

            self.assertTrue((target / ".codex-plugin" / "plugin.json").is_file())
            self.assertTrue((target / "skills" / "workspace-tool-setup" / "SKILL.md").is_file())
            self.assertTrue((target / "config" / "tools.example.json").is_file())
            self.assertTrue((target / "pyproject.toml").is_file())
            self.assertFalse((target / "config" / "tools.local.json").exists())
            self.assertFalse((target / "adapters" / "SkyrimPluginTextTool" / "bin").exists())
            self.assertFalse((target / "adapters" / "SkyrimPluginTextTool" / "obj").exists())
            self.assertTrue((target / "adapters" / "SkyrimPexStringTool" / "SkyrimPexStringTool.csproj").is_file())
            self.assertFalse((target / "adapters" / "SkyrimPexStringTool" / "bin").exists())
            self.assertFalse((target / "adapters" / "SkyrimPexStringTool" / "obj").exists())

    def test_plugin_copy_failure_preserves_existing_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "plugin-copy"
            marker = target / "old-install.txt"
            target.mkdir(parents=True)
            marker.write_text("old", encoding="utf-8")

            with patch("install_codex_plugin.shutil.copytree", side_effect=OSError("copy failed")):
                with self.assertRaises(OSError):
                    copy_plugin_source(ROOT, target)

            self.assertTrue(marker.is_file())
            self.assertFalse((target.parent / ".plugin-copy.staged").exists())

    def test_plugin_copy_refuses_target_inside_source_repository(self) -> None:
        with self.assertRaises(ValueError):
            copy_plugin_source(ROOT, ROOT / "plugins" / "skyrim-mod-chs-translation")

    def test_personal_marketplace_source_path_is_relative_to_marketplace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
            target = plugin_install_path(marketplace_path)

            self.assertEqual(marketplace_root(marketplace_path), home)
            self.assertEqual(target, home / ".agents" / "plugins" / "skyrim-mod-chs-translation")
            self.assertEqual(
                marketplace_source_path(target, marketplace_path),
                "./.agents/plugins/skyrim-mod-chs-translation",
            )

    def test_project_release_source_root_ignores_workspace_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"SKYRIM_CHS_WORKSPACE_ROOT": temp_dir}):
                self.assertEqual(package_source_root(), ROOT)

    def test_project_release_reports_untracked_files(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout="scripts/dotnet_adapter_cache.py\0README.tmp\0",
        )
        with patch("package_project_release.run_git", return_value=completed):
            untracked = git_untracked_files(ROOT)

        self.assertIn("scripts/dotnet_adapter_cache.py", untracked)

    def test_project_release_refuses_untracked_files_by_default(self) -> None:
        with (
            patch("package_project_release.git_tracked_files", return_value=[ROOT / "README.md"]),
            patch("package_project_release.git_untracked_files", return_value=["scripts/new_tool.py"]),
            patch("package_project_release.write_zip") as write_zip,
            patch("sys.stdout", new_callable=StringIO) as output,
        ):
            code = package_release_main(["--version", "1.0.1"])

        self.assertEqual(code, 1)
        self.assertIn("non-ignored untracked files", output.getvalue())
        write_zip.assert_not_called()

    def test_project_release_allows_explicit_untracked_exclusion(self) -> None:
        output_dir = ROOT / "out" / "test-package-unit"
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (
                patch("package_project_release.git_tracked_files", return_value=[ROOT / "README.md"]),
                patch("package_project_release.git_untracked_files", return_value=["scripts/new_tool.py"]),
                patch("package_project_release.write_zip", return_value=(0, [])) as write_zip,
                patch("package_project_release.sha256_file", return_value="0" * 64),
                patch("package_project_release.git_commit", return_value="HEAD"),
                patch("package_project_release.git_dirty", return_value=False),
                patch("sys.stdout", new_callable=StringIO),
            ):
                code = package_release_main(
                    [
                        "--version",
                        "1.0.1",
                        "--output-dir",
                        "out/test-package-unit",
                        "--allow-untracked-excluded",
                    ]
                )
        finally:
            __import__("shutil").rmtree(output_dir, ignore_errors=True)

        self.assertEqual(code, 0)
        write_zip.assert_called_once()

    def test_run_command_decodes_unexpected_bytes_without_crashing(self) -> None:
        code, output = run_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'prefix\\xae suffix')",
            ],
            cwd=ROOT,
            env={},
        )

        self.assertEqual(code, 0)
        self.assertIn("prefix", output)
        self.assertIn("suffix", output)

    def test_tool_setup_ask_defaults_to_manual_on_eof(self) -> None:
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=EOFError),
            patch("sys.stdout", new_callable=StringIO),
        ):
            self.assertEqual(resolve_tool_setup_mode("ask"), "manual")

    def test_tool_setup_refuses_plugin_repository_root(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_workspace_root(ROOT)

    def test_tool_setup_cli_reports_invalid_root_without_traceback(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "setup_workspace_tools.py"), "--mode", "manual"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: Refusing to run tool setup inside the plugin source repository.", result.stdout)
        self.assertNotIn("Traceback", result.stdout)

    def test_tool_setup_requires_workspace_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RuntimeError):
                validate_workspace_root(Path(temp_dir))

    def test_tool_setup_accepts_initialized_workspace_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".skyrim-chs-workspace.json").write_text("{}\n", encoding="utf-8")

            validate_workspace_root(root)

    def test_tool_setup_has_no_workspace_local_installer_surface(self) -> None:
        for removed_name in (
            "install_github_archive",
            "install_dotnet_sdk",
            "install_python_requirements",
            "build_dotnet_adapters",
            "update_tools_config",
            "replace_dir_preserving_old",
        ):
            self.assertFalse(hasattr(setup_workspace_tools, removed_name))

    def test_shared_tool_sources_are_exactly_pinned(self) -> None:
        self.assertRegex(DOTNET_SDK_VERSION, r"^8\.0\.\d+$")
        self.assertRegex(DOTNET_SDK_SHA256, r"^[0-9a-f]{64}$")
        for spec in GITHUB_ARCHIVES.values():
            self.assertNotIn("refs/heads", spec["url"])
            self.assertRegex(spec["ref"], r"^[0-9a-f]{40}$")
            self.assertRegex(spec["sha256"], r"^[0-9a-f]{64}$")

    def test_dotnet_adapter_project_excludes_default_build_artifacts(self) -> None:
        for project in (
            ROOT / "adapters" / "SkyrimPluginTextTool" / "SkyrimPluginTextTool.csproj",
            ROOT / "adapters" / "SkyrimPexStringTool" / "SkyrimPexStringTool.csproj",
        ):
            project_text = project.read_text(encoding="utf-8")

            self.assertIn('<Compile Remove="bin\\**;obj\\**" />', project_text)
            self.assertIn('<EmbeddedResource Remove="bin\\**;obj\\**" />', project_text)
            self.assertIn('<None Remove="bin\\**;obj\\**" />', project_text)

    def test_init_workspace_uses_bootstrap_python_before_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts_dir = "Scripts" if os.name == "nt" else "bin"
            executable = "python.exe" if os.name == "nt" else "python"
            legacy_python = root / "tools" / "python-venv" / scripts_dir / executable
            legacy_python.parent.mkdir(parents=True)
            legacy_python.write_text("", encoding="utf-8")

            self.assertEqual(init_workspace_python(root), Path(sys.executable))

    def test_python_script_command_leaves_runtime_selection_to_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict(
                os.environ,
                {
                    "SKYRIM_CHS_WORKSPACE_ROOT": str(root),
                    "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
                },
            ):
                command = python_script_command("scripts/audit_translation_readiness.py")

            self.assertTrue(command.startswith("python "))
            self.assertIn("audit_translation_readiness.py", command)


if __name__ == "__main__":
    unittest.main()

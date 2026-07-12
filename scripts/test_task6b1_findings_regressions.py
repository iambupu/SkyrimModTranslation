from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import export_agent_context  # noqa: E402


class Task6B1FindingsProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_parent = ROOT / ".tmp" / "test-task6b1-findings"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.tempdir.cleanup)

    def workspace(self, name: str, game_id: str) -> Path:
        root = Path(self.tempdir.name) / name
        for relative in ("mod", "work", "qa", "out", "source", "translated", "glossary", ".workflow", "traces", "config"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        marker = {
            "schema_version": 2,
            "kind": "bethesda-mod-chs-translation-workspace",
            "plugin_name": "skyrim-mod-chs-translation",
            "plugin_root": str(ROOT),
            "game_id": game_id,
            "game_profile": game_id,
        }
        (root / ".skyrim-chs-workspace.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return root

    def run_script(
        self,
        workspace: Path,
        script_name: str,
        *args: str,
        plugin_root: Path = ROOT,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
            "SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root),
        }
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script_name), *args],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def write_dictionary(self, workspace: Path, mod_name: str) -> None:
        path = workspace / "out" / mod_name / "lex_dictionary" / "entries.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"source": "Visible", "target": "可见"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def test_protected_runtime_files_copy_unchanged_from_directory_and_zip(self) -> None:
        protected = {
            "Interface/Menu.swf": b"source-swf",
            "Interface/Menu.gfx": b"source-gfx",
            "F4SE/Plugins/Runtime.dll": b"source-dll",
            "F4SE/Runtime.exe": b"source-exe",
        }
        for source_kind in ("directory", "zip"):
            with self.subTest(source_kind=source_kind):
                workspace = self.workspace(f"protected-{source_kind}", "fallout4")
                mod_name = "ProtectedRuntime"
                if source_kind == "directory":
                    source = workspace / "mod" / mod_name
                    for relative, content in protected.items():
                        path = source / Path(relative)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                    source_arg = f"mod/{mod_name}"
                else:
                    source = workspace / "mod" / f"{mod_name}.zip"
                    with zipfile.ZipFile(source, "w") as archive:
                        for relative, content in protected.items():
                            archive.writestr(relative, content)
                    source_arg = f"mod/{mod_name}.zip"

                for overlay_root in (
                    workspace / "translated" / "final_mod" / mod_name,
                    workspace / "out" / mod_name / "tool_outputs",
                ):
                    for relative in protected:
                        path = overlay_root / Path(relative)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"overlay-must-not-win")
                self.write_dictionary(workspace, mod_name)

                built = self.run_script(
                    workspace,
                    "build_final_mod.py",
                    "--mod-name",
                    mod_name,
                    "--source-mod-dir",
                    source_arg,
                    "--force",
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
                for relative, content in protected.items():
                    self.assertEqual((final_mod / Path(relative)).read_bytes(), content)

                manifest = json.loads((final_mod / "meta" / "manifest.json").read_text(encoding="utf-8"))
                copied = {Path(item).suffix.lower() for item in manifest["BinaryFilesCopiedUnmodified"]}
                self.assertTrue({".swf", ".gfx", ".dll", ".exe"}.issubset(copied))
                self.assertFalse(
                    any(Path(item).suffix.lower() in {".swf", ".gfx", ".dll", ".exe"} for item in manifest["OverlayFiles"])
                )

                validated = self.run_script(
                    workspace,
                    "validate_final_mod.py",
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

                provenance_path = final_mod / "meta" / "provenance.jsonl"
                original = provenance_path.read_text(encoding="utf-8")
                rows = [json.loads(line) for line in original.splitlines() if line]
                swf_row = next(row for row in rows if row["file"].endswith("Interface/Menu.swf"))
                swf_row["transform"] = "translated-overlay"
                provenance_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                rejected = self.run_script(
                    workspace,
                    "validate_final_mod.py",
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                report = (workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8")
                self.assertIn("Protected file was not copied unchanged", report)

                provenance_path.write_text(original, encoding="utf-8")
                (final_mod / "Interface" / "Menu.swf").write_bytes(b"tampered")
                rejected = self.run_script(
                    workspace,
                    "validate_final_mod.py",
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                report = (workspace / "qa" / "final_mod_validation.md").read_text(encoding="utf-8")
                self.assertIn("Provenance file_sha256 mismatch", report)

    def test_profile_policy_drives_production_interface_normalize_and_audit(self) -> None:
        for game_id in ("skyrim-se", "fallout4"):
            with self.subTest(game_id=game_id):
                workspace = self.workspace(f"interface-{game_id}", game_id)
                mod_name = "InterfacePolicy"
                source = workspace / "mod" / mod_name / "Interface" / "translations" / "Example_english.txt"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("$HELLO Hello\n", encoding="utf-8")

                built = self.run_script(
                    workspace,
                    "build_final_mod.py",
                    "--mod-name",
                    mod_name,
                    "--source-mod-dir",
                    f"mod/{mod_name}",
                    "--overlay-translated-files",
                    "false",
                    "--force",
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                final_mod = workspace / "out" / mod_name / "汉化产出" / "final_mod"
                delivered = final_mod / "Interface" / "translations" / "Example_english.txt"
                self.assertTrue(delivered.read_bytes().startswith(b"\xff\xfe"))
                first_bytes = delivered.read_bytes()

                rebuilt = self.run_script(
                    workspace,
                    "build_final_mod.py",
                    "--mod-name",
                    mod_name,
                    "--source-mod-dir",
                    f"mod/{mod_name}",
                    "--overlay-translated-files",
                    "false",
                    "--force",
                )
                self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
                self.assertEqual(delivered.read_bytes(), first_bytes)
                self.assertEqual(first_bytes.count(b"\xff\xfe"), 1)

                audited = self.run_script(
                    workspace,
                    "audit_final_interface_translations.py",
                    "--mod-name",
                    mod_name,
                    "--final-mod-dir",
                    f"out/{mod_name}/汉化产出/final_mod",
                )
                self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
                report = (workspace / "qa" / f"{mod_name}.final_interface_runtime.md").read_text(encoding="utf-8")
                self.assertIn(f"GameId: {game_id}", report)
                self.assertIn("Encoding policy: utf-16-le-bom", report)

    def test_missing_or_unknown_interface_policy_fails_closed_in_production_audit(self) -> None:
        for mutation in ("missing", "unknown"):
            with self.subTest(mutation=mutation):
                workspace = self.workspace(f"invalid-policy-{mutation}", "fallout4")
                final_mod = workspace / "out" / "InvalidPolicy" / "汉化产出" / "final_mod"
                translation = final_mod / "Interface" / "translations" / "Example_english.txt"
                translation.parent.mkdir(parents=True, exist_ok=True)
                translation.write_bytes("$HELLO\t你好\r\n".encode("utf-16"))
                plugin_root = Path(self.tempdir.name) / f"plugin-{mutation}"
                profile_dir = plugin_root / "config" / "game_profiles"
                profile_dir.mkdir(parents=True)
                payload = json.loads((ROOT / "config" / "game_profiles" / "fallout4.json").read_text(encoding="utf-8"))
                if mutation == "missing":
                    payload.pop("interface_translation_encoding", None)
                else:
                    payload["interface_translation_encoding"] = "private-unknown-policy"
                (profile_dir / "fallout4.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

                audited = self.run_script(
                    workspace,
                    "audit_final_interface_translations.py",
                    "--mod-name",
                    "InvalidPolicy",
                    "--final-mod-dir",
                    "out/InvalidPolicy/汉化产出/final_mod",
                    plugin_root=plugin_root,
                )
                self.assertNotEqual(audited.returncode, 0, audited.stdout + audited.stderr)
                self.assertIn("interface_translation_encoding", audited.stdout + audited.stderr)

    def test_export_agent_context_main_uses_strict_bounded_allowlist(self) -> None:
        workspace = self.workspace("agent-packet", "fallout4")
        sentinel = "PRIVATE_SENTINEL_6B1"
        actions = [
            {
                "mod": f"Mod{index}",
                "task_id": f"task-{index}",
                "command": f"python scripts/safe_{index}.py",
                "type": "python",
                "risk": "low",
                "can_run_parallel": True,
                "resource_locks": [f"mod:Mod{index}"],
                "must_read_evidence": [f"qa/mod-{index}.md"],
                "private": sentinel,
            }
            for index in range(export_agent_context.MAX_NEXT_ACTIONS + 5)
        ]
        handoff = {
            "game_id": "fallout4",
            "game_profile_version": 1,
            "game_display_name": "Fallout 4",
            "support_level": "experimental",
            "plugin_adapter": "fallout4-mutagen",
            "plugin_adapter_version": 1,
            "pex_category": "Fallout4",
            "pex_writeback_status": "experimental",
            "archive_delivery": "loose_override",
            "archive_materialization_enabled": True,
            "archive_allow_repack": False,
            "interface_translation_encoding": "utf-16-le-bom",
            "project_state": "blocked",
            "readiness_overall_status": "blocked",
            "workflow_health": {"verdict": "blocked", "blocking_issues": 3, "private": sentinel},
            "task_summary": {"pending_executable": 4, "pending_total": 9, "parallel_safe": 2, "private": sentinel},
            "blocking_mods": [
                {
                    "mod": "Example",
                    "state": "blocked",
                    "primary_blocker": "adapter_not_ready",
                    "task_id": "task-0",
                    "can_run_parallel": False,
                    "resource_locks": ["mod:Example"],
                    "safe_next_action": actions[0],
                    "private_payload": sentinel * 1000,
                }
            ],
            "resume_checkpoint": {
                "checkpoint_id": "checkpoint-6b1",
                "generated_at_utc": "2026-07-12T00:00:00Z",
                "project_state": "blocked",
                "readiness_overall_status": "blocked",
                "next_actions": actions,
                "stale_if_newer_than": {
                    "watch": [
                        {"path": "mod", "fingerprint": hashlib.sha256(b"mod").hexdigest(), "private": sentinel}
                    ]
                },
                "private_payload": sentinel * 2000,
            },
            "private_large_payload": sentinel * 5000,
        }
        (workspace / "qa" / "agent_handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (workspace / "qa" / "codex_handoff.json").write_text(
            json.dumps({"private_codex_payload": sentinel * 5000}),
            encoding="utf-8",
        )

        exported = self.run_script(
            workspace,
            "export_agent_context.py",
            "--agent",
            "opencode",
            "--output",
            "qa/agent_context_prompts/latest.opencode.context.md",
        )
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        packet = workspace / "qa" / "agent_context_prompts" / "latest.opencode.context.md"
        data = packet.read_bytes()
        text = data.decode("utf-8")
        self.assertLessEqual(len(data), export_agent_context.MAX_PACKET_BYTES)
        self.assertNotIn(sentinel, text)
        self.assertNotIn("Agent Handoff", text)
        self.assertNotIn("Codex Handoff Fallback", text)
        self.assertIn("## Workflow Status", text)
        self.assertIn("## Next Actions", text)
        self.assertIn("checkpoint-6b1", text)
        self.assertLessEqual(text.count('"command"'), export_agent_context.MAX_NEXT_ACTIONS)

        original_packet = packet.read_bytes()
        for field, polluted in (
            ("game_id", {"private": sentinel}),
            ("task_summary", {"pending_total": {"private": sentinel}}),
        ):
            poisoned = dict(handoff)
            poisoned[field] = polluted
            (workspace / "qa" / "agent_handoff.json").write_text(
                json.dumps(poisoned, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            rejected = self.run_script(
                workspace,
                "export_agent_context.py",
                "--agent",
                "opencode",
                "--output",
                "qa/agent_context_prompts/latest.opencode.context.md",
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
            self.assertEqual(packet.read_bytes(), original_packet)
            self.assertNotIn(sentinel.encode("utf-8"), packet.read_bytes())

        (workspace / "qa" / "agent_handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {
                "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            },
            clear=False,
        ), mock.patch.object(export_agent_context, "MAX_PACKET_BYTES", 1), mock.patch.object(
            sys,
            "argv",
            [
                "export_agent_context.py",
                "--agent",
                "opencode",
                "--output",
                "qa/agent_context_prompts/latest.opencode.context.md",
            ],
        ):
            self.assertEqual(export_agent_context.main(), 1)
        self.assertEqual(packet.read_bytes(), original_packet)

    def test_init_opencode_game_cli_initializes_and_rejects_marker_conflicts(self) -> None:
        init_temp = tempfile.TemporaryDirectory()
        self.addCleanup(init_temp.cleanup)
        init_root = Path(init_temp.name)
        fallout_workspace = init_root / "opencode-fallout"
        created = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(fallout_workspace),
                "--game",
                "fallout4",
                "--tool-setup",
                "skip",
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        marker_path = fallout_workspace / ".skyrim-chs-workspace.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["game_id"], "fallout4")

        unchanged = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(fallout_workspace),
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(unchanged.returncode, 0, unchanged.stdout + unchanged.stderr)

        conflict = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(fallout_workspace),
                "--game",
                "skyrim-se",
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(conflict.returncode, 0, conflict.stdout + conflict.stderr)
        self.assertIn("conflicts", conflict.stdout + conflict.stderr)
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["game_id"], "fallout4")

        skyrim_workspace = init_root / "opencode-default"
        defaulted = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "init_opencode.py"),
                str(skyrim_workspace),
                "--tool-setup",
                "skip",
                "--skip-refresh",
                "--no-launch",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(defaulted.returncode, 0, defaulted.stdout + defaulted.stderr)
        marker = json.loads((skyrim_workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["game_id"], "skyrim-se")


if __name__ == "__main__":
    unittest.main()

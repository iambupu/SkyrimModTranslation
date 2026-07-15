from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import init_opencode  # noqa: E402
import export_agent_context  # noqa: E402
import refresh_project_handoff_reports  # noqa: E402
import write_agent_handoff  # noqa: E402
from agent_capabilities import capability_config_fingerprint, load_agent_capabilities  # noqa: E402
from game_context import game_context_metadata, load_game_profile  # noqa: E402
from list_agent_skills import skill_rows  # noqa: E402
from write_agent_handoff import (  # noqa: E402
    adapt_handoff_for_agent,
    build_resume_checkpoint,
    checkpoint_actions,
    evaluate_agent_handoff_freshness,
    evaluate_resume_checkpoint,
)
from write_codex_handoff import blocking_handoffs, build_handoff  # noqa: E402
from write_workflow_state import next_actions_from_actions  # noqa: E402
from write_workflow_tasks import task_from_action  # noqa: E402


FRESH_CHECKPOINT_ENV = "SKYRIM_CHS_FRESH_CHECKPOINT_CREDENTIAL"


def checkpoint_credential(payload: dict[str, object]) -> str:
    checkpoint = payload["resume_checkpoint"]
    assert isinstance(checkpoint, dict)
    source = {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "handoff_payload_sha256": checkpoint["handoff_payload_sha256"],
        "generated_at_epoch_ns": checkpoint["generated_at_epoch_ns"],
        "target_agent": checkpoint["target_agent"],
    }
    return "v1:" + write_agent_handoff.canonical_json_sha256(source)


class OpencodeAdapterRegressionTests(unittest.TestCase):
    def test_gui_action_declares_agent_requirement_without_runtime_probe(self) -> None:
        actions, blockers = next_actions_from_actions(
            {
                "repair_candidates": [
                    {
                        "type": "run_command",
                        "command": "python scripts/invoke_lextranslator_gui.py --mode export",
                        "risk": "low",
                        "allowed": True,
                        "resource_locks": ["gui:desktop"],
                    }
                ]
            },
            load_game_profile("skyrim-se"),
        )

        self.assertEqual(blockers, [])
        self.assertEqual(actions[0]["required_agent_capability"], "gui:desktop")
        self.assertNotIn("agent_capability_satisfied", actions[0])
        self.assertNotIn("handoff_target", actions[0])

    def test_codex_keeps_gui_action_while_non_gui_agents_handoff_to_codex(self) -> None:
        action = {
            "type": "run_command",
            "command": "python scripts/invoke_lextranslator_gui.py --mode export",
            "risk": "low",
            "allowed": True,
            "required_agent_capability": "gui:desktop",
        }
        states = [
            {
                "mod": "Example",
                "state": "blocked",
                "last_success_stage": "prepared",
                "blocking_checks": ["decoder_unavailable"],
                "next_actions": [action],
            }
        ]
        codex_rows = blocking_handoffs(states, {"tasks": []})
        self.assertIs(
            codex_rows[0]["safe_next_action"]["agent_capability_satisfied"],
            True,
        )
        config = load_agent_capabilities()
        for agent in ("opencode", "claude-code"):
            payload: dict[str, object] = {
                "blocking_mods": json.loads(json.dumps(codex_rows)),
                "safe_next_actions": [action],
            }
            adapt_handoff_for_agent(payload, config, agent)
            row = payload["blocking_mods"][0]
            adapted = row["safe_next_action"]
            self.assertIs(adapted["agent_capability_satisfied"], False)
            self.assertEqual(adapted["error_code"], "agent_capability_missing")
            self.assertEqual(adapted["handoff_target"], "codex")
            self.assertEqual(row["agent_action_status"], "blocked")
            self.assertEqual(payload["safe_next_actions"], [])
            self.assertEqual(checkpoint_actions(payload), [])

    def test_codex_fails_closed_for_unknown_agent_capability(self) -> None:
        states = [
            {
                "mod": "Example",
                "state": "blocked",
                "blocking_checks": ["unknown_agent_capability"],
                "next_actions": [
                    {
                        "type": "run_command",
                        "command": "python scripts/example.py",
                        "risk": "low",
                        "allowed": True,
                        "required_agent_capability": "agent:unknown",
                    }
                ],
            }
        ]

        action = blocking_handoffs(states, {"tasks": []})[0]["safe_next_action"]

        self.assertIs(action["agent_capability_satisfied"], False)
        self.assertIs(action["allowed"], False)
        self.assertEqual(action["error_code"], "agent_capability_missing")

    def test_unknown_agent_capability_is_not_schedulable_or_safe(self) -> None:
        context = load_game_profile("skyrim-se")
        actions, blockers = next_actions_from_actions(
            {
                "repair_candidates": [
                    {
                        "type": "run_command",
                        "command": "python scripts/example.py",
                        "risk": "low",
                        "allowed": True,
                        "required_agent_capability": "agent:unknown",
                    }
                ]
            },
            context,
        )
        self.assertEqual(actions[0]["error_code"], "agent_capability_unknown")
        self.assertIs(actions[0]["allowed"], False)
        self.assertIn("agent_capability:agent:unknown:unknown", blockers)

        task = task_from_action(
            mod_name="Example",
            state="blocked",
            last_success="prepared",
            action=actions[0],
            action_index=0,
            source="next_actions",
        )
        self.assertEqual(task["status"], "pending_manual")
        self.assertIs(task["executable"], False)
        self.assertEqual(task["error_code"], "agent_capability_unknown")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metadata = game_context_metadata(context)
            state_path = root / "qa" / "workflow_state.json"
            readiness_path = root / "qa" / "translation_readiness.json"
            health_path = root / "qa" / "workflow_health.json"
            tasks_path = root / "qa" / "workflow_tasks.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        **metadata,
                        "generated_at": "now",
                        "states": [
                            {
                                "mod": "Example",
                                "state": "blocked",
                                "blocking_checks": blockers,
                                "next_actions": actions,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            readiness_path.write_text(json.dumps(metadata), encoding="utf-8")
            health_path.write_text(json.dumps(metadata), encoding="utf-8")
            tasks_path.write_text(json.dumps({**metadata, "tasks": []}), encoding="utf-8")
            with patch("write_codex_handoff.current_game_context", return_value=context):
                payload, _issues = build_handoff(
                    root,
                    state_path,
                    readiness_path,
                    health_path,
                    tasks_path,
                )

        self.assertEqual(payload["safe_next_actions"], [])

    def test_default_report_refresh_does_not_generate_cross_adapter_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            scripts: list[str] = []

            def run_step(_root: Path, script: str, _args: list[str]) -> SimpleNamespace:
                scripts.append(script)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(refresh_project_handoff_reports, "project_root", return_value=workspace),
                patch.object(refresh_project_handoff_reports, "WorkflowLock"),
                patch.object(refresh_project_handoff_reports, "run_python_script", side_effect=run_step),
                patch.object(sys, "argv", ["refresh_project_handoff_reports.py"]),
            ):
                exit_code = refresh_project_handoff_reports.main()

            self.assertEqual(exit_code, 0)
            self.assertIn("write_codex_handoff.py", scripts)
            self.assertNotIn("write_agent_handoff.py", scripts)
            self.assertFalse((workspace / "qa" / "agent_handoff.json").exists())

    def test_agent_handoff_freshness_is_bound_to_target_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            payload = {
                "target_agent": "opencode",
                "resume_checkpoint": {},
            }
            with patch("write_agent_handoff.current_game_context", return_value=load_game_profile("skyrim-se")):
                result = evaluate_agent_handoff_freshness(
                    workspace,
                    payload,
                    expected_agent="claude-code",
                )

        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "target_agent_mismatch" for row in result["reasons"])
        )

    def test_agent_handoff_freshness_rejects_top_level_target_tampering(self) -> None:
        context = load_game_profile("skyrim-se")
        config = load_agent_capabilities()
        payload = {
            **game_context_metadata(context),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(config),
            "project_state": "ready",
            "readiness_overall_status": "ready",
            "source_reports": {},
            "task_summary": {},
            "blocking_mods": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
            payload["target_agent"] = "claude-code"
            with (
                patch("write_agent_handoff.current_game_context", return_value=context),
                patch("write_agent_handoff.load_agent_capabilities", return_value=config),
            ):
                result = evaluate_agent_handoff_freshness(
                    root,
                    payload,
                    expected_agent="claude-code",
                )

        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "checkpoint_target_agent_mismatch" for row in result["reasons"])
        )

    def test_agent_handoff_freshness_rejects_checkpoint_target_tampering(self) -> None:
        context = load_game_profile("skyrim-se")
        config = load_agent_capabilities()
        payload = {
            **game_context_metadata(context),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(config),
            "project_state": "ready",
            "readiness_overall_status": "ready",
            "source_reports": {},
            "task_summary": {},
            "blocking_mods": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
            payload["resume_checkpoint"]["target_agent"] = "claude-code"
            with (
                patch("write_agent_handoff.current_game_context", return_value=context),
                patch("write_agent_handoff.load_agent_capabilities", return_value=config),
            ):
                result = evaluate_agent_handoff_freshness(
                    root,
                    payload,
                    expected_agent="opencode",
                )

        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "checkpoint_target_agent_mismatch" for row in result["reasons"])
        )

    def test_checkpoint_id_is_bound_to_checkpoint_content(self) -> None:
        payload = {
            "target_agent": "opencode",
            "project_state": "ready",
            "readiness_overall_status": "ready",
            "source_reports": {},
            "task_summary": {},
            "blocking_mods": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = build_resume_checkpoint(root, payload)
            checkpoint["project_state"] = "blocked"

            result = evaluate_resume_checkpoint(root, checkpoint)

        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "checkpoint_id_mismatch" for row in result["reasons"])
        )

    def test_agent_capability_config_change_invalidates_checkpoint(self) -> None:
        context = load_game_profile("skyrim-se")
        config = load_agent_capabilities()
        payload = {
            **game_context_metadata(context),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(config),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
            changed_config = json.loads(json.dumps(config))
            changed_config["agents"]["opencode"]["gui_handoff_target"] = "manual-review"
            with (
                patch("write_agent_handoff.current_game_context", return_value=context),
                patch("write_agent_handoff.load_agent_capabilities", return_value=changed_config),
            ):
                result = evaluate_agent_handoff_freshness(
                    root,
                    payload,
                    expected_agent="opencode",
                )

        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "agent_capabilities_changed" for row in result["reasons"])
        )

    def test_context_export_freshness_rejects_other_target_agent(self) -> None:
        context = load_game_profile("skyrim-se")
        config = load_agent_capabilities()
        payload = {
            **game_context_metadata(context),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(config),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
            handoff_path = root / "qa" / "agent_handoff.json"
            handoff_path.parent.mkdir(parents=True)
            handoff_path.write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch("write_agent_handoff.current_game_context", return_value=context),
                patch("write_agent_handoff.load_agent_capabilities", return_value=config),
            ):
                result = export_agent_context.handoff_checkpoint_freshness(
                    root,
                    handoff_path,
                    expected_agent="claude-code",
                )

        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "target_agent_mismatch" for row in result["reasons"])
        )

    def test_context_export_reuses_valid_same_chain_credential_without_rescan(self) -> None:
        context = load_game_profile("skyrim-se")
        config = load_agent_capabilities()
        payload = {
            **game_context_metadata(context),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(config),
            "project_state": "ready",
            "readiness_overall_status": "ready",
            "source_reports": {},
            "task_summary": {},
            "blocking_mods": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
            handoff_path = root / "qa" / "agent_handoff.json"
            handoff_path.parent.mkdir(parents=True)
            handoff_path.write_text(json.dumps(payload), encoding="utf-8")
            credential = checkpoint_credential(payload)
            output = "qa/agent_context_prompts/latest.opencode.context.md"
            with (
                patch.object(export_agent_context, "project_root", return_value=root),
                patch.object(export_agent_context, "load_agent_capabilities", return_value=config),
                patch("write_agent_handoff.current_game_context", return_value=context),
                patch("write_agent_handoff.load_agent_capabilities", return_value=config),
                patch.dict(os.environ, {FRESH_CHECKPOINT_ENV: credential}),
                patch.object(
                    write_agent_handoff,
                    "path_snapshot",
                    wraps=write_agent_handoff.path_snapshot,
                ) as snapshots,
                patch.object(
                    sys,
                    "argv",
                    ["export_agent_context.py", "--agent", "opencode", "--output", output],
                ),
            ):
                exit_code = export_agent_context.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(snapshots.call_count, 0)

    def test_independent_context_export_still_scans_freshness(self) -> None:
        context = load_game_profile("skyrim-se")
        config = load_agent_capabilities()
        payload = {
            **game_context_metadata(context),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(config),
            "project_state": "ready",
            "readiness_overall_status": "ready",
            "source_reports": {},
            "task_summary": {},
            "blocking_mods": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
            handoff_path = root / "qa" / "agent_handoff.json"
            handoff_path.parent.mkdir(parents=True)
            handoff_path.write_text(json.dumps(payload), encoding="utf-8")
            output = "qa/agent_context_prompts/latest.opencode.context.md"
            with (
                patch.object(export_agent_context, "project_root", return_value=root),
                patch.object(export_agent_context, "load_agent_capabilities", return_value=config),
                patch("write_agent_handoff.current_game_context", return_value=context),
                patch("write_agent_handoff.load_agent_capabilities", return_value=config),
                patch.dict(os.environ, {FRESH_CHECKPOINT_ENV: ""}),
                patch.object(
                    write_agent_handoff,
                    "path_snapshot",
                    wraps=write_agent_handoff.path_snapshot,
                ) as snapshots,
                patch.object(
                    sys,
                    "argv",
                    ["export_agent_context.py", "--agent", "opencode", "--output", output],
                ),
            ):
                exit_code = export_agent_context.main()

            self.assertEqual(exit_code, 0)
            self.assertGreater(snapshots.call_count, 0)

    def test_expired_same_chain_credential_is_stale(self) -> None:
        context = load_game_profile("skyrim-se")
        config = load_agent_capabilities()
        payload = {
            **game_context_metadata(context),
            "target_agent": "opencode",
            "agent_capabilities_sha256": capability_config_fingerprint(config),
            "project_state": "ready",
            "readiness_overall_status": "ready",
            "source_reports": {},
            "task_summary": {},
            "blocking_mods": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = build_resume_checkpoint(root, payload)
            checkpoint["generated_at_epoch_ns"] = time.time_ns() - 120_000_000_000
            checkpoint["checkpoint_id"] = write_agent_handoff.checkpoint_id_for(checkpoint)
            payload["resume_checkpoint"] = checkpoint
            credential = checkpoint_credential(payload)
            with (
                patch("write_agent_handoff.current_game_context", return_value=context),
                patch("write_agent_handoff.load_agent_capabilities", return_value=config),
            ):
                result = evaluate_agent_handoff_freshness(
                    root,
                    payload,
                    expected_agent="opencode",
                    checkpoint_credential=credential,
                )

        self.assertFalse(result["fresh"])
        self.assertTrue(
            any(row.get("reason") == "checkpoint_credential_expired" for row in result["reasons"])
        )

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
            calls: list[tuple[list[str], dict[str, object]]] = []
            credential = "v1:" + "a" * 64

            def run_step(command: list[str], **kwargs: object) -> SimpleNamespace:
                calls.append((command, kwargs))
                stdout = (
                    f"AGENT_HANDOFF_CREDENTIAL={credential}\n"
                    if Path(command[1]).name == "write_agent_handoff.py"
                    else ""
                )
                return SimpleNamespace(returncode=0, stdout=stdout)

            with patch("init_opencode.run_checked", side_effect=run_step):
                init_opencode.refresh_handoff_and_context(workspace)

            commands = [command for command, _kwargs in calls]
            scripts = [Path(command[1]).name for command in commands]
            self.assertLess(scripts.index("write_codex_handoff.py"), scripts.index("write_agent_handoff.py"))
            self.assertLess(scripts.index("write_agent_handoff.py"), scripts.index("export_agent_context.py"))
            export_kwargs = next(
                kwargs
                for command, kwargs in calls
                if Path(command[1]).name == "export_agent_context.py"
            )
            self.assertEqual(export_kwargs["env"].get(FRESH_CHECKPOINT_ENV), credential)

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

            def run_step(command: list[str], **_kwargs: object) -> SimpleNamespace:
                commands.append(command)
                stdout = (
                    f"AGENT_HANDOFF_CREDENTIAL={'v1:' + 'a' * 64}\n"
                    if Path(command[1]).name == "write_agent_handoff.py"
                    else ""
                )
                return SimpleNamespace(returncode=0, stdout=stdout)

            with patch("init_opencode.run_checked", side_effect=run_step):
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

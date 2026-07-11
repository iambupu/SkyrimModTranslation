from __future__ import annotations

import sys
import unittest
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import claim_workflow_task as claim_tasks  # noqa: E402
import resume_workflow  # noqa: E402
import run_workflow_tasks  # noqa: E402
import write_workflow_tasks  # noqa: E402


def workflow_task(
    task_id: str,
    resource: str,
    *,
    mod: str = "BigMod",
    status: str = "pending",
    owner: str = "",
    lease_until: str = "",
    parallel: bool = True,
    evidence: str = "",
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "mod": mod,
        "stage": "translated",
        "kind": "translate_shard",
        "status": status,
        "command": "python scripts/validate_translation.py",
        "executable": True,
        "can_run_parallel": parallel,
        "dependencies": [],
        "resource_locks": [resource],
        "evidence": evidence,
        "claim_owner": owner,
        "lease_until": lease_until,
        "risk": "low",
    }


class WorkflowTaskParallelismTests(unittest.TestCase):
    def with_workspace_env(self, workspace: Path):
        class EnvGuard:
            def __enter__(guard_self):
                guard_self.old_workspace = os.environ.get("SKYRIM_CHS_WORKSPACE_ROOT")
                guard_self.old_plugin = os.environ.get("SKYRIM_CHS_PLUGIN_ROOT")
                os.environ["SKYRIM_CHS_WORKSPACE_ROOT"] = str(workspace)
                os.environ["SKYRIM_CHS_PLUGIN_ROOT"] = str(ROOT)

            def __exit__(guard_self, exc_type, exc, tb):
                if guard_self.old_workspace is None:
                    os.environ.pop("SKYRIM_CHS_WORKSPACE_ROOT", None)
                else:
                    os.environ["SKYRIM_CHS_WORKSPACE_ROOT"] = guard_self.old_workspace
                if guard_self.old_plugin is None:
                    os.environ.pop("SKYRIM_CHS_PLUGIN_ROOT", None)
                else:
                    os.environ["SKYRIM_CHS_PLUGIN_ROOT"] = guard_self.old_plugin

        return EnvGuard()

    def test_project_python_commands_accept_absolute_python_runner(self) -> None:
        script = ROOT / "scripts" / "validate_translation.py"
        runner = ROOT / "tools" / "python-venv" / "Scripts" / "python.exe"
        command = f'"{runner}" "{script}" --help'

        self.assertTrue(write_workflow_tasks.command_is_project_python(command))
        self.assertEqual(write_workflow_tasks.command_script_name(command), "validate_translation.py")
        self.assertTrue(run_workflow_tasks.project_python_argv(ROOT, command)[1].endswith("validate_translation.py"))
        self.assertTrue(resume_workflow.project_python_argv(ROOT, command)[1].endswith("validate_translation.py"))

    def test_agent_handoff_writer_is_not_parallel_safe(self) -> None:
        parallel_safe, resources, notes = write_workflow_tasks.classify_command(
            "python scripts/write_agent_handoff.py"
        )

        self.assertFalse(parallel_safe)
        self.assertIn("global:workflow-state", resources)
        self.assertTrue(notes)

    def test_action_resource_locks_create_parallel_file_lane_task(self) -> None:
        script = ROOT / "scripts" / "validate_translation.py"
        runner = ROOT / "tools" / "python-venv" / "Scripts" / "python.exe"
        task = write_workflow_tasks.task_from_action(
            mod_name="BigMod",
            state="translated",
            last_success="candidates_extracted",
            action={
                "type": "translate_shard",
                "reason": "large_mod_file_shard",
                "command": f'"{runner}" "{script}" --help',
                "risk": "low",
                "resource_locks": ["file:BigMod:Interface/a.txt"],
                "dependencies": ["prep-a"],
                "can_run_parallel": True,
            },
            action_index=0,
            source="recommended_actions",
        )

        self.assertTrue(task["can_run_parallel"])
        self.assertEqual(task["resource_locks"], ["file:BigMod:Interface/a.txt"])
        self.assertEqual(task["dependencies"], ["prep-a"])
        self.assertEqual(write_workflow_tasks.build_resource_lanes([task])[0]["resource_lock"], "file:BigMod:Interface/a.txt")

    def test_mod_locks_conflict_with_file_lanes_but_distinct_file_lanes_do_not(self) -> None:
        self.assertTrue(run_workflow_tasks.resources_conflict({"mod:BigMod"}, {"file:BigMod:Interface/a.txt"}))
        self.assertTrue(claim_tasks.resources_conflict({"resource:BigMod:mcm"}, {"mod:BigMod"}))
        self.assertTrue(resume_workflow.resources_conflict({"mod:BigMod"}, {"resource:BigMod:mcm"}))
        self.assertFalse(run_workflow_tasks.resources_conflict({"file:BigMod:Interface/a.txt"}, {"file:BigMod:Interface/b.txt"}))

    def test_claim_filter_selects_independent_resource_lane(self) -> None:
        future = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "tasks": [
                workflow_task("a", "file:BigMod:Interface/a.txt", status="running", owner="agent:a", lease_until=future),
                workflow_task("b", "file:BigMod:Interface/b.txt"),
                workflow_task("a2", "file:BigMod:Interface/a.txt"),
                workflow_task("m", "mod:BigMod"),
            ]
        }

        selected = claim_tasks.select_task(payload, "", "BigMod", "file:BigMod:Interface/b.txt", parallel_only=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["task_id"], "b")
        self.assertIsNone(claim_tasks.select_task(payload, "", "BigMod", "file:BigMod:Interface/a.txt", parallel_only=True))
        self.assertFalse(run_workflow_tasks.resources_available(payload, workflow_task("m2", "mod:BigMod")))

    def test_empty_running_lease_is_reclaimable(self) -> None:
        payload = {
            "tasks": [
                workflow_task("stale", "file:BigMod:Interface/a.txt", status="running", owner="old-agent", lease_until=""),
                workflow_task("next", "file:BigMod:Interface/a.txt"),
            ]
        }

        selected_by_claim = claim_tasks.select_task(payload, "", "BigMod", "file:BigMod:Interface/a.txt", parallel_only=True)
        selected_by_scheduler = run_workflow_tasks.executable_pending_tasks(payload, include_serial=False, include_gui=False)

        self.assertIsNotNone(selected_by_claim)
        self.assertEqual(selected_by_claim["task_id"], "stale")
        self.assertEqual(selected_by_scheduler[0]["task_id"], "stale")
        self.assertFalse(claim_tasks.lease_is_active(payload["tasks"][0], datetime.now()))

    def test_resume_workflow_reclaims_empty_running_lease(self) -> None:
        payload = {
            "tasks": [
                workflow_task("stale", "file:BigMod:Interface/a.txt", status="running", owner="old-agent", lease_until=""),
                workflow_task("next", "file:BigMod:Interface/a.txt"),
            ]
        }

        selected = resume_workflow.choose_task(payload, "BigMod", "", "file:BigMod:Interface/a.txt", False)

        self.assertEqual(selected["task_id"], "stale")
        self.assertFalse(resume_workflow.lease_is_active(payload["tasks"][0], datetime.now()))

    def test_scheduler_claim_writes_finite_lease_for_stale_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            tasks_path = workspace / "qa" / "workflow_tasks.json"
            tasks_path.parent.mkdir(parents=True)
            tasks_path.write_text(
                json.dumps({"tasks": [workflow_task("stale", "file:BigMod:a.txt", status="running", owner="old-agent")]}),
                encoding="utf-8",
            )

            with self.with_workspace_env(workspace):
                self.assertTrue(run_workflow_tasks.mark_task_running_if_pending(tasks_path, "stale", 3))

            payload = json.loads(tasks_path.read_text(encoding="utf-8-sig"))
            task = payload["tasks"][0]
            self.assertEqual(task["status"], "running")
            self.assertTrue(str(task["claim_owner"]).startswith("pid:"))
            lease_until = datetime.strptime(task["lease_until"], "%Y-%m-%d %H:%M:%S")
            self.assertGreater(lease_until, datetime.now())

    def test_resume_claim_writes_finite_lease_for_stale_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            tasks_path = workspace / "qa" / "workflow_tasks.json"
            tasks_path.parent.mkdir(parents=True)
            tasks_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                **workflow_task("stale", "file:BigMod:a.txt", status="running", owner="old-agent"),
                                "finished_at": "2026-01-01 00:00:00",
                                "exit_code": 1,
                                "output_tail": ["old failure"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.with_workspace_env(workspace):
                self.assertTrue(resume_workflow.mark_task_running_if_pending(tasks_path, "stale", 3))

            payload = json.loads(tasks_path.read_text(encoding="utf-8-sig"))
            task = payload["tasks"][0]
            self.assertEqual(task["status"], "running")
            self.assertTrue(str(task["claim_owner"]).startswith("pid:"))
            lease_until = datetime.strptime(task["lease_until"], "%Y-%m-%d %H:%M:%S")
            self.assertGreater(lease_until, datetime.now())
            self.assertEqual(task["finished_at"], "")
            self.assertIsNone(task["exit_code"])
            self.assertEqual(task["output_tail"], [])

    def test_scheduler_runs_independent_file_lanes_without_shared_lock_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            tasks_path = workspace / "qa" / "workflow_tasks.json"
            tasks_path.parent.mkdir(parents=True)
            task_a = workflow_task("smoke-a", "file:BigMod:Interface/a.txt")
            task_b = workflow_task("smoke-b", "file:BigMod:Interface/b.txt")
            task_a["command"] = "python scripts/validate_translation.py --help"
            task_b["command"] = "python scripts/validate_translation.py --help"
            tasks_path.write_text(
                json.dumps({"schema_version": 1, "tasks": [task_a, task_b]}, ensure_ascii=False),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            }

            scheduled = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "run_workflow_tasks.py"),
                    "--max-workers",
                    "2",
                    "--limit",
                    "2",
                    "--no-refresh",
                    "--timeout-seconds",
                    "60",
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            self.assertEqual(scheduled.returncode, 0, scheduled.stdout + scheduled.stderr)
            payload = json.loads(tasks_path.read_text(encoding="utf-8-sig"))
            self.assertEqual({task["status"] for task in payload["tasks"]}, {"done"})
            self.assertEqual({task["exit_code"] for task in payload["tasks"]}, {0})
            log_path = workspace / "qa" / "workflow_agent_runs.jsonl"
            log_text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
            self.assertNotIn("Resource lock is already held", log_text)
            rows = [json.loads(line) for line in log_text.splitlines() if line.strip()]
            passed_commands = [row for row in rows if row.get("event") == "command" and row.get("status") == "passed"]
            self.assertGreaterEqual(len(passed_commands), 2)

    def test_claim_reclaims_stale_task_and_writes_agent_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            tasks_path = workspace / "qa" / "workflow_tasks.json"
            tasks_path.parent.mkdir(parents=True)
            outside_evidence = str(workspace.parent / "outside_evidence.md")
            tasks_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            workflow_task(
                                "stale",
                                "file:BigMod:a.txt",
                                status="running",
                                owner="old-agent",
                                evidence=outside_evidence,
                            )
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            }

            claimed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "claim_workflow_task.py"),
                    "--task-id",
                    "stale",
                    "--owner",
                    "agent:new",
                    "--parallel-only",
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            self.assertEqual(claimed.returncode, 0, claimed.stderr)
            payload = json.loads(tasks_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(payload["tasks"][0]["claim_owner"], "agent:new")
            rows = [
                json.loads(line)
                for line in (workspace / "qa" / "workflow_agent_runs.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["event"], "claim")
            self.assertEqual(rows[-1]["status"], "started")
            self.assertEqual(rows[-1]["task_id"], "stale")
            self.assertEqual(rows[-1]["owner"], "agent:new")
            self.assertEqual(rows[-1]["evidence"], "")

    def test_release_only_allows_running_task_owned_by_requester(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            tasks_path = workspace / "qa" / "workflow_tasks.json"
            tasks_path.parent.mkdir(parents=True)
            tasks_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            workflow_task("done-task", "file:BigMod:a.txt", status="done"),
                            workflow_task("running-task", "file:BigMod:b.txt", status="running", owner="agent:b"),
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
            }

            rejected = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "claim_workflow_task.py"),
                    "--task-id",
                    "done-task",
                    "--owner",
                    "agent:b",
                    "--release",
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            payload_after_reject = json.loads(tasks_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(payload_after_reject["tasks"][0]["status"], "done")

            released = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "claim_workflow_task.py"),
                    "--task-id",
                    "running-task",
                    "--owner",
                    "agent:b",
                    "--release",
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(released.returncode, 0, released.stderr)
            payload_after_release = json.loads(tasks_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(payload_after_release["tasks"][1]["status"], "pending")
            self.assertEqual(payload_after_release["tasks"][1]["claim_owner"], "")
            self.assertEqual(payload_after_release["tasks"][1]["started_at"], "")


if __name__ == "__main__":
    unittest.main()

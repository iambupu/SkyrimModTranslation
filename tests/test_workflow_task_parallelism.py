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
        "claim_owner": owner,
        "lease_until": lease_until,
        "risk": "low",
    }


class WorkflowTaskParallelismTests(unittest.TestCase):
    def test_project_python_commands_accept_absolute_python_runner(self) -> None:
        script = ROOT / "scripts" / "validate_translation.py"
        runner = ROOT / "tools" / "python-venv" / "Scripts" / "python.exe"
        command = f'"{runner}" "{script}" --help'

        self.assertTrue(write_workflow_tasks.command_is_project_python(command))
        self.assertEqual(write_workflow_tasks.command_script_name(command), "validate_translation.py")
        self.assertTrue(run_workflow_tasks.project_python_argv(ROOT, command)[1].endswith("validate_translation.py"))
        self.assertTrue(resume_workflow.project_python_argv(ROOT, command)[1].endswith("validate_translation.py"))

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

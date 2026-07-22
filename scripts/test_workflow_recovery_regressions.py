from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import write_workflow_state  # noqa: E402
import write_workflow_tasks  # noqa: E402
from workflow_lock import (  # noqa: E402
    RESOURCE_LOCKS_ENV,
    ResourceLock,
    WorkflowLock,
    resource_lock_environment,
)
from workflow_lock import process_is_alive  # noqa: E402


class WorkflowRecoveryRegressionTests(unittest.TestCase):
    def test_reemitted_failed_task_returns_to_pending(self) -> None:
        new_task = {
            "task_id": "same-task",
            "status": "pending",
            "claim_owner": "",
            "lease_until": "",
            "started_at": "",
            "finished_at": "",
            "exit_code": None,
            "output_tail": [],
        }
        previous = {
            "tasks": [
                {
                    **new_task,
                    "status": "failed",
                    "finished_at": "2026-07-10 10:00:00",
                    "exit_code": 1,
                    "output_tail": ["old failure"],
                }
            ]
        }

        merged = write_workflow_tasks.preserve_runtime_fields([new_task], previous)

        self.assertEqual(merged[0]["status"], "pending")
        self.assertEqual(merged[0]["finished_at"], "")
        self.assertIsNone(merged[0]["exit_code"])
        self.assertEqual(merged[0]["output_tail"], [])

    def test_retry_count_counts_completed_retries_not_log_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "qa" / "workflow_agent_runs.jsonl"
            log_path.parent.mkdir(parents=True)
            rows = [
                {"mod": "Example", "event": "claim", "status": "started", "timestamp": "1"},
                {"mod": "Example", "event": "command", "status": "started", "timestamp": "2"},
                {"mod": "Example", "event": "command", "status": "failed", "timestamp": "3"},
                {"mod": "Example", "event": "claim", "status": "started", "timestamp": "4"},
                {"mod": "Example", "event": "complete", "status": "passed", "timestamp": "5"},
            ]
            log_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )

            retry_count, last_attempt = write_workflow_state.agent_attempt_summary(root, "Example")

            self.assertEqual(retry_count, 1)
            self.assertEqual(last_attempt["event"], "complete")
            self.assertEqual(last_attempt["status"], "passed")

    def test_skipped_command_does_not_count_as_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "qa" / "workflow_agent_runs.jsonl"
            log_path.parent.mkdir(parents=True)
            rows = [
                {"mod": "Example", "event": "command", "status": "passed", "timestamp": "1"},
                {"mod": "Example", "event": "command", "status": "skipped", "timestamp": "2"},
            ]
            log_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )

            retry_count, last_attempt = write_workflow_state.agent_attempt_summary(root, "Example")

            self.assertEqual(retry_count, 0)
            self.assertEqual(last_attempt["status"], "passed")

    def test_process_liveness_detects_running_and_exited_child(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(process_is_alive(process.pid))
        finally:
            process.terminate()
            process.wait(timeout=10)
        self.assertFalse(process_is_alive(process.pid))

    def test_resource_lock_reclaims_dead_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock = ResourceLock(root, "mod:Example", "new-owner")
            lock.path.parent.mkdir(parents=True)
            lock.path.write_text(
                json.dumps(
                    {
                        "owner": "dead-owner",
                        "resource": "mod:Example",
                        "pid": 2147483647,
                        "created_at": "2020-01-01 00:00:00",
                        "token": "dead-token",
                    }
                ),
                encoding="utf-8",
            )

            acquired = lock.acquire()
            try:
                self.assertTrue(acquired.acquired)
                payload = json.loads(lock.path.read_text(encoding="utf-8"))
                self.assertEqual(payload["token"], lock.token)
            finally:
                lock.release()

    def test_resource_lock_allows_verified_inherited_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_value = os.environ.pop(RESOURCE_LOCKS_ENV, None)
            outer = ResourceLock(root, "mod:Example", "scheduler").acquire()
            try:
                self.assertNotIn(RESOURCE_LOCKS_ENV, os.environ)
                os.environ[RESOURCE_LOCKS_ENV] = resource_lock_environment(
                    (outer,)
                )
                try:
                    inner = ResourceLock(root, "mod:Example", "leaf").acquire()
                    self.assertTrue(inner.reentrant)
                    self.assertFalse(inner.acquired)
                    inner.release()
                    self.assertTrue(outer.path.is_file())
                finally:
                    os.environ.pop(RESOURCE_LOCKS_ENV, None)
            finally:
                outer.release()
                if old_value is not None:
                    os.environ[RESOURCE_LOCKS_ENV] = old_value

    def test_resource_lock_child_environment_is_task_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = ResourceLock(root, "mod:First", "scheduler:first").acquire()
            second = ResourceLock(root, "mod:Second", "scheduler:second").acquire()
            try:
                first_payload = json.loads(resource_lock_environment((first,)))
                second_payload = json.loads(resource_lock_environment((second,)))
                self.assertEqual(set(first_payload), {"mod:First"})
                self.assertEqual(set(second_payload), {"mod:Second"})
                self.assertNotIn(RESOURCE_LOCKS_ENV, os.environ)
            finally:
                second.release()
                first.release()

    def test_resource_lock_rejects_unverified_inherited_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_value = os.environ.get(RESOURCE_LOCKS_ENV)
            os.environ[RESOURCE_LOCKS_ENV] = json.dumps(
                {
                    "mod:Example": {
                        "path": str(root / "work" / "locks" / "mod_Example.lock"),
                        "token": "forged-token",
                    }
                }
            )
            lock = ResourceLock(root, "mod:Example", "leaf")
            try:
                acquired = lock.acquire()
                self.assertTrue(acquired.acquired)
                self.assertFalse(acquired.reentrant)
            finally:
                lock.release()
                if old_value is None:
                    os.environ.pop(RESOURCE_LOCKS_ENV, None)
                else:
                    os.environ[RESOURCE_LOCKS_ENV] = old_value

    def test_workflow_lock_reclaims_dead_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock = WorkflowLock(root, "new-owner")
            lock.path.parent.mkdir(parents=True)
            lock.path.write_text(
                json.dumps(
                    {
                        "owner": "dead-owner",
                        "pid": 2147483647,
                        "created_at": "2020-01-01 00:00:00",
                        "token": "dead-token",
                    }
                ),
                encoding="utf-8",
            )
            old_token = os.environ.pop("SKYRIM_TRANSLATION_WORKFLOW_LOCK_TOKEN", None)
            old_path = os.environ.pop("SKYRIM_TRANSLATION_WORKFLOW_LOCK_PATH", None)
            try:
                acquired = lock.acquire()
                self.assertTrue(acquired.acquired)
            finally:
                lock.release()
                if old_token is not None:
                    os.environ["SKYRIM_TRANSLATION_WORKFLOW_LOCK_TOKEN"] = old_token
                if old_path is not None:
                    os.environ["SKYRIM_TRANSLATION_WORKFLOW_LOCK_PATH"] = old_path


if __name__ == "__main__":
    unittest.main()

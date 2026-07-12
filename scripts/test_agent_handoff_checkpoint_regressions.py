from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import write_agent_handoff  # noqa: E402
from game_context import game_context_metadata, load_game_profile  # noqa: E402


def minimal_handoff_payload() -> dict[str, object]:
    return {
        **game_context_metadata(load_game_profile("skyrim-se")),
        "project_state": "ready",
        "readiness_overall_status": "ready",
        "source_reports": {},
        "task_summary": {},
        "blocking_mods": [],
    }


class AgentHandoffCheckpointRegressionTests(unittest.TestCase):
    def test_unchanged_checkpoint_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "mod").mkdir()
            (root / "mod" / "sample.txt").write_text("one", encoding="utf-8")

            checkpoint = write_agent_handoff.build_resume_checkpoint(root, minimal_handoff_payload())
            result = write_agent_handoff.evaluate_resume_checkpoint(root, checkpoint)

            self.assertTrue(result["fresh"])
            self.assertEqual(result["reasons"], [])

    def test_watched_file_change_makes_checkpoint_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watched = root / "mod" / "sample.txt"
            watched.parent.mkdir()
            watched.write_text("one", encoding="utf-8")
            checkpoint = write_agent_handoff.build_resume_checkpoint(root, minimal_handoff_payload())

            watched.write_text("two", encoding="utf-8")
            newer = int(checkpoint["generated_at_epoch_ns"]) + 1_000_000_000
            os.utime(watched, ns=(newer, newer))
            result = write_agent_handoff.evaluate_resume_checkpoint(root, checkpoint)

            self.assertFalse(result["fresh"])
            self.assertTrue(any(row["path"] == "mod" for row in result["reasons"]))

    def test_watched_path_deletion_makes_checkpoint_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watched = root / "qa" / "workflow_state.json"
            watched.parent.mkdir()
            watched.write_text("{}", encoding="utf-8")
            checkpoint = write_agent_handoff.build_resume_checkpoint(root, minimal_handoff_payload())

            watched.unlink()
            result = write_agent_handoff.evaluate_resume_checkpoint(root, checkpoint)

            self.assertFalse(result["fresh"])
            self.assertTrue(any(row["path"] == "qa/workflow_state.json" for row in result["reasons"]))

    def test_truncated_watch_snapshot_is_never_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watched = root / "mod"
            watched.mkdir()
            (watched / "a.txt").write_text("a", encoding="utf-8")
            checkpoint = write_agent_handoff.build_resume_checkpoint(root, minimal_handoff_payload())
            truncated = write_agent_handoff.path_snapshot(root, "mod", max_entries=1)
            checkpoint["stale_if_newer_than"]["watch"][0] = truncated

            result = write_agent_handoff.evaluate_resume_checkpoint(root, checkpoint)

            self.assertFalse(result["fresh"])
            self.assertTrue(any(row["reason"] == "stored_snapshot_truncated" for row in result["reasons"]))

    def test_checkpoint_missing_required_watch_path_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = write_agent_handoff.build_resume_checkpoint(root, minimal_handoff_payload())
            checkpoint["stale_if_newer_than"]["watch"] = checkpoint["stale_if_newer_than"]["watch"][1:]

            result = write_agent_handoff.evaluate_resume_checkpoint(root, checkpoint)

            self.assertFalse(result["fresh"])
            self.assertTrue(any(row["reason"] == "missing_required_watch_path" for row in result["reasons"]))

    def test_marker_change_and_handoff_metadata_mismatch_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker_path = root / ".skyrim-chs-workspace.json"
            marker_path.write_text(
                json.dumps({"game_id": "skyrim-se", "game_profile": "skyrim-se"}) + "\n",
                encoding="utf-8",
            )
            payload = minimal_handoff_payload()
            payload["resume_checkpoint"] = write_agent_handoff.build_resume_checkpoint(root, payload)

            marker_path.write_text(
                json.dumps({"game_id": "fallout4", "game_profile": "fallout4"}) + "\n",
                encoding="utf-8",
            )
            result = write_agent_handoff.evaluate_agent_handoff_freshness(root, payload)

            self.assertFalse(result["fresh"])
            self.assertTrue(any(row["path"] == ".skyrim-chs-workspace.json" for row in result["reasons"]))
            self.assertTrue(any(row["reason"] == "game_metadata_mismatch" for row in result["reasons"]))


if __name__ == "__main__":
    unittest.main()

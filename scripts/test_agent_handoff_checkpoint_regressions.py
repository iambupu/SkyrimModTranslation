from __future__ import annotations

import os
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
    def test_large_file_uses_documented_deterministic_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watched = root / "qa" / "large.bin"
            watched.parent.mkdir()
            size = 512 * 1024 + 123
            watched.write_bytes(bytes((index % 251 for index in range(size))))

            snapshot = write_agent_handoff.path_snapshot(root, "qa/large.bin")
            repeated = write_agent_handoff.path_snapshot(root, "qa/large.bin")

            sample_bytes = 64 * 1024
            expected_offsets = [0, (size - sample_bytes) // 2, size - sample_bytes]
            self.assertEqual(snapshot.get("fingerprint_mode"), "sampled_sha256")
            self.assertEqual(snapshot.get("content_sha256"), "")
            self.assertEqual(
                [row["offset"] for row in snapshot.get("samples", [])],
                expected_offsets,
            )
            self.assertTrue(
                all(row["length"] == sample_bytes for row in snapshot.get("samples", []))
            )
            self.assertEqual(snapshot.get("read_bytes"), sample_bytes * 3)
            self.assertEqual(snapshot.get("fingerprint"), repeated.get("fingerprint"))
            self.assertEqual(
                snapshot.get("snapshot_policy", {}).get("large_file_sample_positions"),
                ["start", "middle", "end"],
            )

    def test_directory_read_budget_exhaustion_is_explicit_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watched = root / "mod"
            watched.mkdir()
            for index in range(3):
                (watched / f"{index}.txt").write_bytes(bytes([index]) * 4096)
            kwargs = (
                {"max_read_bytes": 5000}
                if "max_read_bytes" in inspect.signature(write_agent_handoff.path_snapshot).parameters
                else {}
            )
            actual_read_bytes = 0
            original_open = Path.open

            class CountingHandle:
                def __init__(self, handle: object) -> None:
                    self.handle = handle

                def __enter__(self) -> CountingHandle:
                    self.handle.__enter__()
                    return self

                def __exit__(self, *args: object) -> object:
                    return self.handle.__exit__(*args)

                def seek(self, *args: object) -> int:
                    return self.handle.seek(*args)

                def read(self, *args: object) -> bytes:
                    nonlocal actual_read_bytes
                    data = self.handle.read(*args)
                    actual_read_bytes += len(data)
                    return data

            def counting_open(path: Path, *args: object, **open_kwargs: object) -> CountingHandle:
                return CountingHandle(original_open(path, *args, **open_kwargs))

            with mock.patch.object(Path, "open", counting_open):
                snapshot = write_agent_handoff.path_snapshot(root, "mod", **kwargs)

            self.assertFalse(snapshot.get("complete", True))
            self.assertTrue(snapshot.get("truncated", False))
            self.assertEqual(snapshot.get("snapshot_status"), "limit_exceeded")
            self.assertEqual(snapshot.get("limit_reason"), "read_budget_exceeded")
            self.assertLessEqual(snapshot.get("read_bytes", 0), 5000)
            self.assertEqual(actual_read_bytes, snapshot.get("read_bytes"))
            self.assertEqual(
                snapshot.get("snapshot_policy", {}).get("limit_behavior"),
                "fail_closed",
            )

    def test_directory_entry_limit_is_explicit_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watched = root / "mod"
            watched.mkdir()
            (watched / "a.txt").write_text("a", encoding="utf-8")
            (watched / "b.txt").write_text("b", encoding="utf-8")

            snapshot = write_agent_handoff.path_snapshot(root, "mod", max_entries=1)

            self.assertFalse(snapshot.get("complete", True))
            self.assertTrue(snapshot.get("truncated", False))
            self.assertEqual(snapshot.get("snapshot_status"), "limit_exceeded")
            self.assertEqual(snapshot.get("limit_reason"), "max_entries_exceeded")
            self.assertEqual(snapshot.get("max_entries"), 1)

    def test_checkpoint_limits_total_evidence_refs_and_fails_closed(self) -> None:
        payload = minimal_handoff_payload()
        payload["blocking_mods"] = [
            {
                "must_read_evidence": [f"qa/evidence-{index}.md" for index in range(70)]
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            checkpoint = write_agent_handoff.build_resume_checkpoint(root, payload)

            self.assertEqual(len(checkpoint["artifact_refs"]), 64)
            self.assertEqual(checkpoint["evidence_ref_summary"]["limit"], 64)
            self.assertEqual(checkpoint["evidence_ref_summary"]["selected_count"], 64)
            self.assertFalse(checkpoint["evidence_ref_summary"]["complete"])
            self.assertGreaterEqual(
                checkpoint["evidence_ref_summary"]["observed_count_at_least"],
                65,
            )
            self.assertFalse(checkpoint["snapshot_summary"]["complete"])
            self.assertIn(
                "evidence_ref_limit_exceeded",
                checkpoint["snapshot_summary"]["incomplete_reasons"],
            )

    def test_checkpoint_artifacts_and_watches_share_one_read_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / ".skyrim-chs-workspace.json"
            marker.write_bytes(b"m" * 4096)
            artifact = root / ".workflow" / "progress_card.md"
            artifact.parent.mkdir()
            artifact.write_bytes(b"a" * 4096)
            kwargs = (
                {"max_total_read_bytes": 5000}
                if "max_total_read_bytes"
                in inspect.signature(write_agent_handoff.build_resume_checkpoint).parameters
                else {}
            )

            checkpoint = write_agent_handoff.build_resume_checkpoint(
                root,
                minimal_handoff_payload(),
                **kwargs,
            )

            marker_snapshot = checkpoint["stale_if_newer_than"]["watch"][0]
            artifact_snapshot = checkpoint["artifact_refs"][0]["snapshot"]
            self.assertTrue(marker_snapshot["complete"])
            self.assertFalse(artifact_snapshot["complete"])
            self.assertEqual(artifact_snapshot["limit_reason"], "read_budget_exceeded")
            self.assertFalse(checkpoint["snapshot_summary"]["complete"])
            self.assertTrue(checkpoint["snapshot_summary"]["budget_exhausted"])
            self.assertEqual(checkpoint["snapshot_summary"]["read_budget_bytes"], 5000)
            self.assertLessEqual(checkpoint["snapshot_summary"]["read_bytes"], 5000)

    def test_standalone_freshness_reuses_one_decreasing_watch_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".skyrim-chs-workspace.json").write_bytes(b"m" * 4096)
            watched = root / "mod" / "sample.txt"
            watched.parent.mkdir()
            watched.write_bytes(b"w" * 4096)
            kwargs = (
                {"max_total_read_bytes": 9000}
                if "max_total_read_bytes"
                in inspect.signature(write_agent_handoff.build_resume_checkpoint).parameters
                else {}
            )
            checkpoint = write_agent_handoff.build_resume_checkpoint(
                root,
                minimal_handoff_payload(),
                **kwargs,
            )
            observed_budgets: list[int | None] = []
            original_snapshot = write_agent_handoff.path_snapshot

            def tracking_snapshot(*args: object, **snapshot_kwargs: object) -> dict[str, object]:
                observed_budgets.append(snapshot_kwargs.get("max_read_bytes"))
                return original_snapshot(*args, **snapshot_kwargs)

            with mock.patch.object(
                write_agent_handoff,
                "path_snapshot",
                side_effect=tracking_snapshot,
            ):
                result = write_agent_handoff.evaluate_resume_checkpoint(root, checkpoint)

            self.assertTrue(result["fresh"])
            self.assertEqual(observed_budgets[:3], [9000, 4904, 808])

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
            (watched / "b.txt").write_text("b", encoding="utf-8")
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

    def test_incomplete_checkpoint_snapshot_summary_is_stale_with_valid_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = write_agent_handoff.build_resume_checkpoint(root, minimal_handoff_payload())
            checkpoint["snapshot_summary"]["complete"] = False
            checkpoint["snapshot_summary"]["limit_reason"] = "artifact_read_budget_exceeded"
            checkpoint["checkpoint_id"] = write_agent_handoff.checkpoint_id_for(checkpoint)

            result = write_agent_handoff.evaluate_resume_checkpoint(
                root,
                checkpoint,
                verify_current_snapshots=False,
            )

            self.assertFalse(result["fresh"])
            self.assertTrue(
                any(row["reason"] == "checkpoint_snapshot_incomplete" for row in result["reasons"])
            )

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

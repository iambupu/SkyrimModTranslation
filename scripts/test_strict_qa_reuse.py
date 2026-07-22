import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from strict_qa_reuse import (  # noqa: E402
    load_reusable_mechanical_snapshot,
    write_reusable_mechanical_snapshot,
)
import run_non_gui_qa_gates as qa_gates  # noqa: E402


class StrictQaReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.root = base / "workspace"
        self.source_root = base / "plugin"
        self.workspace = self.root / "work" / "extracted_mods" / "Example"
        self.final_mod = self.root / "out" / "Example" / "汉化产出" / "final_mod"
        self.translation = self.root / "translated" / "Example.jsonl"
        self.evidence = self.root / "qa" / "Example.final_text_review_packet.md"
        self.snapshot = self.root / "qa" / "Example.strict_mechanical_snapshot.json"

        for path in (self.workspace, self.final_mod, self.translation.parent, self.evidence.parent, self.source_root / "scripts"):
            path.mkdir(parents=True, exist_ok=True)
        (self.workspace / "Example.esp").write_bytes(b"source")
        (self.final_mod / "Example.esp").write_bytes(b"translated")
        self.translation.write_text('{"Source":"Hello","Translation":"你好"}\n', encoding="utf-8")
        self.evidence.write_text("packet\n", encoding="utf-8")
        (self.source_root / "scripts" / "gate_helper.py").write_text("VERSION = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_snapshot(self) -> None:
        write_reusable_mechanical_snapshot(
            root=self.root,
            snapshot_path=self.snapshot,
            mod_name="Example",
            workspace=self.workspace,
            final_mod=self.final_mod,
            translation_inputs=[self.translation],
            evidence_paths=[self.evidence],
            game_metadata={"GameId": "fallout4"},
            metrics={"coverage_missing": "0"},
            notes=["mechanical checks passed"],
            source_root=self.source_root,
        )

    def load_snapshot(self):
        return load_reusable_mechanical_snapshot(
            root=self.root,
            snapshot_path=self.snapshot,
            mod_name="Example",
            workspace=self.workspace,
            final_mod=self.final_mod,
            translation_inputs=[self.translation],
            evidence_paths=[self.evidence],
            game_metadata={"GameId": "fallout4"},
            source_root=self.source_root,
        )

    def test_reuses_snapshot_when_inputs_and_evidence_are_unchanged(self) -> None:
        self.write_snapshot()

        payload, reason = self.load_snapshot()

        self.assertEqual(reason, "")
        self.assertEqual(payload["Metrics"], {"coverage_missing": "0"})
        self.assertEqual(payload["Notes"], ["mechanical checks passed"])

    def test_rejects_snapshot_when_final_mod_changes(self) -> None:
        self.write_snapshot()
        (self.final_mod / "Example.esp").write_bytes(b"changed")

        payload, reason = self.load_snapshot()

        self.assertIsNone(payload)
        self.assertEqual(reason, "tracked inputs changed")

    def test_rejects_snapshot_when_translation_input_is_added(self) -> None:
        self.write_snapshot()
        added = self.translation.with_name("Extra.jsonl")
        added.write_text("{}\n", encoding="utf-8")

        payload, reason = load_reusable_mechanical_snapshot(
            root=self.root,
            snapshot_path=self.snapshot,
            mod_name="Example",
            workspace=self.workspace,
            final_mod=self.final_mod,
            translation_inputs=[self.translation, added],
            evidence_paths=[self.evidence],
            game_metadata={"GameId": "fallout4"},
            source_root=self.source_root,
        )

        self.assertIsNone(payload)
        self.assertEqual(reason, "translation input set changed")

    def test_rejects_snapshot_when_review_evidence_changes(self) -> None:
        self.write_snapshot()
        self.evidence.write_text("tampered\n", encoding="utf-8")

        payload, reason = self.load_snapshot()

        self.assertIsNone(payload)
        self.assertEqual(reason, "review evidence changed")

    def test_rejects_snapshot_when_external_plugin_source_changes(self) -> None:
        self.write_snapshot()
        (self.source_root / "scripts" / "gate_helper.py").write_text("VERSION = 2\n", encoding="utf-8")

        payload, reason = self.load_snapshot()

        self.assertIsNone(payload)
        self.assertEqual(reason, "tracked inputs changed")

    def test_rejects_non_strict_snapshot(self) -> None:
        self.write_snapshot()
        payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
        payload["StrictComplete"] = False
        self.snapshot.write_text(json.dumps(payload), encoding="utf-8")

        reused, reason = self.load_snapshot()

        self.assertIsNone(reused)
        self.assertEqual(reason, "snapshot is not from strict-complete QA")

    def test_workflow_callers_request_fail_safe_reuse(self) -> None:
        workflow = (ROOT / "scripts" / "run_non_gui_translation_workflow.py").read_text(encoding="utf-8")
        health = (ROOT / "scripts" / "test_workflow_health.py").read_text(encoding="utf-8")
        state = (ROOT / "scripts" / "write_workflow_state.py").read_text(encoding="utf-8")

        for source in (workflow, health, state):
            self.assertIn("--reuse-mechanical-evidence", source)

    def test_main_reuse_path_skips_all_mechanical_subscripts(self) -> None:
        for evidence in qa_gates.model_review_evidence_paths(self.root, "Example"):
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(f"{evidence.name}\n", encoding="utf-8")
        self.write_snapshot_with_all_review_evidence()
        context = SimpleNamespace(
            game_id="fallout4",
            schema_version=2,
            display_name="Fallout 4",
            support_level="experimental",
            interface_translation_encoding="utf-8",
            plugin_root=self.source_root,
        )
        report = self.root / "qa" / "Example.non_gui_qa_gates.md"
        arguments = [
            "run_non_gui_qa_gates.py",
            "--mod-name",
            "Example",
            "--workspace-path",
            str(self.workspace),
            "--final-mod-dir",
            str(self.final_mod),
            "--strict-complete",
            "--reuse-mechanical-evidence",
        ]

        with (
            patch.object(sys, "argv", arguments),
            patch.object(qa_gates, "project_root", return_value=self.root),
            patch.object(qa_gates, "current_game_context", return_value=context),
            patch.object(qa_gates, "find_data_root", side_effect=lambda path, context: path),
            patch.object(qa_gates, "collect_translation_inputs", return_value=[self.translation]),
            patch.object(qa_gates, "collect_model_review_gate_issues", return_value=[]),
            patch.object(qa_gates, "run_python_script", side_effect=AssertionError("mechanical subscript ran")),
            patch.object(qa_gates, "WorkflowLock"),
        ):
            exit_code = qa_gates.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("Reused content-bound strict mechanical evidence", report.read_text(encoding="utf-8"))

    def write_snapshot_with_all_review_evidence(self) -> None:
        write_reusable_mechanical_snapshot(
            root=self.root,
            snapshot_path=self.snapshot,
            mod_name="Example",
            workspace=self.workspace,
            final_mod=self.final_mod,
            translation_inputs=[self.translation],
            evidence_paths=qa_gates.model_review_evidence_paths(self.root, "Example"),
            game_metadata={
                "game_id": "fallout4",
                "game_profile_version": 2,
                "game_display_name": "Fallout 4",
                "support_level": "experimental",
                "interface_translation_encoding": "utf-8",
            },
            metrics={"coverage_missing": "0"},
            notes=[],
            source_root=self.source_root,
        )


if __name__ == "__main__":
    unittest.main()

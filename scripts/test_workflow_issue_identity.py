from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from audit_translation_readiness import output_root_issues
from game_context import game_context_metadata, load_game_profile
from write_workflow_state import build_state
from workflow_issues import aggregate_issue_records, compact_issue_refs, make_issue_record, stable_issue_id


class WorkflowIssueIdentityTests(unittest.TestCase):
    @staticmethod
    def output_row(**overrides: object) -> SimpleNamespace:
        defaults: dict[str, object] = {
            "ModName": "ExampleMod",
            "Workspace": "work/extracted_mods/ExampleMod",
            "WorkspaceExists": True,
            "PluginStageStatus": "passed",
            "PluginStageBlockingIssues": "0",
            "PluginStagePath": "qa/ExampleMod.plugin_translation_stage.json",
            "FinalModExists": True,
            "FinalModDir": "out/ExampleMod/汉化产出/final_mod",
            "ProvenanceStatus": "present",
            "ProvenancePath": "out/ExampleMod/汉化产出/final_mod/meta/provenance.jsonl",
            "UsedCapabilitiesStatus": "passed",
            "UsedCapabilitiesBlockingIssues": "0",
            "UsedCapabilitiesPath": "qa/ExampleMod.used_capabilities.json",
            "PackagedModExists": True,
            "PackagedModPath": "out/ExampleMod/汉化产出/ExampleMod_CHS.zip",
            "TranslationDictionaryStatus": "present",
            "TranslationDictionaryEntries": "1",
            "TranslationDictionaryPath": "out/ExampleMod/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl",
            "PackageValidationStatus": "passed",
            "PackageValidationBlockingIssues": "0",
            "PackageValidationReport": "qa/ExampleMod.chs_package_validation.md",
            "DeliveryMode": "direct-replacement-final-mod",
            "StrictGateBlockingIssues": "0",
            "StrictGateWarnings": "0",
            "CoverageMissing": "0",
            "CoverageBlocking": "0",
            "FinalTextProtectedItems": "0",
            "FinalBinaryProtectedItems": "0",
            "FinalBinaryExportFailures": "0",
            "FinalReviewQualityStatus": "passed",
            "FinalReviewQualityBlockingIssues": "0",
            "FinalReviewQualityWarnings": "0",
            "ModelReviewStatus": "passed",
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_issue_id_normalizes_case_and_path_separators(self) -> None:
        first = stable_issue_id("STRICT_GATE_NOT_CLEAN", "ExampleMod", r"QA\Example.non_gui_qa_gates.md")
        second = stable_issue_id("strict_gate_not_clean", "examplemod", "qa/example.non_gui_qa_gates.md")

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("strict_gate_not_clean:examplemod:"))

    def test_state_projection_keeps_only_issue_id_and_short_code(self) -> None:
        record = make_issue_record(
            code="strict_gate_not_clean",
            mod_name="ExampleMod",
            affected_artifact="qa/Example.non_gui_qa_gates.md",
            severity="error",
            message="Long detailed readiness explanation.",
            evidence_paths=["qa/Example.non_gui_qa_gates.md"],
            reported_by=["translation_readiness"],
        )

        refs = compact_issue_refs([record])

        self.assertEqual(set(refs[0]), {"issue_id", "code"})
        self.assertNotIn("message", refs[0])

    def test_health_aggregation_merges_reporters_and_evidence_once(self) -> None:
        readiness = make_issue_record(
            code="strict_gate_not_clean",
            mod_name="ExampleMod",
            affected_artifact="qa/Example.non_gui_qa_gates.md",
            severity="error",
            message="Strict gate contains blocking findings.",
            evidence_paths=["qa/Example.non_gui_qa_gates.md"],
            reported_by=["translation_readiness"],
        )
        health = make_issue_record(
            code="strict_gate_not_clean",
            mod_name="ExampleMod",
            affected_artifact=r"qa\Example.non_gui_qa_gates.md",
            severity="error",
            message="Duplicate health wording that should not be repeated.",
            evidence_paths=["qa/workflow_health.json", "qa/Example.non_gui_qa_gates.md"],
            reported_by=["workflow_health"],
        )

        aggregated = aggregate_issue_records([readiness, health])

        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated[0]["message"], readiness["message"])
        self.assertEqual(
            aggregated[0]["reported_by"],
            ["translation_readiness", "workflow_health"],
        )
        self.assertEqual(
            aggregated[0]["evidence_paths"],
            ["qa/Example.non_gui_qa_gates.md", "qa/workflow_health.json"],
        )

    def test_protected_text_and_binary_use_distinct_artifacts(self) -> None:
        row = self.output_row(FinalTextProtectedItems="1", FinalBinaryProtectedItems="1")

        issues = output_root_issues(row, "error")
        protected = [issue for issue in issues if issue.code == "protected_review_items"]

        self.assertEqual(len(protected), 2)
        self.assertEqual(
            {issue.affected_artifact for issue in protected},
            {
                "qa/ExampleMod.final_text_review_packet.md",
                "qa/ExampleMod.final_binary_review_packet.md",
            },
        )

    def test_workflow_state_consumes_readiness_issue_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = load_game_profile("fallout4")
            (root / ".skyrim-chs-workspace.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "kind": "bethesda-mod-chs-translation-workspace",
                        "game_id": "fallout4",
                        "game_profile": "fallout4",
                    }
                ),
                encoding="utf-8",
            )
            policy_path = root / "workflow_policy.json"
            policy_path.write_text('{"states":{}}\n', encoding="utf-8")
            readiness_path = root / "translation_readiness.json"
            issue = make_issue_record(
                code="final_review_quality_not_passed",
                mod_name="ExampleMod",
                affected_artifact="qa/ExampleMod.final_review_quality.md",
                severity="error",
                message="Final review has warnings.",
                evidence_paths=["qa/ExampleMod.final_review_quality.md"],
                reported_by=["translation_readiness"],
            )
            readiness_path.write_text(
                json.dumps(
                    {
                        **game_context_metadata(context),
                        "OverallStatus": "blocked",
                        "KnownModOutputs": [
                            {
                                "ModName": "ExampleMod",
                                "Workspace": "work/extracted_mods/ExampleMod",
                                "FinalModDir": "out/ExampleMod/汉化产出/final_mod",
                                "PackagedModPath": "out/ExampleMod/汉化产出/ExampleMod_CHS.zip",
                                "OverallStatus": "blocked_by_qa",
                            }
                        ],
                        "ModInputs": [],
                        "Issues": [issue],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload, _issues = build_state(root, policy_path, readiness_path)

            state = payload["states"][0]
            self.assertIn("final_review_quality_not_passed", state["blocking_checks"])
            self.assertEqual(state["blocking_issues"], compact_issue_refs([issue]))


if __name__ == "__main__":
    unittest.main()

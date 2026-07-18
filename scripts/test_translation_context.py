from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from game_context import load_game_profile  # noqa: E402
from new_final_text_review_packet import ReviewItem, write_packet as write_final_text_packet  # noqa: E402
from new_model_review_packet import collect_rows, write_packet as write_model_review_packet  # noqa: E402
from run_non_gui_qa_gates import translation_context_gate_issues  # noqa: E402
from run_non_gui_translation_workflow import Issue, Step, finish_failed_workflow  # noqa: E402
from update_model_review_contract import build_contract_block, invalidate_stale_verdict  # noqa: E402
from translation_context import (  # noqa: E402
    aggregate_review_rows,
    aggregate_source_rows,
    review_group_sections,
    source_rows_hash,
    validated_translation_context,
    validate_translation_context,
    write_translation_context_packet,
)


class TranslationContextTests(unittest.TestCase):
    def sample_rows(self) -> list[dict[str, object]]:
        return [
            {
                "File": "translated/options.jsonl",
                "Line": 1,
                "Type": "MCM-label",
                "Risk": "candidate",
                "Context": "setting=oralControl",
                "Source": "Yield mouth during oral",
                "Target": "口交时让出口型",
            },
            {
                "File": "translated/options.jsonl",
                "Line": 2,
                "Type": "MCM-label",
                "Risk": "candidate",
                "Context": "setting=oralControlFallback",
                "Source": "Yield mouth during oral",
                "Target": "口交时让出嘴部控制",
            },
        ]

    def test_source_hash_ignores_translation_but_tracks_source_context(self) -> None:
        rows = self.sample_rows()
        original_hash = source_rows_hash(rows)
        translated = [dict(row) for row in rows]
        translated[0]["Target"] = "新的译文"
        changed_context = [dict(row) for row in rows]
        changed_context[0]["Context"] = "setting=different"

        self.assertEqual(source_rows_hash(translated), original_hash)
        self.assertNotEqual(source_rows_hash(changed_context), original_hash)

    def test_candidate_reader_preserves_generic_kind_and_context_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "translated" / "Example.jsonl"
            source.parent.mkdir()
            source.write_text(
                '{"Source":"Open","Target":"打开","Kind":"MCM-label","Context":"setting=door"}\n',
                encoding="utf-8",
            )

            rows = collect_rows(root, [source], include_protected_rows=False)

            self.assertEqual(rows[0]["Type"], "MCM-label")
            self.assertIn("Context=setting=door", rows[0]["Context"])

    def test_duplicate_sources_in_different_contexts_are_not_merged(self) -> None:
        groups = aggregate_source_rows(self.sample_rows())

        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["OccurrenceCount"] == 1 for group in groups))
        self.assertEqual(len({group["GroupId"] for group in groups}), 2)

    def test_review_groups_keep_conflicting_targets_separate_and_flag_semantic_focus(self) -> None:
        rows = self.sample_rows()
        duplicate = dict(rows[1])
        duplicate["Line"] = 3
        groups = aggregate_review_rows([*rows, duplicate])

        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(group["OccurrenceCount"] for group in groups), [1, 2])
        self.assertTrue(all("conflicting-targets" in group["SemanticFocus"] for group in groups))
        self.assertTrue(all("short-ui-label" in group["SemanticFocus"] for group in groups))

    def test_review_group_ids_preserve_case_sensitive_context_identity(self) -> None:
        first = dict(self.sample_rows()[0], Context="$.Label", Target="打开")
        second = dict(first, Line=2, Context="$.label")

        groups = aggregate_review_rows([first, second])

        self.assertEqual(len(groups), 2)
        self.assertEqual(len({group["GroupId"] for group in groups}), 2)

    def test_semantic_focus_summary_aggregates_groups_with_the_same_focus(self) -> None:
        rows = [
            {
                "File": "translated/options.jsonl",
                "Line": index,
                "Type": "MCM-label",
                "Risk": "candidate",
                "Context": f"setting=option{index}",
                "Source": f"Option {index}",
                "Target": f"选项 {index}",
            }
            for index in range(1, 4)
        ]

        groups = aggregate_review_rows(rows)
        semantic = review_group_sections(groups)["semantic_focus_high_risk"]

        self.assertEqual(len(groups), 3)
        self.assertEqual(len(semantic), 1)
        self.assertEqual(len(semantic[0]["MemberGroupIds"]), 3)

    def test_review_sections_cover_conflicts_suspicious_targets_pairs_and_high_risk(self) -> None:
        rows = [
            {
                "File": "translated/options.jsonl",
                "Line": 1,
                "Type": "MCM-label",
                "Risk": "candidate",
                "Context": "setting=door;field=label",
                "Source": "Open door",
                "Target": "开门",
            },
            {
                "File": "translated/options.jsonl",
                "Line": 2,
                "Type": "MCM-help",
                "Risk": "high",
                "Context": "setting=door;field=help",
                "Source": "Open the selected door when this option is enabled.",
                "Target": "启用后打开所选的门。",
            },
            {
                "File": "translated/options.jsonl",
                "Line": 3,
                "Type": "MCM-label",
                "Risk": "candidate",
                "Context": "setting=doorFallback;field=label",
                "Source": "Open door",
                "Target": "开启门",
            },
            {
                "File": "translated/options.jsonl",
                "Line": 4,
                "Type": "MCM-label",
                "Risk": "candidate",
                "Context": "setting=closeDoor;field=label",
                "Source": "Close door",
                "Target": "开门",
            },
        ]

        groups = aggregate_review_rows(rows)
        sections = review_group_sections(groups)

        self.assertEqual(len(sections["source_target_conflicts"]), 1)
        self.assertEqual(len(sections["suspicious_shared_targets"]), 1)
        self.assertEqual(len(sections["label_help_pairs"]), 1)
        self.assertGreaterEqual(len(sections["semantic_focus_high_risk"]), 2)
        covered = {
            group_id
            for section in sections.values()
            for item in section
            for group_id in item["MemberGroupIds"]
        }
        self.assertTrue(covered.issubset({group["GroupId"] for group in groups}))

    def test_context_validation_rejects_stale_or_cross_game_summary(self) -> None:
        rows = self.sample_rows()
        expected_hash = source_rows_hash(rows)
        payload = {
            "schema_version": 1,
            "status": "complete",
            "game_id": "fallout4",
            "game_display_name": "Fallout 4",
            "mod_name": "ExampleMod",
            "source_items_sha256": expected_hash,
            "summary": "该 Mod 管理武器收纳显示及相关配置。",
            "purpose": "控制武器在角色身上的显示方式。",
            "features": ["武器收纳显示", "配置开关"],
            "tone": "简洁、技术性、符合 Fallout 4 设置菜单。",
            "term_preferences": [],
            "ui_label_rules": ["短标签优先表达实际控制对象。"],
            "ambiguous_terms": [],
            "evidence_files": ["translated/options.jsonl"],
            "confidence": "high",
        }

        self.assertEqual(
            validate_translation_context(
                payload,
                expected_game_id="fallout4",
                expected_mod_name="ExampleMod",
                expected_source_hash=expected_hash,
            ),
            [],
        )
        self.assertTrue(
            validate_translation_context(
                payload,
                expected_game_id="skyrim-se",
                expected_mod_name="ExampleMod",
                expected_source_hash=expected_hash,
            )
        )
        self.assertTrue(
            validate_translation_context(
                payload,
                expected_game_id="fallout4",
                expected_mod_name="ExampleMod",
                expected_source_hash="stale-hash",
            )
        )

    def test_context_packet_is_game_specific_and_not_fantasy_biased(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packet = root / "qa" / "ExampleMod.translation_context_packet.md"
            context_path = root / "qa" / "ExampleMod.translation_context.json"

            write_translation_context_packet(
                root,
                "ExampleMod",
                self.sample_rows(),
                load_game_profile("fallout4"),
                packet,
                context_path,
            )

            text = packet.read_text(encoding="utf-8")
            template = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertIn("Fallout 4", text)
            self.assertIn("- Source occurrences: 2", text)
            self.assertIn("- Unique source groups: 2", text)
            self.assertEqual(text.count("source-"), 2)
            self.assertNotIn("Fantasy/game terms", text)
            self.assertEqual(template["status"], "needs_model_analysis")
            self.assertEqual(template["game_id"], "fallout4")
            self.assertEqual(template["source_items_sha256"], source_rows_hash(self.sample_rows()))

    def test_incomplete_template_refreshes_but_completed_summary_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packet = root / "qa" / "Example.translation_context_packet.md"
            context_path = root / "qa" / "Example.translation_context.json"
            context_path.parent.mkdir()
            context_path.write_text(
                '{"schema_version":1,"status":"needs_model_analysis","source_items_sha256":"old"}\n',
                encoding="utf-8",
            )

            write_translation_context_packet(
                root,
                "Example",
                self.sample_rows(),
                load_game_profile("fallout4"),
                packet,
                context_path,
            )
            refreshed = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertEqual(refreshed["source_items_sha256"], source_rows_hash(self.sample_rows()))

            refreshed.update({"status": "complete", "summary": "模型完成的摘要"})
            context_path.write_text(json.dumps(refreshed, ensure_ascii=False), encoding="utf-8")
            before = context_path.read_bytes()
            changed_rows = [dict(row) for row in self.sample_rows()]
            changed_rows[0]["Context"] = "changed"
            write_translation_context_packet(
                root,
                "Example",
                changed_rows,
                load_game_profile("fallout4"),
                packet,
                context_path,
            )
            self.assertEqual(context_path.read_bytes(), before)

    def test_stale_context_summary_is_suppressed_for_review_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qa = root / "qa"
            qa.mkdir()
            mod_name = "Example"
            (qa / f"{mod_name}.translation_context_packet.md").write_text(
                "- Source Items SHA256: current-hash\n",
                encoding="utf-8",
            )
            (qa / f"{mod_name}.translation_context.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "complete",
                        "game_id": "fallout4",
                        "mod_name": mod_name,
                        "source_items_sha256": "stale-hash",
                        "summary": "不应进入校对上下文的旧摘要",
                        "purpose": "旧用途",
                        "features": ["旧功能"],
                        "tone": "旧语气",
                        "term_preferences": [],
                        "ui_label_rules": [],
                        "ambiguous_terms": [],
                        "evidence_files": ["old.jsonl"],
                        "confidence": "high",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload, issues = validated_translation_context(
                root,
                mod_name,
                load_game_profile("fallout4"),
            )

            self.assertEqual(payload["status"], "invalid")
            self.assertEqual(payload["summary"], "")
            self.assertTrue(any("stale" in issue for issue in issues))

    def test_model_review_packet_embeds_completed_context_and_aggregates_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qa = root / "qa"
            qa.mkdir()
            output = qa / "ExampleMod.model_review_packet.md"
            review = qa / "ExampleMod.model_review.md"
            context_path = qa / "ExampleMod.translation_context.json"
            payload = {
                "status": "complete",
                "summary": "该 Mod 管理武器收纳显示及相关配置。",
                "purpose": "控制武器显示。",
                "features": ["武器显示"],
                "tone": "简洁的设置菜单。",
                "term_preferences": [],
                "ui_label_rules": [],
                "ambiguous_terms": [],
                "evidence_files": ["translated/options.jsonl"],
                "confidence": "high",
            }

            write_model_review_packet(
                root,
                "ExampleMod",
                output,
                review,
                self.sample_rows(),
                game_context=load_game_profile("fallout4"),
                context_path=context_path,
                context_payload=payload,
                context_source_hash=source_rows_hash(self.sample_rows()),
            )

            text = output.read_text(encoding="utf-8")
            self.assertIn("Fallout 4", text)
            self.assertIn("该 Mod 管理武器收纳显示及相关配置。", text)
            self.assertIn("conflicting-targets", text)
            self.assertIn("## Source To Multiple Targets", text)
            self.assertIn("## Semantic Focus And High Risk", text)
            self.assertNotIn("Fantasy/game terms", text)
            self.assertEqual(text.count("Yield mouth during oral"), 3)

    def test_final_text_packet_uses_game_context_and_aggregates_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "work" / "Example"
            final_mod = root / "out" / "Example" / "final_mod"
            packet = root / "qa" / "Example.final_text_review_packet.md"
            items_path = root / "qa" / "Example.final_text_review_items.jsonl"
            workspace.mkdir(parents=True)
            final_mod.mkdir(parents=True)
            rows = [
                ReviewItem("MCM/config.json", "MCM-label", "$.label", "Open", "打开"),
                ReviewItem("MCM/config.json", "MCM-label", "$.fallback", "Open", "打开"),
                ReviewItem("MCM/config.json", "MCM-label", "$.legacy", "Open", "开启"),
            ]

            write_final_text_packet(
                root,
                "Example",
                workspace,
                final_mod,
                packet,
                items_path,
                1,
                rows,
                game_context=load_game_profile("fallout4"),
                context_payload={"status": "complete", "summary": "该 Mod 控制武器收纳显示。"},
                context_path=root / "qa" / "Example.translation_context.json",
            )

            text = packet.read_text(encoding="utf-8")
            self.assertEqual(len(items_path.read_text(encoding="utf-8").splitlines()), 3)
            self.assertIn("- Game: Fallout 4 (Experimental)", text)
            self.assertIn("- Aggregated review groups: 3", text)
            self.assertIn("conflicting-targets", text)
            self.assertIn("## Source To Multiple Targets", text)

    def test_translation_context_gate_rejects_incomplete_or_stale_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".skyrim-chs-workspace.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "kind": "bethesda-mod-chs-translation-workspace",
                        "game_id": "skyrim-se",
                        "game_profile": "skyrim-se",
                    }
                ),
                encoding="utf-8",
            )
            qa = root / "qa"
            qa.mkdir()
            (qa / "Example.translation_context_packet.md").write_text(
                "- Source Items SHA256: current-hash\n",
                encoding="utf-8",
            )
            (qa / "Example.translation_context.json").write_text(
                '{"schema_version":1,"status":"needs_model_analysis","game_id":"skyrim-se",'
                '"mod_name":"Example","source_items_sha256":"old-hash"}\n',
                encoding="utf-8",
            )

            issues = translation_context_gate_issues(root, "Example")

            self.assertTrue(any("status must be complete" in issue.Message for issue in issues))
            self.assertTrue(any("stale" in issue.Message for issue in issues))

    def test_mod_context_stage_precedes_final_mod_assembly(self) -> None:
        source = (ROOT / "scripts" / "run_non_gui_translation_workflow.py").read_text(encoding="utf-8")

        self.assertLess(source.rindex('"mod-translation-context"'), source.rindex('"build-final-mod"'))

    def test_failed_workflow_writes_report_then_emits_current_blocked_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = root / "qa" / "workflow.md"
            report.parent.mkdir()
            issues = [Issue("error", "build-final-mod", "build failed", "qa/build.md")]
            steps = [Step("build-final-mod", "failed", "scripts/build_final_mod.py", "qa/build.md", [])]
            calls: list[str] = []

            with patch(
                "run_non_gui_translation_workflow.write_reports",
                side_effect=lambda *args, **kwargs: calls.append("report"),
            ), patch(
                "run_non_gui_translation_workflow.run_python_script",
                side_effect=lambda _root, script, _args: (
                    calls.append(script)
                    or CompletedProcess([script], 0, stdout=f"{script} ok\n", stderr="")
                ),
            ), patch(
                "run_non_gui_translation_workflow.emit_progress_card",
                side_effect=lambda *args, **kwargs: calls.append("card"),
            ) as emit:
                result = finish_failed_workflow(
                    root,
                    report,
                    root / "qa" / "workflow.json",
                    "ExampleMod",
                    "started",
                    root / "work" / "ExampleMod",
                    root / "out" / "ExampleMod" / "final_mod",
                    steps,
                    issues,
                )

            self.assertEqual(result, 1)
            self.assertEqual(
                calls,
                [
                    "report",
                    "audit_translation_readiness.py",
                    "write_workflow_state.py",
                    "write_workflow_tasks.py",
                    "write_codex_handoff.py",
                    "card",
                ],
            )
            self.assertEqual(emit.call_args.kwargs["stage"], "final_mod_built")
            self.assertEqual(emit.call_args.kwargs["status"], "blocked")
            self.assertIn("qa/translation_readiness.json", emit.call_args.kwargs["artifacts"])
            self.assertIn("qa/workflow_state.json", emit.call_args.kwargs["artifacts"])

    def test_model_review_contract_binds_context_source_and_content_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qa = root / "qa"
            qa.mkdir()
            mod_name = "ExampleMod"
            context_packet = qa / f"{mod_name}.translation_context_packet.md"
            context_path = qa / f"{mod_name}.translation_context.json"
            context_packet.write_text("- Source Items SHA256: source-hash\n", encoding="utf-8")
            context_path.write_text('{"status":"complete","summary":"摘要"}\n', encoding="utf-8")
            content_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()

            block = build_contract_block(root, mod_name)

            self.assertIn(f"- Mod context: qa\\{mod_name}.translation_context.json", block)
            self.assertIn("- Mod context Source Items SHA256: source-hash", block)
            self.assertIn(f"- Mod context Content SHA256: {content_hash}", block)

    def test_contract_refresh_invalidates_pass_when_review_evidence_changes(self) -> None:
        old_text = "\n".join(
            [
                "- Reviewed at: 2026-07-16 10:00:00",
                "- Reviewer: Agent model",
                "- Verdict: PASS",
                "- Text Items SHA256: old-text",
                "- Binary Items SHA256: old-binary",
                "- Mod context Source Items SHA256: old-source",
                "- Mod context Content SHA256: old-context",
                "- Final quality RowsChecked: 10",
            ]
        )
        new_block = old_text.replace("old-context", "new-context")

        invalidated = invalidate_stale_verdict(old_text, new_block)

        self.assertIn("- Reviewed at: TODO", invalidated)
        self.assertIn("- Verdict: TODO", invalidated)
        self.assertNotIn("- Verdict: PASS", invalidated)

    def test_contract_refresh_keeps_pass_when_review_evidence_is_unchanged(self) -> None:
        text = "\n".join(
            [
                "- Reviewed at: 2026-07-16 10:00:00",
                "- Reviewer: Agent model",
                "- Verdict: PASS",
                "- Text Items SHA256: text-hash",
                "- Binary Items SHA256: binary-hash",
                "- Mod context Source Items SHA256: source-hash",
                "- Mod context Content SHA256: context-hash",
                "- Final quality RowsChecked: 10",
            ]
        )

        self.assertEqual(invalidate_stale_verdict(text, text), text)

    def test_contract_refresh_invalidates_section_style_pass(self) -> None:
        old_text = "\n".join(
            [
                "- Reviewed at: 2026-07-16 10:00:00",
                "## Verdict",
                "",
                "PASS",
                "- Mod context Content SHA256: old-context",
            ]
        )
        new_block = "- Mod context Content SHA256: new-context"

        invalidated = invalidate_stale_verdict(old_text, new_block)

        self.assertIn("## Verdict\n\nTODO", invalidated)


if __name__ == "__main__":
    unittest.main()

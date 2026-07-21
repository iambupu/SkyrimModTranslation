from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from audit_final_review_quality import audit_row  # noqa: E402
from audit_final_review_quality import should_audit_untranslated_review  # noqa: E402
from game_context import load_game_profile  # noqa: E402
from new_final_text_review_packet import (  # noqa: E402
    ReviewItem,
    collect_ini_file_items,
    collect_json_file_items,
    collect_jsonl_file_items,
    collect_line_items,
    collect_xml_file_items,
    read_text_auto,
    write_packet,
)
from proofread_translation import load_allowed_words, protected_tokens, remove_allowed_ascii_tokens  # noqa: E402
import proofread_translation  # noqa: E402


class FinalReviewQualityTests(unittest.TestCase):
    def test_final_text_review_rejects_unknown_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "broken.txt"
            path.write_bytes(b"\x81")

            with self.assertRaises(UnicodeError):
                read_text_auto(path)

    def test_untranslated_review_blocks_unchanged_english(self) -> None:
        findings = []
        audit_row(
            ROOT,
            ROOT / "qa" / "items.jsonl",
            1,
            {
                "File": "Example.esp",
                "Source": "Clumsy bitch!",
                "Final": "Clumsy bitch!",
                "Risk": "untranslated-review",
                "Context": "record=INFO; subrecord=NAM1",
            },
            findings,
            set(),
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].Severity, "error")
        self.assertEqual(findings[0].Code, "unchanged-english")

    def test_untranslated_review_scope_includes_plugin_file_without_kind(self) -> None:
        self.assertTrue(should_audit_untranslated_review("Example.esp", "", "record=INFO; subrecord=NAM1"))

    def test_untranslated_review_scope_skips_pex_binary_without_plugin_record_context(self) -> None:
        self.assertFalse(should_audit_untranslated_review("scripts/MilkQUEST.pex", "pex-binary", "object=milkquest"))

    def test_untranslated_review_scope_skips_unrelated_text_files(self) -> None:
        self.assertFalse(should_audit_untranslated_review("docs/readme.txt", "", ""))

    def test_protected_path_token_trims_sentence_punctuation(self) -> None:
        tokens = protected_tokens("SKSE/Plugins/StorageUtilData/HentairimExpressions/Config.json。")

        self.assertIn("SKSE/Plugins/StorageUtilData/HentairimExpressions/Config.json", tokens)
        self.assertNotIn("SKSE/Plugins/StorageUtilData/HentairimExpressions/Config.json。", tokens)

    def test_project_allowlist_keeps_framework_names_but_not_real_residual_words(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "mod" / "SKSE" / "Plugins" / "StorageUtilData" / "HentairimExpressions" / "Config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"enableexpressions": true}\n', encoding="utf-8")
            words = load_allowed_words(root)
            remaining = remove_allowed_ascii_tokens(
                "Hentairim enableexpressions SKSE64 SPID SLED master futa classic original",
                words,
            )

            self.assertNotRegex(remaining, r"\bHentairim\b")
            self.assertNotRegex(remaining, r"\benableexpressions\b")
            self.assertNotRegex(remaining, r"\bSKSE64\b")
            self.assertNotRegex(remaining, r"\bSPID\b")
            self.assertNotRegex(remaining, r"\bSLED\b")
            self.assertRegex(remaining, r"\bmaster\b")
            self.assertRegex(remaining, r"\bfuta\b")
            self.assertRegex(remaining, r"\bclassic\b")
            self.assertRegex(remaining, r"\boriginal\b")

    def test_project_allowlist_uses_only_the_current_game_profile_glossary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            glossary = root / "glossary"
            glossary.mkdir()
            (glossary / "skyrim_cn_glossary.md").write_text(
                "| English | Chinese |\n|---|---|\n| SkyrimOnlyTerm | 天际术语 |\n",
                encoding="utf-8",
            )
            (glossary / "fallout4_cn_glossary.md").write_text(
                "| English | Chinese |\n|---|---|\n| FalloutOnlyTerm | 辐射术语 |\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                proofread_translation,
                "current_game_context",
                return_value=load_game_profile("fallout4"),
            ):
                words = load_allowed_words(root)

            self.assertIn("FalloutOnlyTerm", words)
            self.assertNotIn("SkyrimOnlyTerm", words)

    def test_line_items_include_mapping_when_readme_line_count_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "README.md"
            final = root / "README.md.final"
            source.write_text("# Config\n\nOriginal option text\nSecond line\n", encoding="utf-8")
            final.write_text("# Config\n\n中文选项文本\n新增说明\n第二行\n", encoding="utf-8")
            items = []

            collect_line_items(source, final, "README.md", "text-line", items, set())

            contexts = [item.Context for item in items]
            self.assertTrue(any("source_line=" in context and "target_line=" in context for context in contexts))
            self.assertTrue(any("section=Config" in context for context in contexts))
            self.assertTrue(any("line_mapping=source:4 target:5" in context for context in contexts))

    def test_ini_review_includes_changed_comments_without_unchanged_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Config.ini"
            final = root / "Config.final.ini"
            source.write_text("[Main]\n;Enable the feature\nEnabled=1\n", encoding="utf-8")
            final.write_text("[Main]\n;启用此功能\nEnabled=1\n", encoding="utf-8")
            items = []

            collect_ini_file_items(source, final, "Config.ini", items, set())

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].Kind, "ini-comment")
            self.assertEqual(items[0].Source, "Enable the feature")
            self.assertEqual(items[0].Final, "启用此功能")

    def test_final_text_review_rejects_malformed_structured_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            final = root / "final"

            source.write_text('{"label":"Open"}', encoding="utf-8")
            final.write_text('{"label":', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot parse JSON"):
                collect_json_file_items(source, final, "Config.json", [], set())

            source.write_text('{"label":"Open"}\n', encoding="utf-8")
            final.write_text('{broken\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSONL.*line 1"):
                collect_jsonl_file_items(source, final, "Config.jsonl", [], set())

            source.write_text("<root><label>Open</label></root>", encoding="utf-8")
            final.write_text("<root><label></root>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot parse XML"):
                collect_xml_file_items(source, final, "Config.xml", [], set())

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
                ReviewItem("MCM/config.json", "MCM-label", "$.label", "Yield mouth during oral", "口交时让出嘴部控制"),
                ReviewItem("MCM/config.json", "MCM-label", "$.fallback", "Yield mouth during oral", "口交时让出嘴部控制"),
                ReviewItem("MCM/config.json", "MCM-label", "$.legacy", "Yield mouth during oral", "口交时让出口型"),
            ]

            write_packet(
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
            self.assertIn("- Mod summary: 该 Mod 控制武器收纳显示。", text)
            self.assertIn("- Aggregated review groups: 3", text)
            self.assertIn("conflicting-targets", text)
            self.assertNotIn("Fantasy/game terms", text)


if __name__ == "__main__":
    unittest.main()

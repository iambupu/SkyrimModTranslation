from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build_external_glossary_matches import (
    fts_query_for_text,
    parse_markdown_table_dictionary,
    read_jsonl_units,
)


class ExternalGlossaryMatchTests(unittest.TestCase):
    def test_two_letter_term_is_indexable_and_exactly_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            glossary = root / "glossary.md"
            glossary.write_text(
                "| English | 简体中文 |\n| --- | --- |\n| On | 开启 |\n| OK | 确定 |\n",
                encoding="utf-8",
            )

            entries = parse_markdown_table_dictionary(root, glossary)

        self.assertEqual([entry.normalized_source for entry in entries], ["on", "ok"])
        self.assertEqual(fts_query_for_text("on"), '"on"')
        self.assertEqual(fts_query_for_text("ok"), '"ok"')

    def test_stopwords_remain_suppressed_inside_longer_queries(self) -> None:
        self.assertEqual(fts_query_for_text("turn on rifles"), '"rifles" OR "turn"')

    def test_invalid_glossary_encoding_is_not_silently_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            glossary = root / "broken.md"
            glossary.write_bytes(b"\x81")

            with self.assertRaises(UnicodeError):
                parse_markdown_table_dictionary(root, glossary)

    def test_invalid_candidate_jsonl_row_is_not_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidates = root / "broken.jsonl"
            candidates.write_text('{"source":"Broken"\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "line 1"):
                read_jsonl_units(root, candidates)


if __name__ == "__main__":
    unittest.main()

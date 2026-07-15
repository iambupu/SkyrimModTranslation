from __future__ import annotations

import struct
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from build_external_glossary_matches import (
    default_glossary_paths,
    ensure_index,
    glossary_scope,
    read_index_metadata,
)
from game_context import GlossarySource
from glossary_binary_formats import BinaryGlossaryError, decode_eet, decode_sst


def _sst_text(value: str) -> bytes:
    raw = value.encode("utf-16le")
    return struct.pack("<I", len(raw)) + raw


def _sst_record(file_format: bytes, source: str, target: str) -> bytes:
    if file_format == b"SSU5":
        prefix = struct.pack("<I", 0x01001234) + b"FULL" + struct.pack("<HHIB", 0, 0, 7, 1)
    else:
        prefix = (
            struct.pack("<I", 0x01001234)
            + b"WEAPFULL"
            + struct.pack("<HHIH", 0, 0, 7, 1)
        )
    return prefix + _sst_text(source) + _sst_text(target)


def _eet_field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _eet_record(source: str, target: str) -> bytes:
    fields = ("GMST", "00123456", "sExample", "DATA", source, target)
    return b"".join(_eet_field(value) for value in fields)


class BinaryGlossaryFormatTests(unittest.TestCase):
    def test_profile_scopes_rag_sources_to_one_game(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = [
                Path("glossary/mod_terms.md"),
                Path("glossary/fallout4_cn_glossary.md"),
                Path("glossary/eet/fallout4"),
            ]
            for path in selected:
                target = root / path
                if path.suffix:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("fixture", encoding="utf-8")
                else:
                    target.mkdir(parents=True, exist_ok=True)
            skyrim_path = root / "glossary" / "sst" / "skyrim"
            skyrim_path.mkdir(parents=True)
            (skyrim_path / "wrong-game.sst").write_bytes(b"SSU8")
            context = SimpleNamespace(
                game_id="fallout4",
                glossary_sources=(
                    GlossarySource(selected[1], "markdown", frozenset({"rag"}), True),
                    GlossarySource(selected[2], "eet", frozenset({"rag"}), True),
                    GlossarySource(Path("glossary/sst/fallout4"), "sst", frozenset({"xtranslator"}), False),
                ),
            )
            with mock.patch("build_external_glossary_matches.current_game_context", return_value=context):
                paths = default_glossary_paths(root)
                scope = glossary_scope(root, paths)
            self.assertEqual(paths, [path.as_posix() for path in selected])
            self.assertIn('"game_id": "fallout4"', scope)
            self.assertNotIn("skyrim", scope)

    def test_index_is_rebuilt_when_game_identity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            glossary = root / "glossary" / "terms.md"
            glossary.parent.mkdir(parents=True)
            glossary.write_text(
                "| English | 简体中文 |\n|---|---|\n| Workshop | 工房 |\n",
                encoding="utf-8",
            )
            index = root / "work" / "glossary_rag" / "index.sqlite"
            paths = ["glossary/terms.md"]
            with mock.patch(
                "build_external_glossary_matches.current_game_context",
                return_value=SimpleNamespace(game_id="fallout4"),
            ):
                ensure_index(root, index, paths)
            self.assertEqual(read_index_metadata(index)["game_id"], "fallout4")

            with mock.patch(
                "build_external_glossary_matches.current_game_context",
                return_value=SimpleNamespace(game_id="skyrim-se"),
            ):
                ensure_index(root, index, paths)
            self.assertEqual(read_index_metadata(index)["game_id"], "skyrim-se")

    def test_missing_required_profile_source_is_not_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = SimpleNamespace(
                game_id="future-game",
                glossary_sources=(
                    GlossarySource(
                        Path("glossary/eet/future-game"),
                        "eet",
                        frozenset({"rag"}),
                        True,
                    ),
                ),
            )
            with (
                mock.patch("build_external_glossary_matches.current_game_context", return_value=context),
                self.assertRaisesRegex(FileNotFoundError, "future-game"),
            ):
                default_glossary_paths(root)

    def test_decodes_ssu5_and_ssu8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for file_format, header_size in ((b"SSU5", 10), (b"SSU8", 14)):
                path = root / f"{file_format.decode().lower()}.sst"
                header = file_format + bytes(header_size - len(file_format))
                path.write_bytes(header + _sst_record(file_format, "Laser Rifle", "激光步枪"))
                self.assertEqual(decode_sst(path)[0].target, "激光步枪")

    def test_decodes_ssu9_plugin_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.sst"
            plugin = "Example.esp".encode("utf-16le")
            header = b"SSU9\x00" + struct.pack("<II", 1, len(plugin)) + plugin
            path.write_bytes(header + _sst_record(b"SSU9", "Workshop", "工房"))
            self.assertEqual(decode_sst(path)[0].source, "Workshop")

    def test_rejects_truncated_sst(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.sst"
            path.write_bytes(b"SSU8" + bytes(10) + _sst_record(b"SSU8", "Source", "目标")[:-1])
            with self.assertRaises(BinaryGlossaryError):
                decode_sst(path)

    def test_decodes_eet_v2_and_validates_declared_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.eet"
            header = (
                b"EET_"
                + struct.pack("<II", 2, 3)
                + b"GAME"
                + struct.pack("<H", 0)
                + b"LINE"
                + struct.pack("<II", 2, 247)
            )
            path.write_bytes(
                header
                + _eet_record("Laser Rifle", "激光步枪")
                + b"opaque metadata"
                + _eet_record("Workshop", "工房")
                + b"tail"
            )
            entries = decode_eet(path)
            self.assertEqual([(entry.source, entry.target) for entry in entries], [
                ("Laser Rifle", "激光步枪"),
                ("Workshop", "工房"),
            ])

            data = bytearray(path.read_bytes())
            struct.pack_into("<I", data, 22, 3)
            path.write_bytes(data)
            with self.assertRaisesRegex(BinaryGlossaryError, "record count mismatch"):
                decode_eet(path)

    def test_rejects_unknown_eet_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unknown.eet"
            path.write_bytes(b"EET_" + struct.pack("<I", 3) + bytes(22))
            with self.assertRaisesRegex(BinaryGlossaryError, "unsupported EET version"):
                decode_eet(path)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from audit_mod_scale import assess_source, classify_scale, load_scale_config  # noqa: E402
from game_context import load_game_profile  # noqa: E402


CONFIG_PATH = ROOT / "config" / "mod_scale_profiles.json"


def plugin_header(flags: int = 0) -> bytes:
    header = bytearray(24)
    header[:4] = b"TES4"
    header[8:12] = flags.to_bytes(4, byteorder="little", signed=False)
    return bytes(header)


def write_dx10_ba2(path: Path, member_path: str) -> None:
    encoded_name = member_path.encode("utf-8")
    names = struct.pack("<H", len(encoded_name)) + encoded_name
    names_offset = 24 + 24 + 24
    payload_offset = names_offset + len(names)
    file_header = bytearray(24)
    file_header[13] = 1
    chunk = struct.pack("<QIIHHI", payload_offset, 4, 4, 0, 0, 0)
    path.write_bytes(
        struct.pack("<4sI4sIQ", b"BTDX", 1, b"DX10", 1, names_offset)
        + bytes(file_header)
        + chunk
        + names
        + b"DDS!"
    )


def write_gnrl_ba2(path: Path, member_path: str, data: bytes) -> None:
    encoded_name = member_path.encode("utf-8")
    names = struct.pack("<H", len(encoded_name)) + encoded_name
    names_offset = 24 + 36
    payload_offset = names_offset + len(names)
    extension = Path(member_path).suffix.lstrip(".").encode("ascii")[:4].ljust(4, b"\0")
    record = struct.pack(
        "<I4sIIQIII",
        0,
        extension,
        0,
        0,
        payload_offset,
        0,
        len(data),
        0,
    )
    path.write_bytes(
        struct.pack("<4sI4sIQ", b"BTDX", 1, b"GNRL", 1, names_offset)
        + record
        + names
        + data
    )


class ModScaleAssessmentTests(unittest.TestCase):
    def test_highest_metric_level_wins(self) -> None:
        config = load_scale_config(CONFIG_PATH)
        level, metric_levels = classify_scale(
            {
                "max_unpacked_bytes": 1,
                "max_file_count": 1,
                "max_candidate_rows": 280_000,
                "max_archive_count": 1,
            },
            config,
        )

        self.assertEqual(level, "L3")
        self.assertEqual(metric_levels["max_unpacked_bytes"], "L0")
        self.assertEqual(metric_levels["max_candidate_rows"], "L3")

    def test_protected_resource_mod_stays_low_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "ResourceOnly"
            texture = source / "Textures" / "fixture.dds"
            sound = source / "Sound" / "fixture.xwm"
            texture.parent.mkdir(parents=True)
            sound.parent.mkdir(parents=True)
            texture.write_bytes(b"t" * 10)
            sound.write_bytes(b"s" * 20)

            report = assess_source(
                root,
                source,
                "ResourceOnly",
                load_game_profile("skyrim-se"),
                CONFIG_PATH,
            )

        self.assertEqual(report["file_count"], 2)
        self.assertEqual(report["candidate_file_count"], 0)
        self.assertEqual(report["protected_bytes"], 30)
        self.assertEqual(report["unknown_format_count"], 0)
        self.assertEqual(report["risk_level"], "R0")
        self.assertFalse(report["execution_behavior_changed"])

    def test_protected_only_ba2_is_inventoried_without_raising_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "TextureArchive"
            source.mkdir(parents=True)
            write_dx10_ba2(source / "TextureArchive - Textures.ba2", "Textures/fixture.dds")

            report = assess_source(
                root,
                source,
                "TextureArchive",
                load_game_profile("fallout4"),
                CONFIG_PATH,
            )

        self.assertTrue(report["inventory_complete"])
        self.assertEqual(report["archive_count"], 1)
        self.assertEqual(report["opaque_archive_count"], 0)
        self.assertEqual(report["controlled_archive_count"], 0)
        self.assertEqual(report["file_count"], 1)
        self.assertEqual(report["protected_bytes"], 4)
        self.assertEqual(report["risk_level"], "R0")

    def test_ba2_with_translation_candidate_has_controlled_archive_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "TextArchive"
            source.mkdir(parents=True)
            write_gnrl_ba2(
                source / "TextArchive - Main.ba2",
                "Interface/translations/fixture_en.txt",
                b"$HELLO\tHello\n",
            )

            report = assess_source(
                root,
                source,
                "TextArchive",
                load_game_profile("fallout4"),
                CONFIG_PATH,
            )

        self.assertTrue(report["inventory_complete"])
        self.assertEqual(report["controlled_archive_count"], 1)
        self.assertEqual(report["candidate_file_count"], 1)
        self.assertEqual(report["risk_level"], "R1")

    def test_localized_plugin_is_high_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "Localized"
            source.mkdir(parents=True)
            (source / "Localized.esp").write_bytes(plugin_header(0x00000080))

            report = assess_source(
                root,
                source,
                "Localized",
                load_game_profile("skyrim-se"),
                CONFIG_PATH,
            )

        self.assertEqual(report["localized_plugin_count"], 1)
        self.assertEqual(report["risk_level"], "R3")

    def test_fallout4_experimental_plugin_write_is_high_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "FalloutPlugin"
            source.mkdir(parents=True)
            (source / "FalloutPlugin.esp").write_bytes(plugin_header())

            report = assess_source(
                root,
                source,
                "FalloutPlugin",
                load_game_profile("fallout4"),
                CONFIG_PATH,
            )

        self.assertEqual(report["experimental_write_resource_count"], 1)
        self.assertEqual(report["risk_level"], "R3")

    def test_zip_inventory_uses_central_directory_without_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "Packaged.zip"
            source.parent.mkdir(parents=True)
            with zipfile.ZipFile(source, mode="w") as archive:
                archive.writestr("Interface/translations/fixture_english.txt", "$A\tHello\n")
                archive.writestr("Scripts/fixture.pex", b"pex fixture")
                archive.writestr("Textures/fixture.dds", b"texture fixture")

            report = assess_source(
                root,
                source,
                "Packaged",
                load_game_profile("skyrim-se"),
                CONFIG_PATH,
            )

            self.assertFalse((root / "work").exists())

        self.assertEqual(report["source_type"], "zip")
        self.assertEqual(report["estimation_basis"], "zip-central-directory")
        self.assertEqual(report["file_count"], 3)
        self.assertEqual(report["archive_count"], 1)
        self.assertEqual(report["opaque_archive_count"], 0)
        self.assertEqual(report["pex_count"], 1)
        self.assertEqual(report["risk_level"], "R2")

    def test_unsafe_zip_member_is_manual_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "Unsafe.zip"
            source.parent.mkdir(parents=True)
            with zipfile.ZipFile(source, mode="w") as archive:
                archive.writestr("../outside.txt", "unsafe")

            report = assess_source(
                root,
                source,
                "Unsafe",
                load_game_profile("skyrim-se"),
                CONFIG_PATH,
            )

        self.assertFalse(report["inventory_complete"])
        self.assertEqual(report["unsafe_path_count"], 1)
        self.assertEqual(report["risk_level"], "R4")

    def test_zip_link_entry_is_manual_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "Link.zip"
            source.parent.mkdir(parents=True)
            link_info = zipfile.ZipInfo("Interface/link.txt")
            link_info.create_system = 3
            link_info.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(source, mode="w") as archive:
                archive.writestr(link_info, "../../outside.txt")

            report = assess_source(
                root,
                source,
                "Link",
                load_game_profile("skyrim-se"),
                CONFIG_PATH,
            )

        self.assertFalse(report["inventory_complete"])
        self.assertEqual(report["file_count"], 0)
        self.assertEqual(report["unsafe_path_count"], 1)
        self.assertEqual(report["risk_level"], "R4")

    def test_7z_inventory_marks_unread_plugin_traits_incomplete(self) -> None:
        import py7zr

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staged_plugin = root / "fixture.esp"
            staged_plugin.write_bytes(plugin_header())
            source = root / "mod" / "Packaged.7z"
            source.parent.mkdir(parents=True)
            with py7zr.SevenZipFile(source, mode="w") as archive:
                archive.write(staged_plugin, arcname="Packaged.esp")

            report = assess_source(
                root,
                source,
                "Packaged",
                load_game_profile("skyrim-se"),
                CONFIG_PATH,
            )

            self.assertFalse((root / "work").exists())

        self.assertEqual(report["source_type"], "7z")
        self.assertEqual(report["estimation_basis"], "7z-central-directory")
        self.assertEqual(report["plugin_count"], 1)
        self.assertFalse(report["inventory_complete"])
        self.assertTrue(
            any("cannot inspect plugin header traits" in item for item in report["warnings"])
        )

    def test_nested_archive_marks_inventory_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "ArchiveMod.zip"
            source.parent.mkdir(parents=True)
            with zipfile.ZipFile(source, mode="w") as archive:
                archive.writestr("ArchiveMod - Main.ba2", b"BTDX fixture")

            report = assess_source(
                root,
                source,
                "ArchiveMod",
                load_game_profile("fallout4"),
                CONFIG_PATH,
            )

        self.assertFalse(report["inventory_complete"])
        self.assertEqual(report["archive_count"], 2)
        self.assertEqual(report["opaque_archive_count"], 1)
        self.assertEqual(report["risk_level"], "R1")

    def test_rar_source_is_manual_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "Manual.rar"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"RAR fixture")

            report = assess_source(
                root,
                source,
                "Manual",
                load_game_profile("skyrim-se"),
                CONFIG_PATH,
            )

        self.assertFalse(report["inventory_complete"])
        self.assertEqual(report["manual_archive_count"], 1)
        self.assertEqual(report["risk_level"], "R4")

    def test_workspace_config_path_is_recorded_without_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "mod" / "Fixture"
            source.mkdir(parents=True)
            (source / "readme.txt").write_text("Hello", encoding="utf-8")
            custom_config = root / "config" / "custom-scale.json"
            custom_config.parent.mkdir(parents=True)
            custom_config.write_bytes(CONFIG_PATH.read_bytes())

            report = assess_source(
                root,
                source,
                "Fixture",
                load_game_profile("skyrim-se"),
                custom_config,
            )

        self.assertEqual(report["config_path"], "workspace:config/custom-scale.json")


if __name__ == "__main__":
    unittest.main()

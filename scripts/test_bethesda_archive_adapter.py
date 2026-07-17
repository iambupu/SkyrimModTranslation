from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import textwrap
import zlib
from pathlib import Path

import pytest

from archive_execution_policy import validate_materialized_inventory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def write_gnrl_ba2(path: Path, entries: list[tuple[str, bytes, bool]]) -> None:
    names = b"".join(struct.pack("<H", len(name.encode("utf-8"))) + name.encode("utf-8") for name, _data, _packed in entries)
    names_offset = 24 + 36 * len(entries)
    data_offset = names_offset + len(names)
    records: list[bytes] = []
    payloads: list[bytes] = []
    cursor = data_offset
    for name, data, packed in entries:
        stored = zlib.compress(data) if packed else data
        packed_size = len(stored) if packed else 0
        extension = Path(name).suffix.lstrip(".").encode("ascii")[:4].ljust(4, b"\0")
        records.append(
            struct.pack("<I4sIIQIII", 0, extension, 0, 0, cursor, packed_size, len(data), 0)
        )
        payloads.append(stored)
        cursor += len(stored)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack("<4sI4sIQ", b"BTDX", 1, b"GNRL", len(entries), names_offset)
        + b"".join(records)
        + names
        + b"".join(payloads)
    )


def write_dx10_ba2(path: Path, name: str = "Textures/fixture.dds") -> None:
    encoded_name = name.encode("utf-8")
    names = struct.pack("<H", len(encoded_name)) + encoded_name
    names_offset = 24 + 24 + 24
    payload_offset = names_offset + len(names)
    file_header = bytearray(24)
    file_header[13] = 1
    chunk = struct.pack("<QIIHHI", payload_offset, 4, 4, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack("<4sI4sIQ", b"BTDX", 1, b"DX10", 1, names_offset)
        + bytes(file_header)
        + chunk
        + names
        + b"DDS!"
    )


def write_bsa(path: Path, entries: list[tuple[str, str, bytes]]) -> None:
    directories: dict[str, list[tuple[str, bytes]]] = {}
    for directory, name, data in entries:
        directories.setdefault(directory, []).append((name, data))

    directory_names_length = sum(len(directory.encode("utf-8")) + 1 for directory in directories)
    file_names = b"".join(name.encode("utf-8") + b"\0" for files in directories.values() for name, _ in files)
    header_size = 36
    directory_records_size = 24 * len(directories)
    directory_blocks_size = sum(
        1 + len(directory.encode("utf-8")) + 1 + 16 * len(files)
        for directory, files in directories.items()
    )
    payload_cursor = header_size + directory_records_size + directory_blocks_size + len(file_names)

    directory_records: list[bytes] = []
    directory_blocks: list[bytes] = []
    payloads: list[bytes] = []
    for directory, files in directories.items():
        directory_records.append(struct.pack("<QIIQ", 0, len(files), 0, 0))
        encoded_directory = directory.encode("utf-8") + b"\0"
        file_records: list[bytes] = []
        for _name, data in files:
            file_records.append(struct.pack("<QII", 0, len(data), payload_cursor))
            payloads.append(data)
            payload_cursor += len(data)
        directory_blocks.append(
            bytes([len(encoded_directory)]) + encoded_directory + b"".join(file_records)
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack(
            "<4s8I",
            b"BSA\0",
            105,
            header_size,
            0x3,
            len(directories),
            len(entries),
            directory_names_length,
            len(file_names),
            0x20,
        )
        + b"".join(directory_records)
        + b"".join(directory_blocks)
        + file_names
        + b"".join(payloads)
    )


def run_script(root: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "SKYRIM_CHS_WORKSPACE_ROOT": str(root),
        "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


def prepare_workspace(root: Path) -> Path:
    (root / ".skyrim-chs-workspace.json").write_text(json.dumps({"game_id": "fallout4"}), encoding="utf-8")
    for relative in ("mod", "work/archive_extracts", "qa", "out", "config"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    archive = root / "mod" / "Fixture - Main.ba2"
    write_gnrl_ba2(
        archive,
        [
            ("Interface/translations/fixture_en.txt", b"$HELLO\tHello\n", True),
            ("Textures/fixture.dds", b"protected-texture", False),
        ],
    )
    return archive


def test_builtin_ba2_adapter_lists_and_selectively_extracts_gnrl(tmp_path: Path) -> None:
    archive = prepare_workspace(tmp_path)
    list_path = tmp_path / "work" / "entries.jsonl"
    output = tmp_path / "work" / "direct"
    output.mkdir()
    include = tmp_path / "work" / "include.txt"
    include.write_text("Interface/translations/fixture_en.txt\n", encoding="utf-8")
    result = run_script(
        tmp_path,
        "bethesda_archive_adapter.py",
        "--archive-path",
        str(archive),
        "--list-output",
        str(list_path),
        "--output-dir",
        str(output),
        "--include-list",
        str(include),
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in list_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert (output / "Interface" / "translations" / "fixture_en.txt").read_bytes() == b"$HELLO\tHello\n"
    assert not (output / "Textures" / "fixture.dds").exists()


def test_ba2_wrapper_selective_mode_excludes_protected_entries(tmp_path: Path) -> None:
    prepare_workspace(tmp_path)
    (tmp_path / "config" / "tools.local.json").write_text(
        json.dumps({"DecoderTools": {}}),
        encoding="utf-8",
    )
    (tmp_path / "qa" / "Fixture.scale_execution.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "mod-scale-execution",
                "mod_name": "Fixture",
                "game_id": "fallout4",
                "status": "ready",
                "effective": {
                    "max_files": 100,
                    "max_file_bytes": 1024 * 1024,
                    "max_total_bytes": 8 * 1024 * 1024,
                    "timeout_seconds": 30,
                    "extract_mode": "selective",
                },
            }
        ),
        encoding="utf-8",
    )
    result = run_script(
        tmp_path,
        "invoke_ba2_extractor_safe.py",
        "--mod-name",
        "Fixture",
        "--archive-path",
        "mod/Fixture - Main.ba2",
        "--output-dir",
        "work/archive_extracts/Fixture/Fixture - Main",
    )
    assert result.returncode == 0, result.stderr
    extracted = tmp_path / "work" / "archive_extracts" / "Fixture" / "Fixture - Main"
    assert (extracted / "Interface" / "translations" / "fixture_en.txt").is_file()
    assert not (extracted / "Textures" / "fixture.dds").exists()
    execution = json.loads((tmp_path / "qa" / "Fixture.Fixture - Main.archive_execution.json").read_text(encoding="utf-8"))
    assert execution["effective"]["extract_mode"] == "selective"
    assert execution["selected_files"] == 1


def test_builtin_ba2_adapter_accepts_empty_selection_for_dx10_inventory(tmp_path: Path) -> None:
    prepare_workspace(tmp_path)
    archive = tmp_path / "mod" / "Fixture - Textures.ba2"
    write_dx10_ba2(archive)
    output = tmp_path / "work" / "direct-dx10"
    output.mkdir()
    include = tmp_path / "work" / "empty-include.txt"
    include.write_text("", encoding="utf-8")
    result = run_script(
        tmp_path,
        "bethesda_archive_adapter.py",
        "--archive-path",
        str(archive),
        "--output-dir",
        str(output),
        "--include-list",
        str(include),
    )
    assert result.returncode == 0, result.stderr
    assert list(output.rglob("*")) == []


def test_builtin_adapter_rejects_duplicate_windows_paths(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.ba2"
    write_gnrl_ba2(
        archive,
        [
            ("Interface/Test.txt", b"one", False),
            ("interface/test.TXT", b"two", False),
        ],
    )

    result = run_script(
        tmp_path,
        "bethesda_archive_adapter.py",
        "--archive-path",
        str(archive),
        "--list-output",
        str(tmp_path / "entries.jsonl"),
    )

    assert result.returncode != 0
    assert "duplicate Windows path" in result.stderr


def test_builtin_bsa_adapter_lists_and_selectively_extracts(tmp_path: Path) -> None:
    archive = tmp_path / "fixture.bsa"
    write_bsa(
        archive,
        [
            ("Interface\\translations", "fixture_en.txt", b"$HELLO\tHello\n"),
            ("Textures", "fixture.dds", b"protected-texture"),
        ],
    )
    list_path = tmp_path / "entries.jsonl"
    include = tmp_path / "include.txt"
    include.write_text("Interface/translations/fixture_en.txt\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()

    result = run_script(
        tmp_path,
        "bethesda_archive_adapter.py",
        "--archive-path",
        str(archive),
        "--list-output",
        str(list_path),
        "--output-dir",
        str(output),
        "--include-list",
        str(include),
    )

    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in list_path.read_text(encoding="utf-8").splitlines()]
    assert [row["path"] for row in rows] == [
        "Interface/translations/fixture_en.txt",
        "Textures/fixture.dds",
    ]
    assert (output / "Interface" / "translations" / "fixture_en.txt").read_bytes() == b"$HELLO\tHello\n"
    assert not (output / "Textures" / "fixture.dds").exists()


def test_ba2_wrapper_enforces_subprocess_timeout(tmp_path: Path) -> None:
    prepare_workspace(tmp_path)
    adapter = tmp_path / "tools" / "slow_adapter.py"
    adapter.parent.mkdir()
    adapter.write_text(
        textwrap.dedent(
            """
            import argparse
            import time
            parser = argparse.ArgumentParser()
            parser.add_argument('--archive-path')
            parser.add_argument('--output-dir')
            parser.parse_args()
            time.sleep(5)
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "config" / "tools.local.json").write_text(
        json.dumps(
            {
                "DecoderTools": {
                    "Ba2ExtractorPath": "tools/slow_adapter.py",
                    "Ba2ExtractorProtocol": "skyrim-mod-chs.ba2-extractor.v1",
                }
            }
        ),
        encoding="utf-8",
    )
    result = run_script(
        tmp_path,
        "invoke_ba2_extractor_safe.py",
        "--mod-name",
        "Fixture",
        "--archive-path",
        "mod/Fixture - Main.ba2",
        "--output-dir",
        "work/archive_extracts/Fixture/Fixture - Main",
        "--timeout-seconds",
        "1",
    )
    assert result.returncode != 0
    assert "timed out" in (result.stdout + result.stderr).casefold()
    assert not (tmp_path / "work" / "archive_extracts" / "Fixture" / "Fixture - Main").exists()


@pytest.mark.parametrize(
    ("actual", "message"),
    (
        ([], "missing entries"),
        ([{"path": "Interface/value.txt", "size": 5}, {"path": "extra.txt", "size": 1}], "unexpected entries"),
        ([{"path": "Interface/value.txt", "size": 4}], "size mismatches"),
    ),
)
def test_materialized_archive_output_must_match_inventory(
    actual: list[dict[str, object]],
    message: str,
) -> None:
    expected = [{"path": "Interface/value.txt", "size": 5}]
    with pytest.raises(ValueError, match=message):
        validate_materialized_inventory(expected, actual)

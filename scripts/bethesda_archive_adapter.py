"""Project-controlled selective materializer for BSA and Fallout 4 BA2 archives."""

from __future__ import annotations

import argparse
import json
import os
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath
from typing import BinaryIO, Iterable

import lz4.frame
from bethesda_structs.archive.bsa import BSAArchive

from new_ba2_archive_manifest import validate_archive_relative_path


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    size: int
    offset: int = 0
    packed_size: int = 0
    archive_type: str = ""
    compressed: bool = False
    version: int = 0
    embedded_name: bool = False


def _canonical(value: object) -> str:
    text = str(value).replace("\\", "/").lstrip("/")
    return validate_archive_relative_path(text)


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("Archive ended before the declared structure was complete")
    return data


def _read_ba2_names(handle: BinaryIO, offset: int, count: int) -> list[str]:
    handle.seek(offset)
    names: list[str] = []
    for _ in range(count):
        length = struct.unpack("<H", _read_exact(handle, 2))[0]
        raw = _read_exact(handle, length)
        names.append(_canonical(raw.decode("utf-8")))
    return names


def _require_unique_paths(entries: list[ArchiveEntry]) -> list[ArchiveEntry]:
    seen: set[str] = set()
    for entry in entries:
        key = entry.path.replace("\\", "/").casefold()
        if key in seen:
            raise ValueError(f"Archive contains a duplicate Windows path: {entry.path}")
        seen.add(key)
    return entries


def read_ba2_entries(path: Path) -> tuple[str, list[ArchiveEntry]]:
    archive_size = path.stat().st_size
    with path.open("rb") as handle:
        magic, version, archive_type_raw, count, names_offset = struct.unpack(
            "<4sI4sIQ", _read_exact(handle, 24)
        )
        if magic != b"BTDX":
            raise ValueError("BA2 archive does not start with BTDX")
        archive_type = archive_type_raw.decode("ascii", errors="strict")
        if version <= 0 or count > 1_000_000:
            raise ValueError("BA2 header version or file count is invalid")
        records: list[tuple[int, int, int]] = []
        if archive_type == "GNRL":
            for _ in range(count):
                record = struct.unpack("<I4sIIQIII", _read_exact(handle, 36))
                offset, packed_size, unpacked_size = (
                    int(record[4]),
                    int(record[5]),
                    int(record[6]),
                )
                stored_size = packed_size or unpacked_size
                if offset > archive_size or stored_size > archive_size - offset:
                    raise ValueError("BA2 record payload exceeds archive bounds")
                records.append((offset, packed_size, unpacked_size))
        elif archive_type == "DX10":
            for _ in range(count):
                header = _read_exact(handle, 24)
                chunks_count = header[13]
                total_size = 0
                for _chunk in range(chunks_count):
                    chunk = struct.unpack("<QIIHHI", _read_exact(handle, 24))
                    offset = int(chunk[0])
                    stored_size = int(chunk[1]) or int(chunk[2])
                    if offset > archive_size or stored_size > archive_size - offset:
                        raise ValueError("BA2 texture chunk exceeds archive bounds")
                    total_size += int(chunk[2])
                records.append((0, 0, total_size))
        else:
            raise ValueError(f"Unsupported BA2 archive type: {archive_type}")
        names = _read_ba2_names(handle, names_offset, count)
    entries = [
        ArchiveEntry(
            path=name,
            size=unpacked_size,
            offset=offset,
            packed_size=packed_size,
            archive_type=archive_type,
        )
        for name, (offset, packed_size, unpacked_size) in zip(names, records, strict=True)
    ]
    return archive_type, _require_unique_paths(entries)


def read_bsa_entries(path: Path) -> list[ArchiveEntry]:
    archive_size = path.stat().st_size
    entries: list[ArchiveEntry] = []
    with path.open("rb") as handle:
        container = BSAArchive.archive_struct.parse_stream(handle)
        header = container.header
        if header.magic != b"BSA\x00" or header.version not in (103, 104, 105):
            raise ValueError("Unsupported BSA header or version")
        if not header.archive_flags.directories_named or not header.archive_flags.files_named:
            raise ValueError("BSA inventory requires named directories and files")
        file_names = list(container.file_names or [])
        file_index = 0
        for directory_block in container.directory_blocks:
            directory = PureWindowsPath(str(directory_block.name).rstrip("\x00"))
            for record in directory_block.file_records:
                if file_index >= len(file_names):
                    raise ValueError("BSA file-name table ended before file records")
                packed_size = int(record.size) & BSAArchive.SIZE_MASK
                compressed = bool(header.archive_flags.files_compressed) != bool(
                    int(record.size) & BSAArchive.COMPRESSED_MASK
                )
                payload_offset = int(record.offset)
                if payload_offset > archive_size or packed_size > archive_size - payload_offset:
                    raise ValueError("BSA record payload exceeds archive bounds")
                payload_size = packed_size
                if header.archive_flags.files_prefixed:
                    handle.seek(payload_offset)
                    prefix_length = _read_exact(handle, 1)[0]
                    prefix_size = 1 + prefix_length
                    if prefix_size > payload_size:
                        raise ValueError("BSA embedded file-name prefix exceeds record size")
                    payload_offset += prefix_size
                    payload_size -= prefix_size
                if compressed:
                    if payload_size < 4:
                        raise ValueError("Compressed BSA record is missing its original size")
                    handle.seek(payload_offset)
                    unpacked_size = struct.unpack("<I", _read_exact(handle, 4))[0]
                else:
                    unpacked_size = payload_size
                relative = directory / str(file_names[file_index])
                entries.append(
                    ArchiveEntry(
                        path=_canonical(relative),
                        size=unpacked_size,
                        offset=int(record.offset),
                        packed_size=packed_size,
                        archive_type="BSA",
                        compressed=compressed,
                        version=int(header.version),
                        embedded_name=bool(header.archive_flags.files_prefixed),
                    )
                )
                file_index += 1
        if file_index != len(file_names):
            raise ValueError("BSA file-name and file-record counts do not match")
    return _require_unique_paths(entries)


def read_include_list(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    values: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            values.add(_canonical(line).casefold())
    return values


def select_entries(entries: Iterable[ArchiveEntry], includes: set[str] | None) -> list[ArchiveEntry]:
    return [entry for entry in entries if includes is None or entry.path.casefold() in includes]


def validate_limits(entries: list[ArchiveEntry], *, max_files: int, max_file_bytes: int, max_total_bytes: int) -> None:
    if min(max_files, max_file_bytes, max_total_bytes) <= 0:
        raise ValueError("Archive materialization limits must be positive")
    if len(entries) > max_files:
        raise ValueError(f"selected archive file count exceeds limit: {max_files}")
    largest = max((entry.size for entry in entries), default=0)
    if largest > max_file_bytes:
        raise ValueError(f"selected archive file exceeds byte limit: {max_file_bytes}")
    total = sum(entry.size for entry in entries)
    if total > max_total_bytes:
        raise ValueError(f"selected archive bytes exceed limit: {max_total_bytes}")


def _safe_destination(output_dir: Path, relative: str) -> Path:
    destination = (output_dir / Path(*relative.split("/"))).resolve(strict=False)
    try:
        destination.relative_to(output_dir.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"Archive destination escapes output root: {relative}") from exc
    return destination


def _atomic_write(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp") as handle:
        temporary = Path(handle.name)
        handle.write(data)
    os.replace(temporary, destination)


def extract_bsa(path: Path, output_dir: Path, selected: list[ArchiveEntry]) -> None:
    with path.open("rb") as handle:
        for entry in selected:
            handle.seek(entry.offset)
            payload = _read_exact(handle, entry.packed_size)
            if entry.embedded_name:
                if not payload:
                    raise ValueError(f"BSA embedded file-name prefix is missing: {entry.path}")
                prefix_size = 1 + payload[0]
                if prefix_size > len(payload):
                    raise ValueError(f"BSA embedded file-name prefix is invalid: {entry.path}")
                payload = payload[prefix_size:]
            if entry.compressed:
                if len(payload) < 4:
                    raise ValueError(f"Compressed BSA entry is truncated: {entry.path}")
                expected_size = struct.unpack("<I", payload[:4])[0]
                compressed = payload[4:]
                payload = lz4.frame.decompress(compressed) if entry.version >= 105 else zlib.decompress(compressed)
                if expected_size != entry.size:
                    raise ValueError(f"BSA entry size changed between inventory and extraction: {entry.path}")
            if len(payload) != entry.size:
                raise ValueError(f"BSA entry size mismatch: {entry.path}")
            _atomic_write(_safe_destination(output_dir, entry.path), payload)


def extract_ba2(path: Path, output_dir: Path, selected: list[ArchiveEntry], archive_type: str) -> None:
    if not selected:
        return
    if archive_type != "GNRL":
        raise ValueError("Built-in BA2 materialization only extracts GNRL archives; DX10 texture archives are inventory-only")
    with path.open("rb") as handle:
        for entry in selected:
            handle.seek(entry.offset)
            stored_size = entry.packed_size or entry.size
            payload = _read_exact(handle, stored_size)
            if entry.packed_size:
                payload = zlib.decompress(payload)
            if len(payload) != entry.size:
                raise ValueError(f"BA2 entry size mismatch: {entry.path}")
            _atomic_write(_safe_destination(output_dir, entry.path), payload)


def write_list(path: Path, entries: list[ArchiveEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(
                json.dumps(
                    {
                        "path": entry.path,
                        "size": entry.size,
                        "archive_type": entry.archive_type,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="List or selectively materialize project-local BSA/BA2 entries.")
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--list-output", default="")
    parser.add_argument("--include-list", default="")
    parser.add_argument("--max-files", type=int, default=50_000)
    parser.add_argument("--max-file-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=4 * 1024 * 1024 * 1024)
    args = parser.parse_args()

    archive_path = Path(args.archive_path).resolve(strict=True)
    extension = archive_path.suffix.casefold()
    if extension == ".ba2":
        archive_type, entries = read_ba2_entries(archive_path)
    elif extension == ".bsa":
        archive_type, entries = "BSA", read_bsa_entries(archive_path)
    else:
        raise ValueError("ArchivePath must end with .bsa or .ba2")
    if args.list_output:
        write_list(Path(args.list_output).resolve(strict=False), entries)
    if not args.output_dir:
        print(f"Listed {len(entries)} entries ({archive_type}).")
        return 0

    includes = read_include_list(Path(args.include_list).resolve(strict=True) if args.include_list else None)
    selected = select_entries(entries, includes)
    validate_limits(
        selected,
        max_files=args.max_files,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
    )
    output_dir = Path(args.output_dir).resolve(strict=True)
    if extension == ".bsa":
        extract_bsa(archive_path, output_dir, selected)
    else:
        extract_ba2(archive_path, output_dir, selected, archive_type)
    print(f"Materialized {len(selected)} entries ({archive_type}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

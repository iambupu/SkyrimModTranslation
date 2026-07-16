"""Read validated translation pairs from xTranslator SST and EET dictionaries."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path


class BinaryGlossaryError(ValueError):
    """Raised when a binary dictionary cannot be decoded without guessing."""


@dataclass(frozen=True)
class BinaryGlossaryEntry:
    source: str
    target: str


_EET_RECORD_PATTERN = re.compile(rb"\x04\x00\x00\x00[A-Z0-9_*]{4}")
_MAX_TEXT_FIELD_BYTES = 64 * 1024 * 1024


def _read_u16(data: bytes, position: int, label: str) -> tuple[int, int]:
    if position + 2 > len(data):
        raise BinaryGlossaryError(f"unexpected EOF while reading {label} at 0x{position:X}")
    return struct.unpack_from("<H", data, position)[0], position + 2


def _read_u32(data: bytes, position: int, label: str) -> tuple[int, int]:
    if position + 4 > len(data):
        raise BinaryGlossaryError(f"unexpected EOF while reading {label} at 0x{position:X}")
    return struct.unpack_from("<I", data, position)[0], position + 4


def _decode_utf16le(raw: bytes, position: int) -> str:
    if len(raw) % 2:
        raise BinaryGlossaryError(f"odd UTF-16LE byte length at 0x{position:X}")
    try:
        return raw.decode("utf-16le").rstrip("\x00")
    except UnicodeDecodeError as exc:
        raise BinaryGlossaryError(f"invalid UTF-16LE text at 0x{position:X}: {exc}") from exc


def _valid_signature(raw: bytes) -> bool:
    return bool(raw) and all(
        0x30 <= value <= 0x39 or 0x41 <= value <= 0x5A or value in (0x2A, 0x5F)
        for value in raw
    )


def _sst_record_layout(data: bytes, position: int, file_format: bytes) -> str | None:
    if file_format == b"SSU5":
        if position + 12 <= len(data) and _valid_signature(data[position + 4 : position + 8]):
            return "ssu5"
        return None
    if position + 31 <= len(data) and _valid_signature(data[position + 4 : position + 12]):
        return "standard"
    if position + 35 <= len(data) and _valid_signature(data[position + 8 : position + 16]):
        return "extended"
    return None


def _find_sst_record_start(data: bytes, position: int, file_format: bytes) -> int:
    for candidate in range(position, min(len(data), position + 64)):
        if _sst_record_layout(data, candidate, file_format) is not None:
            return candidate
    raise BinaryGlossaryError(f"expected SST record near 0x{position:X}")


def decode_sst(path: Path) -> list[BinaryGlossaryEntry]:
    """Decode SSU5, SSU8, or SSU9 xTranslator SST records without modifying the file."""
    data = path.read_bytes()
    if len(data) < 10 or data[:4] not in {b"SSU5", b"SSU8", b"SSU9"}:
        magic = data[:4].decode("ascii", errors="replace")
        raise BinaryGlossaryError(f"unsupported SST format {magic!r}; expected SSU5, SSU8, or SSU9")

    file_format = data[:4]
    if file_format == b"SSU5":
        position = _find_sst_record_start(data, 10, file_format)
    elif file_format == b"SSU8":
        position = _find_sst_record_start(data, 14, file_format)
    else:
        position = 5
        plugin_count, position = _read_u32(data, position, "SSU9 plugin count")
        if plugin_count > 4096:
            raise BinaryGlossaryError(f"unreasonable SSU9 plugin count: {plugin_count}")
        for plugin_index in range(plugin_count):
            length, position = _read_u32(data, position, f"SSU9 plugin {plugin_index} length")
            if length > _MAX_TEXT_FIELD_BYTES or position + length > len(data):
                raise BinaryGlossaryError(f"SSU9 plugin name exceeds file size at 0x{position:X}")
            _decode_utf16le(data[position : position + length], position)
            position += length
        position = _find_sst_record_start(data, position, file_format)

    entries: list[BinaryGlossaryEntry] = []
    record_count = 0
    while position < len(data):
        record_start = position
        layout = _sst_record_layout(data, position, file_format)
        if layout is None:
            raise BinaryGlossaryError(f"expected SST record at 0x{position:X}")

        _, position = _read_u32(data, position, "SST FormID")
        if layout == "extended":
            _, position = _read_u32(data, position, "SST extended FormID metadata")
        signature = data[position : position + 8]
        if layout == "ssu5":
            if not _valid_signature(signature[:4]):
                raise BinaryGlossaryError(f"invalid SSU5 signature at 0x{position:X}")
        elif not _valid_signature(signature):
            raise BinaryGlossaryError(f"invalid SST signature at 0x{position:X}")
        position += 8

        if layout == "ssu5":
            _, position = _read_u32(data, position, "SSU5 string id")
            if position >= len(data):
                raise BinaryGlossaryError(f"unexpected EOF while reading SSU5 flags at 0x{position:X}")
            position += 1
        else:
            _, position = _read_u16(data, position, "SST record id")
            _, position = _read_u16(data, position, "SST maximum record id")
            _, position = _read_u32(data, position, "SST string id")
            _, position = _read_u16(data, position, "SST flags")

        source_length, position = _read_u32(data, position, "SST source length")
        if source_length > _MAX_TEXT_FIELD_BYTES or position + source_length > len(data):
            raise BinaryGlossaryError(f"SST source exceeds file size at 0x{record_start:X}")
        source = _decode_utf16le(data[position : position + source_length], position)
        position += source_length

        target_length, position = _read_u32(data, position, "SST target length")
        if target_length > _MAX_TEXT_FIELD_BYTES or position + target_length > len(data):
            raise BinaryGlossaryError(f"SST target exceeds file size at 0x{record_start:X}")
        target = _decode_utf16le(data[position : position + target_length], position)
        position += target_length

        if position == len(data) or _sst_record_layout(data, position, file_format) is not None:
            pass
        elif position + 5 <= len(data) and (
            position + 5 == len(data)
            or _sst_record_layout(data, position + 5, file_format) is not None
        ):
            position += 5
        else:
            raise BinaryGlossaryError(f"invalid SST record boundary after 0x{record_start:X}")

        record_count += 1
        if source.strip() and target.strip() and source != target:
            entries.append(BinaryGlossaryEntry(source=source, target=target))

    if record_count == 0:
        raise BinaryGlossaryError("SST file contains no records")
    return entries


def _read_eet_field(data: bytes, position: int) -> tuple[bytes, int] | None:
    if position + 4 > len(data):
        return None
    length = struct.unpack_from("<I", data, position)[0]
    position += 4
    if length > _MAX_TEXT_FIELD_BYTES or position + length > len(data):
        return None
    return data[position : position + length], position + length


def _valid_eet_signature(raw: bytes) -> bool:
    return len(raw) == 4 and _valid_signature(raw) and any(
        0x41 <= value <= 0x5A or value in (0x2A, 0x5F) for value in raw
    )


def decode_eet(path: Path) -> list[BinaryGlossaryEntry]:
    """Decode EET v2 source/target pairs and validate every declared record boundary."""
    data = path.read_bytes()
    if len(data) < 30 or data[:4] != b"EET_":
        raise BinaryGlossaryError("not an EET dictionary")
    version = struct.unpack_from("<I", data, 4)[0]
    if version != 2:
        raise BinaryGlossaryError(f"unsupported EET version {version}; expected version 2")
    if data[12:16] != b"GAME" or data[18:22] != b"LINE":
        raise BinaryGlossaryError("invalid EET v2 header")
    declared_records = struct.unpack_from("<I", data, 22)[0]
    if declared_records == 0:
        raise BinaryGlossaryError("EET file declares no records")

    records: list[tuple[int, int, str, str]] = []
    for candidate in _EET_RECORD_PATTERN.finditer(data, 30):
        position = candidate.start()
        fields: list[bytes] = []
        for _ in range(6):
            parsed = _read_eet_field(data, position)
            if parsed is None:
                break
            raw, position = parsed
            fields.append(raw)
            if len(fields) == 4 and not _valid_eet_signature(raw):
                break
        if len(fields) != 6 or not _valid_eet_signature(fields[0]):
            continue
        try:
            decoded = tuple(field.decode("utf-8-sig") for field in fields)
        except UnicodeDecodeError:
            continue
        records.append((candidate.start(), position, decoded[4], decoded[5]))

    if len(records) != declared_records:
        raise BinaryGlossaryError(
            f"EET record count mismatch: header declares {declared_records}, decoded {len(records)}"
        )
    if records[0][0] != 30:
        raise BinaryGlossaryError(f"first EET record starts at 0x{records[0][0]:X}, expected 0x1E")
    for current, following in zip(records, records[1:]):
        if current[1] > following[0]:
            raise BinaryGlossaryError(f"overlapping EET records at 0x{current[0]:X}")
    if records[-1][1] > len(data):
        raise BinaryGlossaryError("last EET record exceeds file size")

    entries: list[BinaryGlossaryEntry] = []
    for _, _, source, target in records:
        if source.strip() and target.strip() and source != target:
            entries.append(BinaryGlossaryEntry(source=source, target=target))
    return entries

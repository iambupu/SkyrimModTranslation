from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Sequence


STRING_TABLE_TYPES = frozenset({"strings", "dlstrings", "ilstrings"})
PEX_CLASSIFICATIONS = frozenset({"visible", "protected", "manual_review"})
PLUGIN_MASTER_STYLES = frozenset({"full", "light"})


@dataclass(frozen=True)
class PluginIdentityFixture:
    owner_mod_key: str
    local_id: int
    master_style: str
    raw_form_id: int
    evidence_source: str
    full_index: int | None = None
    light_index: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Fallout4PexCallOccurrenceFixture:
    object_name: str
    state_name: str
    function_name: str
    instruction_index: int
    opcode: str
    callee: str
    argument_index: int
    semantic_role: str
    source: str
    classification: str
    visibility_basis: str
    occurrence_id: str
    game_id: str = "fallout4"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StringTableFixture:
    table_type: str
    encoding: str
    entries: tuple[tuple[int, str], ...]
    payload: bytes


def _require_non_empty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def build_plugin_identity_fixture(
    *,
    owner_mod_key: str,
    local_id: int,
    master_style: str,
    evidence_source: str = "synthetic-fixture",
    full_index: int | None = None,
    light_index: int | None = None,
) -> PluginIdentityFixture:
    owner = _require_non_empty_text(owner_mod_key, "owner_mod_key")
    if not owner.casefold().endswith((".esp", ".esm", ".esl")):
        raise ValueError("owner_mod_key must end with .esp, .esm, or .esl")
    if master_style not in PLUGIN_MASTER_STYLES:
        raise ValueError("master_style must be 'full' or 'light'")
    if isinstance(local_id, bool) or not isinstance(local_id, int):
        raise ValueError("local_id must be an integer")
    source = _require_non_empty_text(evidence_source, "evidence_source")

    if master_style == "full":
        if light_index is not None:
            raise ValueError("light_index is not valid for a full identity")
        if isinstance(full_index, bool) or not isinstance(full_index, int):
            raise ValueError("full_index is required for a full identity")
        if not 0 <= full_index <= 0xFD:
            raise ValueError("full_index must be between 0x00 and 0xFD")
        if not 0 <= local_id <= 0xFFFFFF:
            raise ValueError("full local_id must fit in 24 bits")
        raw_form_id = (full_index << 24) | local_id
        return PluginIdentityFixture(
            owner_mod_key=owner,
            local_id=local_id,
            master_style=master_style,
            raw_form_id=raw_form_id,
            evidence_source=source,
            full_index=full_index,
        )

    if full_index is not None:
        raise ValueError("full_index is not valid for a light identity")
    if isinstance(light_index, bool) or not isinstance(light_index, int):
        raise ValueError("light_index is required for a light identity")
    if not 0 <= light_index <= 0xFFF:
        raise ValueError("light_index must fit in 12 bits")
    if not 0 <= local_id <= 0xFFF:
        raise ValueError("light local_id must fit in 12 bits")
    raw_form_id = 0xFE000000 | (light_index << 12) | local_id
    return PluginIdentityFixture(
        owner_mod_key=owner,
        local_id=local_id,
        master_style=master_style,
        raw_form_id=raw_form_id,
        evidence_source=source,
        light_index=light_index,
    )


def build_fallout4_pex_call_occurrence_fixture(
    *,
    object_name: str,
    function_name: str,
    instruction_index: int,
    opcode: str,
    callee: str,
    argument_index: int,
    semantic_role: str,
    source: str,
    classification: str,
    visibility_basis: str,
    state_name: str = "",
) -> Fallout4PexCallOccurrenceFixture:
    if isinstance(instruction_index, bool) or not isinstance(instruction_index, int) or instruction_index < 0:
        raise ValueError("instruction_index must be a non-negative integer")
    if isinstance(argument_index, bool) or not isinstance(argument_index, int) or argument_index < 0:
        raise ValueError("argument_index must be a non-negative integer")
    if classification not in PEX_CLASSIFICATIONS:
        raise ValueError(
            "classification must be visible, protected, or manual_review"
        )
    identity = {
        "argument_index": argument_index,
        "callee": _require_non_empty_text(callee, "callee"),
        "function_name": _require_non_empty_text(function_name, "function_name"),
        "instruction_index": instruction_index,
        "object_name": _require_non_empty_text(object_name, "object_name"),
        "opcode": _require_non_empty_text(opcode, "opcode"),
        "state_name": state_name,
    }
    canonical = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    occurrence_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return Fallout4PexCallOccurrenceFixture(
        object_name=identity["object_name"],
        state_name=state_name,
        function_name=identity["function_name"],
        instruction_index=instruction_index,
        opcode=identity["opcode"],
        callee=identity["callee"],
        argument_index=argument_index,
        semantic_role=_require_non_empty_text(semantic_role, "semantic_role"),
        source=_require_non_empty_text(source, "source"),
        classification=classification,
        visibility_basis=_require_non_empty_text(visibility_basis, "visibility_basis"),
        occurrence_id=occurrence_id,
    )


def build_string_table_fixture(
    table_type: str,
    entries: Sequence[tuple[int, str]],
    *,
    encoding: str = "utf-8",
) -> StringTableFixture:
    normalized_type = _require_non_empty_text(table_type, "table_type").casefold()
    if normalized_type not in STRING_TABLE_TYPES:
        raise ValueError("table_type must be strings, dlstrings, or ilstrings")
    normalized_entries: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    for string_id, text in entries:
        if isinstance(string_id, bool) or not isinstance(string_id, int):
            raise ValueError("string IDs must be integers")
        if not 0 <= string_id <= 0xFFFFFFFF:
            raise ValueError("string IDs must fit in 32 bits")
        if string_id in seen_ids:
            raise ValueError(f"duplicate string ID: {string_id}")
        if not isinstance(text, str):
            raise ValueError("string table values must be text")
        if "\x00" in text:
            raise ValueError("string table values must not contain NUL characters")
        seen_ids.add(string_id)
        normalized_entries.append((string_id, text))
    normalized_entries.sort(key=lambda item: item[0])

    data = bytearray()
    directory = bytearray()
    for string_id, text in normalized_entries:
        offset = len(data)
        encoded = text.encode(encoding) + b"\x00"
        if normalized_type == "strings":
            data.extend(encoded)
        else:
            data.extend(struct.pack("<I", len(encoded)))
            data.extend(encoded)
        directory.extend(struct.pack("<II", string_id, offset))

    header = struct.pack("<II", len(normalized_entries), len(data))
    return StringTableFixture(
        table_type=normalized_type,
        encoding=encoding,
        entries=tuple(normalized_entries),
        payload=bytes(header + directory + data),
    )


FIXTURE_BUILDERS = MappingProxyType(
    {
        "plugin_identity": build_plugin_identity_fixture,
        "fallout4_pex_call_occurrence": build_fallout4_pex_call_occurrence_fixture,
        "string_table": build_string_table_fixture,
    }
)

"""Load the fixture-backed PEX call-site visibility registry."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
CALL_OPCODES = frozenset({"CALLMETHOD", "CALLPARENT", "CALLSTATIC"})
ARGUMENT_CLASSIFICATIONS = frozenset({"visible", "protected"})
FALLBACK_CLASSIFICATION = "manual_review"
_CALLEE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_SEMANTIC_ROLE = re.compile(r"^[a-z][a-z0-9_]*$")


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ValueError(f"{label} fields are invalid: {'; '.join(details)}")


def _non_empty_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} entries must be non-empty strings")
    normalized = [item.strip() for item in value]
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise ValueError(f"{label} contains duplicate entries")
    return normalized


def load_pex_visible_api_registry(
    path: Path,
    *,
    expected_game_id: str | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"PEX visible API registry is invalid JSON: {path}") from exc
    root = _require_object(payload, "PEX visible API registry")
    _require_exact_fields(
        root,
        frozenset(
            {
                "schema_version",
                "game_id",
                "literal_policy",
                "unmatched_classification",
                "dynamic_argument_classification",
                "apis",
            }
        ),
        "PEX visible API registry",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported PEX visible API registry schema_version={root['schema_version']}"
        )
    game_id = root["game_id"]
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError("PEX visible API registry game_id must be non-empty text")
    if expected_game_id is not None and game_id != expected_game_id:
        raise ValueError(
            f"PEX visible API registry game_id {game_id!r} does not match {expected_game_id!r}"
        )
    if root["literal_policy"] != "direct_only":
        raise ValueError("PEX visible API registry literal_policy must be direct_only")
    for field in ("unmatched_classification", "dynamic_argument_classification"):
        if root[field] != FALLBACK_CLASSIFICATION:
            raise ValueError(f"PEX visible API registry {field} must be manual_review")

    apis = root["apis"]
    if not isinstance(apis, list) or not apis:
        raise ValueError("PEX visible API registry apis must be a non-empty array")
    seen_callees: set[str] = set()
    classifications: set[str] = set()
    for api_index, raw_api in enumerate(apis):
        label = f"apis[{api_index}]"
        api = _require_object(raw_api, label)
        _require_exact_fields(
            api,
            frozenset({"callee", "opcode_forms", "arguments", "evidence"}),
            label,
        )
        callee = api["callee"]
        if not isinstance(callee, str) or _CALLEE.fullmatch(callee) is None:
            raise ValueError(f"{label}.callee is invalid: {callee!r}")
        callee_key = callee.casefold()
        if callee_key in seen_callees:
            raise ValueError(f"duplicate PEX visible API callee: {callee}")
        seen_callees.add(callee_key)

        opcodes = _non_empty_strings(api["opcode_forms"], f"{label}.opcode_forms")
        unsupported_opcodes = sorted(set(opcodes) - CALL_OPCODES)
        if unsupported_opcodes:
            raise ValueError(
                f"{label}.opcode_forms contains unsupported values: "
                f"{', '.join(unsupported_opcodes)}"
            )
        _non_empty_strings(api["evidence"], f"{label}.evidence")

        arguments = api["arguments"]
        if not isinstance(arguments, list) or not arguments:
            raise ValueError(f"{label}.arguments must be a non-empty array")
        seen_indexes: set[int] = set()
        for argument_position, raw_argument in enumerate(arguments):
            argument_label = f"{label}.arguments[{argument_position}]"
            argument = _require_object(raw_argument, argument_label)
            _require_exact_fields(
                argument,
                frozenset({"index", "semantic_role", "classification"}),
                argument_label,
            )
            index = argument["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError(f"{argument_label}.index must be a non-negative integer")
            if index in seen_indexes:
                raise ValueError(f"{label}.arguments contains duplicate index {index}")
            seen_indexes.add(index)
            role = argument["semantic_role"]
            if not isinstance(role, str) or _SEMANTIC_ROLE.fullmatch(role) is None:
                raise ValueError(f"{argument_label}.semantic_role is invalid: {role!r}")
            classification = argument["classification"]
            if classification not in ARGUMENT_CLASSIFICATIONS:
                raise ValueError(
                    f"{argument_label}.classification must be visible or protected"
                )
            classifications.add(classification)

    missing_classes = ARGUMENT_CLASSIFICATIONS - classifications
    if missing_classes:
        raise ValueError(
            "PEX visible API registry must include fixture-backed visible and protected "
            f"arguments; missing {', '.join(sorted(missing_classes))}"
        )
    return root

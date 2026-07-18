from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


CONFIG_RELATIVE_PATH = Path("config") / "capability_promotion_gates.json"
GATE_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]*")
REQUIRED_CONSUMER_SURFACES = frozenset({"routing", "provenance", "qa"})
SUPPORTED_ADAPTER_OPERATIONS = frozenset({"inventory", "extract", "apply", "verify"})
_MISSING = object()


def _load_payload(root: Path) -> Any:
    path = root / CONFIG_RELATIVE_PATH
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _safe_relative_path(root: Path, value: object, field: str) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{field} must be a non-empty relative path"
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None, f"{field} must stay inside the repository: {value!r}"
    resolved_root = root.resolve(strict=False)
    resolved = (root / relative).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None, f"{field} resolves outside the repository: {value!r}"
    return resolved, None


def _profile_value(
    root: Path,
    game_id: str,
    segments: Sequence[str],
    cache: dict[str, Any],
) -> Any:
    if game_id not in cache:
        profile_path = root / "config" / "game_profiles" / f"{game_id}.json"
        with profile_path.open("r", encoding="utf-8-sig") as handle:
            cache[game_id] = json.load(handle)
    value: Any = cache[game_id]
    for segment in segments:
        if not isinstance(value, Mapping) or segment not in value:
            return _MISSING
        value = value[segment]
    return value


def _validate_profile_requirements(
    *,
    root: Path,
    gate_id: str,
    field: str,
    requirements: object,
    profile_cache: dict[str, Any],
    errors: list[str],
) -> None:
    if not isinstance(requirements, list) or not requirements:
        errors.append(f"gate={gate_id} {field} must be a non-empty array")
        return
    for index, requirement in enumerate(requirements):
        prefix = f"gate={gate_id} {field}[{index}]"
        if not isinstance(requirement, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        game_id = requirement.get("game_id")
        path = requirement.get("path")
        allowed_values = requirement.get("allowed_values")
        must_be_missing = requirement.get("must_be_missing", False)
        if not isinstance(game_id, str) or not game_id.strip():
            errors.append(f"{prefix}.game_id must be non-empty text")
            continue
        if (
            not isinstance(path, list)
            or not path
            or not all(isinstance(segment, str) and segment for segment in path)
        ):
            errors.append(f"{prefix}.path must be a non-empty string array")
            continue
        if not isinstance(must_be_missing, bool):
            errors.append(f"{prefix}.must_be_missing must be boolean")
            continue
        if must_be_missing and "allowed_values" in requirement:
            errors.append(
                f"{prefix} cannot combine must_be_missing with allowed_values"
            )
            continue
        if not must_be_missing and (
            not isinstance(allowed_values, list) or not allowed_values
        ):
            errors.append(f"{prefix}.allowed_values must be a non-empty array")
            continue
        try:
            actual = _profile_value(root, game_id, path, profile_cache)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{prefix} cannot load game profile: {exc}")
            continue
        if must_be_missing:
            if actual is not _MISSING:
                errors.append(
                    f"{prefix} profile path must remain absent until promotion: "
                    f"{'.'.join(path)}"
                )
        elif actual is _MISSING:
            errors.append(
                f"{prefix} profile path is missing: {'.'.join(path)}"
            )
        elif actual not in allowed_values:
            errors.append(
                f"{prefix} expected one of {allowed_values!r}, found {actual!r}"
            )


def _validate_adapter_requirements(
    gate_id: str,
    requirements: object,
    registry: Mapping[str, Any],
    errors: list[str],
) -> None:
    if not isinstance(requirements, list) or not requirements:
        errors.append(f"gate={gate_id} adapter_requirements must be a non-empty array")
        return
    for index, requirement in enumerate(requirements):
        prefix = f"gate={gate_id} adapter_requirements[{index}]"
        if not isinstance(requirement, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        adapter_id = requirement.get("adapter_id")
        operations = requirement.get("operations")
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            errors.append(f"{prefix}.adapter_id must be non-empty text")
            continue
        if (
            not isinstance(operations, list)
            or not operations
            or not all(operation in SUPPORTED_ADAPTER_OPERATIONS for operation in operations)
        ):
            errors.append(
                f"{prefix}.operations must contain supported adapter operations"
            )
            continue
        spec = registry.get(adapter_id)
        if spec is None:
            errors.append(f"{prefix} adapter is not registered: {adapter_id}")
            continue
        entrypoints = getattr(spec, "entrypoints", {})
        for operation in operations:
            if operation not in entrypoints:
                errors.append(
                    f"{prefix} adapter '{adapter_id}' is missing operation '{operation}'"
                )


def _validate_promotion_evidence(
    *,
    root: Path,
    gate_id: str,
    gate: Mapping[str, Any],
    registry: Mapping[str, Any],
    errors: list[str],
) -> None:
    _validate_adapter_requirements(
        gate_id,
        gate.get("adapter_requirements"),
        registry,
        errors,
    )

    fixture_paths = gate.get("fixture_paths")
    if not isinstance(fixture_paths, list) or not fixture_paths:
        errors.append(f"gate={gate_id} fixture_paths must be a non-empty array")
    else:
        for index, raw_path in enumerate(fixture_paths):
            path, path_error = _safe_relative_path(
                root,
                raw_path,
                f"gate={gate_id} fixture_paths[{index}]",
            )
            if path_error:
                errors.append(path_error)
            elif path is not None and not path.is_file():
                errors.append(
                    f"gate={gate_id} required fixture does not exist: {raw_path}"
                )

    markers = gate.get("consumer_markers")
    if not isinstance(markers, list) or not markers:
        errors.append(f"gate={gate_id} consumer_markers must be a non-empty array")
        return
    surfaces: set[str] = set()
    for index, marker in enumerate(markers):
        prefix = f"gate={gate_id} consumer_markers[{index}]"
        if not isinstance(marker, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        surface = marker.get("surface")
        needle = marker.get("contains")
        if not isinstance(surface, str) or not surface.strip():
            errors.append(f"{prefix}.surface must be non-empty text")
        else:
            surfaces.add(surface)
        if not isinstance(needle, str) or not needle:
            errors.append(f"{prefix}.contains must be non-empty text")
            continue
        path, path_error = _safe_relative_path(root, marker.get("path"), f"{prefix}.path")
        if path_error:
            errors.append(path_error)
            continue
        if path is None or not path.is_file():
            errors.append(f"{prefix} consumer file does not exist")
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            errors.append(f"{prefix} consumer file is not strict UTF-8 text: {exc}")
            continue
        if needle not in content:
            errors.append(
                f"{prefix} consumer marker is missing from {marker.get('path')}: {needle!r}"
            )
    missing_surfaces = sorted(REQUIRED_CONSUMER_SURFACES - surfaces)
    if missing_surfaces:
        errors.append(
            f"gate={gate_id} consumer_markers missing surfaces: {', '.join(missing_surfaces)}"
        )


def validation_errors(
    root: Path,
    payload: Any | None = None,
    *,
    registry: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    errors: list[str] = []
    if payload is None:
        try:
            payload = _load_payload(root)
        except (OSError, json.JSONDecodeError) as exc:
            return (f"cannot load {CONFIG_RELATIVE_PATH.as_posix()}: {exc}",)
    if not isinstance(payload, Mapping):
        return ("capability promotion gate config must be an object",)
    if payload.get("schema_version") != 1:
        errors.append("capability promotion gate schema_version must be 1")
    gates = payload.get("gates")
    if not isinstance(gates, list) or not gates:
        errors.append("capability promotion gates must be a non-empty array")
        return tuple(errors)

    if registry is None:
        from adapter_registry import ADAPTER_REGISTRY

        registry = ADAPTER_REGISTRY

    seen_ids: set[str] = set()
    profile_cache: dict[str, Any] = {}
    for index, gate in enumerate(gates):
        prefix = f"gates[{index}]"
        if not isinstance(gate, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        gate_id = gate.get("id")
        if not isinstance(gate_id, str) or GATE_ID_RE.fullmatch(gate_id) is None:
            errors.append(f"{prefix}.id must be a canonical lowercase gate id")
            continue
        if gate_id in seen_ids:
            errors.append(f"duplicate capability promotion gate id: {gate_id}")
            continue
        seen_ids.add(gate_id)
        enabled = gate.get("promotion_enabled")
        if not isinstance(enabled, bool):
            errors.append(f"gate={gate_id} promotion_enabled must be boolean")
            continue

        active_requirements = (
            "promoted_profile_requirements"
            if enabled
            else "unpromoted_profile_guards"
        )
        _validate_profile_requirements(
            root=root,
            gate_id=gate_id,
            field=active_requirements,
            requirements=gate.get(active_requirements),
            profile_cache=profile_cache,
            errors=errors,
        )
        if enabled:
            _validate_promotion_evidence(
                root=root,
                gate_id=gate_id,
                gate=gate,
                registry=registry,
                errors=errors,
            )

    return tuple(sorted(errors))

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from adapter_contract import AdapterSpec, validate_entrypoint
from capability_resolver import CapabilityDecision, resolve_capability
from game_context import GameContext


CAPABILITY_REQUIRED_OPERATIONS = MappingProxyType(
    {
        "unsupported": (),
        "inventory_only": ("inventory",),
        "read_only": ("inventory", "extract", "verify"),
        "experimental_write": ("inventory", "extract", "apply", "verify"),
        "stable": ("inventory", "extract", "apply", "verify"),
    }
)


def _spec(
    adapter_id: str,
    entrypoints: Mapping[str, str],
    required_options: tuple[str, ...] = (),
) -> AdapterSpec:
    return AdapterSpec(adapter_id, entrypoints, required_options)


ADAPTER_REGISTRY: Mapping[str, AdapterSpec] = MappingProxyType(
    {
        "mutagen-bethesda-plugin": _spec(
            "mutagen-bethesda-plugin",
            {
                "inventory": "builtin:resource-inventory",
                "extract": "export_esp_strings.py",
                "apply": "invoke_mutagen_plugin_text_tool.py",
                "verify": "invoke_mutagen_plugin_text_tool.py",
            },
            (
                "adapter_contract_version",
                "extract_backend",
                "localized_plugin_policy",
                "mutagen_release",
            ),
        ),
        "mutagen-pex": _spec(
            "mutagen-pex",
            {
                "inventory": "builtin:resource-inventory",
                "extract": "invoke_mutagen_pex_string_tool.py",
                "apply": "invoke_mutagen_pex_string_tool.py",
                "verify": "invoke_mutagen_pex_string_tool.py",
            },
            ("pex_category",),
        ),
        "bethesda-bsa": _spec(
            "bethesda-bsa",
            {
                "inventory": "builtin:archive-inventory",
                "extract": "invoke_bsa_file_extractor_safe.py",
                "verify": "builtin:archive-manifest",
            },
        ),
        "bethesda-ba2": _spec(
            "bethesda-ba2",
            {
                "inventory": "builtin:archive-inventory",
                "extract": "invoke_ba2_extractor_safe.py",
                "verify": "builtin:archive-manifest",
            },
        ),
        "loose-text": _spec(
            "loose-text",
            {
                operation: "builtin:loose-text"
                for operation in ("inventory", "extract", "apply", "verify")
            },
        ),
        "bethesda-string-tables": _spec(
            "bethesda-string-tables",
            {
                operation: "builtin:string-tables"
                for operation in ("inventory", "extract", "apply", "verify")
            },
        ),
    }
)


def require_adapter(adapter_id: str, operation: str) -> AdapterSpec:
    if not isinstance(adapter_id, str) or not adapter_id.strip():
        raise ValueError("adapter_id must be a non-empty string")
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation must be a non-empty string")
    normalized_adapter_id = adapter_id.strip()
    normalized_operation = operation.strip()
    spec = ADAPTER_REGISTRY.get(normalized_adapter_id)
    if spec is None:
        raise ValueError(f"unknown adapter '{normalized_adapter_id}'")
    if normalized_operation not in spec.entrypoints:
        raise ValueError(
            f"adapter '{normalized_adapter_id}' does not implement operation "
            f"'{normalized_operation}'"
        )
    return spec


def require_script_entrypoint(adapter_id: str, operation: str) -> str:
    entrypoint = require_adapter(adapter_id, operation).entrypoints[operation]
    if entrypoint.startswith("builtin:"):
        raise ValueError(
            f"adapter '{adapter_id}' operation '{operation}' is not a Python script entrypoint"
        )
    return entrypoint


def require_capability_script_entrypoint(
    context: GameContext,
    capability: str,
    capability_operation: str,
    adapter_operation: str,
) -> tuple[CapabilityDecision, str]:
    decision = resolve_capability(context, capability, capability_operation)
    if not decision.supported:
        raise ValueError(decision.reason)
    if not decision.adapter_id:
        raise ValueError(
            f"Capability '{capability}' for game profile '{context.game_id}' "
            "does not declare an adapter."
        )
    return decision, require_script_entrypoint(decision.adapter_id, adapter_operation)


def _snapshot_registry(
    context: GameContext,
    registry: object,
) -> tuple[dict[str, AdapterSpec], list[str]]:
    prefix = f"game_id={context.game_id} registry"
    if not isinstance(registry, Mapping):
        return {}, [f"{prefix}: registry must be a Mapping"]
    try:
        registry_items = tuple(registry.items())
    except Exception as exc:
        return {}, [f"{prefix}: unable to read registry items: {type(exc).__name__}: {exc}"]

    snapshot: dict[str, AdapterSpec] = {}
    errors: list[str] = []
    keys_by_casefold: dict[str, str] = {}
    for index, item in enumerate(registry_items):
        item_prefix = f"{prefix} item={index}"
        try:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                errors.append(f"{item_prefix}: registry item must be a key/value pair")
                continue
            key, value = item
            if not isinstance(key, str) or not key.strip():
                errors.append(f"{item_prefix}: key must be a non-empty string without whitespace")
                continue
            normalized_key = key.strip()
            key_casefold = normalized_key.casefold()
            previous_key = keys_by_casefold.get(key_casefold)
            if previous_key is not None:
                errors.append(
                    f"{item_prefix}: registry key normalization/casefold collision: "
                    f"{previous_key!r}, {key!r}"
                )
                continue
            keys_by_casefold[key_casefold] = key
            if key != normalized_key:
                errors.append(
                    f"{item_prefix}: registry key must not contain surrounding whitespace: {key!r}"
                )
                continue
            if any(character.isspace() for character in key):
                errors.append(f"{item_prefix}: registry key must not contain whitespace: {key!r}")
                continue
            if not isinstance(value, AdapterSpec):
                errors.append(f"{item_prefix}: value must be an AdapterSpec")
                continue
            if key != value.adapter_id:
                errors.append(
                    f"{item_prefix}: registry key {key!r} does not match AdapterSpec id "
                    f"{value.adapter_id!r}"
                )
                continue
            canonical_spec = AdapterSpec(
                value.adapter_id,
                value.entrypoints,
                value.required_options,
            )
            if canonical_spec != value:
                errors.append(f"{item_prefix}: AdapterSpec is not in canonical immutable form")
                continue
            snapshot[key] = canonical_spec
        except Exception as exc:
            errors.append(
                f"{item_prefix}: invalid registry item: {type(exc).__name__}: {exc}"
            )
    return snapshot, errors


def validate_profile_adapters(
    context: GameContext,
    *,
    registry: object | None = None,
) -> tuple[str, ...]:
    active_registry = ADAPTER_REGISTRY if registry is None else registry
    scripts_dir = context.plugin_root / "scripts"
    registry_snapshot, errors = _snapshot_registry(context, active_registry)

    for capability_name, capability in sorted(context.capabilities.items()):
        prefix = f"game_id={context.game_id} capability={capability_name}"
        required_operations = CAPABILITY_REQUIRED_OPERATIONS.get(capability.level)
        if required_operations is None:
            errors.append(f"{prefix}: unknown capability level '{capability.level}'")
            continue
        if capability.level == "unsupported" and not capability.adapter_id:
            continue
        spec = registry_snapshot.get(capability.adapter_id)
        if spec is None:
            errors.append(f"{prefix}: unknown adapter '{capability.adapter_id}'")
            continue
        if spec.adapter_id != capability.adapter_id:
            errors.append(
                f"{prefix}: registry key '{capability.adapter_id}' does not match "
                f"AdapterSpec id '{spec.adapter_id}'"
            )
            continue

        for operation in required_operations:
            if operation not in spec.entrypoints:
                errors.append(
                    f"{prefix}: adapter '{spec.adapter_id}' is missing operation '{operation}'"
                )

        if capability.level != "unsupported":
            for option_name in spec.required_options:
                value = capability.options.get(option_name)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{prefix}: adapter '{spec.adapter_id}' required option "
                        f"'{option_name}' must be non-empty text"
                    )

        for operation, entrypoint in sorted(spec.entrypoints.items()):
            try:
                validate_entrypoint(entrypoint, scripts_dir)
            except ValueError as exc:
                errors.append(
                    f"{prefix}: adapter '{spec.adapter_id}' operation '{operation}' "
                    f"has invalid entrypoint: {exc}"
                )

    return tuple(sorted(errors))

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from game_context import CAPABILITY_LEVEL_RANKS, GameContext
from resource_model import ResourceDescriptor


OPERATION_MINIMUM_LEVEL = MappingProxyType(
    {
        "inventory": "inventory_only",
        "read": "read_only",
        "write": "experimental_write",
        "strict_complete": "stable",
    }
)


@dataclass(frozen=True)
class CapabilityDecision:
    supported: bool
    capability: str
    operation: str
    level: str
    adapter_id: str | None
    adapter_options: Mapping[str, Any]
    strict_complete_allowed: bool
    error_code: str | None
    reason: str


def _validate_operation(operation: str) -> str:
    if operation not in OPERATION_MINIMUM_LEVEL:
        supported = ", ".join(OPERATION_MINIMUM_LEVEL)
        raise ValueError(
            f"Unknown capability operation '{operation}'. Supported operations: {supported}"
        )
    return OPERATION_MINIMUM_LEVEL[operation]


def _level_supports_operation(level: str, minimum_level: str) -> bool:
    return CAPABILITY_LEVEL_RANKS[level] >= CAPABILITY_LEVEL_RANKS[minimum_level]


def _build_decision(
    *,
    supported: bool,
    capability: str,
    operation: str,
    level: str,
    adapter_id: str | None,
    adapter_options: Mapping[str, Any],
    strict_complete_allowed: bool,
    error_code: str | None,
    reason: str,
) -> CapabilityDecision:
    return CapabilityDecision(
        supported=supported,
        capability=capability,
        operation=operation,
        level=level,
        adapter_id=adapter_id,
        adapter_options=adapter_options,
        strict_complete_allowed=strict_complete_allowed,
        error_code=error_code,
        reason=reason,
    )


def _unsupported_decision(
    capability: str,
    operation: str,
    reason: str,
) -> CapabilityDecision:
    return _build_decision(
        supported=False,
        capability=capability,
        operation=operation,
        level="unsupported",
        adapter_id=None,
        adapter_options=MappingProxyType({}),
        strict_complete_allowed=False,
        error_code="capability_unsupported",
        reason=reason,
    )


def _decision_for_level(
    *,
    capability: str,
    operation: str,
    minimum_level: str,
    level: str,
    adapter_id: str | None,
    adapter_options: Mapping[str, Any],
    unsupported_error_code: str,
    reason_factory: Callable[[bool, str], str],
) -> CapabilityDecision:
    supported = _level_supports_operation(level, minimum_level)
    strict_complete_allowed = supported and (
        operation in {"inventory", "read"} or level == "stable"
    )
    return _build_decision(
        supported=supported,
        capability=capability,
        operation=operation,
        level=level,
        adapter_id=adapter_id,
        adapter_options=adapter_options,
        strict_complete_allowed=strict_complete_allowed,
        error_code=None if supported else unsupported_error_code,
        reason=reason_factory(supported, minimum_level),
    )


def resolve_capability(
    context: GameContext,
    capability: str,
    operation: str,
) -> CapabilityDecision:
    minimum_level = _validate_operation(operation)

    spec = context.capabilities.get(capability)
    if spec is None:
        return _unsupported_decision(
            capability,
            operation,
            reason=f"Capability '{capability}' is not declared by game profile '{context.game_id}'.",
        )

    def reason_factory(supported: bool, required_level: str) -> str:
        if supported:
            return (
                f"Capability '{capability}' level '{spec.level}' satisfies operation "
                f"'{operation}'."
            )
        return (
            f"Capability '{capability}' level '{spec.level}' does not satisfy operation "
            f"'{operation}', which requires '{required_level}'."
        )

    return _decision_for_level(
        capability=capability,
        operation=operation,
        minimum_level=minimum_level,
        level=spec.level,
        adapter_id=spec.adapter_id or None,
        adapter_options=spec.options,
        unsupported_error_code="capability_unsupported",
        reason_factory=reason_factory,
    )


def resolve_resource_capability(
    context: GameContext,
    resource: ResourceDescriptor,
    operation: str,
) -> CapabilityDecision:
    minimum_level = _validate_operation(operation)

    capability = resource.capability
    spec = context.capabilities.get(capability)
    if not capability or spec is None:
        return _unsupported_decision(
            capability,
            operation,
            reason=(
                f"Resource capability '{capability}' is not declared by game profile "
                f"'{context.game_id}' for operation '{operation}'."
            ),
        )

    base_level = spec.level
    effective_level = base_level
    applied_trait_caps: list[tuple[str, str]] = []
    declared_trait_caps = context.resource_model.trait_level_caps.get(capability, {})
    for trait in sorted(resource.traits):
        trait_cap = declared_trait_caps.get(trait)
        if trait_cap is None:
            continue
        applied_trait_caps.append((trait, trait_cap))
        if CAPABILITY_LEVEL_RANKS[trait_cap] < CAPABILITY_LEVEL_RANKS[effective_level]:
            effective_level = trait_cap

    trait_caps_reason = (
        "trait caps ["
        + ", ".join(f"{trait}='{level}'" for trait, level in applied_trait_caps)
        + "]"
        if applied_trait_caps
        else "no declared trait caps"
    )
    base_supported = _level_supports_operation(base_level, minimum_level)

    def reason_factory(supported: bool, required_level: str) -> str:
        summary = (
            f"Capability '{capability}' base level '{base_level}' with {trait_caps_reason} "
            f"has effective level '{effective_level}' for operation '{operation}', which "
            f"requires '{required_level}'."
        )
        if supported:
            return f"{summary} The effective level supports the operation."
        if not base_supported:
            return (
                f"{summary} The root cause is base capability support: the base level "
                "does not satisfy the operation."
            )
        return (
            f"{summary} The root cause is a trait cap: the base level satisfies the "
            "operation but the effective level does not."
        )

    return _decision_for_level(
        capability=capability,
        operation=operation,
        minimum_level=minimum_level,
        level=effective_level,
        adapter_id=spec.adapter_id or None,
        adapter_options=spec.options,
        unsupported_error_code=(
            "experimental_limit" if base_supported else "capability_unsupported"
        ),
        reason_factory=reason_factory,
    )

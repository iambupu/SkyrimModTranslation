from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from game_context import CAPABILITY_LEVEL_RANKS, GameContext


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


def resolve_capability(
    context: GameContext,
    capability: str,
    operation: str,
) -> CapabilityDecision:
    if operation not in OPERATION_MINIMUM_LEVEL:
        supported = ", ".join(OPERATION_MINIMUM_LEVEL)
        raise ValueError(f"Unknown capability operation '{operation}'. Supported operations: {supported}")

    spec = context.capabilities.get(capability)
    if spec is None:
        return CapabilityDecision(
            supported=False,
            capability=capability,
            operation=operation,
            level="unsupported",
            adapter_id=None,
            adapter_options=MappingProxyType({}),
            strict_complete_allowed=False,
            error_code="capability_unsupported",
            reason=f"Capability '{capability}' is not declared by game profile '{context.game_id}'.",
        )

    minimum_level = OPERATION_MINIMUM_LEVEL[operation]
    supported = CAPABILITY_LEVEL_RANKS[spec.level] >= CAPABILITY_LEVEL_RANKS[minimum_level]
    strict_complete_allowed = supported and (
        operation in {"inventory", "read"} or spec.level == "stable"
    )
    return CapabilityDecision(
        supported=supported,
        capability=capability,
        operation=operation,
        level=spec.level,
        adapter_id=spec.adapter_id or None,
        adapter_options=spec.options,
        strict_complete_allowed=strict_complete_allowed,
        error_code=None if supported else "capability_unsupported",
        reason=(
            f"Capability '{capability}' level '{spec.level}' satisfies operation '{operation}'."
            if supported
            else f"Capability '{capability}' level '{spec.level}' does not satisfy operation "
            f"'{operation}', which requires '{minimum_level}'."
        ),
    )

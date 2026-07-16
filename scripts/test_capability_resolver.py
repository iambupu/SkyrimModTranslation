from __future__ import annotations

import importlib
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


LEVELS = (
    "unsupported",
    "inventory_only",
    "read_only",
    "experimental_write",
    "stable",
)
OPERATIONS = ("inventory", "read", "write", "strict_complete")
MINIMUM_LEVEL_INDEX = {
    "inventory": 1,
    "read": 2,
    "write": 3,
    "strict_complete": 4,
}


def resolver_module():
    return importlib.import_module("capability_resolver")


def isolated_resource_model(game_context):
    return game_context.ResourceModel(
        extension_groups=(),
        containers={},
        trait_level_caps={},
    )


def context_with_level(level: str):
    game_context = importlib.import_module("game_context")
    capability = game_context.CapabilitySpec(
        level=level,
        adapter_id="test-adapter",
        options={"mode": "test"},
    )
    return replace(
        game_context.load_game_profile("skyrim-se"),
        capabilities=MappingProxyType({"test.capability": capability}),
        resource_model=isolated_resource_model(game_context),
    )


def plugin_resource(*traits: str, capability: str = "plugin_text"):
    resource_model = importlib.import_module("resource_model")
    return resource_model.ResourceDescriptor(
        relative_path=Path("Example.esp"),
        category="plugin",
        subtype="plugin",
        container="",
        extension=".esp",
        capability=capability,
        traits=frozenset(traits),
    )


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_resolver_enforces_level_operation_matrix(level: str, operation: str) -> None:
    decision = resolver_module().resolve_capability(
        context_with_level(level),
        "test.capability",
        operation,
    )

    expected_supported = LEVELS.index(level) >= MINIMUM_LEVEL_INDEX[operation]
    assert decision.supported is expected_supported
    assert decision.capability == "test.capability"
    assert decision.operation == operation
    assert decision.level == level
    assert decision.adapter_id == "test-adapter"
    assert decision.adapter_options == {"mode": "test"}
    expected_strict = expected_supported and (
        operation in {"inventory", "read"} or level == "stable"
    )
    assert decision.strict_complete_allowed is expected_strict
    assert decision.error_code == (None if expected_supported else "capability_unsupported")
    assert decision.reason


def test_missing_capability_fails_closed() -> None:
    context = context_with_level("stable")

    decision = resolver_module().resolve_capability(context, "missing", "inventory")

    assert decision.supported is False
    assert decision.level == "unsupported"
    assert decision.adapter_id is None
    assert decision.adapter_options == {}
    assert decision.strict_complete_allowed is False
    assert decision.error_code == "capability_unsupported"
    assert "missing" in decision.reason


def test_unknown_operation_raises_value_error() -> None:
    with pytest.raises(ValueError, match="operation"):
        resolver_module().resolve_capability(
            context_with_level("stable"),
            "test.capability",
            "extract",
        )


def test_capability_options_and_decision_are_deeply_immutable() -> None:
    game_context = importlib.import_module("game_context")
    capability = game_context.CapabilitySpec(
        level="stable",
        adapter_id="test-adapter",
        options={"nested": {"values": ["one"]}},
    )
    context = replace(
        game_context.load_game_profile("skyrim-se"),
        capabilities=MappingProxyType({"test.capability": capability}),
        resource_model=isolated_resource_model(game_context),
    )
    decision = resolver_module().resolve_capability(context, "test.capability", "read")

    with pytest.raises(TypeError):
        capability.options["new"] = "value"
    with pytest.raises(TypeError):
        capability.options["nested"]["new"] = "value"
    with pytest.raises(TypeError):
        decision.adapter_options["new"] = "value"
    with pytest.raises(TypeError):
        decision.adapter_options["nested"]["values"][0] = "changed"
    with pytest.raises(FrozenInstanceError):
        decision.supported = False


def test_real_profiles_expose_immutable_capability_maps() -> None:
    game_context = importlib.import_module("game_context")
    skyrim = game_context.load_game_profile("skyrim-se")
    fallout4 = game_context.load_game_profile("fallout4")

    assert set(skyrim.capabilities) == {
        "plugin_text",
        "pex",
        "archive.bsa",
        "archive.ba2",
        "loose_text",
        "string_tables",
    }
    assert set(fallout4.capabilities) == set(skyrim.capabilities)
    assert skyrim.capabilities["plugin_text"].level == "stable"
    assert skyrim.capabilities["pex"].level == "stable"
    assert skyrim.capabilities["loose_text"].level == "stable"
    assert fallout4.capabilities["plugin_text"].level == "experimental_write"
    assert fallout4.capabilities["pex"].level == "experimental_write"
    assert fallout4.capabilities["archive.ba2"].level == "read_only"
    assert fallout4.capabilities["loose_text"].level == "stable"
    assert skyrim.capabilities["plugin_text"].options["mutagen_release"] == "SkyrimSE"
    assert skyrim.capabilities["plugin_text"].options["extract_backend"] == "mutagen-adapter"
    assert skyrim.capabilities["plugin_text"].options["localized_plugin_policy"] == "allow"
    assert fallout4.capabilities["plugin_text"].options["mutagen_release"] == "Fallout4"
    assert fallout4.capabilities["plugin_text"].options["extract_backend"] == "mutagen-adapter"
    assert fallout4.capabilities["plugin_text"].options["localized_plugin_policy"] == "block"
    assert skyrim.capabilities["pex"].options["pex_category"] == "Skyrim"
    assert fallout4.capabilities["pex"].options["pex_category"] == "Fallout4"
    with pytest.raises(TypeError):
        skyrim.capabilities["new"] = skyrim.capabilities["plugin_text"]
    with pytest.raises(TypeError):
        skyrim.format_families["new"] = "format"


def test_unsupported_profile_capability_does_not_require_adapter_or_format_options() -> None:
    game_context = importlib.import_module("game_context")
    capabilities = game_context._load_capabilities(
        {"capabilities": {"pex": {"level": "unsupported"}}}
    )
    decision = resolver_module().resolve_capability(
        replace(
            game_context.load_game_profile("skyrim-se"),
            capabilities=capabilities,
            resource_model=isolated_resource_model(game_context),
        ),
        "pex",
        "read",
    )

    assert capabilities["pex"].adapter_id == ""
    assert capabilities["pex"].options == {}
    assert decision.supported is False
    assert decision.adapter_id is None


def test_game_context_defensively_copies_and_deeply_freezes_capability_inputs() -> None:
    game_context = importlib.import_module("game_context")
    source_families = {"plugin": "original-family"}
    source_options = {"nested": {"values": ["original"]}}
    source_spec = game_context.CapabilitySpec(
        level="stable",
        adapter_id="test-adapter",
        options=source_options,
    )
    source_capabilities = {"test.capability": source_spec}

    context = replace(
        game_context.load_game_profile("skyrim-se"),
        format_families=source_families,
        capabilities=source_capabilities,
        resource_model=isolated_resource_model(game_context),
    )
    source_families["plugin"] = "changed-family"
    source_options["nested"]["values"][0] = "changed"
    source_capabilities["test.capability"] = game_context.CapabilitySpec(
        level="unsupported",
        adapter_id="replacement-adapter",
        options={},
    )

    assert context.format_families == {"plugin": "original-family"}
    assert context.capabilities["test.capability"].level == "stable"
    assert context.capabilities["test.capability"].options["nested"]["values"] == ("original",)
    assert context.capabilities["test.capability"] is not source_spec
    with pytest.raises(TypeError):
        context.format_families["plugin"] = "mutated"
    with pytest.raises(TypeError):
        context.capabilities["test.capability"] = source_spec


@pytest.mark.parametrize(
    (
        "traits",
        "operation",
        "expected_level",
        "expected_supported",
        "expected_strict_allowed",
        "expected_error",
    ),
    [
        ((), "inventory", "experimental_write", True, True, None),
        ((), "read", "experimental_write", True, True, None),
        ((), "write", "experimental_write", True, False, None),
        ((), "strict_complete", "experimental_write", False, False, "capability_unsupported"),
        (("light",), "inventory", "read_only", True, True, None),
        (("light",), "read", "read_only", True, True, None),
        (("light",), "write", "read_only", False, False, "experimental_limit"),
        (("light",), "strict_complete", "read_only", False, False, "capability_unsupported"),
        (("localized",), "inventory", "inventory_only", True, True, None),
        (("localized",), "read", "inventory_only", False, False, "experimental_limit"),
        (("localized",), "write", "inventory_only", False, False, "experimental_limit"),
        (
            ("localized",),
            "strict_complete",
            "inventory_only",
            False,
            False,
            "capability_unsupported",
        ),
        (("light", "localized"), "inventory", "inventory_only", True, True, None),
        (("light", "localized"), "read", "inventory_only", False, False, "experimental_limit"),
        (("light", "localized"), "write", "inventory_only", False, False, "experimental_limit"),
        (
            ("light", "localized"),
            "strict_complete",
            "inventory_only",
            False,
            False,
            "capability_unsupported",
        ),
    ],
)
def test_fallout4_resource_capability_matrix(
    traits: tuple[str, ...],
    operation: str,
    expected_level: str,
    expected_supported: bool,
    expected_strict_allowed: bool,
    expected_error: str | None,
) -> None:
    context = importlib.import_module("game_context").load_game_profile("fallout4")
    resource = plugin_resource(*traits)

    decision = resolver_module().resolve_resource_capability(context, resource, operation)

    assert decision.supported is expected_supported
    assert decision.capability == "plugin_text"
    assert decision.operation == operation
    assert decision.level == expected_level
    assert decision.strict_complete_allowed is expected_strict_allowed
    assert decision.error_code == expected_error
    assert "base level 'experimental_write'" in decision.reason
    assert f"operation '{operation}'" in decision.reason
    for trait in traits:
        expected_cap = "read_only" if trait == "light" else "inventory_only"
        assert f"{trait}='{expected_cap}'" in decision.reason


@pytest.mark.parametrize("capability", ["", "missing"])
def test_resource_with_empty_or_unknown_capability_fails_closed(capability: str) -> None:
    context = importlib.import_module("game_context").load_game_profile("fallout4")

    decision = resolver_module().resolve_resource_capability(
        context,
        plugin_resource(capability=capability),
        "inventory",
    )

    assert decision.supported is False
    assert decision.capability == capability
    assert decision.level == "unsupported"
    assert decision.adapter_id is None
    assert decision.adapter_options == {}
    assert decision.strict_complete_allowed is False
    assert decision.error_code == "capability_unsupported"
    assert "operation 'inventory'" in decision.reason


def test_undeclared_resource_trait_does_not_reduce_capability() -> None:
    context = importlib.import_module("game_context").load_game_profile("fallout4")

    decision = resolver_module().resolve_resource_capability(
        context,
        plugin_resource("master"),
        "write",
    )

    assert decision.supported is True
    assert decision.level == "experimental_write"
    assert decision.error_code is None
    assert "base level 'experimental_write'" in decision.reason
    assert "no declared trait caps" in decision.reason
    assert "operation 'write'" in decision.reason


def test_resource_resolver_rejects_unknown_operation() -> None:
    context = importlib.import_module("game_context").load_game_profile("fallout4")

    with pytest.raises(ValueError, match="operation"):
        resolver_module().resolve_resource_capability(
            context,
            plugin_resource("light"),
            "extract",
        )


def test_skyrim_light_plugin_is_read_only() -> None:
    context = importlib.import_module("game_context").load_game_profile("skyrim-se")

    decision = resolver_module().resolve_resource_capability(
        context,
        plugin_resource("light", "localized"),
        "strict_complete",
    )

    assert decision.supported is False
    assert decision.level == "read_only"
    assert decision.strict_complete_allowed is False
    assert decision.error_code == "experimental_limit"
    assert "base level 'stable'" in decision.reason
    assert "trait cap" in decision.reason
    assert "operation 'strict_complete'" in decision.reason


def test_resource_error_code_and_reason_follow_failure_root_cause() -> None:
    game_context = importlib.import_module("game_context")
    fallout4 = game_context.load_game_profile("fallout4")
    skyrim = game_context.load_game_profile("skyrim-se")
    stable_with_light_cap = replace(
        skyrim,
        resource_model=game_context.ResourceModel(
            extension_groups=skyrim.resource_model.extension_groups,
            containers=skyrim.resource_model.containers,
            trait_level_caps={"plugin_text": {"light": "read_only"}},
        ),
    )

    base_failure = resolver_module().resolve_resource_capability(
        fallout4,
        plugin_resource("light"),
        "strict_complete",
    )
    trait_failure = resolver_module().resolve_resource_capability(
        stable_with_light_cap,
        plugin_resource("light"),
        "strict_complete",
    )

    assert base_failure.level == "read_only"
    assert base_failure.error_code == "capability_unsupported"
    assert "base capability" in base_failure.reason
    assert "base level 'experimental_write'" in base_failure.reason
    assert "effective level 'read_only'" in base_failure.reason
    assert trait_failure.level == "read_only"
    assert trait_failure.error_code == "experimental_limit"
    assert "trait cap" in trait_failure.reason
    assert "base level 'stable'" in trait_failure.reason
    assert "effective level 'read_only'" in trait_failure.reason


def test_resource_decision_preserves_base_adapter_metadata_and_is_immutable() -> None:
    context = importlib.import_module("game_context").load_game_profile("fallout4")
    resource = plugin_resource("light", "localized")
    original_resource = replace(resource)
    base_spec = context.capabilities[resource.capability]

    decision = resolver_module().resolve_resource_capability(context, resource, "inventory")

    assert resource == original_resource
    assert decision.adapter_id == base_spec.adapter_id
    assert decision.adapter_options == base_spec.options
    with pytest.raises(TypeError):
        decision.adapter_options["new"] = "value"
    with pytest.raises(FrozenInstanceError):
        decision.level = "stable"
    with pytest.raises(FrozenInstanceError):
        resource.capability = "loose_text"

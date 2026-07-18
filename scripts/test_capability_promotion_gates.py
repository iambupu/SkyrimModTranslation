from __future__ import annotations

import json
from pathlib import Path

from capability_promotion_gates import CONFIG_RELATIVE_PATH, validation_errors
from capability_resolver import resolve_capability, resolve_resource_capability
from game_context import load_game_profile
from project_paths import source_repo_root
from resource_model import classify_resource


def test_repository_promotion_gate_contract_is_currently_valid() -> None:
    root = source_repo_root()
    payload = json.loads((root / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8"))

    enabled = {
        gate["id"]
        for gate in payload["gates"]
        if gate["promotion_enabled"] is True
    }
    assert enabled == {
        "light-plugin-writeback.skyrim-se",
        "light-plugin-writeback.fallout4",
        "string-tables.skyrim-se",
        "string-tables.fallout4",
        "localized-delivery.skyrim-se",
        "localized-delivery.fallout4",
    }
    assert validation_errors(root, payload) == ()


def test_advanced_capability_fixtures_are_wired_into_github_ci() -> None:
    root = source_repo_root()
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    for marker in (
        "adapters/SkyrimPluginTextTool.Tests/SkyrimPluginTextTool.Tests.csproj",
        "adapters/BethesdaStringTableTool.Tests/BethesdaStringTableTool.Tests.csproj",
        "scripts/test_fallout4_pex_semantic_writeback.py",
        "scripts/test_bethesda_string_table_adapter.py",
        "scripts/test_localized_delivery.py",
    ):
        assert marker in workflow


def test_disabled_gate_rejects_early_profile_promotion(tmp_path: Path) -> None:
    profile_dir = tmp_path / "config" / "game_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "example.json").write_text(
        json.dumps({"capabilities": {"string_tables": {"level": "stable"}}}),
        encoding="utf-8",
    )
    payload = {
        "schema_version": 1,
        "gates": [
            {
                "id": "string-tables.example",
                "promotion_enabled": False,
                "unpromoted_profile_guards": [
                    {
                        "game_id": "example",
                        "path": ["capabilities", "string_tables", "level"],
                        "allowed_values": ["inventory_only"],
                    }
                ],
            }
        ],
    }

    errors = validation_errors(tmp_path, payload, registry={})

    assert len(errors) == 1
    assert "expected one of ['inventory_only'], found 'stable'" in errors[0]


def test_disabled_gate_can_require_future_capability_to_remain_absent(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "config" / "game_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "example.json").write_text(
        json.dumps(
            {"capabilities": {"localized_delivery": {"level": "stable"}}}
        ),
        encoding="utf-8",
    )
    payload = {
        "schema_version": 1,
        "gates": [
            {
                "id": "localized-delivery.example",
                "promotion_enabled": False,
                "unpromoted_profile_guards": [
                    {
                        "game_id": "example",
                        "path": ["capabilities", "localized_delivery"],
                        "must_be_missing": True,
                    }
                ],
            }
        ],
    }

    errors = validation_errors(tmp_path, payload, registry={})

    assert len(errors) == 1
    assert "must remain absent until promotion" in errors[0]


def test_enabled_gate_requires_adapter_fixtures_and_consumers(tmp_path: Path) -> None:
    profile_dir = tmp_path / "config" / "game_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "example.json").write_text(
        json.dumps({"capabilities": {"string_tables": {"level": "stable"}}}),
        encoding="utf-8",
    )
    payload = {
        "schema_version": 1,
        "gates": [
            {
                "id": "string-tables.example",
                "promotion_enabled": True,
                "promoted_profile_requirements": [
                    {
                        "game_id": "example",
                        "path": ["capabilities", "string_tables", "level"],
                        "allowed_values": ["stable"],
                    }
                ],
                "adapter_requirements": [
                    {
                        "adapter_id": "missing-adapter",
                        "operations": ["inventory", "extract", "apply", "verify"],
                    }
                ],
                "fixture_paths": ["scripts/missing_fixture.py"],
                "consumer_markers": [
                    {
                        "surface": "routing",
                        "path": "scripts/missing_route.py",
                        "contains": "string_tables",
                    }
                ],
            }
        ],
    }

    errors = validation_errors(tmp_path, payload, registry={})

    assert any("adapter is not registered" in error for error in errors)
    assert any("required fixture does not exist" in error for error in errors)
    assert any("consumer file does not exist" in error for error in errors)
    assert any("missing surfaces: provenance, qa" in error for error in errors)


def test_light_plugin_writeback_is_experimental_for_both_games() -> None:
    for game_id in ("skyrim-se", "fallout4"):
        context = load_game_profile(game_id)
        resource = classify_resource(context, Path("Fixture.esl"))

        decision = resolve_resource_capability(context, resource, "write")

        assert resource.traits == frozenset({"light"})
        assert decision.supported is True
        assert decision.level == "experimental_write"


def test_raw_load_order_formid_trait_remains_write_blocked() -> None:
    context = load_game_profile("fallout4")
    resource = classify_resource(
        context,
        Path("Fixture.esp"),
        traits=frozenset({"contains_unsupported_light_formids"}),
    )

    decision = resolve_resource_capability(context, resource, "write")

    assert decision.supported is False
    assert decision.level == "read_only"
    assert decision.error_code == "experimental_limit"


def test_string_table_writeback_is_promoted_independently_of_localized_delivery() -> None:
    expected = {
        "skyrim-se": ("experimental_write", False),
        "fallout4": ("experimental_write", False),
    }
    for game_id, (expected_level, strict_complete_allowed) in expected.items():
        context = load_game_profile(game_id)
        resource = classify_resource(context, Path("Strings/Fixture_english.strings"))

        decision = resolve_resource_capability(context, resource, "write")

        assert resource.capability == "string_tables"
        assert decision.supported is True
        assert decision.level == expected_level
        assert decision.strict_complete_allowed is strict_complete_allowed


def test_localized_joint_delivery_capability_is_experimental_for_both_games() -> None:
    for game_id in ("skyrim-se", "fallout4"):
        decision = resolve_capability(
            load_game_profile(game_id),
            "localized_delivery",
            "write",
        )

        assert decision.supported is True
        assert decision.level == "experimental_write"
        assert decision.strict_complete_allowed is False
        assert decision.adapter_id == "bethesda-localized-delivery"
        assert decision.error_code is None

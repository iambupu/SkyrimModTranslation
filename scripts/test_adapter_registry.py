from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"required Task 2 module is missing: {exc.name}")


def context_with_capability(
    *,
    level: str = "stable",
    adapter_id: str = "loose-text",
    options: dict[str, Any] | None = None,
):
    game_context = importlib.import_module("game_context")
    capability = game_context.CapabilitySpec(
        level=level,
        adapter_id=adapter_id,
        options=options or {},
    )
    return replace(
        game_context.load_game_profile("skyrim-se"),
        capabilities=MappingProxyType({"test.capability": capability}),
        resource_model=game_context.ResourceModel(
            extension_groups=(),
            containers={},
            trait_level_caps={},
        ),
    )


def test_static_registry_contains_required_adapters_and_operations() -> None:
    contract = load_module("adapter_contract")
    registry = load_module("adapter_registry")

    assert contract.BUILTIN_HANDLERS == frozenset(
        {
            "resource-inventory",
            "archive-inventory",
            "archive-manifest",
            "loose-text",
        }
    )
    assert set(registry.ADAPTER_REGISTRY) == {
        "mutagen-bethesda-plugin",
        "mutagen-pex",
        "bethesda-bsa",
        "bethesda-ba2",
        "loose-text",
        "bethesda-string-tables",
        "bethesda-localized-delivery",
    }
    assert registry.ADAPTER_REGISTRY["mutagen-bethesda-plugin"].entrypoints == {
        "inventory": "builtin:resource-inventory",
        "extract": "export_esp_strings.py",
        "apply": "invoke_mutagen_plugin_text_tool.py",
        "verify": "invoke_mutagen_plugin_text_tool.py",
    }
    assert registry.ADAPTER_REGISTRY["mutagen-bethesda-plugin"].required_options == (
        "adapter_contract_version",
        "extract_backend",
        "localized_plugin_policy",
        "mutagen_release",
    )
    assert registry.ADAPTER_REGISTRY["mutagen-pex"].entrypoints == {
        "inventory": "builtin:resource-inventory",
        "extract": "invoke_mutagen_pex_string_tool.py",
        "apply": "invoke_mutagen_pex_string_tool.py",
        "verify": "invoke_mutagen_pex_string_tool.py",
    }
    assert registry.ADAPTER_REGISTRY["mutagen-pex"].required_options == ("pex_category",)
    assert registry.ADAPTER_REGISTRY["bethesda-localized-delivery"].entrypoints == {
        operation: "invoke_bethesda_localized_delivery.py"
        for operation in ("inventory", "extract", "apply", "verify")
    }
    assert registry.ADAPTER_REGISTRY["bethesda-localized-delivery"].required_options == ()
    assert registry.ADAPTER_REGISTRY["bethesda-bsa"].entrypoints == {
        "inventory": "builtin:archive-inventory",
        "extract": "invoke_bsa_file_extractor_safe.py",
        "verify": "builtin:archive-manifest",
    }
    assert registry.ADAPTER_REGISTRY["bethesda-ba2"].entrypoints == {
        "inventory": "builtin:archive-inventory",
        "extract": "invoke_ba2_extractor_safe.py",
        "verify": "builtin:archive-manifest",
    }
    assert registry.ADAPTER_REGISTRY["loose-text"].entrypoints == {
        operation: "builtin:loose-text"
        for operation in ("inventory", "extract", "apply", "verify")
    }
    assert registry.ADAPTER_REGISTRY["bethesda-string-tables"].entrypoints == {
        operation: "invoke_bethesda_string_table_tool.py"
        for operation in ("inventory", "extract", "apply", "verify")
    }
    assert registry.ADAPTER_REGISTRY["bethesda-string-tables"].required_options == (
        "source_encoding",
        "source_language",
        "target_encoding",
        "target_language",
    )


def test_all_real_profiles_validate_against_static_registry() -> None:
    game_context = importlib.import_module("game_context")
    registry = load_module("adapter_registry")

    for game_id in game_context.supported_game_ids():
        context = game_context.load_game_profile(game_id)
        assert registry.validate_profile_adapters(context) == ()


@pytest.mark.parametrize(
    ("adapter_id", "operation", "match"),
    (
        ("unknown-adapter", "inventory", "unknown adapter"),
        ("loose-text", "unknown-operation", "operation"),
        ("", "inventory", "adapter_id"),
        ("loose-text", "", "operation"),
    ),
)
def test_require_adapter_fails_closed(
    adapter_id: str,
    operation: str,
    match: str,
) -> None:
    registry = load_module("adapter_registry")

    with pytest.raises(ValueError, match=match):
        registry.require_adapter(adapter_id, operation)


def test_require_adapter_rejects_operation_missing_from_registered_spec() -> None:
    registry = load_module("adapter_registry")

    with pytest.raises(ValueError, match="does not implement.*apply"):
        registry.require_adapter("bethesda-bsa", "apply")


def test_profile_validation_reports_missing_operation_and_required_option_stably() -> None:
    registry = load_module("adapter_registry")
    contract = load_module("adapter_contract")
    context = context_with_capability(
        adapter_id="test-adapter",
        options={"required_one": "present", "required_two": "  "},
    )
    test_registry = MappingProxyType(
        {
            "test-adapter": contract.AdapterSpec(
                adapter_id="test-adapter",
                entrypoints={"inventory": "builtin:resource-inventory"},
                required_options=("required_one", "required_two", "required_three"),
            )
        }
    )

    errors = registry.validate_profile_adapters(context, registry=test_registry)

    assert errors == tuple(sorted(errors))
    assert any("game_id=skyrim-se capability=test.capability" in error for error in errors)
    assert any("missing operation 'extract'" in error for error in errors)
    assert any("missing operation 'apply'" in error for error in errors)
    assert any("missing operation 'verify'" in error for error in errors)
    assert any("required option 'required_two'" in error for error in errors)
    assert any("required option 'required_three'" in error for error in errors)


class GetTrapMapping(Mapping[str, object]):
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, _key: str, _default: object = None) -> object:
        raise AssertionError("validate_profile_adapters must not trust Mapping.get")


class MalformedItemsMapping(GetTrapMapping):
    def items(self):
        return ("not-a-key-value-pair",)


class ExplodingLengthItem(list[object]):
    def __len__(self) -> int:
        raise RuntimeError("malicious item length")


class ExplodingIterationItem(list[object]):
    def __len__(self) -> int:
        return 2

    def __iter__(self):
        raise RuntimeError("malicious item iteration")


class SingleItemMapping(GetTrapMapping):
    def __init__(self, item: object) -> None:
        super().__init__({})
        self._item = item

    def items(self):
        return (self._item,)


class ExplodingItemsCallMapping(GetTrapMapping):
    def items(self):
        raise RuntimeError("malicious items call")


class ExplodingItemsMaterializationMapping(GetTrapMapping):
    def items(self):
        def rows():
            raise RuntimeError("malicious items materialization")
            yield

        return rows()


class ExplodingEntryPointsMapping(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        raise RuntimeError("malicious AdapterSpec entrypoints")


def test_profile_validation_snapshots_registry_items_without_using_get() -> None:
    registry = load_module("adapter_registry")
    contract = load_module("adapter_contract")
    spec = contract.AdapterSpec(
        "loose-text",
        {
            operation: "builtin:loose-text"
            for operation in contract.ADAPTER_OPERATIONS
        },
        (),
    )

    errors = registry.validate_profile_adapters(
        context_with_capability(adapter_id="loose-text"),
        registry=GetTrapMapping({"loose-text": spec}),
    )

    assert errors == ()


def test_profile_validation_reports_malformed_registry_items_stably() -> None:
    registry = load_module("adapter_registry")

    errors = registry.validate_profile_adapters(
        context_with_capability(adapter_id="loose-text"),
        registry=MalformedItemsMapping({}),
    )

    assert errors == tuple(sorted(errors))
    assert any("game_id=skyrim-se registry item=0" in error for error in errors)
    assert any("key/value pair" in error for error in errors)


@pytest.mark.parametrize(
    "malicious_registry",
    (
        ExplodingItemsCallMapping({}),
        ExplodingItemsMaterializationMapping({}),
        SingleItemMapping(ExplodingLengthItem()),
        SingleItemMapping(ExplodingIterationItem()),
    ),
)
def test_profile_validation_contains_all_malicious_registry_item_exceptions(
    malicious_registry: Mapping[str, object],
) -> None:
    registry = load_module("adapter_registry")

    errors = registry.validate_profile_adapters(
        context_with_capability(adapter_id="loose-text"),
        registry=malicious_registry,
    )

    assert errors
    assert errors == tuple(sorted(errors))
    assert all("game_id=skyrim-se" in error for error in errors)


@pytest.mark.parametrize(
    "mutation",
    ("delete_adapter_id", "delete_entrypoints", "corrupt_entrypoints"),
)
def test_profile_validation_rebuilds_and_rejects_damaged_adapter_specs(
    mutation: str,
) -> None:
    registry = load_module("adapter_registry")
    contract = load_module("adapter_contract")
    spec = contract.AdapterSpec(
        "loose-text",
        {"inventory": "builtin:resource-inventory"},
        (),
    )
    if mutation == "delete_adapter_id":
        object.__delattr__(spec, "adapter_id")
    elif mutation == "delete_entrypoints":
        object.__delattr__(spec, "entrypoints")
    else:
        object.__setattr__(spec, "entrypoints", ExplodingEntryPointsMapping())

    errors = registry.validate_profile_adapters(
        context_with_capability(adapter_id="loose-text"),
        registry={"loose-text": spec},
    )

    assert errors
    assert errors == tuple(sorted(errors))
    assert all("game_id=skyrim-se" in error for error in errors)
    assert any("registry item=0" in error for error in errors)


@pytest.mark.parametrize(
    ("malformed_registry", "expected"),
    (
        ([], "must be a Mapping"),
        ({"loose-text": object()}, "must be an AdapterSpec"),
        (
            {
                "loose-text": lambda contract: contract.AdapterSpec(
                    "different-adapter",
                    {"inventory": "builtin:resource-inventory"},
                    (),
                )
            },
            "does not match AdapterSpec id",
        ),
        (
            {
                " loose-text ": lambda contract: contract.AdapterSpec(
                    "loose-text",
                    {"inventory": "builtin:resource-inventory"},
                    (),
                )
            },
            "surrounding whitespace",
        ),
        (
            {
                "loose-text": lambda contract: contract.AdapterSpec(
                    "loose-text",
                    {"inventory": "builtin:resource-inventory"},
                    (),
                ),
                "LOOSE-TEXT": lambda contract: contract.AdapterSpec(
                    "LOOSE-TEXT",
                    {"inventory": "builtin:resource-inventory"},
                    (),
                ),
            },
            "collision",
        ),
    ),
)
def test_profile_validation_rejects_malformed_custom_registry_stably(
    malformed_registry: object,
    expected: str,
) -> None:
    registry = load_module("adapter_registry")
    contract = load_module("adapter_contract")
    if isinstance(malformed_registry, dict):
        resolved_registry = {
            key: value(contract) if callable(value) else value
            for key, value in malformed_registry.items()
        }
    else:
        resolved_registry = malformed_registry

    errors = registry.validate_profile_adapters(
        context_with_capability(adapter_id="loose-text"),
        registry=resolved_registry,
    )

    assert errors
    assert errors == tuple(sorted(errors))
    assert all("game_id=skyrim-se" in error for error in errors)
    assert any(expected in error for error in errors)


def test_unsupported_capability_still_rejects_typo_adapter_id() -> None:
    registry = load_module("adapter_registry")
    context = context_with_capability(level="unsupported", adapter_id="loose-txet")

    errors = registry.validate_profile_adapters(context)

    assert len(errors) == 1
    assert "unknown adapter 'loose-txet'" in errors[0]
    assert "capability=test.capability" in errors[0]


def test_unsupported_capability_may_omit_adapter_and_options() -> None:
    registry = load_module("adapter_registry")
    context = context_with_capability(level="unsupported", adapter_id="")

    assert registry.validate_profile_adapters(context) == ()


def test_require_script_entrypoint_rejects_builtin_handler() -> None:
    registry = load_module("adapter_registry")

    assert (
        registry.require_script_entrypoint("mutagen-bethesda-plugin", "extract")
        == "export_esp_strings.py"
    )
    with pytest.raises(ValueError, match="not a Python script"):
        registry.require_script_entrypoint("loose-text", "extract")


def test_require_capability_script_entrypoint_resolves_profile_adapter() -> None:
    game_context = importlib.import_module("game_context")
    registry = load_module("adapter_registry")
    context = game_context.load_game_profile("skyrim-se")

    decision, entrypoint = registry.require_capability_script_entrypoint(
        context,
        "pex",
        "read",
        "extract",
    )

    assert decision.adapter_id == context.capabilities["pex"].adapter_id
    assert entrypoint == registry.ADAPTER_REGISTRY[decision.adapter_id].entrypoints["extract"]


def test_require_capability_script_entrypoint_fails_closed_when_unsupported() -> None:
    registry = load_module("adapter_registry")
    context = context_with_capability(level="unsupported", adapter_id="")

    with pytest.raises(ValueError, match="does not satisfy operation"):
        registry.require_capability_script_entrypoint(
            context,
            "test.capability",
            "read",
            "extract",
        )


@pytest.mark.parametrize(
    "entrypoint",
    (
        str(SCRIPTS / "export_esp_strings.py"),
        "../export_esp_strings.py",
        "folder/export_esp_strings.py",
        r"folder\export_esp_strings.py",
        "builtin:",
        "builtin:Bad_Handler",
        "builtin:../handler",
        "builtin:not-a-real-handler",
    ),
)
def test_adapter_spec_rejects_unsafe_or_unknown_entrypoints(entrypoint: str) -> None:
    contract = load_module("adapter_contract")

    with pytest.raises(ValueError, match="entrypoint"):
        contract.AdapterSpec(
            adapter_id="test-adapter",
            entrypoints={"inventory": entrypoint},
            required_options=(),
        )


def test_adapter_spec_defensively_copies_and_freezes_inputs() -> None:
    contract = load_module("adapter_contract")
    entrypoints = {"inventory": "builtin:resource-inventory"}
    required_options = ["mode"]

    spec = contract.AdapterSpec("test-adapter", entrypoints, required_options)
    entrypoints["inventory"] = "builtin:changed"
    required_options[0] = "changed"

    assert spec.entrypoints == {"inventory": "builtin:resource-inventory"}
    assert spec.required_options == ("mode",)
    with pytest.raises(TypeError):
        spec.entrypoints["inventory"] = "builtin:changed"
    with pytest.raises(FrozenInstanceError):
        spec.adapter_id = "changed"


@pytest.mark.parametrize(
    ("entrypoints", "expected"),
    (
        ({" inventory ": "builtin:resource-inventory"}, "surrounding whitespace"),
        (
            {
                "inventory": "builtin:resource-inventory",
                " inventory ": "builtin:resource-inventory",
            },
            "collision",
        ),
        (
            {
                "inventory": "builtin:resource-inventory",
                "Inventory": "builtin:resource-inventory",
            },
            "collision",
        ),
        ({"Inventory": "builtin:resource-inventory"}, "unknown operation"),
    ),
)
def test_adapter_spec_rejects_noncanonical_or_colliding_operation_keys(
    entrypoints: dict[str, str],
    expected: str,
) -> None:
    contract = load_module("adapter_contract")

    with pytest.raises(ValueError, match=expected):
        contract.AdapterSpec("test-adapter", entrypoints, ())


def test_script_entrypoint_existence_is_checked_only_with_explicit_scripts_dir(
    tmp_path: Path,
) -> None:
    contract = load_module("adapter_contract")

    assert contract.validate_entrypoint("missing_adapter_script.py") == "missing_adapter_script.py"
    with pytest.raises(ValueError, match="does not exist"):
        contract.validate_entrypoint("missing_adapter_script.py", tmp_path)


def test_adapter_artifact_and_result_are_frozen_and_deeply_immutable() -> None:
    contract = load_module("adapter_contract")
    artifact = contract.AdapterArtifact("out/file.esp", "a" * 64)
    artifacts = [artifact]
    evidence_files = ["qa/evidence.json"]
    warnings = ["review warning"]
    blockers: list[str] = []

    result = contract.AdapterResult(
        status="success",
        error_code=None,
        operation="apply",
        adapter_id="test-adapter",
        artifacts=artifacts,
        evidence_files=evidence_files,
        warnings=warnings,
        blockers=blockers,
    )
    artifacts.clear()
    evidence_files.clear()
    warnings.clear()
    blockers.append("late blocker")

    assert result.artifacts == (artifact,)
    assert result.evidence_files == ("qa/evidence.json",)
    assert result.warnings == ("review warning",)
    assert result.blockers == ()
    assert contract.validate_adapter_result(result) is result
    with pytest.raises(FrozenInstanceError):
        artifact.path = "changed"
    with pytest.raises(FrozenInstanceError):
        result.status = "error"


@pytest.mark.parametrize("sha256", ("A" * 64, "a" * 63, "g" * 64, ""))
def test_adapter_artifact_rejects_noncanonical_sha256(sha256: str) -> None:
    contract = load_module("adapter_contract")

    with pytest.raises(ValueError, match="sha256"):
        contract.AdapterArtifact("out/file.esp", sha256)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    (
        ({"status": "unknown"}, "status"),
        ({"operation": ""}, "operation"),
        ({"operation": "unknown-operation"}, "operation"),
        ({"adapter_id": ""}, "adapter_id"),
        ({"status": "error", "error_code": None}, "error_code"),
        ({"status": "success", "error_code": "unexpected"}, "error_code"),
    ),
)
def test_adapter_result_fails_closed_on_invalid_identity_or_status(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    contract = load_module("adapter_contract")
    arguments = {
        "status": "success",
        "error_code": None,
        "operation": "inventory",
        "adapter_id": "test-adapter",
        "artifacts": (),
        "evidence_files": (),
        "warnings": (),
        "blockers": (),
    }
    arguments.update(kwargs)

    with pytest.raises(ValueError, match=match):
        contract.AdapterResult(**arguments)


def test_error_result_may_have_no_artifacts() -> None:
    contract = load_module("adapter_contract")

    result = contract.AdapterResult(
        status="error",
        error_code="adapter_failed",
        operation="apply",
        adapter_id="test-adapter",
        artifacts=(),
        evidence_files=("qa/failure.json",),
        warnings=(),
        blockers=("tool failed",),
    )

    assert contract.validate_adapter_result(result) is result
    assert result.artifacts == ()


def test_error_result_does_not_require_a_blocker() -> None:
    contract = load_module("adapter_contract")

    result = contract.AdapterResult(
        status="error",
        error_code="adapter_failed",
        operation="extract",
        adapter_id="test-adapter",
        artifacts=(),
        evidence_files=(),
        warnings=(),
        blockers=(),
    )

    assert result.blockers == ()


@pytest.mark.parametrize(
    ("status", "error_code", "blockers", "expected"),
    (
        ("success", None, ("unexpected blocker",), "Successful.*blockers"),
        ("blocked", "adapter_blocked", (), "Blocked.*blocker"),
    ),
)
def test_adapter_result_enforces_status_blocker_invariants(
    status: str,
    error_code: str | None,
    blockers: tuple[str, ...],
    expected: str,
) -> None:
    contract = load_module("adapter_contract")

    with pytest.raises(ValueError, match=expected):
        contract.AdapterResult(
            status=status,
            error_code=error_code,
            operation="inventory",
            adapter_id="test-adapter",
            artifacts=(),
            evidence_files=(),
            warnings=(),
            blockers=blockers,
        )


def test_validate_adapter_result_rejects_object_setattr_tampering() -> None:
    contract = load_module("adapter_contract")
    result = contract.AdapterResult(
        status="success",
        error_code=None,
        operation="inventory",
        adapter_id="test-adapter",
        artifacts=(),
        evidence_files=(),
        warnings=(),
        blockers=(),
    )
    object.__setattr__(result, "blockers", ("injected blocker",))

    with pytest.raises(ValueError, match="Successful.*blockers"):
        contract.validate_adapter_result(result)


@pytest.mark.parametrize(
    ("field", "invalid_value", "expected"),
    (
        ("sha256", "invalid", "sha256"),
        ("path", "   ", "path"),
    ),
)
def test_validate_adapter_result_rejects_nested_artifact_tampering(
    field: str,
    invalid_value: str,
    expected: str,
) -> None:
    contract = load_module("adapter_contract")
    artifact = contract.AdapterArtifact("out/file.esp", "a" * 64)
    result = contract.AdapterResult(
        status="success",
        error_code=None,
        operation="apply",
        adapter_id="test-adapter",
        artifacts=(artifact,),
        evidence_files=("qa/evidence.json",),
        warnings=(),
        blockers=(),
    )
    object.__setattr__(artifact, field, invalid_value)

    with pytest.raises(ValueError, match=expected):
        contract.validate_adapter_result(result)


@pytest.mark.parametrize(
    ("artifacts", "evidence_files", "match"),
    (
        ((), ("qa/evidence.json",), "artifact"),
        (("artifact",), (), "evidence"),
    ),
)
def test_successful_apply_requires_artifact_and_evidence(
    artifacts: tuple[object, ...],
    evidence_files: tuple[str, ...],
    match: str,
) -> None:
    contract = load_module("adapter_contract")
    normalized_artifacts = (
        (contract.AdapterArtifact("out/file.esp", "a" * 64),)
        if artifacts
        else ()
    )
    with pytest.raises(ValueError, match=match):
        contract.AdapterResult(
            status="success",
            error_code=None,
            operation="apply",
            adapter_id="test-adapter",
            artifacts=normalized_artifacts,
            evidence_files=evidence_files,
            warnings=(),
            blockers=(),
        )


def test_ci_profile_check_loads_profiles_and_calls_registry_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci = importlib.import_module("ci_validate_repo")
    profile_dir = tmp_path / "config" / "game_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "example.json").write_text("{}\n", encoding="utf-8")
    loaded: list[str] = []
    validated: list[object] = []
    sentinel_context = object()

    def fake_load(game_id: str):
        loaded.append(game_id)
        return sentinel_context

    def fake_validate(context: object):
        validated.append(context)
        return ("game_id=example capability=plugin_text: registry failure",)

    monkeypatch.setattr(ci, "load_game_profile", fake_load)
    monkeypatch.setattr(ci, "registry_validate_profile_adapters", fake_validate)
    reporter = ci.Reporter()

    ci.validate_game_profile_adapters(tmp_path, reporter)

    assert loaded == ["example"]
    assert validated == [sentinel_context]
    assert len(reporter.failed) == 1
    assert "game_id=example capability=plugin_text" in reporter.failed[0].detail


def test_ci_main_invokes_game_profile_adapter_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci = importlib.import_module("ci_validate_repo")
    called: list[Path] = []

    monkeypatch.setattr(ci, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(ci, "load_json_file", lambda *_args: {})
    for name, value in vars(ci).copy().items():
        if name.startswith("validate_") and callable(value):
            monkeypatch.setattr(ci, name, lambda *_args, **_kwargs: None)

    def profile_check(root: Path, reporter: object) -> None:
        called.append(root)
        reporter.check("game profile adapter registry", True, "called")

    monkeypatch.setattr(ci, "validate_game_profile_adapters", profile_check)

    assert ci.main(["--strict"]) == 0
    assert called == [tmp_path]


def test_ci_main_reports_missing_registry_script_instead_of_import_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ci = importlib.import_module("ci_validate_repo")
    contract = load_module("adapter_contract")
    registry = load_module("adapter_registry")
    profile_dir = tmp_path / "config" / "game_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "example.json").write_text("{}\n", encoding="utf-8")
    context = replace(
        context_with_capability(adapter_id="missing-script-adapter"),
        game_id="example",
        plugin_root=tmp_path,
    )
    missing_script_registry = MappingProxyType(
        {
            "missing-script-adapter": contract.AdapterSpec(
                adapter_id="missing-script-adapter",
                entrypoints={
                    "inventory": "missing_adapter_script.py",
                    "extract": "builtin:loose-text",
                    "apply": "builtin:loose-text",
                    "verify": "builtin:loose-text",
                },
                required_options=(),
            )
        }
    )

    monkeypatch.setattr(registry, "ADAPTER_REGISTRY", missing_script_registry)
    monkeypatch.setattr(ci, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(ci, "load_json_file", lambda *_args: {})
    monkeypatch.setattr(ci, "load_game_profile", lambda _game_id: context)
    for name, value in vars(ci).copy().items():
        if (
            name.startswith("validate_")
            and name != "validate_game_profile_adapters"
            and callable(value)
        ):
            monkeypatch.setattr(ci, name, lambda *_args, **_kwargs: None)

    assert ci.main(["--strict"]) == 1
    output = capsys.readouterr().out
    assert "[FAIL] game profile adapter registry: example" in output
    assert "missing_adapter_script.py" in output
    assert "does not exist" in output

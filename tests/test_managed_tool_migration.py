from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TEST_WORKSPACE_ID = "9032171e-8641-44e2-9c14-0342557fbfa1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import managed_tool_migration as migration  # noqa: E402
import managed_tool_provisioning as provisioning  # noqa: E402
import managed_tool_resolver as resolver  # noqa: E402
from managed_tool_resolver import (  # noqa: E402
    ToolPathProvenance,
    adapter_uses_managed_binding,
    classify_configured_tool_path,
    leased_payload_path,
    load_workspace_tool_config,
    managed_binding_health,
    resolve_tool_for_diagnostics,
)
from managed_tool_store import (  # noqa: E402
    ManagedToolStoreError,
    WorkspaceBindingEntry,
    bind_workspace,
    build_decoder_key,
    create_staging_directory,
    ensure_store_layout,
    make_entry_manifest,
    make_tool_key,
    new_binding,
    publish_movable_entry,
    resolve_managed_store_roots,
    validate_entry,
)
from smt_windows import ManagedProcessEnvironmentError  # noqa: E402


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "workspace_id": TEST_WORKSPACE_ID,
                "game_id": "skyrim-se",
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _write_session(
    workspace: Path,
    *,
    workspace_id: str,
    game_id: str = "skyrim-se",
) -> None:
    digest = "a" * 64
    (workspace / ".workflow").mkdir(exist_ok=True)
    (workspace / ".workflow" / "smt-session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": workspace_id,
                "mod_name": "ExampleMod",
                "game_id": game_id,
                "fingerprint_algorithm": "smt-input-v1",
                "input_identity": f"smt-input-v1:{game_id}:zip:{digest}",
                "source_kind": "zip",
                "source_display_name": "ExampleMod.zip",
                "source_sha256": digest,
                "import_relative_path": "mod/ExampleMod.zip",
                "imported_sha256": digest,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _legacy_bsa(workspace: Path, *, valid_pin: bool = True) -> Path:
    root = workspace / "tools" / "BSAFileExtractor"
    root.mkdir(parents=True)
    (root / "BSAFileExtractor.py").write_text("print('legacy')\n", encoding="utf-8")
    spec = provisioning.GITHUB_ARCHIVES["BSAFileExtractor"]
    (root / ".skyrim-chs-tool.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "installed_at": "2026-01-01T00:00:00",
                "name": "BSAFileExtractor",
                "source_type": "github-archive",
                "url": spec["url"],
                "ref": spec["ref"] if valid_pin else "different",
                "archive_sha256": spec["sha256"],
            }
        ),
        encoding="utf-8",
    )
    return root


def _bsa_key():
    spec = provisioning.GITHUB_ARCHIVES["BSAFileExtractor"]
    return build_decoder_key(
        tool_name="BSAFileExtractor",
        pinned_ref=spec["ref"],
        source=spec["url"],
        archive_sha256=spec["sha256"],
        installer_schema=provisioning.INSTALLER_SCHEMA,
    )


def _write_import_proof(root: Path, entry_id: str) -> None:
    inventory = list(migration._proof_inventory(root))
    (root / migration.LEGACY_IMPORT_PROOF_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entry_id": entry_id,
                "entry_point": "BSAFileExtractor.py",
                "payload_inventory": inventory,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_exact_generated_legacy_candidate_imports_by_copy_only(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    legacy = _legacy_bsa(workspace)
    key = _bsa_key()
    _write_import_proof(legacy, key.entry_id)
    source_before = {
        path.relative_to(legacy).as_posix(): path.read_bytes()
        for path in legacy.rglob("*")
        if path.is_file()
    }
    discovery = migration.discover_legacy_candidates(workspace)
    assert discovery.blockers == ()
    candidate = discovery.candidates["decoder-bsafileextractor"]
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)

    published, diagnostics = migration.import_movable_legacy_entry(
        roots,
        candidate,
        key,
        entry_point="BSAFileExtractor.py",
        source={"type": "github-archive"},
        producer_version="test",
    )

    assert published is not None
    assert validate_entry(
        roots,
        key.tool_kind,
        key.key_digest,
        deep=True,
    ).healthy
    assert "imported by copy" in " ".join(diagnostics)
    assert source_before == {
        path.relative_to(legacy).as_posix(): path.read_bytes()
        for path in legacy.rglob("*")
        if path.is_file()
    }


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 no-delete sharing")
def test_legacy_copy_pins_source_identity_for_the_entire_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    victim = source / "tool.exe"
    victim.write_bytes(b"original")
    target = tmp_path / "target"
    real_copytree = shutil.copytree
    replacement_blocked = False

    def racing_copytree(*args: object, **kwargs: object) -> Path:
        nonlocal replacement_blocked
        try:
            victim.unlink()
        except OSError:
            replacement_blocked = True
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(migration.shutil, "copytree", racing_copytree)

    migration.copy_legacy_tree_safely(source, target)

    assert replacement_blocked is True
    assert (target / "tool.exe").read_bytes() == b"original"


def test_legacy_candidate_without_complete_inventory_falls_back(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _legacy_bsa(workspace)
    discovery = migration.discover_legacy_candidates(workspace)
    candidate = discovery.candidates["decoder-bsafileextractor"]
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)

    published, diagnostics = migration.import_movable_legacy_entry(
        roots,
        candidate,
        _bsa_key(),
        entry_point="BSAFileExtractor.py",
        source={"type": "github-archive"},
        producer_version="test",
    )

    assert published is None
    assert "normal provisioning is required" in " ".join(diagnostics)
    assert candidate.payload_root.is_dir()


def test_unknown_legacy_pin_is_preserved_and_blocks_auto_migration(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    legacy = _legacy_bsa(workspace, valid_pin=False)

    discovery = migration.discover_legacy_candidates(workspace)

    assert discovery.candidates == {}
    assert discovery.blockers
    assert legacy.is_dir()


def test_workspace_tool_config_rejects_invalid_utf8_as_a_managed_error(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    config = workspace / "config" / "tools.local.json"
    config.parent.mkdir()
    config.write_bytes(b"\xff")

    with pytest.raises(ManagedToolStoreError, match="valid UTF-8 JSON"):
        load_workspace_tool_config(workspace)


def test_valid_external_override_precedes_managed_binding(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "external" / "dotnet.exe"
    external.parent.mkdir()
    external.write_bytes(b"manual")
    resolution = classify_configured_tool_path(
        workspace,
        {"DecoderTools": {"DotNetSdkPath": str(external)}},
        "DotNetSdkPath",
    )
    assert resolution.provenance is ToolPathProvenance.USER_EXTERNAL
    assert resolution.path == external.resolve()


def test_reparse_at_an_exact_legacy_path_is_not_reclassified_as_external(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    external_tool = external / "dotnet.exe"
    external_tool.write_bytes(b"external")
    (external / ".skyrim-chs-tool.json").write_text(
        json.dumps(
            {
                "name": "dotnet-sdk",
                "source_type": "dotnet-install",
                "install_script_source": (
                    "vendored:scripts/vendor/dotnet-install.ps1"
                ),
                "install_script_sha256": (
                    "6585899aed55ff6ae13dbe1e8c3b878f2d00433520e7efbe250b75db948b7da9"
                ),
                "sdk_version": "8.0.422",
            }
        ),
        encoding="utf-8",
    )
    legacy_parent = workspace / "tools" / "dotnet-sdk"
    legacy_parent.mkdir(parents=True)
    legacy_tool = legacy_parent / "dotnet.exe"
    try:
        legacy_tool.symlink_to(external_tool)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    resolution = classify_configured_tool_path(
        workspace,
        {"DecoderTools": {"DotNetSdkPath": "tools/dotnet-sdk/dotnet.exe"}},
        "DotNetSdkPath",
    )

    assert resolution.provenance is ToolPathProvenance.LEGACY_UNKNOWN
    assert resolution.path == legacy_tool


def test_proven_legacy_path_yields_to_a_managed_binding(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".workflow").mkdir()
    legacy = _legacy_bsa(workspace)
    key = _bsa_key()
    _write_import_proof(legacy, key.entry_id)
    candidate = migration.discover_legacy_candidates(
        workspace
    ).candidates["decoder-bsafileextractor"]
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)
    published, _diagnostics = migration.import_movable_legacy_entry(
        roots,
        candidate,
        key,
        entry_point="BSAFileExtractor.py",
        source={"type": "github-archive"},
        producer_version="test",
    )
    assert published is not None
    binding = new_binding(
        workspace_id=TEST_WORKSPACE_ID,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "decoder-bsafileextractor",
                key.tool_kind,
                key.key_digest,
                "BSAFileExtractor.py",
            ),
        ),
    )
    bind_workspace(roots, workspace, binding)
    config = {
        "DecoderTools": {
            "BsaFileExtractorPath": (
                "tools/BSAFileExtractor/BSAFileExtractor.py"
            )
        }
    }

    diagnostic = resolve_tool_for_diagnostics(
        workspace,
        config,
        "BsaFileExtractorPath",
        roots=roots,
    )
    with leased_payload_path(
        workspace,
        config,
        "BsaFileExtractorPath",
        roots=roots,
    ) as runtime:
        assert diagnostic.provenance is ToolPathProvenance.MANAGED_BINDING
        assert runtime.provenance is ToolPathProvenance.MANAGED_BINDING
        assert runtime.path == published / "BSAFileExtractor.py"


def test_proven_legacy_path_is_not_a_diagnostic_or_runtime_fallback(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".workflow").mkdir()
    legacy = _legacy_bsa(workspace)
    key = _bsa_key()
    _write_import_proof(legacy, key.entry_id)
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)
    config = {
        "DecoderTools": {
            "BsaFileExtractorPath": (
                "tools/BSAFileExtractor/BSAFileExtractor.py"
            )
        }
    }

    diagnostic = resolve_tool_for_diagnostics(
        workspace,
        config,
        "BsaFileExtractorPath",
        roots=roots,
    )

    assert diagnostic.provenance is ToolPathProvenance.MISSING
    assert diagnostic.path is None
    assert diagnostic.diagnostics
    with pytest.raises((ManagedToolStoreError, ManagedProcessEnvironmentError)):
        with leased_payload_path(
            workspace,
            config,
            "BsaFileExtractorPath",
            roots=roots,
        ):
            pass


def test_managed_adapter_pairs_with_bound_sdk_despite_external_override(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".workflow").mkdir()
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)
    key = make_tool_key("dotnet-sdk", {"version": "test"})
    staging = create_staging_directory(roots, prefix="dotnet")
    managed_dotnet = staging / "dotnet.exe"
    managed_dotnet.write_bytes(b"managed")
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "test"},
        critical_entries=("dotnet.exe",),
        producer_version="test",
    )
    published = publish_movable_entry(roots, staging, manifest)
    binding = new_binding(
        workspace_id=TEST_WORKSPACE_ID,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "dotnet-sdk",
                key.tool_kind,
                key.key_digest,
                "dotnet.exe",
            ),
        ),
    )
    bind_workspace(roots, workspace, binding)
    external_dotnet = tmp_path / "external" / "dotnet.exe"
    external_dotnet.parent.mkdir()
    external_dotnet.write_bytes(b"external")
    external_adapter = tmp_path / "external" / "adapter.dll"
    external_adapter.write_bytes(b"adapter")
    managed_adapter_config = {
        "DecoderTools": {
            "DotNetSdkPath": str(external_dotnet),
            "MutagenCliPath": "scripts/invoke_mutagen_plugin_text_tool.py",
        }
    }
    external_adapter_config = {
        "DecoderTools": {
            "DotNetSdkPath": str(external_dotnet),
            "MutagenCliPath": str(external_adapter),
        }
    }

    assert adapter_uses_managed_binding(
        workspace,
        managed_adapter_config,
        "MutagenCliPath",
    )
    assert not adapter_uses_managed_binding(
        workspace,
        external_adapter_config,
        "MutagenCliPath",
    )
    with leased_payload_path(
        workspace,
        managed_adapter_config,
        "DotNetSdkPath",
        roots=roots,
        managed_only=True,
    ) as managed_runtime:
        assert managed_runtime.provenance is ToolPathProvenance.MANAGED_BINDING
        assert managed_runtime.path == published / "dotnet.exe"
    with leased_payload_path(
        workspace,
        external_adapter_config,
        "DotNetSdkPath",
        roots=roots,
    ) as external_runtime:
        assert external_runtime.provenance is ToolPathProvenance.USER_EXTERNAL
        assert external_runtime.path == external_dotnet


def test_resolver_rejects_healthy_wrong_kind_binding_entry(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".workflow").mkdir()
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)
    key = make_tool_key("decoder-not-an-adapter", {"version": "test"})
    staging = create_staging_directory(roots, prefix="wrong-kind")
    (staging / "SkyrimPluginTextTool.dll").write_bytes(b"not-an-adapter")
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "test"},
        critical_entries=("SkyrimPluginTextTool.dll",),
        producer_version="test",
    )
    publish_movable_entry(roots, staging, manifest)
    binding = new_binding(
        workspace_id=TEST_WORKSPACE_ID,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "adapter-skyrimplugintexttool",
                key.tool_kind,
                key.key_digest,
                "SkyrimPluginTextTool.dll",
            ),
        ),
    )
    bind_workspace(roots, workspace, binding)
    config = {
        "DecoderTools": {
            "MutagenCliPath": "scripts/invoke_mutagen_plugin_text_tool.py",
        }
    }

    diagnostic = resolve_tool_for_diagnostics(
        workspace,
        config,
        "MutagenCliPath",
        roots=roots,
    )

    assert diagnostic.provenance is ToolPathProvenance.MISSING
    assert any("binding identity differs" in item for item in diagnostic.diagnostics)
    binding_healthy, binding_diagnostics = managed_binding_health(
        workspace,
        roots=roots,
    )
    assert binding_healthy is False
    assert any(
        "binding identity differs" in item for item in binding_diagnostics
    )
    with pytest.raises(
        ManagedToolStoreError,
        match="binding identity differs",
    ):
        with leased_payload_path(
            workspace,
            config,
            "MutagenCliPath",
            roots=roots,
        ):
            pass


def test_binding_health_reuses_deep_snapshot_without_live_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".workflow").mkdir()
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)
    key = make_tool_key("decoder-bsafileextractor", {"version": "test"})
    staging = create_staging_directory(roots, prefix="snapshot-health")
    (staging / "BSAFileExtractor.py").write_bytes(b"managed decoder")
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "test"},
        critical_entries=("BSAFileExtractor.py",),
        producer_version="test",
    )
    publish_movable_entry(roots, staging, manifest)
    binding = new_binding(
        workspace_id=TEST_WORKSPACE_ID,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "decoder-bsafileextractor",
                key.tool_kind,
                key.key_digest,
                "BSAFileExtractor.py",
            ),
        ),
    )
    bind_workspace(roots, workspace, binding)
    snapshot = (
        {
            "entry_id": key.entry_id,
            "tool_kind": key.tool_kind,
            "key_digest": key.key_digest,
            "status": "healthy",
            "key_inputs": dict(manifest.key_inputs),
            "critical_entries": list(manifest.critical_entries),
        },
    )

    def unexpected_live_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("doctor snapshot health must not resolve the live entry")

    monkeypatch.setattr(
        resolver,
        "resolve_bound_entry",
        unexpected_live_resolution,
    )

    assert managed_binding_health(
        workspace,
        roots=roots,
        entry_snapshot=snapshot,
    ) == (True, ())


def test_diagnostics_and_runtime_reject_foreign_workspace_binding(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".workflow").mkdir()
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)
    key = make_tool_key("decoder-bsafileextractor", {"version": "test"})
    staging = create_staging_directory(roots, prefix="foreign-binding")
    (staging / "BSAFileExtractor.py").write_bytes(b"managed decoder")
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "test"},
        critical_entries=("BSAFileExtractor.py",),
        producer_version="test",
    )
    publish_movable_entry(roots, staging, manifest)
    binding = new_binding(
        workspace_id="74b45dc8-d1a1-46b6-8e37-2c648019b789",
        game_id="fallout4",
        entries=(
            WorkspaceBindingEntry(
                "decoder-bsafileextractor",
                key.tool_kind,
                key.key_digest,
                "BSAFileExtractor.py",
            ),
        ),
    )
    bind_workspace(roots, workspace, binding)
    config = {
        "DecoderTools": {
            "BsaFileExtractorPath": "scripts/invoke_bsa_file_extractor_safe.py",
        }
    }

    diagnostic = resolve_tool_for_diagnostics(
        workspace,
        config,
        "BsaFileExtractorPath",
        roots=roots,
    )
    assert diagnostic.provenance is ToolPathProvenance.MISSING
    assert any(
        "identity differs" in item for item in diagnostic.diagnostics
    )
    binding_healthy, binding_diagnostics = managed_binding_health(
        workspace,
        roots=roots,
    )
    assert binding_healthy is False
    assert any("identity differs" in item for item in binding_diagnostics)
    with pytest.raises(ManagedToolStoreError, match="identity differs"):
        with leased_payload_path(
            workspace,
            config,
            "BsaFileExtractorPath",
            roots=roots,
        ):
            pass


def test_legacy_marker_uses_matching_session_identity_without_read_only_mutation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    marker_path = workspace / ".skyrim-chs-workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.pop("workspace_id")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    _write_session(workspace, workspace_id=TEST_WORKSPACE_ID)
    before = marker_path.read_bytes()
    store_base = tmp_path / "store"
    store_base.mkdir()
    roots = resolve_managed_store_roots(store_base)
    ensure_store_layout(roots)
    key = make_tool_key("decoder-bsafileextractor", {"version": "legacy-session"})
    staging = create_staging_directory(roots, prefix="legacy-session")
    (staging / "BSAFileExtractor.py").write_bytes(b"managed decoder")
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "test"},
        critical_entries=("BSAFileExtractor.py",),
        producer_version="test",
    )
    published = publish_movable_entry(roots, staging, manifest)
    bind_workspace(
        roots,
        workspace,
        new_binding(
            workspace_id=TEST_WORKSPACE_ID,
            game_id="skyrim-se",
            entries=(
                WorkspaceBindingEntry(
                    "decoder-bsafileextractor",
                    key.tool_kind,
                    key.key_digest,
                    "BSAFileExtractor.py",
                ),
            ),
        ),
    )
    config = {
        "DecoderTools": {
            "BsaFileExtractorPath": "scripts/invoke_bsa_file_extractor_safe.py",
        }
    }

    diagnostic = resolve_tool_for_diagnostics(
        workspace,
        config,
        "BsaFileExtractorPath",
        roots=roots,
    )
    assert diagnostic.provenance is ToolPathProvenance.PLUGIN_WRAPPER
    assert diagnostic.usable is True
    with leased_payload_path(
        workspace,
        config,
        "BsaFileExtractorPath",
        roots=roots,
    ) as leased:
        assert leased.path == published / "BSAFileExtractor.py"
    assert marker_path.read_bytes() == before


@pytest.mark.parametrize(
    ("relative_path", "adapter_marker", "sdk_marker"),
    (
        (
            "scripts/invoke_mutagen_plugin_text_tool.py",
            "adapter_dll = ensure_adapter_dll(root, tool_config, leases)",
            "resolved_dotnet = dotnet_path(root, tool_config, leases)",
        ),
        (
            "scripts/invoke_mutagen_pex_string_tool.py",
            "adapter_dll = ensure_adapter_dll(root, tool_config, leases)",
            "resolved_dotnet = dotnet_path(root, tool_config, leases)",
        ),
        (
            "scripts/invoke_bethesda_string_table_tool.py",
            "adapter_dll = ensure_adapter_dll(root, tool_config, leases)",
            "resolved_dotnet = configured_dotnet_path(root, tool_config, leases)",
        ),
        (
            "scripts/export_esp_strings.py",
            '"MutagenCliPath",\n        command="export plugin strings"',
            '"DotNetSdkPath",\n        command="export plugin strings"',
        ),
        (
            "scripts/invoke_bethesda_localized_delivery.py",
            '"MutagenCliPath",\n        command="run localized plugin inventory"',
            '"DotNetSdkPath",\n        command="run localized plugin inventory"',
        ),
    ),
)
def test_managed_adapter_runtime_acquires_adapter_before_sdk(
    relative_path: str,
    adapter_marker: str,
    sdk_marker: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    adapter_index = source.index(adapter_marker)
    sdk_index = source.index(sdk_marker, adapter_index)

    assert adapter_index < sdk_index

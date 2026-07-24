from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import managed_tool_store as managed_store  # noqa: E402
from managed_tool_store import (  # noqa: E402
    ENTRY_MANIFEST_NAME,
    HEALTHY_COMMIT_NAME,
    CatalogReference,
    EntryManifest,
    EntryStatus,
    ManagedToolPathError,
    ManagedToolSchemaError,
    ManagedToolStoreError,
    ReferenceStatus,
    WorkspaceBinding,
    WorkspaceBindingEntry,
    atomic_create_json_no_replace,
    bind_workspace,
    build_adapter_key,
    build_decoder_key,
    build_dotnet_sdk_key,
    build_payload_inventory,
    build_python_key,
    canonical_sha256,
    commit_entry,
    ensure_store_layout,
    entry_directory,
    load_catalog,
    leased_bound_entry,
    make_entry_manifest,
    make_tool_key,
    managed_path,
    new_binding,
    normalize_relative_path,
    publish_movable_entry,
    read_workspace_binding,
    reconcile_catalog_references,
    referenced_entry_ids,
    reserve_catalog_reference,
    resolve_bound_entry,
    resolve_managed_store_roots,
    validate_entry,
    write_workspace_binding,
    write_manifest,
)
from smt_windows import (  # noqa: E402
    ManagedProcessEnvironmentError,
    remove_regular_tree,
)


def _roots(tmp_path: Path):
    return resolve_managed_store_roots(tmp_path)


def _healthy_entry(
    tmp_path: Path,
    *,
    tool_kind: str = "decoder-example",
    payload_name: str = "tool.exe",
):
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    key = make_tool_key(tool_kind, {"version": "1.0.0"})
    target = entry_directory(roots, key.tool_kind, key.key_digest)
    target.mkdir(parents=True)
    (target / payload_name).write_bytes(b"payload")
    manifest = make_entry_manifest(
        key=key,
        entry_root=target,
        source={"kind": "test"},
        critical_entries=[payload_name],
        producer_version="test",
    )
    write_manifest(target, manifest, entries_root=roots.entries)
    commit_entry(target, manifest, entries_root=roots.entries)
    return roots, key, target, manifest


def test_roots_are_separate_and_resolution_is_read_only(tmp_path: Path) -> None:
    roots = _roots(tmp_path)

    assert roots.payload == (
        tmp_path / "SkyrimModTranslation" / "managed-tools" / "v1"
    )
    assert roots.control == (
        tmp_path / "SkyrimModTranslation" / "managed-tool-state" / "v1"
    )
    assert not roots.application.exists()

    ensure_store_layout(roots)

    assert roots.entries.is_dir()
    assert roots.locks.is_dir()
    assert roots.payload.parent != roots.control.parent


def test_canonical_key_is_order_independent_and_backend_sensitive() -> None:
    first = make_tool_key("decoder-example", {"version": "1", "arch": "x64"})
    second = make_tool_key("decoder-example", {"arch": "x64", "version": "1"})
    assert first.key_digest == second.key_digest
    assert canonical_sha256({"b": [2, 1], "a": True}) == canonical_sha256(
        {"a": True, "b": [2, 1]}
    )

    common = {
        "implementation": "CPython",
        "full_version": "3.14.4",
        "architecture": "AMD64",
        "base_interpreter_identity": {"sha256": "a" * 64},
        "runtime_lock_sha256": "b" * 64,
        "installer_backend_version": "1.0",
        "installer_schema": 1,
    }
    uv_key = build_python_key(installer_backend="uv", **common)
    pip_key = build_python_key(installer_backend="pip", **common)
    assert uv_key.key_digest != pip_key.key_digest


def test_decoder_key_rejects_unpinned_digest() -> None:
    with pytest.raises(ManagedToolSchemaError, match="SHA-256"):
        build_decoder_key(
            tool_name="example",
            pinned_ref="v1",
            source="https://example.invalid/tool.zip",
            archive_sha256="latest",
            installer_schema=1,
        )


def test_dotnet_and_adapter_keys_change_with_identity_inputs() -> None:
    first_sdk = build_dotnet_sdk_key(
        version="8.0.100",
        architecture="x64",
        source="https://example.invalid/dotnet.zip",
        package_sha256="c" * 64,
        installer_schema=1,
    )
    second_sdk = build_dotnet_sdk_key(
        version="8.0.101",
        architecture="x64",
        source="https://example.invalid/dotnet.zip",
        package_sha256="d" * 64,
        installer_schema=1,
    )
    common = {
        "adapter_name": "example",
        "project_digest": "e" * 64,
        "configuration": "Release",
        "target_framework": "net8.0",
        "rid": "win-x64",
        "architecture": "x64",
        "installer_schema": 1,
    }
    first = build_adapter_key(
        source_digest="f" * 64,
        sdk_entry_id=first_sdk.entry_id,
        **common,
    )
    changed_source = build_adapter_key(
        source_digest="a" * 64,
        sdk_entry_id=first_sdk.entry_id,
        **common,
    )
    changed_sdk = build_adapter_key(
        source_digest="f" * 64,
        sdk_entry_id=second_sdk.entry_id,
        **common,
    )

    assert first.key_digest != changed_source.key_digest
    assert first.key_digest != changed_sdk.key_digest


@pytest.mark.parametrize(
    "value",
    (
        "../escape",
        "/absolute",
        r"C:\escape",
        "folder//file",
        "folder/../file",
        "folder/trailing.",
        "folder/file:stream",
        "CON/file",
    ),
)
def test_managed_relative_path_rejects_windows_aliases(value: str) -> None:
    with pytest.raises(ManagedToolPathError):
        normalize_relative_path(value)


def test_managed_path_rejects_escape_and_reparse(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    with pytest.raises(ManagedToolPathError):
        managed_path(roots.payload, "..\\outside")

    target = roots.entries / "link"
    try:
        target.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(ManagedToolPathError, match="reparse|symlink|junction"):
        managed_path(
            roots.entries,
            "link",
            must_exist=True,
            kind="directory",
        )


def test_regular_tree_removal_rejects_reparse_without_touching_external(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    safe_file = tree / "safe.txt"
    safe_file.write_text("safe", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    link = tree / "redirect"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(
        ManagedProcessEnvironmentError,
        match="unsafe directory",
    ):
        remove_regular_tree(tree, tmp_path, label="test tree")

    assert safe_file.is_file()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_manifest_and_commit_produce_a_healthy_entry(tmp_path: Path) -> None:
    roots, key, target, manifest = _healthy_entry(tmp_path)

    result = validate_entry(roots, key.tool_kind, key.key_digest, deep=True)

    assert result.status is EntryStatus.HEALTHY
    assert result.manifest == manifest
    assert (target / ENTRY_MANIFEST_NAME).is_file()
    assert (target / HEALTHY_COMMIT_NAME).is_file()


def test_manifest_rejects_key_inputs_that_do_not_match_digest(
    tmp_path: Path,
) -> None:
    _roots_value, _key, _target, manifest = _healthy_entry(tmp_path)
    payload = manifest.to_payload()
    payload["key_inputs"] = {"version": "forged"}

    with pytest.raises(
        ManagedToolSchemaError,
        match="canonical key inputs",
    ):
        EntryManifest.from_payload(payload)


def test_manifest_rejects_a_timestamp_without_timezone(tmp_path: Path) -> None:
    _roots_value, _key, _target, manifest = _healthy_entry(tmp_path)
    payload = manifest.to_payload()
    payload["created_at"] = "2026-01-01T00:00:00"

    with pytest.raises(ManagedToolSchemaError, match="timezone"):
        EntryManifest.from_payload(payload)


def test_manifest_rejects_duplicate_and_reserved_inventory_paths(
    tmp_path: Path,
) -> None:
    _roots_value, _key, _target, manifest = _healthy_entry(tmp_path)
    duplicate = manifest.to_payload()
    duplicate["payload_inventory"].append(
        dict(duplicate["payload_inventory"][0])
    )
    with pytest.raises(ManagedToolPathError, match="duplicated"):
        EntryManifest.from_payload(duplicate)

    reserved = manifest.to_payload()
    reserved["payload_inventory"][0]["path"] = ENTRY_MANIFEST_NAME
    with pytest.raises(
        ManagedToolSchemaError,
        match="cannot include managed entry metadata",
    ):
        EntryManifest.from_payload(reserved)


def test_fast_and_deep_validation_detect_different_damage(tmp_path: Path) -> None:
    roots, key, target, _manifest = _healthy_entry(tmp_path)
    (target / "unrecorded.txt").write_text("extra", encoding="utf-8")

    assert validate_entry(roots, key.tool_kind, key.key_digest).healthy
    deep = validate_entry(roots, key.tool_kind, key.key_digest, deep=True)

    assert deep.status is EntryStatus.DAMAGED
    assert "inventory" in " ".join(deep.diagnostics)


def test_commit_rejects_manifest_change(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    key = make_tool_key("decoder-example", {"version": "1"})
    target = entry_directory(roots, key.tool_kind, key.key_digest)
    target.mkdir(parents=True)
    (target / "tool.exe").write_bytes(b"payload")
    manifest = make_entry_manifest(
        key=key,
        entry_root=target,
        source={"kind": "test"},
        critical_entries=["tool.exe"],
        producer_version="test",
    )
    write_manifest(target, manifest, entries_root=roots.entries)
    payload = json.loads((target / ENTRY_MANIFEST_NAME).read_text(encoding="utf-8"))
    payload["producer_version"] = "changed"
    (target / ENTRY_MANIFEST_NAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(Exception, match="changed before commit"):
        commit_entry(target, manifest, entries_root=roots.entries)


def test_atomic_commit_is_no_replace(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    target = roots.control / "commit.json"
    atomic_create_json_no_replace(
        target,
        {"schema_version": 1},
        allowed_root=roots.control,
    )

    with pytest.raises(FileExistsError):
        atomic_create_json_no_replace(
            target,
            {"schema_version": 1},
            allowed_root=roots.control,
        )


def test_movable_publication_uses_verified_final_entry(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    staging = roots.staging / "decoder-test"
    staging.mkdir()
    (staging / "tool.exe").write_bytes(b"payload")
    key = make_tool_key("decoder-example", {"version": "1"})
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"kind": "test"},
        critical_entries=["tool.exe"],
        producer_version="test",
    )

    published = publish_movable_entry(roots, staging, manifest)

    assert published == entry_directory(roots, key.tool_kind, key.key_digest)
    assert not staging.exists()
    assert validate_entry(roots, key.tool_kind, key.key_digest, deep=True).healthy


def test_pending_reference_protects_entries_before_binding(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    workspace_id = str(uuid.uuid4())
    generation = str(uuid.uuid4())
    entry_id = f"decoder-example:{'a' * 64}"

    reference = reserve_catalog_reference(
        roots,
        workspace_id=workspace_id,
        workspace_path=tmp_path / "workspace",
        game_id="skyrim-se",
        generation=generation,
        entry_ids=[entry_id],
    )
    catalog = load_catalog(roots)

    assert reference.status is ReferenceStatus.PENDING
    assert entry_id in referenced_entry_ids(catalog)


def test_repeated_reservation_refreshes_registered_workspace_path(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    workspace_id = str(uuid.uuid4())
    generation = str(uuid.uuid4())
    entry_id = f"decoder-example:{'a' * 64}"
    first_workspace = tmp_path / "old-location"
    moved_workspace = tmp_path / "new-location"
    reserve_catalog_reference(
        roots,
        workspace_id=workspace_id,
        workspace_path=first_workspace,
        game_id="skyrim-se",
        generation=generation,
        entry_ids=[entry_id],
    )

    refreshed = reserve_catalog_reference(
        roots,
        workspace_id=workspace_id,
        workspace_path=moved_workspace,
        game_id="skyrim-se",
        generation=generation,
        entry_ids=[entry_id],
    )

    assert refreshed.workspace_path == str(moved_workspace.resolve(strict=False))
    stored = CatalogReference.from_payload(
        load_catalog(roots)["references"][refreshed.reference_id]
    )
    assert stored.workspace_path == refreshed.workspace_path


def test_binding_transaction_promotes_reference_and_resolves_entry(
    tmp_path: Path,
) -> None:
    roots, key, target, _manifest = _healthy_entry(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    workspace_id = str(uuid.uuid4())
    binding = new_binding(
        workspace_id=workspace_id,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                logical_name="ExampleTool",
                tool_kind=key.tool_kind,
                key_digest=key.key_digest,
                entry_point="tool.exe",
            ),
        ),
    )

    bind_workspace(roots, workspace, binding)
    loaded = read_workspace_binding(workspace)
    resolved, selected = resolve_bound_entry(roots, workspace, "exampletool")
    catalog = load_catalog(roots)
    references = [
        CatalogReference.from_payload(value)
        for value in catalog["references"].values()
    ]

    assert loaded == binding
    assert selected.entry_id == key.entry_id
    assert resolved == target / "tool.exe"
    assert [reference.status for reference in references] == [ReferenceStatus.ACTIVE]


def test_runtime_lease_nests_entry_lock_under_lifecycle_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, key, target, _manifest = _healthy_entry(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    binding = new_binding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "example",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )
    write_workspace_binding(workspace, binding)
    events: list[str] = []

    @contextmanager
    def lifecycle_guard(*_args: object, **_kwargs: object):
        events.append("enter-lifecycle")
        try:
            yield
        finally:
            events.append("exit-lifecycle")

    @contextmanager
    def entry_guard(*_args: object, **_kwargs: object):
        events.append("enter-entry")
        try:
            yield
        finally:
            events.append("exit-entry")

    monkeypatch.setattr(managed_store, "store_lifecycle_lock", lifecycle_guard)
    monkeypatch.setattr(managed_store, "entry_lock", entry_guard)

    with leased_bound_entry(roots, workspace, "example") as (path, _entry):
        assert path == target / "tool.exe"
        assert events == ["enter-lifecycle", "enter-entry"]

    assert events == [
        "enter-lifecycle",
        "enter-entry",
        "exit-entry",
        "exit-lifecycle",
    ]


def test_binding_rejects_adapter_sdk_dependency_mismatch(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    sdk_key = make_tool_key("dotnet-sdk", {"version": "one"})
    other_sdk_key = make_tool_key("dotnet-sdk", {"version": "two"})
    adapter_key = build_adapter_key(
        adapter_name="ExampleAdapter",
        source_digest="a" * 64,
        project_digest="b" * 64,
        sdk_entry_id=other_sdk_key.entry_id,
        configuration="Release",
        target_framework="net8.0",
        rid="portable",
        architecture="amd64",
        installer_schema=1,
    )

    def publish(key: object, entry_point: str) -> None:
        target = entry_directory(roots, key.tool_kind, key.key_digest)
        target.mkdir(parents=True)
        (target / entry_point).write_bytes(b"payload")
        manifest = make_entry_manifest(
            key=key,
            entry_root=target,
            source={"kind": "test"},
            critical_entries=(entry_point,),
            producer_version="test",
        )
        write_manifest(target, manifest, entries_root=roots.entries)
        commit_entry(target, manifest, entries_root=roots.entries)

    publish(sdk_key, "dotnet.exe")
    publish(adapter_key, "ExampleAdapter.dll")
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    binding = new_binding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "dotnet-sdk",
                sdk_key.tool_kind,
                sdk_key.key_digest,
                "dotnet.exe",
            ),
            WorkspaceBindingEntry(
                "adapter-exampleadapter",
                adapter_key.tool_kind,
                adapter_key.key_digest,
                "ExampleAdapter.dll",
            ),
        ),
    )

    with pytest.raises(
        ManagedToolStoreError,
        match="SDK dependency differs",
    ):
        bind_workspace(roots, workspace, binding)

    write_workspace_binding(workspace, binding)
    with pytest.raises(
        ManagedToolStoreError,
        match="SDK dependency differs",
    ):
        resolve_bound_entry(
            roots,
            workspace,
            "adapter-exampleadapter",
        )


def test_binding_rejects_noncritical_entry_point(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    key = make_tool_key("decoder-example", {"version": "1.0.0"})
    target = entry_directory(roots, key.tool_kind, key.key_digest)
    target.mkdir(parents=True)
    (target / "tool.exe").write_bytes(b"critical")
    (target / "support.exe").write_bytes(b"non-critical")
    manifest = make_entry_manifest(
        key=key,
        entry_root=target,
        source={"kind": "test"},
        critical_entries=("tool.exe",),
        producer_version="test",
    )
    write_manifest(target, manifest, entries_root=roots.entries)
    commit_entry(target, manifest, entries_root=roots.entries)

    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    binding = new_binding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "example",
                key.tool_kind,
                key.key_digest,
                "support.exe",
            ),
        ),
    )

    with pytest.raises(
        ManagedToolStoreError,
        match="not a verified critical entry",
    ):
        bind_workspace(roots, workspace, binding)

    write_workspace_binding(workspace, binding)
    with pytest.raises(
        ManagedToolStoreError,
        match="not a verified critical entry",
    ):
        resolve_bound_entry(roots, workspace, "example")


def test_binding_rejects_invalid_schema_before_reservation(tmp_path: Path) -> None:
    roots, key, _target, _manifest = _healthy_entry(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    entry = WorkspaceBindingEntry(
        "example",
        key.tool_kind,
        key.key_digest,
        "tool.exe",
    )
    binding = new_binding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        entries=(entry, entry),
    )

    with pytest.raises(
        ManagedToolSchemaError,
        match="duplicate logical names",
    ):
        bind_workspace(roots, workspace, binding)

    assert load_catalog(roots)["references"] == {}
    assert not (workspace / ".workflow" / "managed-tools.json").exists()


def test_successful_binding_retains_an_unresolved_pending_generation(
    tmp_path: Path,
) -> None:
    roots, key, _target, _manifest = _healthy_entry(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    workspace_id = str(uuid.uuid4())
    pending = reserve_catalog_reference(
        roots,
        workspace_id=workspace_id,
        workspace_path=workspace,
        game_id="skyrim-se",
        generation=str(uuid.uuid4()),
        entry_ids=(key.entry_id,),
    )
    binding = new_binding(
        workspace_id=workspace_id,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "example",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )

    bind_workspace(roots, workspace, binding)

    references = {
        reference_id: CatalogReference.from_payload(payload)
        for reference_id, payload in load_catalog(roots)["references"].items()
    }
    assert references[pending.reference_id].status is ReferenceStatus.PENDING
    assert (
        references[f"{binding.workspace_id}:{binding.generation}"].status
        is ReferenceStatus.ACTIVE
    )


def test_concurrent_same_workspace_binding_keeps_final_generation_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, key, _target, _manifest = _healthy_entry(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    workspace_id = str(uuid.uuid4())
    entry = WorkspaceBindingEntry(
        "example",
        key.tool_kind,
        key.key_digest,
        "tool.exe",
    )
    first = new_binding(
        workspace_id=workspace_id,
        game_id="skyrim-se",
        entries=(entry,),
    )
    second = new_binding(
        workspace_id=workspace_id,
        game_id="skyrim-se",
        entries=(entry,),
    )
    local_catalog_lock = threading.Lock()

    @contextmanager
    def serialized_catalog_lock(*_args: object, **_kwargs: object):
        with local_catalog_lock:
            yield

    monkeypatch.setattr(
        managed_store,
        "catalog_lock",
        serialized_catalog_lock,
    )
    original_write = managed_store.write_workspace_binding
    first_written = threading.Event()
    second_finished = threading.Event()

    def controlled_write(
        selected_workspace: Path,
        binding: WorkspaceBinding,
    ) -> Path:
        path = original_write(selected_workspace, binding)
        if binding.generation == first.generation:
            first_written.set()
            second_finished.wait(timeout=0.5)
        return path

    monkeypatch.setattr(
        managed_store,
        "write_workspace_binding",
        controlled_write,
    )
    failures: list[BaseException] = []

    def commit(binding: WorkspaceBinding) -> None:
        try:
            bind_workspace(roots, workspace, binding)
        except BaseException as exc:
            failures.append(exc)
        finally:
            if binding.generation == second.generation:
                second_finished.set()

    first_thread = threading.Thread(target=commit, args=(first,))
    second_thread = threading.Thread(target=commit, args=(second,))
    first_thread.start()
    assert first_written.wait(timeout=2.0)
    second_thread.start()
    first_thread.join(timeout=5.0)
    second_thread.join(timeout=5.0)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert failures == []
    final_binding = read_workspace_binding(workspace)
    references = {
        reference_id: CatalogReference.from_payload(payload)
        for reference_id, payload in load_catalog(roots)["references"].items()
    }
    active_generations = {
        reference.generation
        for reference in references.values()
        if reference.status is ReferenceStatus.ACTIVE
    }
    assert final_binding.generation == second.generation
    assert active_generations == {final_binding.generation}
    assert (
        references[f"{workspace_id}:{first.generation}"].status
        is ReferenceStatus.STALE
    )


def test_workspace_binding_rejects_unknown_schema_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    payload = WorkspaceBinding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        generation=str(uuid.uuid4()),
        generated_at="2026-01-01T00:00:00+00:00",
        validation_level="complete",
        validation_result="healthy",
        entries=(),
    ).to_payload()
    payload["unexpected"] = True

    with pytest.raises(ManagedToolSchemaError, match="extra"):
        WorkspaceBinding.from_payload(payload)


def test_workspace_binding_rejects_non_healthy_validation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    payload = WorkspaceBinding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        generation=str(uuid.uuid4()),
        generated_at="2026-01-01T00:00:00+00:00",
        validation_level="complete",
        validation_result="healthy",
        entries=(),
    ).to_payload()
    payload["validation_result"] = "partial"

    with pytest.raises(ManagedToolSchemaError, match="complete and healthy"):
        WorkspaceBinding.from_payload(payload)


def test_catalog_reference_rejects_duplicate_entries_and_relative_workspace(
    tmp_path: Path,
) -> None:
    entry_id = f"decoder-example:{'a' * 64}"
    reference = CatalogReference(
        workspace_id=str(uuid.uuid4()),
        workspace_path=str((tmp_path / "workspace").resolve(strict=False)),
        game_id="skyrim-se",
        generation=str(uuid.uuid4()),
        status=ReferenceStatus.PENDING,
        entry_ids=(entry_id,),
        updated_at="2026-01-01T00:00:00+00:00",
    )
    duplicate = reference.to_payload()
    duplicate["entry_ids"].append(entry_id)
    with pytest.raises(ManagedToolSchemaError, match="duplicate entry"):
        CatalogReference.from_payload(duplicate)

    relative = reference.to_payload()
    relative["workspace_path"] = "relative-workspace"
    with pytest.raises(ManagedToolSchemaError, match="must be absolute"):
        CatalogReference.from_payload(relative)


def test_binding_rejects_an_unavailable_entry(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    ensure_store_layout(roots)
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    missing = make_tool_key("decoder-example", {"version": "missing"})
    binding = new_binding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "example",
                missing.tool_kind,
                missing.key_digest,
                "tool.exe",
            ),
        ),
    )

    with pytest.raises(ManagedToolStoreError, match="cannot bind unavailable"):
        bind_workspace(roots, workspace, binding)
    assert not (workspace / ".workflow" / "managed-tools.json").exists()
    reference = CatalogReference.from_payload(
        load_catalog(roots)["references"][
            f"{binding.workspace_id}:{binding.generation}"
        ]
    )
    assert reference.status is ReferenceStatus.PENDING
    assert reference.entry_ids == (missing.entry_id,)


def test_deep_inventory_rejects_multiple_hardlinks(tmp_path: Path) -> None:
    entry = tmp_path / "entry"
    entry.mkdir()
    first = entry / "first.bin"
    second = entry / "second.bin"
    first.write_bytes(b"same file")
    try:
        os.link(first, second)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="hardlinks"):
        build_payload_inventory(entry)


def test_pending_reference_is_retained_without_binding_then_promoted_by_exact_binding(
    tmp_path: Path,
) -> None:
    roots, key, _target, _manifest = _healthy_entry(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".workflow").mkdir(parents=True)
    binding = new_binding(
        workspace_id=str(uuid.uuid4()),
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "example",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )
    reference = reserve_catalog_reference(
        roots,
        workspace_id=binding.workspace_id,
        workspace_path=workspace,
        game_id=binding.game_id,
        generation=binding.generation,
        entry_ids=(key.entry_id,),
    )

    first = reconcile_catalog_references(roots)
    assert reference.reference_id in first["retained"]
    current = CatalogReference.from_payload(
        load_catalog(roots)["references"][reference.reference_id]
    )
    assert current.status is ReferenceStatus.PENDING

    write_workspace_binding(workspace, binding)
    second = reconcile_catalog_references(roots)
    assert reference.reference_id in second["promoted"]
    promoted = CatalogReference.from_payload(
        load_catalog(roots)["references"][reference.reference_id]
    )
    assert promoted.status is ReferenceStatus.ACTIVE

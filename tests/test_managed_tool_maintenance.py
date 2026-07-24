from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import managed_tool_maintenance as maintenance  # noqa: E402
from managed_tool_maintenance import (  # noqa: E402
    apply_plan,
    create_plan,
    inspect_store,
)
from managed_tool_store import (  # noqa: E402
    CatalogReference,
    MaintenancePlan,
    ManagedToolStoreError,
    ReferenceStatus,
    WorkspaceBindingEntry,
    bind_workspace,
    commit_entry,
    create_staging_directory,
    ensure_store_layout,
    load_catalog,
    make_entry_manifest,
    make_tool_key,
    new_binding,
    publish_movable_entry,
    resolve_managed_store_roots,
    store_lifecycle_lock,
    validate_entry,
    write_manifest,
    write_workspace_binding,
    write_catalog,
)
from smt_windows import SmtProcessFileLock  # noqa: E402


def roots(tmp_path: Path):
    return resolve_managed_store_roots(tmp_path)


def publish_dummy_entry(tmp_path: Path, name: str = "tool"):
    store = roots(tmp_path)
    ensure_store_layout(store)
    key = make_tool_key("test-tool", {"name": name, "version": "1"})
    staging = create_staging_directory(store, prefix="test-tool")
    executable = staging / "tool.exe"
    executable.write_bytes(name.encode("utf-8"))
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "test"},
        critical_entries=("tool.exe",),
        producer_version="test",
    )
    path = publish_movable_entry(store, staging, manifest)
    return store, key, path


def make_workspace(
    tmp_path: Path,
    *,
    game_id: str = "skyrim-se",
    workspace_id: str | None = None,
) -> Path:
    workspace = tmp_path / f"workspace-{uuid.uuid4()}"
    (workspace / ".workflow").mkdir(parents=True)
    (workspace / "config").mkdir()
    marker = {
        "schema_version": 2,
        "kind": "bethesda-mod-chs-translation-workspace",
        "workspace_id": workspace_id or str(uuid.uuid4()),
        "game_id": game_id,
    }
    (workspace / ".skyrim-chs-workspace.json").write_text(
        json.dumps(marker),
        encoding="utf-8",
    )
    return workspace


def write_session(
    workspace: Path,
    *,
    workspace_id: str,
    game_id: str = "skyrim-se",
) -> None:
    digest = "a" * 64
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


def test_absent_store_inspection_is_strictly_read_only(tmp_path: Path) -> None:
    store = roots(tmp_path)
    result = inspect_store(store)
    assert result.payload_exists is False
    assert result.control_exists is False
    assert not store.application.exists()


@pytest.mark.parametrize(
    ("root_name", "diagnostic_prefix"),
    (
        ("payload", "managed-payload-unplannable:"),
        ("control", "managed-reference-unplannable:"),
    ),
)
def test_non_directory_store_root_is_reported_as_unsafe(
    tmp_path: Path,
    root_name: str,
    diagnostic_prefix: str,
) -> None:
    store = roots(tmp_path)
    target = getattr(store, root_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unexpected", encoding="utf-8")

    result = inspect_store(store)

    assert getattr(result, f"{root_name}_exists") is True
    assert any(
        item.startswith(diagnostic_prefix)
        for item in result.diagnostics
    )


@pytest.mark.parametrize("child_name", ("entries", "staging", "trash"))
def test_non_directory_payload_child_is_reported_as_unsafe(
    tmp_path: Path,
    child_name: str,
) -> None:
    store = roots(tmp_path)
    ensure_store_layout(store)
    target = getattr(store, child_name)
    target.rmdir()
    target.write_text("unexpected", encoding="utf-8")

    result = inspect_store(store)

    assert any(
        item.startswith("managed-payload-unplannable:")
        and child_name in item
        for item in result.diagnostics
    )


def test_unused_plan_and_apply_remove_only_unreferenced_entry(tmp_path: Path) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("clean-unused", roots=store)
    selected = [
        row
        for row in plan.candidates
        if row.get("candidate_type") == "entry" and row.get("included")
    ]
    assert [row["entry_id"] for row in selected] == [key.entry_id]

    result = apply_plan(
        plan.plan_id,
        plan.confirmation_token,
        roots=store,
    )
    assert result.outcome == "success"
    assert result.removed_entry_ids == (key.entry_id,)
    assert not path.exists()
    assert store.control.is_dir()


def test_corrupt_catalog_blocks_cleanup_instead_of_assuming_no_references(
    tmp_path: Path,
) -> None:
    store, _key, path = publish_dummy_entry(tmp_path)
    store.catalog.write_text("{invalid", encoding="utf-8")

    inspection = inspect_store(store)
    assert any(
        "managed-reference-unplannable:" in item
        for item in inspection.diagnostics
    )
    with pytest.raises(
        ManagedToolStoreError,
        match="cannot safely determine registered references",
    ):
        create_plan("clean-unused", roots=store)
    assert path.is_dir()


def test_non_file_catalog_blocks_cleanup_instead_of_looking_empty(
    tmp_path: Path,
) -> None:
    store, _key, path = publish_dummy_entry(tmp_path)
    store.catalog.mkdir()

    inspection = inspect_store(store)
    assert any(
        "managed-reference-unplannable:" in item
        for item in inspection.diagnostics
    )
    with pytest.raises(
        ManagedToolStoreError,
        match="cannot safely determine registered references",
    ):
        create_plan("clean-unused", roots=store)
    assert path.is_dir()


def test_inspection_does_not_hash_an_unsafe_manifest_reparse_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _key, path = publish_dummy_entry(tmp_path)
    external_manifest = tmp_path / "external-manifest.json"
    external_manifest.write_text('{"outside": true}', encoding="utf-8")
    manifest_path = path / "manifest.json"
    manifest_path.unlink()
    try:
        os.symlink(external_manifest, manifest_path)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    hashed: list[Path] = []
    real_sha256 = maintenance.sha256_file

    def recording_sha256(candidate: Path) -> str:
        hashed.append(candidate)
        return real_sha256(candidate)

    monkeypatch.setattr(maintenance, "sha256_file", recording_sha256)
    inspection = inspect_store(store)

    assert inspection.entries[0]["status"] == "damaged"
    assert manifest_path not in hashed


def test_inspection_detects_noncritical_payload_corruption(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    support = path / "support.dat"
    support.write_bytes(b"support")
    manifest = make_entry_manifest(
        key=key,
        entry_root=path,
        source={"kind": "test"},
        critical_entries=("tool.exe",),
        producer_version="test",
    )
    write_manifest(path, manifest, entries_root=store.entries)
    (path / "healthy.commit.json").unlink()
    commit_entry(path, manifest, entries_root=store.entries)
    support.write_bytes(b"changed")

    fast = validate_entry(store, key.tool_kind, key.key_digest)
    inspection = inspect_store(store)

    assert fast.healthy
    assert inspection.entries[0]["status"] == "damaged"


def test_unused_plan_excludes_every_registered_reference_state(
    tmp_path: Path,
) -> None:
    store, key, _path = publish_dummy_entry(tmp_path)
    workspace = make_workspace(tmp_path)
    marker = json.loads(
        (workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8")
    )
    binding = new_binding(
        workspace_id=marker["workspace_id"],
        game_id=marker["game_id"],
        entries=(
            WorkspaceBindingEntry(
                "test",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )
    bind_workspace(store, workspace, binding)
    plan = create_plan("clean-unused", roots=store)
    candidate = next(
        row for row in plan.candidates if row.get("entry_id") == key.entry_id
    )
    assert candidate["included"] is False
    assert candidate["referenced_by"]


def test_wrong_confirmation_token_changes_nothing(tmp_path: Path) -> None:
    store, _key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("clean-unused", roots=store)
    with pytest.raises(ManagedToolStoreError, match="token"):
        apply_plan(plan.plan_id, "0" * 64, roots=store)
    assert path.is_dir()


def test_plan_schema_rejects_atomicity_and_candidate_tampering(
    tmp_path: Path,
) -> None:
    store, _key, _path = publish_dummy_entry(tmp_path)
    plan = create_plan("clean-unused", roots=store)
    wrong_atomicity = plan.to_payload()
    wrong_atomicity["atomicity_policy"] = "all-or-nothing"
    with pytest.raises(ManagedToolStoreError, match="atomicity"):
        MaintenancePlan.from_payload(wrong_atomicity)

    wrong_candidate = plan.to_payload()
    wrong_candidate["candidates"][0]["relative_path"] = "../outside"
    with pytest.raises(ManagedToolStoreError, match="relative|traversal"):
        MaintenancePlan.from_payload(wrong_candidate)

    duplicate_candidate = plan.to_payload()
    duplicate_candidate["candidates"].append(
        dict(duplicate_candidate["candidates"][0])
    )
    with pytest.raises(ManagedToolStoreError, match="duplicate candidates"):
        MaintenancePlan.from_payload(duplicate_candidate)


def test_forged_traversal_candidate_is_rejected_without_changes(
    tmp_path: Path,
) -> None:
    store, _key, path = publish_dummy_entry(tmp_path)
    outside = store.payload.parent / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    plan = create_plan("clean-unused", roots=store)
    plan_path = store.maintenance_plans / f"{plan.plan_id}.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["candidates"][0]["relative_path"] = "../outside"
    token_payload = maintenance._plan_token_payload(
        plan_id=payload["plan_id"],
        operation=payload["operation"],
        atomicity_policy=payload["atomicity_policy"],
        created_at=payload["created_at"],
        expires_at=payload["expires_at"],
        candidates=payload["candidates"],
        references=payload["references"],
    )
    payload["confirmation_token"] = maintenance.canonical_sha256(token_payload)
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManagedToolStoreError, match="traversal|relative"):
        apply_plan(
            plan.plan_id,
            payload["confirmation_token"],
            roots=store,
        )
    assert path.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_plan_drift_is_rejected(tmp_path: Path) -> None:
    store, _key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("clean-unused", roots=store)
    (path / "tool.exe").write_bytes(b"changed")
    result = apply_plan(plan.plan_id, plan.confirmation_token, roots=store)
    assert result.outcome == "partial"
    assert result.removed_entry_ids == ()
    assert result.retained_entry_ids == (_key.entry_id,)
    assert any("changed after planning" in item for item in result.diagnostics)
    assert path.is_dir()


def test_full_uninstall_preserves_control_and_workspace_binding(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    workspace = make_workspace(tmp_path)
    marker = json.loads(
        (workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8")
    )
    binding = new_binding(
        workspace_id=marker["workspace_id"],
        game_id=marker["game_id"],
        entries=(
            WorkspaceBindingEntry(
                "test",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )
    binding_path = bind_workspace(store, workspace, binding)
    plan = create_plan("uninstall", roots=store)
    result = apply_plan(
        plan.plan_id,
        plan.confirmation_token,
        roots=store,
    )
    assert result.outcome == "success"
    assert not path.exists()
    assert store.control.is_dir()
    assert store.locks.is_dir()
    assert (store.maintenance_plans / f"{plan.plan_id}.json").is_file()
    assert (store.maintenance_results / f"{plan.plan_id}.json").is_file()
    assert binding_path.is_file()


def test_expired_plan_is_rejected_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("clean-unused", roots=store)
    real_datetime = datetime

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> "FutureDateTime":
            value = real_datetime.now(timezone.utc) + timedelta(hours=2)
            return cls.fromtimestamp(value.timestamp(), tz=timezone.utc)

    monkeypatch.setattr(maintenance, "datetime", FutureDateTime)
    with pytest.raises(ManagedToolStoreError, match="expired"):
        apply_plan(plan.plan_id, plan.confirmation_token, roots=store)
    assert path.is_dir()


def test_stale_reference_release_rechecks_catalog_under_lock(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    reference = CatalogReference(
        workspace_id=str(uuid.uuid4()),
        workspace_path=str(tmp_path / "missing-workspace"),
        game_id="skyrim-se",
        generation=str(uuid.uuid4()),
        status=ReferenceStatus.STALE,
        entry_ids=(key.entry_id,),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_catalog(
        store,
        {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "references": {reference.reference_id: reference.to_payload()},
        },
    )
    plan = create_plan(
        "clean-unused",
        roots=store,
        release_stale_reference_ids=(reference.reference_id,),
    )
    active = CatalogReference(
        workspace_id=reference.workspace_id,
        workspace_path=reference.workspace_path,
        game_id=reference.game_id,
        generation=reference.generation,
        status=ReferenceStatus.ACTIVE,
        entry_ids=reference.entry_ids,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_catalog(
        store,
        {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "references": {active.reference_id: active.to_payload()},
        },
    )

    with pytest.raises(ManagedToolStoreError, match="catalog changed"):
        apply_plan(plan.plan_id, plan.confirmation_token, roots=store)

    current = load_catalog(store)["references"]
    assert current[active.reference_id]["status"] == "active"
    assert path.is_dir()


def test_busy_entry_is_retained_by_best_effort_cleanup(tmp_path: Path) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("clean-unused", roots=store)
    with SmtProcessFileLock(
        store.locks / "entries" / f"{key.tool_kind}-{key.key_digest}.lock",
        "shared",
        1.0,
        allowed_root=store.control,
    ):
        result = apply_plan(
            plan.plan_id,
            plan.confirmation_token,
            roots=store,
            lock_timeout_seconds=0.0,
        )
    assert result.outcome == "partial"
    assert result.retained_entry_ids == (key.entry_id,)
    assert path.is_dir()


def test_busy_entry_blocks_all_or_nothing_uninstall(tmp_path: Path) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("uninstall", roots=store)
    with SmtProcessFileLock(
        store.locks / "entries" / f"{key.tool_kind}-{key.key_digest}.lock",
        "shared",
        1.0,
        allowed_root=store.control,
    ):
        result = apply_plan(
            plan.plan_id,
            plan.confirmation_token,
            roots=store,
            lock_timeout_seconds=0.0,
        )
    assert result.outcome == "blocked"
    assert result.removed_entry_ids == ()
    assert path.is_dir()


def test_invalid_entry_blocks_all_or_nothing_uninstall(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    (path / "tool.exe").write_bytes(b"damaged-before-plan")
    plan = create_plan("uninstall", roots=store)

    result = apply_plan(
        plan.plan_id,
        plan.confirmation_token,
        roots=store,
    )

    assert result.outcome == "blocked"
    assert result.retained_entry_ids == (key.entry_id,)
    assert any(
        "invalid candidates" in diagnostic
        for diagnostic in result.diagnostics
    )
    assert path.is_dir()


def test_unsafe_remnant_blocks_uninstall_before_catalog_or_entry_changes(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    workspace = make_workspace(tmp_path)
    marker = json.loads(
        (workspace / ".skyrim-chs-workspace.json").read_text(encoding="utf-8")
    )
    binding = new_binding(
        workspace_id=marker["workspace_id"],
        game_id=marker["game_id"],
        entries=(
            WorkspaceBindingEntry(
                "test-tool",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )
    bind_workspace(store, workspace, binding)
    before_catalog = load_catalog(store)

    unsafe = store.staging / "unsafe-remnant"
    unsafe.mkdir()
    outside = tmp_path / "outside-remnant"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")
    try:
        (unsafe / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    plan = create_plan("uninstall", roots=store)
    unsafe_candidate = next(
        candidate
        for candidate in plan.candidates
        if candidate.get("relative_path") == "staging/unsafe-remnant"
    )
    assert unsafe_candidate["status"] == "invalid"

    result = apply_plan(
        plan.plan_id,
        plan.confirmation_token,
        roots=store,
    )

    assert result.outcome == "blocked"
    assert result.removed_entry_ids == ()
    assert path.is_dir()
    assert load_catalog(store) == before_catalog
    assert sentinel.read_text(encoding="utf-8") == "outside"


def test_unregistered_payload_content_blocks_uninstall_planning(
    tmp_path: Path,
) -> None:
    store = roots(tmp_path)
    ensure_store_layout(store)
    unexpected = store.payload / "unexpected-content"
    unexpected.mkdir()
    (unexpected / "payload.bin").write_bytes(b"unknown")

    inspection = inspect_store(store)

    assert any(
        diagnostic.startswith(maintenance.UNPLANNABLE_PAYLOAD_PREFIX)
        for diagnostic in inspection.diagnostics
    )
    with pytest.raises(
        ManagedToolStoreError,
        match="cannot safely enumerate",
    ):
        create_plan("uninstall", roots=store)
    assert unexpected.is_dir()


def test_inspection_rejects_a_reparse_payload_root_without_traversing_it(
    tmp_path: Path,
) -> None:
    store = roots(tmp_path)
    store.application.mkdir(parents=True)
    outside = tmp_path / "outside-payload"
    (outside / "entries").mkdir(parents=True)
    sentinel = outside / "entries" / "must-not-be-read.txt"
    sentinel.write_text("outside", encoding="utf-8")
    store.payload.parent.mkdir(parents=True, exist_ok=True)
    try:
        store.payload.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    inspection = inspect_store(store)

    assert inspection.entries == ()
    assert any(
        diagnostic.startswith(maintenance.UNPLANNABLE_PAYLOAD_PREFIX)
        for diagnostic in inspection.diagnostics
    )
    assert sentinel.read_text(encoding="utf-8") == "outside"


def test_active_shared_provisioning_blocks_full_uninstall(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("uninstall", roots=store)

    with store_lifecycle_lock(
        store,
        mode="shared",
        timeout_seconds=1.0,
        command="test active provisioning",
    ):
        result = apply_plan(
            plan.plan_id,
            plan.confirmation_token,
            roots=store,
            lock_timeout_seconds=0.0,
        )

    assert result.outcome == "blocked"
    assert result.retained_entry_ids == (key.entry_id,)
    assert path.is_dir()


def test_logical_byte_accounting_comes_from_manifest(tmp_path: Path) -> None:
    store, _key, _path = publish_dummy_entry(tmp_path, name="123456789")
    plan = create_plan("clean-unused", roots=store)
    included = next(row for row in plan.candidates if row.get("included"))
    result = apply_plan(plan.plan_id, plan.confirmation_token, roots=store)
    assert included["logical_bytes"] == len(b"123456789")
    assert result.logical_bytes_removed == included["logical_bytes"]


def test_stale_reference_requires_explicit_release(tmp_path: Path) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    reference = CatalogReference(
        workspace_id=str(uuid.uuid4()),
        workspace_path=str(tmp_path / "missing-workspace"),
        game_id="skyrim-se",
        generation=str(uuid.uuid4()),
        status=ReferenceStatus.STALE,
        entry_ids=(key.entry_id,),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_catalog(
        store,
        {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "references": {reference.reference_id: reference.to_payload()},
        },
    )
    retained = create_plan("clean-unused", roots=store)
    retained_row = next(
        row for row in retained.candidates if row.get("entry_id") == key.entry_id
    )
    assert retained_row["included"] is False

    released = create_plan(
        "clean-unused",
        roots=store,
        release_stale_reference_ids=(reference.reference_id,),
    )
    released_row = next(
        row for row in released.candidates if row.get("entry_id") == key.entry_id
    )
    assert released_row["included"] is True
    result = apply_plan(
        released.plan_id,
        released.confirmation_token,
        roots=store,
    )
    assert result.outcome == "success"
    assert not path.exists()


def test_missing_active_workspace_is_observed_stale_and_can_be_released(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    reference = CatalogReference(
        workspace_id=str(uuid.uuid4()),
        workspace_path=str(tmp_path / "deleted-workspace"),
        game_id="skyrim-se",
        generation=str(uuid.uuid4()),
        status=ReferenceStatus.ACTIVE,
        entry_ids=(key.entry_id,),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_catalog(
        store,
        {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "references": {reference.reference_id: reference.to_payload()},
        },
    )

    inspection = inspect_store(store)
    observed = next(
        row
        for row in inspection.references
        if row["workspace_id"] == reference.workspace_id
    )
    assert observed["status"] == "active"
    assert observed["observed_classification"] == "stale"

    plan = create_plan(
        "clean-unused",
        roots=store,
        release_stale_reference_ids=(reference.reference_id,),
    )
    candidate = next(
        row for row in plan.candidates if row.get("entry_id") == key.entry_id
    )
    assert candidate["included"] is True
    result = apply_plan(
        plan.plan_id,
        plan.confirmation_token,
        roots=store,
    )
    assert result.outcome == "success"
    assert not path.exists()
    assert reference.reference_id not in load_catalog(store)["references"]


def test_restored_workspace_blocks_an_observed_stale_release(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    workspace_id = str(uuid.uuid4())
    workspace = make_workspace(tmp_path, workspace_id=workspace_id)
    binding = new_binding(
        workspace_id=workspace_id,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "test",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )
    reference = CatalogReference(
        workspace_id=workspace_id,
        workspace_path=str(workspace),
        game_id="skyrim-se",
        generation=binding.generation,
        status=ReferenceStatus.PENDING,
        entry_ids=(key.entry_id,),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_catalog(
        store,
        {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "references": {reference.reference_id: reference.to_payload()},
        },
    )
    plan = create_plan(
        "clean-unused",
        roots=store,
        release_stale_reference_ids=(reference.reference_id,),
    )
    assert plan.references[0]["observed_classification"] == "stale"

    write_workspace_binding(workspace, binding)

    with pytest.raises(
        ManagedToolStoreError,
        match="classification changed",
    ):
        apply_plan(
            plan.plan_id,
            plan.confirmation_token,
            roots=store,
        )
    assert path.is_dir()
    assert reference.reference_id in load_catalog(store)["references"]


def test_legacy_marker_matching_session_keeps_valid_reference_active(
    tmp_path: Path,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    workspace_id = str(uuid.uuid4())
    workspace = make_workspace(tmp_path, workspace_id=workspace_id)
    marker_path = workspace / ".skyrim-chs-workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.pop("workspace_id")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    write_session(workspace, workspace_id=workspace_id)
    binding = new_binding(
        workspace_id=workspace_id,
        game_id="skyrim-se",
        entries=(
            WorkspaceBindingEntry(
                "test",
                key.tool_kind,
                key.key_digest,
                "tool.exe",
            ),
        ),
    )
    write_workspace_binding(workspace, binding)
    reference = CatalogReference(
        workspace_id=workspace_id,
        workspace_path=str(workspace),
        game_id="skyrim-se",
        generation=binding.generation,
        status=ReferenceStatus.ACTIVE,
        entry_ids=(key.entry_id,),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_catalog(
        store,
        {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "references": {reference.reference_id: reference.to_payload()},
        },
    )

    inspection = inspect_store(store)
    observed = next(
        row
        for row in inspection.references
        if row["workspace_id"] == workspace_id
    )

    assert observed["observed_classification"] == "valid"
    assert path.is_dir()


def test_interrupted_delete_is_reported_and_trash_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, key, path = publish_dummy_entry(tmp_path)
    plan = create_plan("clean-unused", roots=store)
    monkeypatch.setattr(
        maintenance,
        "_remove_validated_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated")),
    )
    result = apply_plan(plan.plan_id, plan.confirmation_token, roots=store)
    assert result.outcome == "partial"
    assert key.entry_id in result.retained_entry_ids
    assert not path.exists()
    assert (store.trash / plan.plan_id).is_dir()
    assert "plan trash remains" in " ".join(result.diagnostics)

"""Plan, apply, and inspect the shared managed-tool cache safely."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from file_utils import (
    discover_regular_files,
    sha256_file,
    validate_regular_path_under,
)
from managed_tool_resolver import read_workspace_identity_evidence
from managed_tool_store import (
    ENTRY_MANIFEST_NAME,
    CatalogReference,
    EntryManifest,
    MaintenancePlan,
    MaintenanceResult,
    ManagedStoreRoots,
    ManagedToolSchemaError,
    ManagedToolStoreError,
    acquire_entry_locks,
    atomic_write_json,
    canonical_sha256,
    catalog_lock,
    empty_catalog,
    ensure_store_layout,
    entry_directory,
    entry_lock,
    load_catalog,
    managed_path,
    normalize_relative_path,
    read_workspace_binding,
    resolve_managed_store_roots,
    store_lifecycle_lock,
    utc_now,
    validate_entry,
    write_catalog,
)
from smt_windows import (
    ManagedProcessEnvironmentError,
    SmtLockTimeoutError,
    process_file_lock_is_available,
    publish_path_no_replace,
    read_regular_single_link_bytes,
    remove_regular_tree,
    validate_regular_single_link_file,
)


PLAN_TTL_MINUTES = 30
REFERENCE_COVERAGE = "known-registered-only"
UNPLANNABLE_PAYLOAD_PREFIX = "managed-payload-unplannable:"
UNPLANNABLE_REFERENCE_PREFIX = "managed-reference-unplannable:"


@dataclass(frozen=True)
class StoreInspection:
    payload_exists: bool
    control_exists: bool
    entries: tuple[Mapping[str, Any], ...]
    references: tuple[Mapping[str, Any], ...]
    staging: tuple[Mapping[str, Any], ...]
    trash: tuple[Mapping[str, Any], ...]
    diagnostics: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation": "inspect",
            "status": "ok",
            "reference_coverage": REFERENCE_COVERAGE,
            "payload_exists": self.payload_exists,
            "control_exists": self.control_exists,
            "entries": list(self.entries),
            "references": list(self.references),
            "staging": list(self.staging),
            "trash": list(self.trash),
            "logical_bytes": sum(
                int(row.get("logical_bytes", 0)) for row in self.entries
            ),
            "diagnostics": list(self.diagnostics),
        }


def _split_entry_id(entry_id: str) -> tuple[str, str]:
    kind, separator, digest = entry_id.partition(":")
    if (
        not separator
        or not kind
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ManagedToolSchemaError(f"invalid managed entry id: {entry_id}")
    return kind, digest


def _entry_lock_path(roots: ManagedStoreRoots, entry_id: str) -> Path:
    kind, digest = _split_entry_id(entry_id)
    return roots.locks / "entries" / f"{kind}-{digest}.lock"


def _reference_index(
    references: Iterable[CatalogReference],
) -> dict[str, list[CatalogReference]]:
    result: dict[str, list[CatalogReference]] = {}
    for reference in references:
        for entry_id in reference.entry_ids:
            result.setdefault(entry_id, []).append(reference)
    return result


def _observed_reference_classification(
    reference: CatalogReference,
) -> tuple[str, str | None]:
    workspace = Path(reference.workspace_path)
    try:
        if not workspace.is_dir():
            raise ManagedToolStoreError("registered workspace path is unavailable")
        identity = read_workspace_identity_evidence(workspace)
        marker_workspace_id = identity.effective_workspace_id
        if marker_workspace_id is None:
            raise ManagedToolStoreError(
                "registered legacy workspace marker has no provable identity"
            )
        if (
            marker_workspace_id != reference.workspace_id
            or identity.game_id != reference.game_id
        ):
            raise ManagedToolStoreError(
                "registered workspace marker identity does not match the catalog"
            )
        binding = read_workspace_binding(workspace)
        if (
            binding.workspace_id != reference.workspace_id
            or binding.game_id != reference.game_id
            or binding.generation != reference.generation
            or tuple(sorted(entry.entry_id for entry in binding.entries))
            != tuple(sorted(reference.entry_ids))
        ):
            raise ManagedToolStoreError(
                "registered workspace binding does not match the catalog reference"
            )
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        ManagedProcessEnvironmentError,
        ManagedToolStoreError,
    ) as exc:
        return "stale", str(exc)
    return "valid", None


def _manifest_logical_bytes(manifest: EntryManifest | None) -> int:
    if manifest is None:
        return 0
    return sum(item.size for item in manifest.payload_inventory)


def _entry_rows(
    roots: ManagedStoreRoots,
    references: Sequence[CatalogReference],
    diagnostics: list[str],
    *,
    probe_locks: bool,
) -> tuple[Mapping[str, Any], ...]:
    if not os.path.lexists(roots.entries):
        return ()
    validate_regular_path_under(
        roots.entries,
        roots.payload,
        kind="directory",
        label="managed entries root",
    )
    reference_index = _reference_index(references)
    rows: list[Mapping[str, Any]] = []
    for kind_path in sorted(roots.entries.iterdir(), key=lambda path: path.name.casefold()):
        try:
            validate_regular_path_under(
                kind_path,
                roots.entries,
                kind="directory",
                label="managed tool kind",
            )
        except (OSError, ValueError) as exc:
            diagnostics.append(
                f"{UNPLANNABLE_PAYLOAD_PREFIX} "
                f"{kind_path.name}: {exc}"
            )
            continue
        for entry_path in sorted(
            kind_path.iterdir(),
            key=lambda path: path.name.casefold(),
        ):
            entry_id = f"{kind_path.name}:{entry_path.name.casefold()}"
            try:
                _split_entry_id(entry_id)
                validation = validate_entry(
                    roots,
                    kind_path.name,
                    entry_path.name.casefold(),
                    deep=True,
                )
                manifest = validation.manifest
                manifest_digest = (
                    sha256_file(entry_path / ENTRY_MANIFEST_NAME)
                    if manifest is not None
                    else ""
                )
                lock_path = _entry_lock_path(roots, entry_id)
                lock_available = (
                    process_file_lock_is_available(
                        lock_path,
                        roots.control,
                    )
                    if probe_locks
                    else False
                )
                linked = reference_index.get(entry_id, [])
                rows.append(
                    {
                        "entry_id": entry_id,
                        "tool_kind": kind_path.name,
                        "key_digest": entry_path.name.casefold(),
                        "relative_path": entry_path.relative_to(roots.payload).as_posix(),
                        "status": validation.status.value,
                        "logical_bytes": _manifest_logical_bytes(manifest),
                        "manifest_sha256": manifest_digest,
                        "key_inputs": dict(manifest.key_inputs) if manifest else {},
                        "critical_entries": (
                            list(manifest.critical_entries) if manifest else []
                        ),
                        "source": dict(manifest.source) if manifest else {},
                        "platform": dict(manifest.platform) if manifest else {},
                        "referenced_by": [
                            {
                                "reference_id": reference.reference_id,
                                "status": reference.status.value,
                                "workspace_id": reference.workspace_id,
                                "workspace_path": reference.workspace_path,
                            }
                            for reference in linked
                        ],
                        "exclusive_lock_available": lock_available,
                        "busy": not lock_available,
                    }
                )
            except (OSError, ValueError, ManagedToolStoreError) as exc:
                diagnostics.append(
                    f"{UNPLANNABLE_PAYLOAD_PREFIX} "
                    f"{entry_id}: {exc}"
                )
    return tuple(rows)


def _tree_logical_bytes(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in discover_regular_files(path, label="managed cache remnant")
    )


def _remnant_rows(path: Path, allowed_root: Path) -> tuple[Mapping[str, Any], ...]:
    if not os.path.lexists(path):
        return ()
    validate_regular_path_under(
        path,
        allowed_root,
        kind="directory",
        label="managed cache remnant root",
    )
    rows: list[Mapping[str, Any]] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
        relative = child.relative_to(allowed_root).as_posix()
        try:
            if child.is_dir():
                validate_regular_path_under(
                    child,
                    allowed_root,
                    kind="directory",
                    label="managed cache remnant",
                )
                logical_bytes = _tree_logical_bytes(child)
                kind = "directory"
            else:
                validate_regular_single_link_file(
                    child,
                    allowed_root,
                    label="managed cache remnant",
                )
                logical_bytes = child.stat().st_size
                kind = "file"
            rows.append(
                {
                    "relative_path": normalize_relative_path(relative),
                    "kind": kind,
                    "logical_bytes": logical_bytes,
                }
            )
        except (OSError, ValueError, ManagedToolStoreError):
            rows.append(
                {
                    "relative_path": normalize_relative_path(relative),
                    "kind": "invalid",
                    "logical_bytes": 0,
                }
            )
    return tuple(rows)


def inspect_store(
    roots: ManagedStoreRoots | None = None,
) -> StoreInspection:
    """Inspect without creating payload, control, lock, plan, or result files."""

    roots = roots or resolve_managed_store_roots()
    payload_exists = os.path.lexists(roots.payload)
    control_exists = os.path.lexists(roots.control)
    diagnostics: list[str] = []
    if not payload_exists and not control_exists:
        return StoreInspection(False, False, (), (), (), (), ())
    payload_safe = payload_exists
    control_safe = control_exists
    if payload_exists:
        try:
            validate_regular_path_under(
                roots.payload,
                roots.base,
                kind="directory",
                label="managed payload root",
            )
        except (OSError, ValueError) as exc:
            payload_safe = False
            diagnostics.append(
                f"{UNPLANNABLE_PAYLOAD_PREFIX} unsafe payload root: {exc}"
            )
    if control_exists:
        try:
            validate_regular_path_under(
                roots.control,
                roots.base,
                kind="directory",
                label="managed control root",
            )
        except (OSError, ValueError) as exc:
            control_safe = False
            diagnostics.append(f"{UNPLANNABLE_REFERENCE_PREFIX} control root: {exc}")
    if payload_safe:
        expected_payload_children = {
            roots.entries.name.casefold(),
            roots.staging.name.casefold(),
            roots.trash.name.casefold(),
        }
        for child in roots.payload.iterdir():
            if child.name.casefold() not in expected_payload_children:
                diagnostics.append(
                    f"{UNPLANNABLE_PAYLOAD_PREFIX} "
                    f"unexpected payload child {child.name}"
                )
    references: list[CatalogReference] = []
    reference_rows: list[Mapping[str, Any]] = []
    if control_safe and os.path.lexists(roots.catalog):
        try:
            catalog = load_catalog(roots)
            references = [
                CatalogReference.from_payload(raw)
                for raw in catalog["references"].values()
            ]
            for reference in references:
                classification, reason = _observed_reference_classification(
                    reference
                )
                row = reference.to_payload()
                row["observed_classification"] = classification
                reference_rows.append(row)
                if reason is not None:
                    diagnostics.append(
                        f"{reference.reference_id}: observed stale reference: {reason}; "
                        "retained conservatively"
                    )
        except (
            OSError,
            ValueError,
            ManagedProcessEnvironmentError,
            ManagedToolStoreError,
        ) as exc:
            diagnostics.append(f"{UNPLANNABLE_REFERENCE_PREFIX} catalog: {exc}")
    entry_rows: tuple[Mapping[str, Any], ...] = ()
    staging: tuple[Mapping[str, Any], ...] = ()
    trash: tuple[Mapping[str, Any], ...] = ()
    if payload_safe:
        try:
            entry_rows = _entry_rows(
                roots,
                references,
                diagnostics,
                probe_locks=control_safe,
            )
        except (
            OSError,
            ValueError,
            ManagedProcessEnvironmentError,
            ManagedToolStoreError,
        ) as exc:
            diagnostics.append(
                f"{UNPLANNABLE_PAYLOAD_PREFIX} entries root: {exc}"
            )
        for origin, path in (("staging", roots.staging), ("trash", roots.trash)):
            if not os.path.lexists(path):
                continue
            try:
                rows = _remnant_rows(path, roots.payload)
                if origin == "staging":
                    staging = rows
                else:
                    trash = rows
            except (
                OSError,
                ValueError,
                ManagedProcessEnvironmentError,
                ManagedToolStoreError,
            ) as exc:
                diagnostics.append(
                    f"{UNPLANNABLE_PAYLOAD_PREFIX} {origin} root: {exc}"
                )
    if staging:
        diagnostics.append("managed staging remnants require an explicit plan")
    if trash:
        diagnostics.append("interrupted managed trash requires an explicit plan")
    return StoreInspection(
        payload_exists,
        control_exists,
        entry_rows,
        tuple(reference_rows),
        staging,
        trash,
        tuple(diagnostics),
    )


def _plan_token_payload(
    *,
    plan_id: str,
    operation: str,
    atomicity_policy: str,
    created_at: str,
    expires_at: str,
    candidates: Sequence[Mapping[str, Any]],
    references: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_id": plan_id,
        "operation": operation,
        "atomicity_policy": atomicity_policy,
        "created_at": created_at,
        "expires_at": expires_at,
        "candidates": list(candidates),
        "references": list(references),
    }


def _plan_path(roots: ManagedStoreRoots, plan_id: str) -> Path:
    normalized = str(uuid.UUID(plan_id))
    return roots.maintenance_plans / f"{normalized}.json"


def _result_path(roots: ManagedStoreRoots, plan_id: str) -> Path:
    normalized = str(uuid.UUID(plan_id))
    return roots.maintenance_results / f"{normalized}.json"


def create_plan(
    operation: str,
    *,
    roots: ManagedStoreRoots | None = None,
    release_stale_reference_ids: Sequence[str] = (),
    ttl_minutes: int = PLAN_TTL_MINUTES,
) -> MaintenancePlan:
    if operation not in {"clean-unused", "uninstall"}:
        raise ValueError("maintenance operation must be clean-unused or uninstall")
    if ttl_minutes < 1:
        raise ValueError("maintenance plan TTL must be positive")
    roots = roots or resolve_managed_store_roots()
    ensure_store_layout(roots)
    inspection = inspect_store(roots)
    unplannable_payload = [
        diagnostic
        for diagnostic in inspection.diagnostics
        if diagnostic.startswith(UNPLANNABLE_PAYLOAD_PREFIX)
    ]
    unplannable_references = [
        diagnostic
        for diagnostic in inspection.diagnostics
        if diagnostic.startswith(UNPLANNABLE_REFERENCE_PREFIX)
    ]
    if unplannable_references:
        raise ManagedToolStoreError(
            "maintenance cannot safely determine registered references: "
            + " | ".join(unplannable_references)
        )
    if operation == "uninstall" and unplannable_payload:
        raise ManagedToolStoreError(
            "full uninstall cannot safely enumerate the managed payload: "
            + " | ".join(unplannable_payload)
        )
    reference_observations = {
        f"{raw['workspace_id']}:{raw['generation']}": str(
            raw["observed_classification"]
        )
        for raw in inspection.references
    }
    references = [
        CatalogReference.from_payload(
            {
                key: value
                for key, value in raw.items()
                if key != "observed_classification"
            }
        )
        for raw in inspection.references
    ]
    release_ids = set(release_stale_reference_ids)
    known_ids = {reference.reference_id for reference in references}
    unknown_release = sorted(release_ids - known_ids)
    if unknown_release:
        raise ManagedToolStoreError(
            "stale reference release IDs are unknown: " + ", ".join(unknown_release)
        )
    for reference in references:
        if (
            reference.reference_id in release_ids
            and reference_observations[reference.reference_id] != "stale"
        ):
            raise ManagedToolStoreError(
                "only observed-stale references can be explicitly released by "
                "unused cleanup"
            )
    effective_references = [
        reference
        for reference in references
        if reference.reference_id not in release_ids
    ]
    referenced = _reference_index(effective_references)
    candidates: list[Mapping[str, Any]] = []
    for row in inspection.entries:
        entry_id = str(row["entry_id"])
        included = operation == "uninstall" or (
            not referenced.get(entry_id)
            and row.get("status") == "healthy"
            and row.get("busy") is False
        )
        candidates.append(
            {
                "candidate_type": "entry",
                "entry_id": entry_id,
                "relative_path": row["relative_path"],
                "logical_bytes": row["logical_bytes"],
                "manifest_sha256": row["manifest_sha256"],
                "status": row["status"],
                "referenced_by": [
                    item["reference_id"]
                    for item in row.get("referenced_by", [])
                    if isinstance(item, dict)
                    and item.get("reference_id") not in release_ids
                ],
                "busy": row["busy"],
                "included": included,
                "effect": (
                    "detach-and-delete"
                    if included
                    else "retain-referenced-or-unavailable"
                ),
            }
        )
    if operation == "uninstall":
        for origin, rows in (
            ("staging", inspection.staging),
            ("trash", inspection.trash),
        ):
            for row in rows:
                candidates.append(
                    {
                        "candidate_type": "remnant",
                        "origin": origin,
                        "relative_path": row["relative_path"],
                        "logical_bytes": row["logical_bytes"],
                        "manifest_sha256": "",
                        "status": row["kind"],
                        "referenced_by": [],
                        "busy": False,
                        "included": True,
                        "effect": "delete-confirmed-remnant",
                    }
                )
    created = datetime.now(timezone.utc)
    expires = created + timedelta(minutes=ttl_minutes)
    plan_id = str(uuid.uuid4())
    atomicity = (
        "best-effort-per-entry"
        if operation == "clean-unused"
        else "all-or-nothing"
    )
    reference_rows = []
    for reference in references:
        row = reference.to_payload()
        row["observed_classification"] = reference_observations[
            reference.reference_id
        ]
        row["effect"] = (
            "release-explicit-stale-reference"
            if reference.reference_id in release_ids
            else (
                "invalidate-known-binding"
                if operation == "uninstall"
                else "retain"
            )
        )
        reference_rows.append(row)
    token_payload = _plan_token_payload(
        plan_id=plan_id,
        operation=operation,
        atomicity_policy=atomicity,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        candidates=candidates,
        references=reference_rows,
    )
    token = canonical_sha256(token_payload)
    plan = MaintenancePlan(
        plan_id=plan_id,
        operation=operation,
        atomicity_policy=atomicity,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        candidates=tuple(candidates),
        references=tuple(reference_rows),
        confirmation_token=token,
    )
    atomic_write_json(
        _plan_path(roots, plan.plan_id),
        plan.to_payload(),
        allowed_root=roots.control,
    )
    return plan


def read_plan(roots: ManagedStoreRoots, plan_id: str) -> MaintenancePlan:
    path = validate_regular_single_link_file(
        _plan_path(roots, plan_id),
        roots.control,
        label="managed maintenance plan",
    )
    import json

    return MaintenancePlan.from_payload(
        json.loads(
            read_regular_single_link_bytes(
                path,
                roots.control,
                label="managed maintenance plan",
            ).decode("utf-8")
        )
    )


def _current_candidate(
    roots: ManagedStoreRoots,
    candidate: Mapping[str, Any],
) -> Mapping[str, Any]:
    if candidate.get("candidate_type") != "entry":
        relative = normalize_relative_path(str(candidate["relative_path"]))
        path = managed_path(roots.payload, relative)
        if not os.path.lexists(path):
            return {"missing": True, "relative_path": relative}
        if path.is_dir():
            validate_regular_path_under(
                path,
                roots.payload,
                kind="directory",
                label="maintenance remnant",
            )
            logical_bytes = _tree_logical_bytes(path)
            kind = "directory"
        else:
            validate_regular_single_link_file(
                path,
                roots.payload,
                label="maintenance remnant",
            )
            logical_bytes = path.stat().st_size
            kind = "file"
        return {
            "missing": False,
            "relative_path": relative,
            "logical_bytes": logical_bytes,
            "status": kind,
        }
    entry_id = str(candidate["entry_id"])
    kind, digest = _split_entry_id(entry_id)
    validation = validate_entry(roots, kind, digest, deep=True)
    manifest_path = validation.entry_path / ENTRY_MANIFEST_NAME
    return {
        "missing": validation.status.value == "missing",
        "entry_id": entry_id,
        "relative_path": validation.entry_path.relative_to(roots.payload).as_posix(),
        "logical_bytes": _manifest_logical_bytes(validation.manifest),
        "manifest_sha256": (
            sha256_file(manifest_path) if manifest_path.is_file() else ""
        ),
        "status": validation.status.value,
    }


def _candidate_matches_plan(
    roots: ManagedStoreRoots,
    candidate: Mapping[str, Any],
) -> bool:
    current = _current_candidate(roots, candidate)
    if current.get("missing"):
        return False
    keys = (
        ("relative_path", "relative_path"),
        ("logical_bytes", "logical_bytes"),
        ("status", "status"),
    )
    if candidate.get("candidate_type") == "entry":
        keys += (
            ("entry_id", "entry_id"),
            ("manifest_sha256", "manifest_sha256"),
        )
    return all(current[left] == candidate[right] for left, right in keys)


def _remove_validated_tree(path: Path, allowed_root: Path) -> None:
    remove_regular_tree(
        path,
        allowed_root,
        label="managed maintenance deletion tree",
    )


def _write_result(
    roots: ManagedStoreRoots,
    result: MaintenanceResult,
) -> MaintenanceResult:
    atomic_write_json(
        _result_path(roots, result.plan_id),
        result.to_payload(),
        allowed_root=roots.control,
    )
    return result


def _validate_plan_header(
    plan: MaintenancePlan,
    confirmation_token: str,
) -> None:
    if confirmation_token != plan.confirmation_token:
        raise ManagedToolStoreError("maintenance confirmation token does not match")
    token_payload = _plan_token_payload(
        plan_id=plan.plan_id,
        operation=plan.operation,
        atomicity_policy=plan.atomicity_policy,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
        candidates=plan.candidates,
        references=plan.references,
    )
    if canonical_sha256(token_payload) != plan.confirmation_token:
        raise ManagedToolStoreError("stored maintenance plan token is invalid")
    expires = datetime.fromisoformat(plan.expires_at)
    if expires.tzinfo is None or datetime.now(timezone.utc) >= expires:
        raise ManagedToolStoreError("maintenance plan expired; create a new plan")


def _released_reference_ids(plan: MaintenancePlan) -> set[str]:
    return {
        f"{row['workspace_id']}:{row['generation']}"
        for row in plan.references
        if row.get("effect") == "release-explicit-stale-reference"
    }


def _catalog_matches_plan(
    roots: ManagedStoreRoots,
    plan: MaintenancePlan,
) -> bool:
    current = load_catalog(roots)
    current_rows = current["references"]
    planned = {
        f"{row['workspace_id']}:{row['generation']}": {
            key: value
            for key, value in row.items()
            if key not in {"effect", "observed_classification"}
        }
        for row in plan.references
    }
    return current_rows == planned


def _reference_classifications_match_plan(
    roots: ManagedStoreRoots,
    plan: MaintenancePlan,
) -> bool:
    current = load_catalog(roots)["references"]
    for row in plan.references:
        reference_id = f"{row['workspace_id']}:{row['generation']}"
        raw = current.get(reference_id)
        if raw is None:
            return False
        reference = CatalogReference.from_payload(raw)
        classification, _reason = _observed_reference_classification(reference)
        if classification != row["observed_classification"]:
            return False
    return True


def _uninstall_target_set_matches(
    roots: ManagedStoreRoots,
    entries: Sequence[Mapping[str, Any]],
    remnants: Sequence[Mapping[str, Any]],
) -> bool:
    inspection = inspect_store(roots)
    if any(
        diagnostic.startswith(UNPLANNABLE_PAYLOAD_PREFIX)
        for diagnostic in inspection.diagnostics
    ):
        return False
    current_entry_ids = {
        str(row["entry_id"]) for row in inspection.entries
    }
    planned_entry_ids = {
        str(row["entry_id"]) for row in entries
    }
    current_remnants = {
        str(row["relative_path"])
        for row in (*inspection.staging, *inspection.trash)
    }
    planned_remnants = {
        str(row["relative_path"]) for row in remnants
    }
    return (
        current_entry_ids == planned_entry_ids
        and current_remnants == planned_remnants
    )


def _detach_entry(
    roots: ManagedStoreRoots,
    plan_id: str,
    candidate: Mapping[str, Any],
) -> Path:
    entry_id = str(candidate["entry_id"])
    kind, digest = _split_entry_id(entry_id)
    source = entry_directory(roots, kind, digest)
    plan_trash = roots.trash / plan_id
    plan_trash.mkdir(exist_ok=True)
    validate_regular_path_under(
        plan_trash,
        roots.trash,
        kind="directory",
        label="maintenance plan trash",
    )
    target = plan_trash / f"{kind}-{digest}"
    publish_path_no_replace(source, target)
    return target


def apply_plan(
    plan_id: str,
    confirmation_token: str,
    *,
    roots: ManagedStoreRoots | None = None,
    lock_timeout_seconds: float = 0.0,
) -> MaintenanceResult:
    roots = roots or resolve_managed_store_roots()
    plan = read_plan(roots, plan_id)
    _validate_plan_header(plan, confirmation_token)
    included_entries = [
        candidate
        for candidate in plan.candidates
        if candidate.get("included") is True
        and candidate.get("candidate_type") == "entry"
    ]
    included_remnants = [
        candidate
        for candidate in plan.candidates
        if candidate.get("included") is True
        and candidate.get("candidate_type") == "remnant"
    ]
    removed: list[str] = []
    retained: list[str] = []
    diagnostics: list[str] = []
    logical_bytes_removed = 0

    if plan.atomicity_policy == "all-or-nothing":
        entry_ids = [str(candidate["entry_id"]) for candidate in included_entries]
        try:
            with store_lifecycle_lock(
                roots,
                mode="exclusive",
                timeout_seconds=lock_timeout_seconds,
                command=f"apply full managed cache uninstall {plan.plan_id}",
            ), catalog_lock(
                roots,
                mode="exclusive",
                timeout_seconds=lock_timeout_seconds,
                command=f"apply full managed cache uninstall {plan.plan_id}",
            ), acquire_entry_locks(
                roots,
                entry_ids,
                mode="exclusive",
                timeout_seconds=lock_timeout_seconds,
                command=f"apply full managed cache uninstall {plan.plan_id}",
            ):
                if not _catalog_matches_plan(roots, plan):
                    raise ManagedToolStoreError(
                        "catalog changed after planning; create a new plan"
                    )
                if not _reference_classifications_match_plan(roots, plan):
                    raise ManagedToolStoreError(
                        "workspace reference classification changed after planning"
                    )
                if not _uninstall_target_set_matches(
                    roots,
                    included_entries,
                    included_remnants,
                ):
                    raise ManagedToolStoreError(
                        "managed payload target set changed after planning"
                    )
                invalid_candidates = [
                    str(
                        candidate.get("entry_id")
                        or candidate.get("relative_path")
                        or "unknown"
                    )
                    for candidate in (*included_entries, *included_remnants)
                    if (
                        candidate.get("status") != "healthy"
                        if candidate.get("candidate_type") == "entry"
                        else candidate.get("status") not in {"directory", "file"}
                    )
                ]
                if invalid_candidates:
                    raise ManagedToolStoreError(
                        "full uninstall contains invalid candidates: "
                        + ", ".join(invalid_candidates)
                    )
                if not all(
                    _candidate_matches_plan(roots, candidate)
                    for candidate in (*included_entries, *included_remnants)
                ):
                    raise ManagedToolStoreError(
                        "maintenance candidates changed after planning"
                    )
                detached: list[tuple[Mapping[str, Any], Path]] = []
                try:
                    for candidate in included_entries:
                        detached.append(
                            (
                                candidate,
                                _detach_entry(roots, plan.plan_id, candidate),
                            )
                        )
                    catalog = empty_catalog()
                    write_catalog(roots, catalog)
                except BaseException:
                    for candidate, trash_path in reversed(detached):
                        kind, digest = _split_entry_id(str(candidate["entry_id"]))
                        target = entry_directory(roots, kind, digest)
                        if trash_path.exists() and not target.exists():
                            publish_path_no_replace(trash_path, target)
                    raise
                deletion_failed = False
                for candidate, trash_path in detached:
                    try:
                        _remove_validated_tree(trash_path, roots.trash)
                        removed.append(str(candidate["entry_id"]))
                        logical_bytes_removed += int(candidate["logical_bytes"])
                    except (OSError, ValueError, ManagedToolStoreError) as exc:
                        deletion_failed = True
                        retained.append(str(candidate["entry_id"]))
                        diagnostics.append(
                            f"detached trash remains for {candidate['entry_id']}: {exc}"
                        )
                for candidate in included_remnants:
                    path = managed_path(
                        roots.payload,
                        str(candidate["relative_path"]),
                    )
                    try:
                        if path.is_dir():
                            _remove_validated_tree(path, roots.payload)
                        else:
                            validate_regular_single_link_file(
                                path,
                                roots.payload,
                                label="maintenance remnant",
                            ).unlink()
                        logical_bytes_removed += int(candidate["logical_bytes"])
                    except (OSError, ValueError, ManagedToolStoreError) as exc:
                        deletion_failed = True
                        diagnostics.append(
                            f"confirmed remnant remains at "
                            f"{candidate['relative_path']}: {exc}"
                        )
                plan_trash = roots.trash / plan.plan_id
                if plan_trash.is_dir():
                    try:
                        plan_trash.rmdir()
                    except OSError:
                        deletion_failed = True
                        diagnostics.append(
                            f"plan trash remains after uninstall: {plan_trash}"
                        )
                outcome = "interrupted" if deletion_failed else "success"
        except (SmtLockTimeoutError, OSError, ValueError, ManagedToolStoreError) as exc:
            retained.extend(entry_ids)
            diagnostics.append(str(exc))
            outcome = "blocked"
        result = MaintenanceResult(
            plan_id=plan.plan_id,
            operation=plan.operation,
            outcome=outcome,
            logical_bytes_removed=logical_bytes_removed,
            removed_entry_ids=tuple(removed),
            retained_entry_ids=tuple(retained),
            diagnostics=tuple(diagnostics),
            completed_at=utc_now(),
        )
        return _write_result(roots, result)

    if plan.atomicity_policy != "best-effort-per-entry":
        raise ManagedToolStoreError("maintenance plan atomicity policy is invalid")
    released_ids = _released_reference_ids(plan)
    lifecycle_context = (
        store_lifecycle_lock(
            roots,
            mode="exclusive",
            timeout_seconds=lock_timeout_seconds,
            command=f"release stale managed references for {plan.plan_id}",
        )
        if released_ids
        else nullcontext()
    )
    with lifecycle_context:
        with catalog_lock(
            roots,
            mode="exclusive",
            timeout_seconds=lock_timeout_seconds,
            command=f"validate cleanup catalog for {plan.plan_id}",
        ):
            if not _catalog_matches_plan(roots, plan):
                raise ManagedToolStoreError(
                    "catalog changed after planning; create a new plan"
                )
            if not _reference_classifications_match_plan(roots, plan):
                raise ManagedToolStoreError(
                    "workspace reference classification changed after planning"
                )
            if released_ids:
                catalog = load_catalog(roots)
                references = dict(catalog["references"])
                for reference_id in released_ids:
                    references.pop(reference_id, None)
                catalog["references"] = references
                write_catalog(roots, catalog)
    for candidate in included_entries:
        entry_id = str(candidate["entry_id"])
        kind, digest = _split_entry_id(entry_id)
        try:
            with catalog_lock(
                roots,
                mode="exclusive",
                timeout_seconds=lock_timeout_seconds,
                command=f"protect unused-entry detach {entry_id}",
            ), entry_lock(
                roots,
                kind,
                digest,
                mode="exclusive",
                timeout_seconds=lock_timeout_seconds,
                command=f"clean unused managed entry {entry_id}",
            ):
                current_catalog = load_catalog(roots)
                current_references = _reference_index(
                    CatalogReference.from_payload(raw)
                    for raw in current_catalog["references"].values()
                )
                if current_references.get(entry_id):
                    raise ManagedToolStoreError(
                        "entry became referenced after planning"
                    )
                if not _candidate_matches_plan(roots, candidate):
                    raise ManagedToolStoreError(
                        "entry changed after planning"
                    )
                detached = _detach_entry(roots, plan.plan_id, candidate)
            _remove_validated_tree(detached, roots.trash)
            removed.append(entry_id)
            logical_bytes_removed += int(candidate["logical_bytes"])
        except (SmtLockTimeoutError, OSError, ValueError, ManagedToolStoreError) as exc:
            retained.append(entry_id)
            diagnostics.append(f"{entry_id}: {exc}")
    plan_trash = roots.trash / plan.plan_id
    if plan_trash.is_dir():
        try:
            plan_trash.rmdir()
        except OSError:
            diagnostics.append(f"plan trash remains after cleanup: {plan_trash}")
    if not included_entries and not released_ids:
        outcome = "no-op"
    elif retained:
        outcome = "partial"
    else:
        outcome = "success"
    result = MaintenanceResult(
        plan_id=plan.plan_id,
        operation=plan.operation,
        outcome=outcome,
        logical_bytes_removed=logical_bytes_removed,
        removed_entry_ids=tuple(removed),
        retained_entry_ids=tuple(retained),
        diagnostics=tuple(diagnostics),
        completed_at=utc_now(),
    )
    return _write_result(roots, result)

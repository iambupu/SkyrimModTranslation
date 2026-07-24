"""Machine-scoped immutable managed-tool storage for SMT workspaces.

The module deliberately contains only standard-library and project-local
dependencies.  Public controllers use it from their bootstrap interpreter
before the shared Python runtime exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import tempfile
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal, Mapping, Sequence

from file_utils import (
    create_regular_directory_under,
    discover_regular_files,
    sha256_file,
    validate_regular_path_under,
)
from smt_windows import (
    ManagedProcessEnvironmentError,
    PinnedDirectoryHandle,
    SmtProcessFileLock,
    get_local_app_data_path,
    publish_path_no_replace,
    read_regular_single_link_bytes,
    remove_regular_tree,
    validate_regular_single_link_file,
)


STORE_LAYOUT_VERSION = 1
ENTRY_SCHEMA_VERSION = 1
COMMIT_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1
BINDING_SCHEMA_VERSION = 1
MAINTENANCE_PLAN_SCHEMA_VERSION = 1
MAINTENANCE_RESULT_SCHEMA_VERSION = 1

APP_DIRECTORY_NAME = "SkyrimModTranslation"
PAYLOAD_DIRECTORY_PARTS = ("managed-tools", f"v{STORE_LAYOUT_VERSION}")
CONTROL_DIRECTORY_PARTS = ("managed-tool-state", f"v{STORE_LAYOUT_VERSION}")
ENTRY_MANIFEST_NAME = "manifest.json"
HEALTHY_COMMIT_NAME = "healthy.commit.json"
INCOMPLETE_MARKER_NAME = ".incomplete"
WORKSPACE_BINDING_RELATIVE_PATH = Path(".workflow") / "managed-tools.json"

_TOOL_KIND_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class ManagedToolStoreError(RuntimeError):
    """Base error for managed-tool state and storage failures."""


class ManagedToolSchemaError(ManagedToolStoreError):
    """A managed-tool JSON document violates its frozen schema."""


class ManagedToolPathError(ManagedToolStoreError):
    """A managed-tool path violates containment or identity rules."""


class EntryStatus(str, Enum):
    HEALTHY = "healthy"
    MISSING = "missing"
    DAMAGED = "damaged"
    INCOMPATIBLE = "incompatible"


class ReferenceStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    STALE = "stale"


@dataclass(frozen=True)
class ManagedStoreRoots:
    base: Path
    application: Path
    payload: Path
    control: Path
    entries: Path
    staging: Path
    trash: Path
    catalog: Path
    locks: Path
    maintenance_plans: Path
    maintenance_results: Path


@dataclass(frozen=True)
class ToolKey:
    tool_kind: str
    inputs: Mapping[str, Any]
    key_digest: str

    @property
    def entry_id(self) -> str:
        return f"{self.tool_kind}:{self.key_digest}"


@dataclass(frozen=True)
class PayloadInventoryItem:
    path: str
    size: int
    sha256: str

    def to_payload(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_payload(cls, payload: object) -> "PayloadInventoryItem":
        row = _strict_object(payload, {"path", "size", "sha256"}, "payload inventory row")
        relative = normalize_relative_path(_required_string(row, "path"))
        size = row["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManagedToolSchemaError("payload inventory size must be a non-negative integer")
        digest = _required_sha256(row, "sha256")
        return cls(relative, size, digest)


@dataclass(frozen=True)
class EntryManifest:
    tool_kind: str
    key_digest: str
    key_inputs: Mapping[str, Any]
    source: Mapping[str, Any]
    platform: Mapping[str, Any]
    critical_entries: tuple[str, ...]
    payload_inventory: tuple[PayloadInventoryItem, ...]
    producer_version: str
    created_at: str
    schema_version: int = ENTRY_SCHEMA_VERSION

    @property
    def entry_id(self) -> str:
        return f"{self.tool_kind}:{self.key_digest}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entry_id": self.entry_id,
            "tool_kind": self.tool_kind,
            "key_digest": self.key_digest,
            "key_inputs": _normalize_json(self.key_inputs),
            "source": _normalize_json(self.source),
            "platform": _normalize_json(self.platform),
            "critical_entries": list(self.critical_entries),
            "payload_inventory": [item.to_payload() for item in self.payload_inventory],
            "producer_version": self.producer_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "EntryManifest":
        row = _strict_object(
            payload,
            {
                "schema_version",
                "entry_id",
                "tool_kind",
                "key_digest",
                "key_inputs",
                "source",
                "platform",
                "critical_entries",
                "payload_inventory",
                "producer_version",
                "created_at",
            },
            "entry manifest",
        )
        if row["schema_version"] != ENTRY_SCHEMA_VERSION:
            raise ManagedToolSchemaError(
                f"unsupported entry manifest schema: {row['schema_version']!r}"
            )
        tool_kind = validate_tool_kind(_required_string(row, "tool_kind"))
        key_digest = _required_sha256(row, "key_digest")
        if row["entry_id"] != f"{tool_kind}:{key_digest}":
            raise ManagedToolSchemaError("entry manifest entry_id does not match its key")
        key_inputs = _required_object(row, "key_inputs")
        if make_tool_key(tool_kind, key_inputs).key_digest != key_digest:
            raise ManagedToolSchemaError(
                "entry manifest key_digest does not match its canonical key inputs"
            )
        source = _required_object(row, "source")
        platform_payload = _required_object(row, "platform")
        critical_raw = row["critical_entries"]
        if not isinstance(critical_raw, list) or not all(
            isinstance(value, str) for value in critical_raw
        ):
            raise ManagedToolSchemaError("critical_entries must be an array of strings")
        critical_entries = tuple(normalize_relative_path(value) for value in critical_raw)
        if len({value.casefold() for value in critical_entries}) != len(critical_entries):
            raise ManagedToolSchemaError("critical_entries contain a case-fold collision")
        inventory_raw = row["payload_inventory"]
        if not isinstance(inventory_raw, list):
            raise ManagedToolSchemaError("payload_inventory must be an array")
        inventory = tuple(PayloadInventoryItem.from_payload(value) for value in inventory_raw)
        _reject_casefold_collisions(item.path for item in inventory)
        reserved_inventory_names = {
            ENTRY_MANIFEST_NAME.casefold(),
            HEALTHY_COMMIT_NAME.casefold(),
            INCOMPLETE_MARKER_NAME.casefold(),
        }
        if any(
            item.path.casefold() in reserved_inventory_names
            for item in inventory
        ):
            raise ManagedToolSchemaError(
                "payload inventory cannot include managed entry metadata"
            )
        producer_version = _required_string(row, "producer_version")
        created_at = _required_aware_timestamp(row, "created_at")
        return cls(
            tool_kind=tool_kind,
            key_digest=key_digest,
            key_inputs=_normalize_json(key_inputs),
            source=_normalize_json(source),
            platform=_normalize_json(platform_payload),
            critical_entries=critical_entries,
            payload_inventory=inventory,
            producer_version=producer_version,
            created_at=created_at,
        )


@dataclass(frozen=True)
class EntryValidation:
    status: EntryStatus
    entry_path: Path
    diagnostics: tuple[str, ...] = ()
    manifest: EntryManifest | None = None

    @property
    def healthy(self) -> bool:
        return self.status is EntryStatus.HEALTHY


@dataclass(frozen=True)
class WorkspaceBindingEntry:
    logical_name: str
    tool_kind: str
    key_digest: str
    entry_point: str

    @property
    def entry_id(self) -> str:
        return f"{self.tool_kind}:{self.key_digest}"

    def to_payload(self) -> dict[str, str]:
        return {
            "logical_name": self.logical_name,
            "entry_id": self.entry_id,
            "tool_kind": self.tool_kind,
            "key_digest": self.key_digest,
            "entry_point": self.entry_point,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "WorkspaceBindingEntry":
        row = _strict_object(
            payload,
            {"logical_name", "entry_id", "tool_kind", "key_digest", "entry_point"},
            "workspace binding entry",
        )
        logical_name = _required_string(row, "logical_name")
        tool_kind = validate_tool_kind(_required_string(row, "tool_kind"))
        key_digest = _required_sha256(row, "key_digest")
        entry_point = normalize_relative_path(_required_string(row, "entry_point"))
        if row["entry_id"] != f"{tool_kind}:{key_digest}":
            raise ManagedToolSchemaError("workspace binding entry_id does not match its key")
        return cls(logical_name, tool_kind, key_digest, entry_point)


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_id: str
    game_id: str
    generation: str
    generated_at: str
    validation_level: str
    validation_result: str
    entries: tuple[WorkspaceBindingEntry, ...]
    schema_version: int = BINDING_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "game_id": self.game_id,
            "generation": self.generation,
            "generated_at": self.generated_at,
            "validation_level": self.validation_level,
            "validation_result": self.validation_result,
            "entries": [entry.to_payload() for entry in self.entries],
        }

    @classmethod
    def from_payload(cls, payload: object) -> "WorkspaceBinding":
        row = _strict_object(
            payload,
            {
                "schema_version",
                "workspace_id",
                "game_id",
                "generation",
                "generated_at",
                "validation_level",
                "validation_result",
                "entries",
            },
            "workspace binding",
        )
        if row["schema_version"] != BINDING_SCHEMA_VERSION:
            raise ManagedToolSchemaError(
                f"unsupported workspace binding schema: {row['schema_version']!r}"
            )
        workspace_id = _required_uuid(row, "workspace_id")
        generation = _required_uuid(row, "generation")
        game_id = _required_string(row, "game_id")
        generated_at = _required_aware_timestamp(row, "generated_at")
        validation_level = _required_string(row, "validation_level")
        validation_result = _required_string(row, "validation_result")
        if validation_level != "complete" or validation_result != "healthy":
            raise ManagedToolSchemaError(
                "workspace binding validation must be complete and healthy"
            )
        entries_raw = row["entries"]
        if not isinstance(entries_raw, list):
            raise ManagedToolSchemaError("workspace binding entries must be an array")
        entries = tuple(WorkspaceBindingEntry.from_payload(value) for value in entries_raw)
        logical_names = [entry.logical_name.casefold() for entry in entries]
        if len(logical_names) != len(set(logical_names)):
            raise ManagedToolSchemaError("workspace binding contains duplicate logical names")
        return cls(
            workspace_id,
            game_id,
            generation,
            generated_at,
            validation_level,
            validation_result,
            entries,
        )


@dataclass(frozen=True)
class CatalogReference:
    workspace_id: str
    workspace_path: str
    game_id: str
    generation: str
    status: ReferenceStatus
    entry_ids: tuple[str, ...]
    updated_at: str

    @property
    def reference_id(self) -> str:
        return f"{self.workspace_id}:{self.generation}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_path": self.workspace_path,
            "game_id": self.game_id,
            "generation": self.generation,
            "status": self.status.value,
            "entry_ids": list(self.entry_ids),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "CatalogReference":
        row = _strict_object(
            payload,
            {
                "workspace_id",
                "workspace_path",
                "game_id",
                "generation",
                "status",
                "entry_ids",
                "updated_at",
            },
            "catalog reference",
        )
        workspace_id = _required_uuid(row, "workspace_id")
        generation = _required_uuid(row, "generation")
        try:
            status = ReferenceStatus(_required_string(row, "status"))
        except ValueError as exc:
            raise ManagedToolSchemaError("catalog reference status is invalid") from exc
        entry_ids_raw = row["entry_ids"]
        if not isinstance(entry_ids_raw, list) or not all(
            isinstance(value, str) for value in entry_ids_raw
        ):
            raise ManagedToolSchemaError("catalog reference entry_ids must be strings")
        if len(entry_ids_raw) != len(set(entry_ids_raw)):
            raise ManagedToolSchemaError(
                "catalog reference contains duplicate entry IDs"
            )
        entry_ids = tuple(sorted(entry_ids_raw))
        for entry_id in entry_ids:
            _validate_entry_id(entry_id)
        workspace_path = _required_string(row, "workspace_path")
        if not Path(workspace_path).is_absolute():
            raise ManagedToolSchemaError(
                "catalog reference workspace_path must be absolute"
            )
        return cls(
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            game_id=_required_string(row, "game_id"),
            generation=generation,
            status=status,
            entry_ids=entry_ids,
            updated_at=_required_aware_timestamp(row, "updated_at"),
        )


@dataclass(frozen=True)
class MaintenancePlan:
    plan_id: str
    operation: str
    atomicity_policy: str
    created_at: str
    expires_at: str
    candidates: tuple[Mapping[str, Any], ...]
    references: tuple[Mapping[str, Any], ...]
    confirmation_token: str
    schema_version: int = MAINTENANCE_PLAN_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "operation": self.operation,
            "atomicity_policy": self.atomicity_policy,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "candidates": [_normalize_json(value) for value in self.candidates],
            "references": [_normalize_json(value) for value in self.references],
            "confirmation_token": self.confirmation_token,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "MaintenancePlan":
        row = _strict_object(
            payload,
            {
                "schema_version",
                "plan_id",
                "operation",
                "atomicity_policy",
                "created_at",
                "expires_at",
                "candidates",
                "references",
                "confirmation_token",
            },
            "maintenance plan",
        )
        if row["schema_version"] != MAINTENANCE_PLAN_SCHEMA_VERSION:
            raise ManagedToolSchemaError("unsupported maintenance plan schema")
        operation = _required_string(row, "operation")
        expected_atomicity = {
            "clean-unused": "best-effort-per-entry",
            "uninstall": "all-or-nothing",
        }.get(operation)
        if expected_atomicity is None:
            raise ManagedToolSchemaError("maintenance plan operation is invalid")
        atomicity_policy = _required_string(row, "atomicity_policy")
        if atomicity_policy != expected_atomicity:
            raise ManagedToolSchemaError(
                "maintenance plan atomicity does not match its operation"
            )
        created_at = _required_aware_timestamp(row, "created_at")
        expires_at = _required_aware_timestamp(row, "expires_at")
        if datetime.fromisoformat(expires_at) <= datetime.fromisoformat(created_at):
            raise ManagedToolSchemaError(
                "maintenance plan expiry must be later than creation"
            )
        candidates = tuple(
            _validate_maintenance_candidate(value, operation=operation)
            for value in _object_array(
                row["candidates"],
                "maintenance candidates",
            )
        )
        references = tuple(
            _validate_maintenance_reference(value, operation=operation)
            for value in _object_array(
                row["references"],
                "maintenance references",
            )
        )
        candidate_ids = [
            (
                f"entry:{candidate['entry_id']}"
                if candidate["candidate_type"] == "entry"
                else f"remnant:{candidate['relative_path'].casefold()}"
            )
            for candidate in candidates
        ]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ManagedToolSchemaError(
                "maintenance plan contains duplicate candidates"
            )
        reference_ids = [
            f"{reference['workspace_id']}:{reference['generation']}"
            for reference in references
        ]
        if len(reference_ids) != len(set(reference_ids)):
            raise ManagedToolSchemaError(
                "maintenance plan contains duplicate references"
            )
        retained_reference_ids = {
            reference_id
            for reference_id, reference in zip(
                reference_ids,
                references,
                strict=True,
            )
            if reference["effect"] != "release-explicit-stale-reference"
        }
        for candidate in candidates:
            if not set(candidate["referenced_by"]).issubset(
                retained_reference_ids
            ):
                raise ManagedToolSchemaError(
                    "maintenance candidate references an unknown or released "
                    "catalog reference"
                )
        return cls(
            plan_id=_required_uuid(row, "plan_id"),
            operation=operation,
            atomicity_policy=atomicity_policy,
            created_at=created_at,
            expires_at=expires_at,
            candidates=candidates,
            references=references,
            confirmation_token=_required_sha256(row, "confirmation_token"),
        )


@dataclass(frozen=True)
class MaintenanceResult:
    plan_id: str
    operation: str
    outcome: str
    logical_bytes_removed: int
    removed_entry_ids: tuple[str, ...]
    retained_entry_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
    completed_at: str
    schema_version: int = MAINTENANCE_RESULT_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "operation": self.operation,
            "outcome": self.outcome,
            "logical_bytes_removed": self.logical_bytes_removed,
            "removed_entry_ids": list(self.removed_entry_ids),
            "retained_entry_ids": list(self.retained_entry_ids),
            "diagnostics": list(self.diagnostics),
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "MaintenanceResult":
        row = _strict_object(
            payload,
            {
                "schema_version",
                "plan_id",
                "operation",
                "outcome",
                "logical_bytes_removed",
                "removed_entry_ids",
                "retained_entry_ids",
                "diagnostics",
                "completed_at",
            },
            "maintenance result",
        )
        if row["schema_version"] != MAINTENANCE_RESULT_SCHEMA_VERSION:
            raise ManagedToolSchemaError("unsupported maintenance result schema")
        logical_bytes_removed = row["logical_bytes_removed"]
        if (
            not isinstance(logical_bytes_removed, int)
            or isinstance(logical_bytes_removed, bool)
            or logical_bytes_removed < 0
        ):
            raise ManagedToolSchemaError(
                "maintenance logical_bytes_removed must be non-negative"
            )
        removed = _entry_id_array(row["removed_entry_ids"], "removed_entry_ids")
        retained = _entry_id_array(row["retained_entry_ids"], "retained_entry_ids")
        diagnostics = _string_array(row["diagnostics"], "diagnostics")
        operation = _required_string(row, "operation")
        if operation not in {"clean-unused", "uninstall"}:
            raise ManagedToolSchemaError("maintenance result operation is invalid")
        outcome = _required_string(row, "outcome")
        allowed_outcomes = {
            "clean-unused": {"no-op", "success", "partial", "blocked"},
            "uninstall": {"success", "blocked", "interrupted"},
        }
        if outcome not in allowed_outcomes[operation]:
            raise ManagedToolSchemaError(
                "maintenance result outcome is invalid for its operation"
            )
        if set(removed) & set(retained):
            raise ManagedToolSchemaError(
                "maintenance result cannot both remove and retain one entry"
            )
        return cls(
            plan_id=_required_uuid(row, "plan_id"),
            operation=operation,
            outcome=outcome,
            logical_bytes_removed=logical_bytes_removed,
            removed_entry_ids=removed,
            retained_entry_ids=retained,
            diagnostics=diagnostics,
            completed_at=_required_aware_timestamp(row, "completed_at"),
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_managed_store_roots(test_base: Path | str | None = None) -> ManagedStoreRoots:
    """Resolve payload/control paths without creating either store."""

    base = Path(test_base) if test_base is not None else get_local_app_data_path()
    base = base.expanduser().resolve(strict=True)
    application = base / APP_DIRECTORY_NAME
    payload = application.joinpath(*PAYLOAD_DIRECTORY_PARTS)
    control = application.joinpath(*CONTROL_DIRECTORY_PARTS)
    return ManagedStoreRoots(
        base=base,
        application=application,
        payload=payload,
        control=control,
        entries=payload / "entries",
        staging=payload / "staging",
        trash=payload / "trash",
        catalog=control / "catalog.json",
        locks=control / "locks",
        maintenance_plans=control / "maintenance-plans",
        maintenance_results=control / "maintenance-results",
    )


def ensure_store_layout(roots: ManagedStoreRoots) -> None:
    validate_regular_path_under(
        roots.base,
        roots.base,
        kind="directory",
        label="managed-tool base",
    )
    for path in (
        roots.application,
        roots.payload,
        roots.entries,
        roots.staging,
        roots.trash,
        roots.control,
        roots.locks,
        roots.maintenance_plans,
        roots.maintenance_results,
    ):
        create_regular_directory_under(
            path,
            roots.base,
            label="managed-tool store directory",
        )


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        _normalize_json(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def make_tool_key(tool_kind: str, inputs: Mapping[str, Any]) -> ToolKey:
    tool_kind = validate_tool_kind(tool_kind)
    normalized_inputs = _normalize_json(inputs)
    digest = canonical_sha256(
        {
            "schema_version": STORE_LAYOUT_VERSION,
            "tool_kind": tool_kind,
            "inputs": normalized_inputs,
        }
    )
    return ToolKey(tool_kind, normalized_inputs, digest)


def build_python_key(
    *,
    implementation: str,
    full_version: str,
    architecture: str,
    base_interpreter_identity: Mapping[str, Any],
    runtime_lock_sha256: str,
    installer_backend: str,
    installer_backend_version: str,
    installer_schema: int,
) -> ToolKey:
    return make_tool_key(
        "python-runtime",
        {
            "implementation": implementation,
            "full_version": full_version,
            "architecture": architecture,
            "base_interpreter_identity": base_interpreter_identity,
            "runtime_lock_sha256": _validate_sha256(runtime_lock_sha256),
            "installer_backend": installer_backend,
            "installer_backend_version": installer_backend_version,
            "installer_schema": installer_schema,
        },
    )


def build_dotnet_sdk_key(
    *,
    version: str,
    architecture: str,
    source: str,
    package_sha256: str,
    installer_schema: int,
) -> ToolKey:
    return make_tool_key(
        "dotnet-sdk",
        {
            "version": version,
            "architecture": architecture,
            "source": source,
            "package_sha256": _validate_sha256(package_sha256),
            "installer_schema": installer_schema,
        },
    )


def build_decoder_key(
    *,
    tool_name: str,
    pinned_ref: str,
    source: str,
    archive_sha256: str,
    installer_schema: int,
) -> ToolKey:
    return make_tool_key(
        f"decoder-{tool_name.casefold()}",
        {
            "tool_name": tool_name,
            "pinned_ref": pinned_ref,
            "source": source,
            "archive_sha256": _validate_sha256(archive_sha256),
            "installer_schema": installer_schema,
        },
    )


def build_adapter_key(
    *,
    adapter_name: str,
    source_digest: str,
    project_digest: str,
    sdk_entry_id: str,
    configuration: str,
    target_framework: str,
    rid: str,
    architecture: str,
    installer_schema: int,
) -> ToolKey:
    _validate_entry_id(sdk_entry_id)
    return make_tool_key(
        f"adapter-{adapter_name.casefold()}",
        {
            "adapter_name": adapter_name,
            "source_digest": _validate_sha256(source_digest),
            "project_digest": _validate_sha256(project_digest),
            "sdk_entry_id": sdk_entry_id,
            "configuration": configuration,
            "target_framework": target_framework,
            "rid": rid,
            "architecture": architecture,
            "installer_schema": installer_schema,
        },
    )


def validate_tool_kind(value: str) -> str:
    if not _TOOL_KIND_RE.fullmatch(value):
        raise ManagedToolSchemaError(f"invalid managed tool kind: {value!r}")
    return value


def normalize_relative_path(value: str | Path) -> str:
    raw = unicodedata.normalize("NFC", os.fspath(value)).replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ManagedToolPathError("managed relative path is empty or contains NUL")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ManagedToolPathError(f"managed path must be relative: {value}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManagedToolPathError(f"managed path contains traversal or empty segments: {value}")
    normalized_parts: list[str] = []
    for part in parts:
        if ":" in part or part != part.rstrip(" ."):
            raise ManagedToolPathError(f"managed path contains a Windows alias: {value}")
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ManagedToolPathError(f"managed path uses a reserved Windows name: {value}")
        normalized_parts.append(part)
    normalized = PurePosixPath(*normalized_parts).as_posix()
    if normalized in {"", "."}:
        raise ManagedToolPathError("managed relative path is empty")
    return normalized


def managed_path(
    root: Path,
    relative: str | Path,
    *,
    must_exist: bool = False,
    kind: Literal["file", "directory"] | None = None,
    label: str = "managed path",
) -> Path:
    normalized = normalize_relative_path(relative)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    if not must_exist:
        try:
            common = os.path.commonpath((os.path.abspath(candidate), os.path.abspath(root)))
        except ValueError as exc:
            raise ManagedToolPathError(f"{label} escaped its root") from exc
        if os.path.normcase(common) != os.path.normcase(os.path.abspath(root)):
            raise ManagedToolPathError(f"{label} escaped its root")
        return candidate
    if kind is None:
        raise ValueError("kind is required when must_exist is true")
    try:
        if kind == "file":
            return validate_regular_single_link_file(
                candidate,
                root,
                label=label,
            )
        return validate_regular_path_under(
            candidate,
            root,
            kind=kind,
            label=label,
        )
    except (OSError, ValueError) as exc:
        raise ManagedToolPathError(str(exc)) from exc


def entry_directory(roots: ManagedStoreRoots, tool_kind: str, key_digest: str) -> Path:
    return managed_path(
        roots.entries,
        f"{validate_tool_kind(tool_kind)}/{_validate_sha256(key_digest)}",
    )


def current_platform_identity() -> dict[str, str]:
    return {
        "system": platform.system().casefold(),
        "machine": platform.machine().casefold(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }


def build_payload_inventory(entry_root: Path) -> tuple[PayloadInventoryItem, ...]:
    files = discover_regular_files(entry_root, label="managed entry payload")
    items: list[PayloadInventoryItem] = []
    for path in files:
        relative = path.relative_to(entry_root).as_posix()
        if relative in {ENTRY_MANIFEST_NAME, HEALTHY_COMMIT_NAME, INCOMPLETE_MARKER_NAME}:
            continue
        normalized = normalize_relative_path(relative)
        items.append(PayloadInventoryItem(normalized, path.stat().st_size, sha256_file(path)))
    items.sort(key=lambda item: item.path.casefold())
    _reject_casefold_collisions(item.path for item in items)
    return tuple(items)


def make_entry_manifest(
    *,
    key: ToolKey,
    entry_root: Path,
    source: Mapping[str, Any],
    critical_entries: Sequence[str],
    producer_version: str,
    platform_identity: Mapping[str, Any] | None = None,
) -> EntryManifest:
    inventory = build_payload_inventory(entry_root)
    inventory_paths = {item.path.casefold() for item in inventory}
    critical = tuple(normalize_relative_path(value) for value in critical_entries)
    for relative in critical:
        if relative.casefold() not in inventory_paths:
            raise ManagedToolStoreError(
                f"critical entry is absent from the payload inventory: {relative}"
            )
    return EntryManifest(
        tool_kind=key.tool_kind,
        key_digest=key.key_digest,
        key_inputs=key.inputs,
        source=_normalize_json(source),
        platform=_normalize_json(platform_identity or current_platform_identity()),
        critical_entries=critical,
        payload_inventory=inventory,
        producer_version=producer_version,
        created_at=utc_now(),
    )


def manifest_sha256(manifest: EntryManifest) -> str:
    return canonical_sha256(manifest.to_payload())


def atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    allowed_root: Path,
) -> None:
    _prepare_atomic_parent(path, allowed_root)
    with PinnedDirectoryHandle(path.parent, allowed_root):
        if os.path.lexists(path):
            validate_regular_single_link_file(
                path,
                allowed_root,
                label="managed JSON target",
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(
                    json.dumps(
                        _normalize_json(payload),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                )
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            validate_regular_single_link_file(
                temporary,
                allowed_root,
                label="managed JSON temporary file",
            )
            os.replace(temporary, path)
            validate_regular_single_link_file(
                path,
                allowed_root,
                label="managed JSON result",
            )
        finally:
            temporary.unlink(missing_ok=True)


def atomic_create_json_no_replace(
    path: Path,
    payload: Mapping[str, Any],
    *,
    allowed_root: Path,
) -> None:
    _prepare_atomic_parent(path, allowed_root)
    with PinnedDirectoryHandle(path.parent, allowed_root):
        if os.path.lexists(path):
            raise FileExistsError(path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(
                    json.dumps(
                        _normalize_json(payload),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                )
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            validate_regular_single_link_file(
                temporary,
                allowed_root,
                label="managed commit temporary file",
            )
            publish_path_no_replace(temporary, path)
            validate_regular_single_link_file(
                path,
                allowed_root,
                label="managed commit file",
            )
        finally:
            temporary.unlink(missing_ok=True)


def write_manifest(entry_root: Path, manifest: EntryManifest, *, entries_root: Path) -> Path:
    path = entry_root / ENTRY_MANIFEST_NAME
    atomic_write_json(path, manifest.to_payload(), allowed_root=entries_root)
    return path


def commit_entry(entry_root: Path, manifest: EntryManifest, *, entries_root: Path) -> Path:
    manifest_path = validate_regular_single_link_file(
        entry_root / ENTRY_MANIFEST_NAME,
        entries_root,
        label="managed entry manifest",
    )
    manifest_bytes = read_regular_single_link_bytes(
        manifest_path,
        entries_root,
        label="managed entry manifest",
    )
    on_disk_manifest = EntryManifest.from_payload(
        _decode_json_object(manifest_bytes, manifest_path)
    )
    if on_disk_manifest.to_payload() != manifest.to_payload():
        raise ManagedToolStoreError("managed entry manifest changed before commit")
    commit_path = entry_root / HEALTHY_COMMIT_NAME
    atomic_create_json_no_replace(
        commit_path,
        {
            "schema_version": COMMIT_SCHEMA_VERSION,
            "key_digest": manifest.key_digest,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        allowed_root=entries_root,
    )
    return commit_path


def validate_entry(
    roots: ManagedStoreRoots,
    tool_kind: str,
    key_digest: str,
    *,
    deep: bool = False,
    expected_platform: Mapping[str, Any] | None = None,
) -> EntryValidation:
    target = entry_directory(roots, tool_kind, key_digest)
    if not os.path.lexists(target):
        return EntryValidation(EntryStatus.MISSING, target, ("entry directory is missing",))
    diagnostics: list[str] = []
    try:
        validated_target = validate_regular_path_under(
            target,
            roots.entries,
            kind="directory",
            label="managed entry",
        )
        manifest_path = validate_regular_single_link_file(
            validated_target / ENTRY_MANIFEST_NAME,
            roots.entries,
            label="managed entry manifest",
        )
        commit_path = validate_regular_single_link_file(
            validated_target / HEALTHY_COMMIT_NAME,
            roots.entries,
            label="managed healthy commit",
        )
        manifest_bytes = read_regular_single_link_bytes(
            manifest_path,
            roots.entries,
            label="managed entry manifest",
        )
        manifest = EntryManifest.from_payload(
            _decode_json_object(manifest_bytes, manifest_path)
        )
        commit = _strict_object(
            _read_json_object(commit_path, roots.entries),
            {"schema_version", "key_digest", "manifest_sha256"},
            "healthy commit",
        )
        if commit["schema_version"] != COMMIT_SCHEMA_VERSION:
            raise ManagedToolSchemaError("unsupported healthy commit schema")
        if _required_sha256(commit, "key_digest") != manifest.key_digest:
            raise ManagedToolSchemaError("healthy commit key does not match manifest")
        if _required_sha256(commit, "manifest_sha256") != hashlib.sha256(
            manifest_bytes
        ).hexdigest():
            raise ManagedToolSchemaError("healthy commit manifest digest does not match")
    except FileNotFoundError as exc:
        return EntryValidation(EntryStatus.DAMAGED, target, (f"entry metadata is missing: {exc}",))
    except (
        OSError,
        ValueError,
        ManagedProcessEnvironmentError,
        ManagedToolStoreError,
    ) as exc:
        return EntryValidation(EntryStatus.DAMAGED, target, (str(exc),))

    if manifest.tool_kind != tool_kind or manifest.key_digest != key_digest:
        diagnostics.append("entry identity does not match the requested key")
        return EntryValidation(
            EntryStatus.INCOMPATIBLE,
            target,
            tuple(diagnostics),
            manifest,
        )
    if expected_platform is not None and _normalize_json(manifest.platform) != _normalize_json(
        expected_platform
    ):
        diagnostics.append("entry platform does not match the requested platform")
        return EntryValidation(
            EntryStatus.INCOMPATIBLE,
            target,
            tuple(diagnostics),
            manifest,
        )
    inventory_by_path = {item.path.casefold(): item for item in manifest.payload_inventory}
    try:
        for relative in manifest.critical_entries:
            item = inventory_by_path.get(relative.casefold())
            if item is None:
                raise ManagedToolSchemaError(
                    f"critical entry is absent from manifest inventory: {relative}"
                )
            path = managed_path(
                target,
                relative,
                must_exist=True,
                kind="file",
                label="managed critical entry",
            )
            if path.stat().st_size != item.size or sha256_file(path) != item.sha256:
                raise ManagedToolStoreError(f"critical entry hash mismatch: {relative}")
        if deep:
            actual_inventory = build_payload_inventory(target)
            if tuple(item.to_payload() for item in actual_inventory) != tuple(
                item.to_payload() for item in manifest.payload_inventory
            ):
                raise ManagedToolStoreError("complete payload inventory does not match")
    except (
        OSError,
        ValueError,
        ManagedProcessEnvironmentError,
        ManagedToolStoreError,
    ) as exc:
        return EntryValidation(EntryStatus.DAMAGED, target, (str(exc),), manifest)
    return EntryValidation(EntryStatus.HEALTHY, target, (), manifest)


def create_staging_directory(roots: ManagedStoreRoots, *, prefix: str) -> Path:
    ensure_store_layout(roots)
    validate_tool_kind(prefix)
    path = roots.staging / f"{prefix}-{uuid.uuid4()}"
    path.mkdir()
    return validate_regular_path_under(
        path,
        roots.staging,
        kind="directory",
        label="managed staging directory",
    )


def publish_movable_entry(
    roots: ManagedStoreRoots,
    staging_directory: Path,
    manifest: EntryManifest,
    *,
    entry_lock_held: bool = False,
) -> Path:
    ensure_store_layout(roots)
    staging_directory = validate_regular_path_under(
        staging_directory,
        roots.staging,
        kind="directory",
        label="managed staging payload",
    )
    write_manifest(staging_directory, manifest, entries_root=roots.payload)
    commit_entry(staging_directory, manifest, entries_root=roots.payload)
    target = entry_directory(roots, manifest.tool_kind, manifest.key_digest)
    def publish_under_lock() -> Path:
        create_regular_directory_under(
            target.parent,
            roots.entries,
            label="managed entry kind directory",
        )
        try:
            publish_path_no_replace(staging_directory, target)
        except FileExistsError:
            existing = validate_entry(
                roots,
                manifest.tool_kind,
                manifest.key_digest,
                deep=True,
            )
            if not existing.healthy:
                raise ManagedToolStoreError(
                    "a competing managed entry exists but failed validation"
                )
            remove_regular_tree(
                staging_directory,
                roots.staging,
                label="competing managed staging payload",
            )
        result = validate_entry(
            roots,
            manifest.tool_kind,
            manifest.key_digest,
            deep=True,
        )
        if not result.healthy:
            raise ManagedToolStoreError(
                "published managed entry failed validation: "
                + " | ".join(result.diagnostics)
            )
        return result.entry_path
    if entry_lock_held:
        return publish_under_lock()
    with entry_lock(
        roots,
        manifest.tool_kind,
        manifest.key_digest,
        mode="exclusive",
        timeout_seconds=300.0,
        command="publish managed-tool entry",
    ):
        return publish_under_lock()


def catalog_lock(
    roots: ManagedStoreRoots,
    *,
    mode: Literal["shared", "exclusive"],
    timeout_seconds: float,
    command: str | None = None,
) -> SmtProcessFileLock:
    ensure_store_layout(roots)
    return SmtProcessFileLock(
        roots.locks / "catalog.lock",
        mode,
        timeout_seconds,
        command=command,
        allowed_root=roots.control,
    )


def store_lifecycle_lock(
    roots: ManagedStoreRoots,
    *,
    mode: Literal["shared", "exclusive"],
    timeout_seconds: float,
    command: str | None = None,
) -> SmtProcessFileLock:
    """Coordinate shared provisioning with destructive full-store uninstall."""

    ensure_store_layout(roots)
    return SmtProcessFileLock(
        roots.locks / "store-lifecycle.lock",
        mode,
        timeout_seconds,
        command=command,
        allowed_root=roots.control,
    )


def entry_lock(
    roots: ManagedStoreRoots,
    tool_kind: str,
    key_digest: str,
    *,
    mode: Literal["shared", "exclusive"],
    timeout_seconds: float,
    command: str | None = None,
) -> SmtProcessFileLock:
    ensure_store_layout(roots)
    name = f"{validate_tool_kind(tool_kind)}-{_validate_sha256(key_digest)}.lock"
    return SmtProcessFileLock(
        roots.locks / "entries" / name,
        mode,
        timeout_seconds,
        command=command,
        allowed_root=roots.control,
    )


@contextmanager
def acquire_entry_locks(
    roots: ManagedStoreRoots,
    entry_ids: Sequence[str],
    *,
    mode: Literal["shared", "exclusive"],
    timeout_seconds: float,
    command: str | None = None,
) -> Iterator[tuple[SmtProcessFileLock, ...]]:
    parsed = sorted((_split_entry_id(value) for value in set(entry_ids)))
    locks: list[SmtProcessFileLock] = []
    with ExitStack() as stack:
        for tool_kind, key_digest in parsed:
            lock = entry_lock(
                roots,
                tool_kind,
                key_digest,
                mode=mode,
                timeout_seconds=timeout_seconds,
                command=command,
            )
            stack.enter_context(lock)
            locks.append(lock)
        yield tuple(locks)


def empty_catalog() -> dict[str, Any]:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "updated_at": utc_now(),
        "references": {},
    }


def load_catalog(roots: ManagedStoreRoots) -> dict[str, Any]:
    if not os.path.lexists(roots.catalog):
        return empty_catalog()
    if not roots.catalog.is_file():
        raise ManagedToolPathError(
            f"managed-tool catalog is not a regular file: {roots.catalog}"
        )
    validate_regular_single_link_file(
        roots.catalog,
        roots.control,
        label="managed-tool catalog",
    )
    payload = _strict_object(
        _read_json_object(roots.catalog, roots.control),
        {"schema_version", "updated_at", "references"},
        "managed-tool catalog",
    )
    if payload["schema_version"] != CATALOG_SCHEMA_VERSION:
        raise ManagedToolSchemaError(
            f"unsupported managed-tool catalog schema: {payload['schema_version']!r}"
        )
    _required_aware_timestamp(payload, "updated_at")
    references = payload["references"]
    if not isinstance(references, dict) or not all(
        isinstance(key, str) for key in references
    ):
        raise ManagedToolSchemaError("managed-tool catalog references must be an object")
    normalized_references: dict[str, Any] = {}
    for reference_id, raw in sorted(references.items()):
        reference = CatalogReference.from_payload(raw)
        if reference.reference_id != reference_id:
            raise ManagedToolSchemaError("catalog reference id does not match its payload")
        normalized_references[reference_id] = reference.to_payload()
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "updated_at": payload["updated_at"],
        "references": normalized_references,
    }


def write_catalog(roots: ManagedStoreRoots, payload: Mapping[str, Any]) -> None:
    normalized = load_catalog_payload(payload)
    normalized["updated_at"] = utc_now()
    atomic_write_json(roots.catalog, normalized, allowed_root=roots.control)


def load_catalog_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = _strict_object(
        payload,
        {"schema_version", "updated_at", "references"},
        "managed-tool catalog",
    )
    if row["schema_version"] != CATALOG_SCHEMA_VERSION:
        raise ManagedToolSchemaError("unsupported managed-tool catalog schema")
    references_raw = row["references"]
    if not isinstance(references_raw, dict):
        raise ManagedToolSchemaError("catalog references must be an object")
    references: dict[str, Any] = {}
    for reference_id, raw in references_raw.items():
        if not isinstance(reference_id, str):
            raise ManagedToolSchemaError("catalog reference ids must be strings")
        reference = CatalogReference.from_payload(raw)
        if reference.reference_id != reference_id:
            raise ManagedToolSchemaError("catalog reference id does not match its payload")
        references[reference_id] = reference.to_payload()
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "updated_at": _required_string(row, "updated_at"),
        "references": dict(sorted(references.items())),
    }


def reserve_catalog_reference(
    roots: ManagedStoreRoots,
    *,
    workspace_id: str,
    workspace_path: Path,
    game_id: str,
    generation: str,
    entry_ids: Sequence[str],
    timeout_seconds: float = 10.0,
) -> CatalogReference:
    workspace_id = str(uuid.UUID(workspace_id))
    generation = str(uuid.UUID(generation))
    normalized_entry_ids = tuple(sorted(set(entry_ids)))
    for entry_id in normalized_entry_ids:
        _validate_entry_id(entry_id)
    reference = CatalogReference(
        workspace_id=workspace_id,
        workspace_path=str(workspace_path.resolve(strict=False)),
        game_id=game_id,
        generation=generation,
        status=ReferenceStatus.PENDING,
        entry_ids=normalized_entry_ids,
        updated_at=utc_now(),
    )
    with catalog_lock(
        roots,
        mode="exclusive",
        timeout_seconds=timeout_seconds,
        command="reserve managed-tool binding",
    ):
        catalog = load_catalog(roots)
        references = dict(catalog["references"])
        existing = references.get(reference.reference_id)
        if existing is not None:
            existing_reference = CatalogReference.from_payload(existing)
            if (
                existing_reference.workspace_id != reference.workspace_id
                or existing_reference.game_id != reference.game_id
                or existing_reference.entry_ids != reference.entry_ids
            ):
                raise ManagedToolStoreError(
                    "binding generation is already reserved for different entries"
                )
            refreshed = CatalogReference(
                workspace_id=existing_reference.workspace_id,
                workspace_path=reference.workspace_path,
                game_id=existing_reference.game_id,
                generation=existing_reference.generation,
                status=existing_reference.status,
                entry_ids=existing_reference.entry_ids,
                updated_at=utc_now(),
            )
            references[reference.reference_id] = refreshed.to_payload()
            catalog["references"] = references
            write_catalog(roots, catalog)
            return refreshed
        references[reference.reference_id] = reference.to_payload()
        catalog["references"] = references
        write_catalog(roots, catalog)
    return reference


def replace_pending_catalog_reference(
    roots: ManagedStoreRoots,
    *,
    workspace_id: str,
    workspace_path: Path,
    game_id: str,
    old_generation: str,
    new_generation: str,
    entry_ids: Sequence[str],
    timeout_seconds: float = 10.0,
) -> CatalogReference:
    """Atomically replace one unpublished binding reservation."""

    workspace_id = str(uuid.UUID(workspace_id))
    old_generation = str(uuid.UUID(old_generation))
    new_generation = str(uuid.UUID(new_generation))
    normalized_entry_ids = tuple(sorted(set(entry_ids)))
    for entry_id in normalized_entry_ids:
        _validate_entry_id(entry_id)
    old_reference_id = f"{workspace_id}:{old_generation}"
    replacement = CatalogReference(
        workspace_id=workspace_id,
        workspace_path=str(workspace_path.resolve(strict=False)),
        game_id=game_id,
        generation=new_generation,
        status=ReferenceStatus.PENDING,
        entry_ids=normalized_entry_ids,
        updated_at=utc_now(),
    )
    with catalog_lock(
        roots,
        mode="exclusive",
        timeout_seconds=timeout_seconds,
        command="replace pending managed-tool binding reservation",
    ):
        catalog = load_catalog(roots)
        references = dict(catalog["references"])
        old_payload = references.get(old_reference_id)
        if old_payload is None:
            raise ManagedToolStoreError(
                "pending catalog reference to replace is missing"
            )
        old_reference = CatalogReference.from_payload(old_payload)
        if (
            old_reference.workspace_id != workspace_id
            or old_reference.game_id != game_id
            or old_reference.status is not ReferenceStatus.PENDING
        ):
            raise ManagedToolStoreError(
                "catalog reference replacement requires the exact pending "
                "workspace reservation"
            )
        existing_payload = references.get(replacement.reference_id)
        if (
            existing_payload is not None
            and replacement.reference_id != old_reference_id
        ):
            existing = CatalogReference.from_payload(existing_payload)
            if (
                existing.workspace_id != replacement.workspace_id
                or existing.game_id != replacement.game_id
                or existing.status is not ReferenceStatus.PENDING
                or existing.entry_ids != replacement.entry_ids
            ):
                raise ManagedToolStoreError(
                    "replacement binding generation is already reserved "
                    "for different entries"
                )
        references.pop(old_reference_id)
        references[replacement.reference_id] = replacement.to_payload()
        catalog["references"] = references
        write_catalog(roots, catalog)
    return replacement


def _prepare_catalog_promotion(
    catalog: Mapping[str, Any],
    *,
    workspace_id: str,
    generation: str,
) -> tuple[CatalogReference, dict[str, Any]]:
    workspace_id = str(uuid.UUID(workspace_id))
    generation = str(uuid.UUID(generation))
    reference_id = f"{workspace_id}:{generation}"
    prepared = load_catalog_payload(catalog)
    references = dict(prepared["references"])
    if reference_id not in references:
        raise ManagedToolStoreError("pending catalog reference is missing")
    current = CatalogReference.from_payload(references[reference_id])
    if current.status not in {ReferenceStatus.PENDING, ReferenceStatus.ACTIVE}:
        raise ManagedToolStoreError("catalog reference cannot be promoted from stale")
    updated = CatalogReference(
        workspace_id=current.workspace_id,
        workspace_path=current.workspace_path,
        game_id=current.game_id,
        generation=current.generation,
        status=ReferenceStatus.ACTIVE,
        entry_ids=current.entry_ids,
        updated_at=utc_now(),
    )
    for other_id, raw in list(references.items()):
        other = CatalogReference.from_payload(raw)
        if (
            other_id != reference_id
            and other.workspace_id == workspace_id
            and other.status is ReferenceStatus.ACTIVE
        ):
            references[other_id] = CatalogReference(
                workspace_id=other.workspace_id,
                workspace_path=other.workspace_path,
                game_id=other.game_id,
                generation=other.generation,
                status=ReferenceStatus.STALE,
                entry_ids=other.entry_ids,
                updated_at=utc_now(),
            ).to_payload()
    references[reference_id] = updated.to_payload()
    prepared["references"] = references
    return updated, prepared


def promote_catalog_reference(
    roots: ManagedStoreRoots,
    *,
    workspace_id: str,
    generation: str,
    timeout_seconds: float = 10.0,
) -> CatalogReference:
    with catalog_lock(
        roots,
        mode="exclusive",
        timeout_seconds=timeout_seconds,
        command="promote managed-tool binding",
    ):
        updated, catalog = _prepare_catalog_promotion(
            load_catalog(roots),
            workspace_id=workspace_id,
            generation=generation,
        )
        write_catalog(roots, catalog)
    return updated


def reconcile_catalog_references(
    roots: ManagedStoreRoots,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, tuple[str, ...]]:
    """Conservatively reconcile only references proven by exact bindings."""

    promoted: list[str] = []
    stale: list[str] = []
    retained: list[str] = []
    with catalog_lock(
        roots,
        mode="exclusive",
        timeout_seconds=timeout_seconds,
        command="reconcile managed-tool references",
    ):
        catalog = load_catalog(roots)
        references = dict(catalog["references"])
        changed = False
        for reference_id, raw in list(references.items()):
            reference = CatalogReference.from_payload(raw)
            workspace = Path(reference.workspace_path)
            binding: WorkspaceBinding | None = None
            try:
                binding = read_workspace_binding(workspace)
            except (
                OSError,
                ValueError,
                ManagedToolStoreError,
                ManagedProcessEnvironmentError,
            ):
                binding = None
            exact = bool(
                binding is not None
                and binding.workspace_id == reference.workspace_id
                and binding.game_id == reference.game_id
                and binding.generation == reference.generation
                and tuple(sorted(entry.entry_id for entry in binding.entries))
                == reference.entry_ids
            )
            if reference.status is ReferenceStatus.PENDING and exact:
                references[reference_id] = CatalogReference(
                    workspace_id=reference.workspace_id,
                    workspace_path=str(workspace.resolve(strict=False)),
                    game_id=reference.game_id,
                    generation=reference.generation,
                    status=ReferenceStatus.ACTIVE,
                    entry_ids=reference.entry_ids,
                    updated_at=utc_now(),
                ).to_payload()
                for other_id, other_raw in list(references.items()):
                    if other_id == reference_id:
                        continue
                    other = CatalogReference.from_payload(other_raw)
                    if (
                        other.workspace_id == reference.workspace_id
                        and other.status is ReferenceStatus.ACTIVE
                    ):
                        references[other_id] = CatalogReference(
                            workspace_id=other.workspace_id,
                            workspace_path=other.workspace_path,
                            game_id=other.game_id,
                            generation=other.generation,
                            status=ReferenceStatus.STALE,
                            entry_ids=other.entry_ids,
                            updated_at=utc_now(),
                        ).to_payload()
                        stale.append(other_id)
                promoted.append(reference_id)
                changed = True
            else:
                # Missing or mismatched evidence never releases a reference.
                retained.append(reference_id)
        if changed:
            catalog["references"] = references
            write_catalog(roots, catalog)
    return {
        "promoted": tuple(sorted(promoted)),
        "stale": tuple(sorted(stale)),
        "retained": tuple(sorted(retained)),
    }


def write_workspace_binding(workspace: Path, binding: WorkspaceBinding) -> Path:
    workspace = workspace.resolve(strict=True)
    validate_regular_path_under(
        workspace,
        workspace,
        kind="directory",
        label="workspace",
    )
    path = workspace / WORKSPACE_BINDING_RELATIVE_PATH
    atomic_write_json(path, binding.to_payload(), allowed_root=workspace)
    return path


def read_workspace_binding(workspace: Path) -> WorkspaceBinding:
    workspace = workspace.resolve(strict=True)
    path = validate_regular_single_link_file(
        workspace / WORKSPACE_BINDING_RELATIVE_PATH,
        workspace,
        label="managed-tool workspace binding",
    )
    return WorkspaceBinding.from_payload(_read_json_object(path, workspace))


def _validate_binding_adapter_sdk_dependency(
    binding: WorkspaceBinding,
    adapter_entry: WorkspaceBindingEntry,
    adapter_manifest: EntryManifest | None,
) -> None:
    if not adapter_entry.tool_kind.startswith("adapter-"):
        return
    if adapter_manifest is None:
        raise ManagedToolStoreError(
            f"managed adapter has no validated manifest: {adapter_entry.logical_name}"
        )
    sdk_entry_id = adapter_manifest.key_inputs.get("sdk_entry_id")
    if not isinstance(sdk_entry_id, str):
        raise ManagedToolStoreError(
            f"managed adapter key has no SDK dependency: {adapter_entry.logical_name}"
        )
    sdk_entries = [
        entry
        for entry in binding.entries
        if entry.logical_name.casefold() == "dotnet-sdk"
    ]
    if len(sdk_entries) != 1 or sdk_entries[0].entry_id != sdk_entry_id:
        raise ManagedToolStoreError(
            f"managed adapter SDK dependency differs from the workspace binding: "
            f"{adapter_entry.logical_name}"
        )


def _validate_binding_entry_point(
    entry: WorkspaceBindingEntry,
    manifest: EntryManifest | None,
) -> None:
    if manifest is None:
        raise ManagedToolStoreError(
            f"managed binding entry has no validated manifest: {entry.logical_name}"
        )
    critical = {value.casefold() for value in manifest.critical_entries}
    if entry.entry_point.casefold() not in critical:
        raise ManagedToolStoreError(
            f"managed binding entry point is not a verified critical entry: "
            f"{entry.logical_name}: {entry.entry_point}"
        )


def bind_workspace(
    roots: ManagedStoreRoots,
    workspace: Path,
    binding: WorkspaceBinding,
    *,
    timeout_seconds: float = 10.0,
) -> Path:
    binding = WorkspaceBinding.from_payload(binding.to_payload())
    reserve_catalog_reference(
        roots,
        workspace_id=binding.workspace_id,
        workspace_path=workspace,
        game_id=binding.game_id,
        generation=binding.generation,
        entry_ids=[entry.entry_id for entry in binding.entries],
        timeout_seconds=timeout_seconds,
    )
    validations: dict[str, EntryValidation] = {}
    for entry in binding.entries:
        validation = validate_entry(
            roots,
            entry.tool_kind,
            entry.key_digest,
            deep=True,
        )
        if not validation.healthy:
            raise ManagedToolStoreError(
                f"cannot bind unavailable managed entry {entry.logical_name}: "
                + " | ".join(validation.diagnostics)
            )
        validations[entry.entry_id] = validation
        managed_path(
            validation.entry_path,
            entry.entry_point,
            must_exist=True,
            kind="file",
            label=f"managed binding entry point {entry.logical_name}",
        )
    for entry in binding.entries:
        manifest = validations[entry.entry_id].manifest
        _validate_binding_entry_point(entry, manifest)
        _validate_binding_adapter_sdk_dependency(
            binding,
            entry,
            manifest,
        )
    with catalog_lock(
        roots,
        mode="exclusive",
        timeout_seconds=timeout_seconds,
        command="commit managed-tool workspace binding",
    ):
        updated, catalog = _prepare_catalog_promotion(
            load_catalog(roots),
            workspace_id=binding.workspace_id,
            generation=binding.generation,
        )
        if (
            updated.game_id != binding.game_id
            or updated.entry_ids
            != tuple(sorted(entry.entry_id for entry in binding.entries))
        ):
            raise ManagedToolStoreError(
                "pending catalog reference does not match the workspace binding"
            )
        path = write_workspace_binding(workspace, binding)
        write_catalog(roots, catalog)
    return path


def resolve_bound_entry(
    roots: ManagedStoreRoots,
    workspace: Path,
    logical_name: str,
    *,
    deep: bool = False,
    expected_workspace_id: str | None = None,
    expected_game_id: str | None = None,
) -> tuple[Path, WorkspaceBindingEntry]:
    binding = read_workspace_binding(workspace)
    _validate_expected_binding_identity(
        binding,
        expected_workspace_id=expected_workspace_id,
        expected_game_id=expected_game_id,
    )
    selected = next(
        (
            entry
            for entry in binding.entries
            if entry.logical_name.casefold() == logical_name.casefold()
        ),
        None,
    )
    if selected is None:
        raise ManagedToolStoreError(f"managed-tool binding is missing: {logical_name}")
    validation = validate_entry(
        roots,
        selected.tool_kind,
        selected.key_digest,
        deep=deep,
    )
    if not validation.healthy:
        raise ManagedToolStoreError(
            f"managed-tool binding is unavailable: {logical_name}: "
            + " | ".join(validation.diagnostics)
        )
    _validate_binding_entry_point(
        selected,
        validation.manifest,
    )
    _validate_binding_adapter_sdk_dependency(
        binding,
        selected,
        validation.manifest,
    )
    path = managed_path(
        validation.entry_path,
        selected.entry_point,
        must_exist=True,
        kind="file",
        label=f"managed-tool entry point {logical_name}",
    )
    return path, selected


@contextmanager
def leased_bound_entry(
    roots: ManagedStoreRoots,
    workspace: Path,
    logical_name: str,
    *,
    timeout_seconds: float = 10.0,
    deep: bool = False,
    command: str | None = None,
    expected_workspace_id: str | None = None,
    expected_game_id: str | None = None,
) -> Iterator[tuple[Path, WorkspaceBindingEntry]]:
    """Resolve a binding while retaining its shared runtime lease."""

    binding = read_workspace_binding(workspace)
    _validate_expected_binding_identity(
        binding,
        expected_workspace_id=expected_workspace_id,
        expected_game_id=expected_game_id,
    )
    selected = next(
        (
            entry
            for entry in binding.entries
            if entry.logical_name.casefold() == logical_name.casefold()
        ),
        None,
    )
    if selected is None:
        raise ManagedToolStoreError(f"managed-tool binding is missing: {logical_name}")
    with store_lifecycle_lock(
        roots,
        mode="shared",
        timeout_seconds=timeout_seconds,
        command=command or f"protect managed tool use {logical_name}",
    ), entry_lock(
        roots,
        selected.tool_kind,
        selected.key_digest,
        mode="shared",
        timeout_seconds=timeout_seconds,
        command=command or f"lease managed tool {logical_name}",
    ):
        validation = validate_entry(
            roots,
            selected.tool_kind,
            selected.key_digest,
            deep=deep,
        )
        if not validation.healthy:
            raise ManagedToolStoreError(
                f"managed-tool binding is unavailable: {logical_name}: "
                + " | ".join(validation.diagnostics)
            )
        _validate_binding_entry_point(
            selected,
            validation.manifest,
        )
        _validate_binding_adapter_sdk_dependency(
            binding,
            selected,
            validation.manifest,
        )
        path = managed_path(
            validation.entry_path,
            selected.entry_point,
            must_exist=True,
            kind="file",
            label=f"managed-tool entry point {logical_name}",
        )
        yield path, selected


def _validate_expected_binding_identity(
    binding: WorkspaceBinding,
    *,
    expected_workspace_id: str | None,
    expected_game_id: str | None,
) -> None:
    if (expected_workspace_id is None) != (expected_game_id is None):
        raise ValueError(
            "expected workspace and game identities must be supplied together"
        )
    if expected_workspace_id is None:
        return
    normalized_workspace_id = str(uuid.UUID(expected_workspace_id))
    if (
        binding.workspace_id != normalized_workspace_id
        or binding.game_id != expected_game_id
    ):
        raise ManagedToolStoreError(
            "managed-tool binding identity differs from the current workspace marker"
        )


def new_binding(
    *,
    workspace_id: str,
    game_id: str,
    entries: Sequence[WorkspaceBindingEntry],
    validation_level: str = "complete",
    validation_result: str = "healthy",
) -> WorkspaceBinding:
    return WorkspaceBinding(
        workspace_id=str(uuid.UUID(workspace_id)),
        game_id=game_id,
        generation=str(uuid.uuid4()),
        generated_at=utc_now(),
        validation_level=validation_level,
        validation_result=validation_result,
        entries=tuple(entries),
    )


def referenced_entry_ids(catalog: Mapping[str, Any]) -> set[str]:
    normalized = load_catalog_payload(catalog)
    result: set[str] = set()
    for raw in normalized["references"].values():
        reference = CatalogReference.from_payload(raw)
        if reference.status in {
            ReferenceStatus.PENDING,
            ReferenceStatus.ACTIVE,
            ReferenceStatus.STALE,
        }:
            result.update(reference.entry_ids)
    return result


def _prepare_atomic_parent(path: Path, allowed_root: Path) -> None:
    if not allowed_root.exists():
        raise ManagedToolPathError(f"managed JSON root does not exist: {allowed_root}")
    create_regular_directory_under(
        path.parent,
        allowed_root,
        label="managed JSON directory",
    )


def _strict_object(
    payload: object,
    required_keys: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise ManagedToolSchemaError(f"{label} must be a JSON object")
    actual = set(payload)
    if actual != required_keys:
        missing = sorted(required_keys - actual)
        extra = sorted(actual - required_keys)
        raise ManagedToolSchemaError(
            f"{label} keys are invalid; missing={missing}, extra={extra}"
        )
    return dict(payload)


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ManagedToolSchemaError(f"{key} must be a non-empty string")
    return value


def _required_object(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict) or not all(isinstance(name, str) for name in value):
        raise ManagedToolSchemaError(f"{key} must be an object")
    return _normalize_json(value)


def _object_array(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, dict) and all(isinstance(key, str) for key in item)
        for item in value
    ):
        raise ManagedToolSchemaError(f"{label} must be an array of objects")
    return tuple(_normalize_json(item) for item in value)


def _string_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ManagedToolSchemaError(f"{label} must be an array of strings")
    return tuple(value)


def _entry_id_array(value: object, label: str) -> tuple[str, ...]:
    entries = _string_array(value, label)
    for entry_id in entries:
        _validate_entry_id(entry_id)
    if len(set(entries)) != len(entries):
        raise ManagedToolSchemaError(f"{label} contains duplicate entry IDs")
    return entries


def _required_sha256(payload: Mapping[str, Any], key: str) -> str:
    return _validate_sha256(_required_string(payload, key))


def _required_uuid(payload: Mapping[str, Any], key: str) -> str:
    try:
        return str(uuid.UUID(_required_string(payload, key)))
    except ValueError as exc:
        raise ManagedToolSchemaError(f"{key} must be a UUID") from exc


def _required_aware_timestamp(
    payload: Mapping[str, Any],
    key: str,
) -> str:
    value = _required_string(payload, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ManagedToolSchemaError(
            f"{key} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManagedToolSchemaError(f"{key} must include a timezone")
    return value


def _required_non_negative_integer(
    payload: Mapping[str, Any],
    key: str,
) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManagedToolSchemaError(f"{key} must be a non-negative integer")
    return value


def _required_boolean(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ManagedToolSchemaError(f"{key} must be a boolean")
    return value


def _validate_reference_id(value: str) -> str:
    workspace_id, separator, generation = value.partition(":")
    if not separator:
        raise ManagedToolSchemaError(
            f"invalid maintenance reference id: {value!r}"
        )
    try:
        return f"{uuid.UUID(workspace_id)}:{uuid.UUID(generation)}"
    except ValueError as exc:
        raise ManagedToolSchemaError(
            f"invalid maintenance reference id: {value!r}"
        ) from exc


def _validate_maintenance_candidate(
    payload: Mapping[str, Any],
    *,
    operation: str,
) -> Mapping[str, Any]:
    candidate_type = payload.get("candidate_type")
    common_keys = {
        "candidate_type",
        "relative_path",
        "logical_bytes",
        "manifest_sha256",
        "status",
        "referenced_by",
        "busy",
        "included",
        "effect",
    }
    if candidate_type == "entry":
        row = _strict_object(
            payload,
            common_keys | {"entry_id"},
            "maintenance entry candidate",
        )
        entry_id = _required_string(row, "entry_id")
        tool_kind, key_digest = _split_entry_id(entry_id)
        relative_path = normalize_relative_path(
            _required_string(row, "relative_path")
        )
        expected_path = f"entries/{tool_kind}/{key_digest}"
        if relative_path.casefold() != expected_path.casefold():
            raise ManagedToolSchemaError(
                "maintenance entry candidate path does not match its entry id"
            )
        status = _required_string(row, "status")
        if status not in {value.value for value in EntryStatus}:
            raise ManagedToolSchemaError(
                "maintenance entry candidate status is invalid"
            )
        manifest_digest = row.get("manifest_sha256")
        if manifest_digest != "":
            manifest_digest = _validate_sha256(
                _required_string(row, "manifest_sha256")
            )
        referenced_by = tuple(
            _validate_reference_id(value)
            for value in _string_array(
                row["referenced_by"],
                "maintenance referenced_by",
            )
        )
        if len(referenced_by) != len(set(referenced_by)):
            raise ManagedToolSchemaError(
                "maintenance candidate contains duplicate references"
            )
        included = _required_boolean(row, "included")
        effect = _required_string(row, "effect")
        expected_effect = (
            "detach-and-delete"
            if included
            else "retain-referenced-or-unavailable"
        )
        if effect != expected_effect:
            raise ManagedToolSchemaError(
                "maintenance entry candidate effect is inconsistent"
            )
        busy = _required_boolean(row, "busy")
        if operation == "uninstall" and not included:
            raise ManagedToolSchemaError(
                "full uninstall must include every entry candidate"
            )
        if operation == "clean-unused" and included and (
            status != EntryStatus.HEALTHY.value
            or busy
            or referenced_by
        ):
            raise ManagedToolSchemaError(
                "unused cleanup included an ineligible entry candidate"
            )
        return {
            "candidate_type": "entry",
            "entry_id": entry_id,
            "relative_path": relative_path,
            "logical_bytes": _required_non_negative_integer(
                row,
                "logical_bytes",
            ),
            "manifest_sha256": manifest_digest,
            "status": status,
            "referenced_by": list(referenced_by),
            "busy": busy,
            "included": included,
            "effect": effect,
        }
    if candidate_type != "remnant":
        raise ManagedToolSchemaError(
            "maintenance candidate_type must be entry or remnant"
        )
    if operation != "uninstall":
        raise ManagedToolSchemaError(
            "maintenance remnants are valid only for full uninstall"
        )
    row = _strict_object(
        payload,
        common_keys | {"origin"},
        "maintenance remnant candidate",
    )
    origin = _required_string(row, "origin")
    if origin not in {"staging", "trash"}:
        raise ManagedToolSchemaError("maintenance remnant origin is invalid")
    relative_path = normalize_relative_path(
        _required_string(row, "relative_path")
    )
    if relative_path.split("/", 1)[0].casefold() != origin:
        raise ManagedToolSchemaError(
            "maintenance remnant path does not match its origin"
        )
    status = _required_string(row, "status")
    if status not in {"directory", "file", "invalid"}:
        raise ManagedToolSchemaError("maintenance remnant status is invalid")
    if (
        row.get("manifest_sha256") != ""
        or row.get("referenced_by") != []
        or row.get("busy") is not False
        or row.get("included") is not True
        or row.get("effect") != "delete-confirmed-remnant"
    ):
        raise ManagedToolSchemaError(
            "maintenance remnant candidate contract is invalid"
        )
    return {
        "candidate_type": "remnant",
        "origin": origin,
        "relative_path": relative_path,
        "logical_bytes": _required_non_negative_integer(row, "logical_bytes"),
        "manifest_sha256": "",
        "status": status,
        "referenced_by": [],
        "busy": False,
        "included": True,
        "effect": "delete-confirmed-remnant",
    }


def _validate_maintenance_reference(
    payload: Mapping[str, Any],
    *,
    operation: str,
) -> Mapping[str, Any]:
    row = _strict_object(
        payload,
        {
            "workspace_id",
            "workspace_path",
            "game_id",
            "generation",
            "status",
            "entry_ids",
            "updated_at",
            "observed_classification",
            "effect",
        },
        "maintenance reference",
    )
    effect = _required_string(row, "effect")
    observed_classification = _required_string(
        row,
        "observed_classification",
    )
    if observed_classification not in {"valid", "stale"}:
        raise ManagedToolSchemaError(
            "maintenance reference observed classification is invalid"
        )
    reference = CatalogReference.from_payload(
        {
            key: value
            for key, value in row.items()
            if key not in {"effect", "observed_classification"}
        }
    )
    if operation == "uninstall":
        allowed_effects = {"invalidate-known-binding"}
    else:
        allowed_effects = {"retain", "release-explicit-stale-reference"}
    if effect not in allowed_effects:
        raise ManagedToolSchemaError(
            "maintenance reference effect is invalid for its operation"
        )
    if (
        effect == "release-explicit-stale-reference"
        and observed_classification != "stale"
    ):
        raise ManagedToolSchemaError(
            "only observed-stale references can be released by a maintenance plan"
        )
    normalized = reference.to_payload()
    normalized["observed_classification"] = observed_classification
    normalized["effect"] = effect
    return normalized


def _validate_sha256(value: str) -> str:
    normalized = value.casefold()
    if not _SHA256_RE.fullmatch(normalized):
        raise ManagedToolSchemaError(f"invalid SHA-256 digest: {value!r}")
    return normalized


def _validate_entry_id(value: str) -> None:
    tool_kind, separator, digest = value.partition(":")
    if not separator:
        raise ManagedToolSchemaError(f"invalid managed entry id: {value!r}")
    validate_tool_kind(tool_kind)
    _validate_sha256(digest)


def _split_entry_id(value: str) -> tuple[str, str]:
    _validate_entry_id(value)
    tool_kind, digest = value.split(":", 1)
    return tool_kind, digest


def _normalize_json(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise ManagedToolSchemaError("floating-point values are not allowed in stable JSON")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ManagedToolSchemaError("stable JSON object keys must be strings")
        return {
            key: _normalize_json(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    raise ManagedToolSchemaError(f"unsupported stable JSON value: {type(value).__name__}")


def _decode_json_object(payload_bytes: bytes, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedToolSchemaError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManagedToolSchemaError(f"JSON file must contain an object: {path}")
    return payload


def _read_json_object(path: Path, allowed_root: Path) -> dict[str, Any]:
    try:
        payload = read_regular_single_link_bytes(
            path,
            allowed_root,
            label="managed JSON file",
        )
    except (OSError, ManagedProcessEnvironmentError) as exc:
        raise ManagedToolSchemaError(f"invalid JSON file {path}: {exc}") from exc
    return _decode_json_object(payload, path)


def _reject_casefold_collisions(paths: Sequence[str] | Iterator[str]) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        normalized = normalize_relative_path(path)
        key = normalized.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise ManagedToolPathError(
                "managed paths are duplicated or collide after case folding: "
                f"{previous!r}, {normalized!r}"
            )
        seen[key] = normalized

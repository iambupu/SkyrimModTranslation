"""Copy-only migration helpers for exact project-generated legacy tool roots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from file_utils import discover_regular_files, sha256_file, validate_regular_path_under
from managed_tool_resolver import (
    FIELD_RULES,
    ToolPathProvenance,
    classify_configured_tool_path,
)
from managed_tool_store import (
    ManagedStoreRoots,
    ManagedToolStoreError,
    ToolKey,
    create_staging_directory,
    make_entry_manifest,
    publish_movable_entry,
)
from smt_windows import (
    PinnedImportTree,
    read_regular_single_link_bytes,
    remove_regular_tree,
    validate_regular_single_link_file,
)


LEGACY_IMPORT_PROOF_NAME = ".skyrim-chs-managed-import.json"
LEGACY_IMPORT_PROOF_SCHEMA = 1


@dataclass(frozen=True)
class LegacyCandidate:
    field: str
    logical_name: str
    payload_root: Path
    configured_path: Path
    logical_bytes: int


@dataclass(frozen=True)
class LegacyDiscovery:
    candidates: Mapping[str, LegacyCandidate]
    blockers: tuple[str, ...]
    diagnostics: tuple[str, ...]


def _payload_root(field: str, configured_path: Path) -> Path:
    if field == "PythonRuntimePath":
        return configured_path.parent.parent
    rule = FIELD_RULES[field]
    return configured_path if rule.path_type == "directory" else configured_path.parent


def _logical_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in discover_regular_files(root, label="legacy managed-tool candidate")
    )


def _legacy_tree_entries(
    root: Path,
) -> tuple[tuple[str, Literal["file", "directory"]], ...]:
    entries: list[tuple[str, Literal["file", "directory"]]] = []
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in directory_names:
            entries.append(
                (
                    (current_path / name).relative_to(root).as_posix(),
                    "directory",
                )
            )
        for name in file_names:
            entries.append(
                (
                    (current_path / name).relative_to(root).as_posix(),
                    "file",
                )
            )
    entries.sort(key=lambda item: (item[0].casefold(), item[0]))
    names = [relative.casefold() for relative, _entry_type in entries]
    if len(names) != len(set(names)):
        raise ManagedToolStoreError(
            "legacy managed-tool tree contains a case-fold collision"
        )
    return tuple(entries)


def copy_legacy_tree_safely(source: Path, target: Path) -> None:
    """Copy a legacy tree while every source object remains identity-bound."""

    entries = _legacy_tree_entries(source)
    with PinnedImportTree(
        source,
        source,
        entries,
        root_type="directory",
        allow_rename=False,
    ) as binding:
        shutil.copytree(
            source,
            target,
            copy_function=shutil.copy2,
            symlinks=True,
        )
        binding.verify(source)


def discover_legacy_candidates(
    workspace: Path,
    *,
    fields: Sequence[str] | None = None,
) -> LegacyDiscovery:
    """Inspect only registered exact legacy locations; never scan arbitrary tools/."""

    workspace = workspace.resolve(strict=True)
    selected_fields = set(fields) if fields is not None else None
    candidates: dict[str, LegacyCandidate] = {}
    blockers: list[str] = []
    diagnostics: list[str] = []
    for field, rule in FIELD_RULES.items():
        if selected_fields is not None and field not in selected_fields:
            continue
        if rule.legacy_relative is None or rule.logical_name is None:
            continue
        exact = Path(os.path.abspath(workspace / rule.legacy_relative))
        if not os.path.lexists(exact):
            continue
        synthetic = {"DecoderTools": {field: rule.legacy_relative}}
        resolution = classify_configured_tool_path(workspace, synthetic, field)
        if resolution.provenance is ToolPathProvenance.LEGACY_UNKNOWN:
            blockers.append(
                f"{field}: exact legacy location contains unproven content; "
                "automatic setup will not overwrite, migrate, or delete it"
            )
            continue
        if resolution.provenance is not ToolPathProvenance.LEGACY_GENERATED:
            diagnostics.extend(resolution.diagnostics)
            continue
        assert resolution.path is not None
        root = _payload_root(field, resolution.path)
        try:
            root = validate_regular_path_under(
                root,
                workspace,
                kind="directory",
                label=f"{field} legacy payload",
            )
            size = _logical_bytes(root)
        except (OSError, ValueError) as exc:
            blockers.append(f"{field}: unsafe legacy payload: {exc}")
            continue
        candidates[rule.logical_name] = LegacyCandidate(
            field=field,
            logical_name=rule.logical_name,
            payload_root=root,
            configured_path=resolution.path,
            logical_bytes=size,
        )
    return LegacyDiscovery(candidates, tuple(blockers), tuple(diagnostics))


def _proof_inventory(payload_root: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for path in discover_regular_files(
        payload_root,
        label="legacy managed-tool import proof",
    ):
        relative = path.relative_to(payload_root).as_posix()
        if relative.casefold() == LEGACY_IMPORT_PROOF_NAME.casefold():
            continue
        rows.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return tuple(sorted(rows, key=lambda row: str(row["path"]).casefold()))


def legacy_payload_proves_key(
    candidate: LegacyCandidate,
    key: ToolKey,
    *,
    entry_point: str,
) -> tuple[bool, tuple[str, ...]]:
    """Require an exact full-inventory proof before importing legacy bytes."""

    proof_path = candidate.payload_root / LEGACY_IMPORT_PROOF_NAME
    if not os.path.lexists(proof_path):
        return False, (
            f"{candidate.logical_name}: legacy manifest proves project ownership "
            "but has no complete managed-import inventory; normal provisioning is required",
        )
    try:
        proof_path = validate_regular_single_link_file(
            proof_path,
            candidate.payload_root,
            label="legacy managed-tool import proof",
        )
        payload = json.loads(
            read_regular_single_link_bytes(
                proof_path,
                candidate.payload_root,
                label="legacy managed-tool import proof",
            ).decode("utf-8-sig")
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        return False, (f"{candidate.logical_name}: invalid migration proof: {exc}",)
    expected_fields = {
        "schema_version",
        "entry_id",
        "entry_point",
        "payload_inventory",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        return False, (f"{candidate.logical_name}: migration proof schema differs",)
    if (
        payload.get("schema_version") != LEGACY_IMPORT_PROOF_SCHEMA
        or payload.get("entry_id") != key.entry_id
        or payload.get("entry_point") != entry_point
    ):
        return False, (f"{candidate.logical_name}: migration proof identity differs",)
    expected_inventory = payload.get("payload_inventory")
    if not isinstance(expected_inventory, list):
        return False, (f"{candidate.logical_name}: migration inventory is invalid",)
    actual_inventory = list(_proof_inventory(candidate.payload_root))
    if expected_inventory != actual_inventory:
        return False, (f"{candidate.logical_name}: migration payload hashes differ",)
    try:
        validate_regular_single_link_file(
            candidate.payload_root.joinpath(*entry_point.split("/")),
            candidate.payload_root,
            label="legacy managed-tool entry point",
        )
    except (OSError, ValueError, RuntimeError):
        return False, (f"{candidate.logical_name}: migration entry point is missing",)
    return True, ()


def import_movable_legacy_entry(
    roots: ManagedStoreRoots,
    candidate: LegacyCandidate,
    key: ToolKey,
    *,
    entry_point: str,
    source: Mapping[str, Any],
    producer_version: str,
    entry_lock_held: bool = False,
) -> tuple[Path | None, tuple[str, ...]]:
    """Copy a proven movable legacy payload into private staging and publish it."""

    proven, diagnostics = legacy_payload_proves_key(
        candidate,
        key,
        entry_point=entry_point,
    )
    if not proven:
        return None, diagnostics
    staging = create_staging_directory(roots, prefix=f"legacy-{key.tool_kind}")
    payload = staging / "payload"
    try:
        copy_legacy_tree_safely(candidate.payload_root, payload)
        # Recompute all hashes from the copied tree.  The legacy source is never
        # moved, edited, or used after this point.
        copied = LegacyCandidate(
            candidate.field,
            candidate.logical_name,
            payload,
            payload.joinpath(*entry_point.split("/")),
            candidate.logical_bytes,
        )
        copied_ok, copied_diagnostics = legacy_payload_proves_key(
            copied,
            key,
            entry_point=entry_point,
        )
        if not copied_ok:
            return None, copied_diagnostics
        manifest = make_entry_manifest(
            key=key,
            entry_root=payload,
            source={
                **dict(source),
                "migration": "copy-only",
                "legacy_inventory_sha256": hashlib.sha256(
                    json.dumps(
                        list(_proof_inventory(payload)),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            },
            critical_entries=(entry_point,),
            producer_version=producer_version,
        )
        published = publish_movable_entry(
            roots,
            payload,
            manifest,
            entry_lock_held=entry_lock_held,
        )
        try:
            staging.rmdir()
        except OSError:
            pass
        return published, (
            f"{candidate.logical_name}: imported by copy; "
            f"legacy duplicate retained ({candidate.logical_bytes} logical bytes)",
        )
    finally:
        if staging.exists():
            try:
                remove_regular_tree(
                    staging,
                    roots.staging,
                    label="legacy managed-tool staging",
                )
            except (OSError, ValueError, RuntimeError):
                # Preserve an unsafe or concurrently changed remnant for the
                # explicit maintenance inspector instead of following it.
                pass

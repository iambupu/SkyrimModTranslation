"""Field-specific manual, wrapper, legacy, and managed-tool resolution."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

from dotnet_adapter_cache import adapter_source_hash
from file_utils import sha256_file
from managed_tool_store import (
    ManagedStoreRoots,
    ManagedToolStoreError,
    WorkspaceBinding,
    WorkspaceBindingEntry,
    leased_bound_entry,
    read_workspace_binding,
    resolve_bound_entry,
    resolve_managed_store_roots,
)
from project_paths import safe_file_name
from smt_fingerprint import FINGERPRINT_ALGORITHM
from smt_windows import (
    ManagedProcessEnvironmentError,
    PinnedDirectoryHandle,
    read_regular_single_link_bytes,
    validate_regular_single_link_file,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TOOL_MANIFEST_NAME = ".skyrim-chs-tool.json"
LEGACY_IMPORT_PROOF_NAME = ".skyrim-chs-managed-import.json"
LEGACY_DOTNET_INSTALL_SOURCE = "vendored:scripts/vendor/dotnet-install.ps1"
LEGACY_DOTNET_INSTALL_SHA256 = (
    "6585899aed55ff6ae13dbe1e8c3b878f2d00433520e7efbe250b75db948b7da9"
)
WORKSPACE_MARKER_SCHEMA_VERSION = 2
WORKSPACE_MARKER_KIND = "bethesda-mod-chs-translation-workspace"
SMT_SESSION_SCHEMA_VERSION = 1
SMT_SESSION_RELATIVE_PATH = Path(".workflow") / "smt-session.json"
SMT_SESSION_FIELDS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "mod_name",
        "game_id",
        "fingerprint_algorithm",
        "input_identity",
        "source_kind",
        "source_display_name",
        "source_sha256",
        "import_relative_path",
        "imported_sha256",
        "created_at",
    }
)


class ToolPathProvenance(str, Enum):
    USER_EXTERNAL = "user-external"
    PLUGIN_WRAPPER = "plugin-wrapper"
    LEGACY_GENERATED = "legacy-generated"
    LEGACY_UNKNOWN = "legacy-unknown"
    MANAGED_BINDING = "managed-binding"
    MISSING = "missing"


@dataclass(frozen=True)
class FieldResolutionRule:
    field: str
    logical_name: str | None
    path_type: Literal["file", "directory"]
    managed_projection: Literal["entry-point", "entry-parent"] = "entry-point"
    plugin_wrapper: str | None = None
    legacy_relative: str | None = None
    legacy_manifest_name: str | None = None
    legacy_manifest_values: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ToolPathResolution:
    field: str
    provenance: ToolPathProvenance
    path: Path | None
    logical_name: str | None
    diagnostics: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.path is not None and self.provenance not in {
            ToolPathProvenance.LEGACY_UNKNOWN,
            ToolPathProvenance.MISSING,
        }


@dataclass(frozen=True)
class WorkspaceIdentityEvidence:
    marker_path: Path
    marker_payload: Mapping[str, Any]
    game_id: str
    marker_workspace_id: str | None
    session_workspace_id: str | None

    @property
    def effective_workspace_id(self) -> str | None:
        return self.marker_workspace_id or self.session_workspace_id


FIELD_RULES: Mapping[str, FieldResolutionRule] = {
    "PythonRuntimePath": FieldResolutionRule(
        field="PythonRuntimePath",
        logical_name="python-runtime",
        path_type="file",
        legacy_relative=(
            "tools/python-venv/Scripts/python.exe"
            if os.name == "nt"
            else "tools/python-venv/bin/python"
        ),
        legacy_manifest_name=LEGACY_IMPORT_PROOF_NAME,
        legacy_manifest_values={"schema_version": "1"},
    ),
    "DotNetSdkPath": FieldResolutionRule(
        field="DotNetSdkPath",
        logical_name="dotnet-sdk",
        path_type="file",
        legacy_relative="tools/dotnet-sdk/dotnet.exe",
        legacy_manifest_name=TOOL_MANIFEST_NAME,
        legacy_manifest_values={
            "name": "dotnet-sdk",
            "source_type": "dotnet-install",
            "install_script_source": LEGACY_DOTNET_INSTALL_SOURCE,
            "install_script_sha256": LEGACY_DOTNET_INSTALL_SHA256,
            "sdk_version": "8.0.422",
        },
    ),
    "ChampollionSourceDir": FieldResolutionRule(
        field="ChampollionSourceDir",
        logical_name="decoder-champollion",
        path_type="directory",
        managed_projection="entry-parent",
        legacy_relative="tools/Champollion",
        legacy_manifest_name=TOOL_MANIFEST_NAME,
        legacy_manifest_values={
            "name": "Champollion",
            "source_type": "github-archive",
            "url": (
                "https://codeload.github.com/Orvid/Champollion/zip/"
                "bc961a0bdfb4831f8240e6dacee0818b4bf81e00"
            ),
            "ref": "bc961a0bdfb4831f8240e6dacee0818b4bf81e00",
            "archive_sha256": (
                "f83f626d40a88cd8e11189a908f503f8b8bcd4072e1294187687857528739b46"
            ),
        },
    ),
    "BsaFileExtractorPath": FieldResolutionRule(
        field="BsaFileExtractorPath",
        logical_name="decoder-bsafileextractor",
        path_type="file",
        plugin_wrapper="scripts/invoke_bsa_file_extractor_safe.py",
        legacy_relative="tools/BSAFileExtractor/BSAFileExtractor.py",
        legacy_manifest_name=TOOL_MANIFEST_NAME,
        legacy_manifest_values={
            "name": "BSAFileExtractor",
            "source_type": "github-archive",
            "url": (
                "https://codeload.github.com/Sw4T/BSAFileExtractor/zip/"
                "cce03dfc294f1f31fa0af0fe1d2ef3b5dde67c27"
            ),
            "ref": "cce03dfc294f1f31fa0af0fe1d2ef3b5dde67c27",
            "archive_sha256": (
                "9c7138fbb6672f032c4c7a86526104ec4cbd7db9eca1672d49d73f2cfc9ea86a"
            ),
        },
    ),
    "MutagenCliPath": FieldResolutionRule(
        field="MutagenCliPath",
        logical_name="adapter-skyrimplugintexttool",
        path_type="file",
        plugin_wrapper="scripts/invoke_mutagen_plugin_text_tool.py",
        legacy_relative=(
            "tools/dotnet-adapters/SkyrimPluginTextTool/"
            "SkyrimPluginTextTool.dll"
        ),
        legacy_manifest_name=".skyrim-chs-adapter.json",
        legacy_manifest_values={"adapter_name": "SkyrimPluginTextTool"},
    ),
    "PexStringToolPath": FieldResolutionRule(
        field="PexStringToolPath",
        logical_name="adapter-skyrimpexstringtool",
        path_type="file",
        plugin_wrapper="scripts/invoke_mutagen_pex_string_tool.py",
        legacy_relative=(
            "tools/dotnet-adapters/SkyrimPexStringTool/"
            "SkyrimPexStringTool.dll"
        ),
        legacy_manifest_name=".skyrim-chs-adapter.json",
        legacy_manifest_values={"adapter_name": "SkyrimPexStringTool"},
    ),
    "BethesdaStringTableToolPath": FieldResolutionRule(
        field="BethesdaStringTableToolPath",
        logical_name="adapter-bethesdastringtabletool",
        path_type="file",
        legacy_relative=(
            "tools/dotnet-adapters/BethesdaStringTableTool/"
            "BethesdaStringTableTool.dll"
        ),
        legacy_manifest_name=".skyrim-chs-adapter.json",
        legacy_manifest_values={"adapter_name": "BethesdaStringTableTool"},
    ),
}


def _configured_value(config: Mapping[str, Any], field: str) -> str:
    decoder = config.get("DecoderTools")
    if not isinstance(decoder, dict):
        return ""
    value = decoder.get(field)
    return str(value).strip() if value is not None else ""


def load_workspace_tool_config(
    workspace: Path,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Read tools.local.json without following aliases or reopening by path."""

    workspace = workspace.resolve(strict=True)
    config_path = config_path or workspace / "config" / "tools.local.json"
    if not os.path.lexists(config_path):
        return {}
    try:
        payload = json.loads(
            read_regular_single_link_bytes(
                config_path,
                workspace,
                label="workspace tools config",
            ).decode("utf-8-sig")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedToolStoreError(
            f"tools.local.json is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ManagedToolStoreError("tools.local.json must contain an object")
    return payload


def _read_identity_json(
    path: Path,
    workspace: Path,
    *,
    label: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(
            read_regular_single_link_bytes(
                path,
                workspace,
                label=label,
            ).decode("utf-8-sig")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedToolStoreError(
            f"{label} is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ManagedToolStoreError(f"{label} must be an object")
    return payload


def _normalized_workspace_uuid(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ManagedToolStoreError(f"{label} must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ManagedToolStoreError(f"{label} is not a valid UUID") from exc


def _is_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validated_session_workspace_id(
    session: Mapping[str, Any],
    *,
    marker_game_id: str,
) -> str:
    if set(session) != SMT_SESSION_FIELDS:
        raise ManagedToolStoreError(
            "SMT session does not satisfy the immutable schema v1 fields"
        )
    if type(session.get("schema_version")) is not int or (
        session["schema_version"] != SMT_SESSION_SCHEMA_VERSION
    ):
        raise ManagedToolStoreError("SMT session is not supported schema v1")
    if any(
        type(session[field]) is not str
        for field in SMT_SESSION_FIELDS - {"schema_version"}
    ):
        raise ManagedToolStoreError("SMT session schema v1 fields must be strings")
    session_workspace_id = _normalized_workspace_uuid(
        session["workspace_id"],
        label="SMT session workspace_id",
    )
    game_id = session["game_id"]
    if game_id != marker_game_id:
        raise ManagedToolStoreError(
            "workspace marker and SMT session game identities differ"
        )
    source_kind = session["source_kind"]
    if (
        not game_id
        or source_kind not in {"directory", "zip", "7z"}
        or session["fingerprint_algorithm"] != FINGERPRINT_ALGORITHM
    ):
        raise ManagedToolStoreError("SMT session game/source identity is invalid")
    mod_name = session["mod_name"]
    if (
        safe_file_name(mod_name) != mod_name
        or len(mod_name.encode("utf-16-le")) // 2 > 80
    ):
        raise ManagedToolStoreError("SMT session mod_name is unsafe")
    expected_prefix = f"{FINGERPRINT_ALGORITHM}:{game_id}:{source_kind}:"
    input_identity = session["input_identity"]
    if not input_identity.startswith(expected_prefix):
        raise ManagedToolStoreError("SMT session composite identity is inconsistent")
    identity_digest = input_identity[len(expected_prefix) :]
    source_digest = session["source_sha256"]
    if (
        not _is_sha256(identity_digest)
        or source_digest != identity_digest
        or session["imported_sha256"] != source_digest
    ):
        raise ManagedToolStoreError("SMT session source/import digest is invalid")
    relative = Path(session["import_relative_path"].replace("\\", "/"))
    extension = {"directory": "", "zip": ".zip", "7z": ".7z"}[source_kind]
    expected_import_name = f"{mod_name}{extension}"
    if (
        relative.is_absolute()
        or relative.parts[:1] != ("mod",)
        or len(relative.parts) != 2
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.name != safe_file_name(relative.name)
        or relative.name != expected_import_name
    ):
        raise ManagedToolStoreError(
            "SMT session import path must be one direct mod/ child"
        )
    source_name = session["source_display_name"] or relative.name
    if Path(source_name).name != source_name or safe_file_name(source_name) != source_name:
        raise ManagedToolStoreError("SMT session source display name is unsafe")
    try:
        created_at = datetime.fromisoformat(session["created_at"])
    except ValueError as exc:
        raise ManagedToolStoreError("SMT session created_at is not ISO-8601") from exc
    if created_at.tzinfo is None:
        raise ManagedToolStoreError("SMT session created_at must include a timezone")
    return session_workspace_id


def read_workspace_identity_evidence(workspace: Path) -> WorkspaceIdentityEvidence:
    workspace = workspace.resolve(strict=True)
    marker_path = validate_regular_single_link_file(
        workspace / ".skyrim-chs-workspace.json",
        workspace,
        label="workspace marker",
    )
    payload = _read_identity_json(marker_path, workspace, label="workspace marker")
    if type(payload.get("schema_version")) is not int or (
        payload["schema_version"] != WORKSPACE_MARKER_SCHEMA_VERSION
    ):
        raise ManagedToolStoreError("workspace marker is not supported schema v2")
    if payload.get("kind") != WORKSPACE_MARKER_KIND:
        raise ManagedToolStoreError("workspace marker kind is invalid")
    game_id = payload.get("game_id")
    if not isinstance(game_id, str) or not game_id:
        raise ManagedToolStoreError("workspace marker is missing game_id")
    marker_workspace_id = (
        _normalized_workspace_uuid(
            payload["workspace_id"],
            label="workspace marker workspace_id",
        )
        if "workspace_id" in payload
        else None
    )

    session_workspace_id: str | None = None
    session_path = workspace / SMT_SESSION_RELATIVE_PATH
    if os.path.lexists(session_path):
        validate_regular_single_link_file(
            session_path,
            workspace,
            label="SMT session",
        )
        session = _read_identity_json(session_path, workspace, label="SMT session")
        session_workspace_id = _validated_session_workspace_id(
            session,
            marker_game_id=game_id,
        )
        if (
            marker_workspace_id is not None
            and session_workspace_id != marker_workspace_id
        ):
            raise ManagedToolStoreError(
                "workspace marker and SMT session workspace identities differ"
            )
    return WorkspaceIdentityEvidence(
        marker_path=marker_path,
        marker_payload=payload,
        game_id=game_id,
        marker_workspace_id=marker_workspace_id,
        session_workspace_id=session_workspace_id,
    )


def _workspace_identity(workspace: Path) -> tuple[str, str]:
    evidence = read_workspace_identity_evidence(workspace)
    if evidence.effective_workspace_id is None:
        raise ManagedToolStoreError(
            "legacy workspace marker has no workspace_id; run auto tool setup "
            "to upgrade it"
        )
    return evidence.effective_workspace_id, evidence.game_id


def _candidate_from_value(workspace: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return Path(os.path.abspath(candidate))
    plugin_candidate = PLUGIN_ROOT / candidate
    if candidate.parts and candidate.parts[0].casefold() == "scripts":
        return Path(os.path.abspath(plugin_candidate))
    return Path(os.path.abspath(workspace / candidate))


def _legacy_manifest(rule: FieldResolutionRule, legacy_path: Path) -> Path | None:
    if not rule.legacy_manifest_name:
        return None
    if rule.field == "PythonRuntimePath":
        return legacy_path.parent.parent / rule.legacy_manifest_name
    if rule.path_type == "directory":
        return legacy_path / rule.legacy_manifest_name
    return legacy_path.parent / rule.legacy_manifest_name


def _legacy_identity_matches(rule: FieldResolutionRule, legacy_path: Path) -> bool:
    try:
        with ExitStack() as stack:
            if rule.path_type == "file":
                validated_legacy = validate_regular_single_link_file(
                    legacy_path,
                    Path(legacy_path.anchor),
                    label=f"{rule.field} legacy tool",
                )
            else:
                pin = stack.enter_context(
                    PinnedDirectoryHandle(
                        legacy_path,
                        Path(legacy_path.anchor),
                    )
                )
                if pin.final_path is None:
                    return False
                validated_legacy = pin.final_path
            manifest = _legacy_manifest(rule, validated_legacy)
            if manifest is None:
                return False
            manifest = validate_regular_single_link_file(
                manifest,
                Path(manifest.anchor),
                label=f"{rule.field} legacy manifest",
            )
            payload = json.loads(
                read_regular_single_link_bytes(
                    manifest,
                    Path(manifest.anchor),
                    label=f"{rule.field} legacy manifest",
                ).decode("utf-8-sig")
            )
            expected = rule.legacy_manifest_values or {}
            if not isinstance(payload, dict) or not all(
                str(payload.get(key)) == value
                for key, value in expected.items()
            ):
                return False
            adapter_name = expected.get("adapter_name")
            if not adapter_name:
                return True
            project = (
                PLUGIN_ROOT
                / "adapters"
                / adapter_name
                / f"{adapter_name}.csproj"
            )
            project = validate_regular_single_link_file(
                project,
                PLUGIN_ROOT,
                label=f"{rule.field} adapter project",
            )
            expected_source_hash = adapter_source_hash(
                project,
                source_root=PLUGIN_ROOT,
            )
            dll_hash = sha256_file(validated_legacy)
            return (
                payload.get("source_hash") == expected_source_hash
                and payload.get("adapter_dll_sha256") == dll_hash
            )
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        ManagedProcessEnvironmentError,
        ManagedToolStoreError,
    ):
        return False


def classify_configured_tool_path(
    workspace: Path,
    config: Mapping[str, Any],
    field: str,
) -> ToolPathResolution:
    """Classify an explicit field without silently broadening its authority."""

    workspace = workspace.resolve(strict=True)
    try:
        rule = FIELD_RULES[field]
    except KeyError as exc:
        raise ManagedToolStoreError(f"managed-tool field is not registered: {field}") from exc
    value = _configured_value(config, field)
    if not value:
        return ToolPathResolution(
            field,
            ToolPathProvenance.MISSING,
            None,
            rule.logical_name,
        )
    candidate = _candidate_from_value(workspace, value)
    if rule.plugin_wrapper is not None:
        wrapper = (PLUGIN_ROOT / rule.plugin_wrapper).resolve(strict=False)
        if candidate == wrapper:
            if not wrapper.is_file():
                return ToolPathResolution(
                    field,
                    ToolPathProvenance.MISSING,
                    None,
                    rule.logical_name,
                    ("registered plugin wrapper is missing",),
                )
            validate_regular_single_link_file(
                wrapper,
                PLUGIN_ROOT,
                label=f"{field} plugin wrapper",
            )
            return ToolPathResolution(
                field,
                ToolPathProvenance.PLUGIN_WRAPPER,
                wrapper,
                rule.logical_name,
            )
    legacy = (
        Path(os.path.abspath(workspace / rule.legacy_relative))
        if rule.legacy_relative
        else None
    )
    if legacy is not None and candidate == legacy:
        if not os.path.lexists(legacy):
            return ToolPathResolution(
                field,
                ToolPathProvenance.MISSING,
                None,
                rule.logical_name,
                ("configured legacy path does not exist",),
            )
        if _legacy_identity_matches(rule, legacy):
            return ToolPathResolution(
                field,
                ToolPathProvenance.LEGACY_GENERATED,
                legacy,
                rule.logical_name,
            )
        return ToolPathResolution(
            field,
            ToolPathProvenance.LEGACY_UNKNOWN,
            legacy,
            rule.logical_name,
            (
                "known legacy location lacks the exact project manifest or pin; "
                "automatic setup will not overwrite, migrate, or delete it",
            ),
        )
    workspace_tools = Path(os.path.abspath(workspace / "tools"))
    try:
        candidate.relative_to(workspace_tools)
        is_workspace_tool_path = True
    except ValueError:
        is_workspace_tool_path = False
    if is_workspace_tool_path:
        return ToolPathResolution(
            field,
            ToolPathProvenance.LEGACY_UNKNOWN,
            candidate,
            rule.logical_name,
            (
                "workspace tools path is not an exact proven legacy location",
            ),
        )
    try:
        if rule.path_type == "file":
            validated = validate_regular_single_link_file(
                candidate,
                Path(candidate.anchor),
                label=f"{field} external tool",
            )
        else:
            with PinnedDirectoryHandle(candidate, Path(candidate.anchor)) as pin:
                if pin.final_path is None:
                    raise ManagedToolStoreError(
                        f"{field} external directory could not be pinned"
                    )
                validated = pin.final_path
    except (
        OSError,
        ValueError,
        ManagedProcessEnvironmentError,
        ManagedToolStoreError,
    ) as exc:
        return ToolPathResolution(
            field,
            ToolPathProvenance.MISSING,
            None,
            rule.logical_name,
            (str(exc),),
        )
    return ToolPathResolution(
        field,
        ToolPathProvenance.USER_EXTERNAL,
        validated,
        rule.logical_name,
    )


def _project_managed_path(
    entry_point: Path,
    rule: FieldResolutionRule,
) -> Path:
    if rule.managed_projection == "entry-parent":
        return entry_point.parent
    if rule.plugin_wrapper is not None:
        return (PLUGIN_ROOT / rule.plugin_wrapper).resolve(strict=True)
    return entry_point


def _validate_binding_resolution(
    rule: FieldResolutionRule,
    entry: WorkspaceBindingEntry,
) -> None:
    expected = rule.logical_name
    if expected is None:
        raise ManagedToolStoreError(
            f"{rule.field} has no managed binding identity"
        )
    if (
        entry.logical_name.casefold() != expected.casefold()
        or entry.tool_kind.casefold() != expected.casefold()
    ):
        raise ManagedToolStoreError(
            f"{rule.field} binding identity differs: expected {expected}, "
            f"found {entry.logical_name}/{entry.tool_kind}"
        )


def resolve_tool_for_diagnostics(
    workspace: Path,
    config: Mapping[str, Any],
    field: str,
    *,
    roots: ManagedStoreRoots | None = None,
) -> ToolPathResolution:
    """Resolve without retaining a lease; suitable only for read-only reporting."""

    explicit = classify_configured_tool_path(workspace, config, field)
    if explicit.provenance in {
        ToolPathProvenance.USER_EXTERNAL,
        ToolPathProvenance.LEGACY_UNKNOWN,
    }:
        return explicit
    rule = FIELD_RULES[field]
    if rule.logical_name is None:
        return explicit
    roots = roots or resolve_managed_store_roots()
    try:
        workspace_id, game_id = _workspace_identity(workspace)
        entry_point, entry = resolve_bound_entry(
            roots,
            workspace,
            rule.logical_name,
            expected_workspace_id=workspace_id,
            expected_game_id=game_id,
        )
        _validate_binding_resolution(rule, entry)
    except (
        OSError,
        ValueError,
        ManagedToolStoreError,
        ManagedProcessEnvironmentError,
    ) as exc:
        return ToolPathResolution(
            field,
            ToolPathProvenance.MISSING,
            None,
            rule.logical_name,
            (str(exc),),
        )
    managed_path = _project_managed_path(entry_point, rule)
    if explicit.provenance is ToolPathProvenance.PLUGIN_WRAPPER:
        managed_path = explicit.path
    return ToolPathResolution(
        field,
        (
            ToolPathProvenance.PLUGIN_WRAPPER
            if explicit.provenance is ToolPathProvenance.PLUGIN_WRAPPER
            else ToolPathProvenance.MANAGED_BINDING
        ),
        managed_path,
        rule.logical_name,
    )


def adapter_uses_managed_binding(
    workspace: Path,
    config: Mapping[str, Any],
    adapter_field: str,
) -> bool:
    """Return whether an adapter must run with its bound managed SDK."""

    resolution = classify_configured_tool_path(
        workspace,
        config,
        adapter_field,
    )
    if resolution.provenance is ToolPathProvenance.LEGACY_UNKNOWN:
        raise ManagedToolStoreError(
            f"{adapter_field} points at unknown legacy-looking content: "
            + " | ".join(resolution.diagnostics)
        )
    return resolution.provenance is not ToolPathProvenance.USER_EXTERNAL


@contextmanager
def leased_tool_path(
    workspace: Path,
    config: Mapping[str, Any],
    field: str,
    *,
    roots: ManagedStoreRoots | None = None,
    timeout_seconds: float = 10.0,
    command: str | None = None,
) -> Iterator[ToolPathResolution]:
    """Resolve one runtime path and retain managed entry use protection."""

    explicit = classify_configured_tool_path(workspace, config, field)
    if explicit.provenance is ToolPathProvenance.LEGACY_UNKNOWN:
        raise ManagedToolStoreError(
            f"{field} points at unknown legacy-looking content: "
            + " | ".join(explicit.diagnostics)
        )
    if explicit.provenance is ToolPathProvenance.USER_EXTERNAL:
        yield explicit
        return
    rule = FIELD_RULES[field]
    if rule.logical_name is None:
        raise ManagedToolStoreError(f"{field} has no configured or managed tool")
    roots = roots or resolve_managed_store_roots()
    workspace_id, game_id = _workspace_identity(workspace)
    lease_stack = ExitStack()
    try:
        entry_point, entry = lease_stack.enter_context(
            leased_bound_entry(
                roots,
                workspace,
                rule.logical_name,
                timeout_seconds=timeout_seconds,
                command=command or f"resolve {field}",
                expected_workspace_id=workspace_id,
                expected_game_id=game_id,
            )
        )
        _validate_binding_resolution(rule, entry)
    except (
        OSError,
        ValueError,
        ManagedToolStoreError,
        ManagedProcessEnvironmentError,
    ):
        lease_stack.close()
        raise
    try:
        path = _project_managed_path(entry_point, rule)
        provenance = ToolPathProvenance.MANAGED_BINDING
        if explicit.provenance is ToolPathProvenance.PLUGIN_WRAPPER:
            path = explicit.path
            provenance = ToolPathProvenance.PLUGIN_WRAPPER
        yield ToolPathResolution(
            field,
            provenance,
            path,
            rule.logical_name,
        )
    finally:
        lease_stack.close()


@contextmanager
def leased_payload_path(
    workspace: Path,
    config: Mapping[str, Any],
    field: str,
    *,
    roots: ManagedStoreRoots | None = None,
    timeout_seconds: float = 10.0,
    command: str | None = None,
    managed_only: bool = False,
) -> Iterator[ToolPathResolution]:
    """Resolve a payload, optionally requiring its shared managed binding."""

    explicit = classify_configured_tool_path(workspace, config, field)
    if explicit.provenance is ToolPathProvenance.LEGACY_UNKNOWN:
        raise ManagedToolStoreError(
            f"{field} points at unknown legacy-looking content: "
            + " | ".join(explicit.diagnostics)
        )
    if (
        not managed_only
        and explicit.provenance is ToolPathProvenance.USER_EXTERNAL
    ):
        yield explicit
        return
    rule = FIELD_RULES[field]
    if rule.logical_name is None:
        raise ManagedToolStoreError(f"{field} has no managed payload mapping")
    roots = roots or resolve_managed_store_roots()
    workspace_id, game_id = _workspace_identity(workspace)
    lease_stack = ExitStack()
    try:
        entry_point, entry = lease_stack.enter_context(
            leased_bound_entry(
                roots,
                workspace,
                rule.logical_name,
                timeout_seconds=timeout_seconds,
                command=command or f"lease payload for {field}",
                expected_workspace_id=workspace_id,
                expected_game_id=game_id,
            )
        )
        _validate_binding_resolution(rule, entry)
    except (
        OSError,
        ValueError,
        ManagedToolStoreError,
        ManagedProcessEnvironmentError,
    ):
        lease_stack.close()
        raise
    try:
        yield ToolPathResolution(
            field,
            ToolPathProvenance.MANAGED_BINDING,
            (
                entry_point.parent
                if rule.managed_projection == "entry-parent"
                else entry_point
            ),
            rule.logical_name,
        )
    finally:
        lease_stack.close()


def managed_binding_health(
    workspace: Path,
    *,
    roots: ManagedStoreRoots | None = None,
    entry_snapshot: tuple[Mapping[str, Any], ...] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    roots = roots or resolve_managed_store_roots()
    diagnostics: list[str] = []
    rules_by_logical_name = {
        rule.logical_name.casefold(): rule
        for rule in FIELD_RULES.values()
        if rule.logical_name is not None
    }
    try:
        workspace_id, game_id = _workspace_identity(workspace)
        binding = read_workspace_binding(workspace)
        if (
            binding.workspace_id != workspace_id
            or binding.game_id != game_id
        ):
            raise ManagedToolStoreError(
                "managed-tool binding identity differs from the current "
                "workspace marker"
            )
    except (
        OSError,
        ValueError,
        ManagedToolStoreError,
        ManagedProcessEnvironmentError,
    ) as exc:
        return False, (str(exc),)
    snapshot_index = (
        {
            str(row.get("entry_id")): row
            for row in entry_snapshot
            if isinstance(row, Mapping)
        }
        if entry_snapshot is not None
        else None
    )
    for entry in binding.entries:
        rule = rules_by_logical_name.get(entry.logical_name.casefold())
        if rule is None:
            diagnostics.append(
                f"{entry.logical_name}: managed binding logical name is not registered"
            )
            continue
        try:
            _validate_binding_resolution(rule, entry)
            if snapshot_index is not None:
                _validate_snapshot_binding_entry(
                    binding,
                    entry,
                    snapshot_index.get(entry.entry_id),
                )
            else:
                _entry_point, resolved_entry = resolve_bound_entry(
                    roots,
                    workspace,
                    entry.logical_name,
                )
                _validate_binding_resolution(rule, resolved_entry)
        except (
            OSError,
            ValueError,
            ManagedToolStoreError,
            ManagedProcessEnvironmentError,
        ) as exc:
            diagnostics.append(f"{entry.logical_name}: {exc}")
    return not diagnostics, tuple(diagnostics)


def _validate_snapshot_binding_entry(
    binding: WorkspaceBinding,
    entry: WorkspaceBindingEntry,
    row: Mapping[str, Any] | None,
) -> None:
    if row is None:
        raise ManagedToolStoreError(
            f"managed-tool binding is unavailable: {entry.logical_name}: "
            "entry is absent from the doctor snapshot"
        )
    if (
        row.get("entry_id") != entry.entry_id
        or row.get("tool_kind") != entry.tool_kind
        or row.get("key_digest") != entry.key_digest
        or row.get("status") != "healthy"
    ):
        raise ManagedToolStoreError(
            f"managed-tool binding is unavailable: {entry.logical_name}: "
            "snapshot identity or health differs"
        )
    critical_entries = row.get("critical_entries")
    if not isinstance(critical_entries, list) or not all(
        isinstance(value, str) for value in critical_entries
    ):
        raise ManagedToolStoreError(
            f"managed-tool binding snapshot lacks critical-entry evidence: "
            f"{entry.logical_name}"
        )
    if entry.entry_point.casefold() not in {
        value.casefold() for value in critical_entries
    }:
        raise ManagedToolStoreError(
            f"managed binding entry point is not a verified critical entry: "
            f"{entry.logical_name}: {entry.entry_point}"
        )
    if not entry.tool_kind.startswith("adapter-"):
        return
    key_inputs = row.get("key_inputs")
    if not isinstance(key_inputs, Mapping):
        raise ManagedToolStoreError(
            f"managed adapter snapshot has no key inputs: {entry.logical_name}"
        )
    sdk_entry_id = key_inputs.get("sdk_entry_id")
    sdk_entries = [
        candidate
        for candidate in binding.entries
        if candidate.logical_name.casefold() == "dotnet-sdk"
    ]
    if (
        not isinstance(sdk_entry_id, str)
        or len(sdk_entries) != 1
        or sdk_entries[0].entry_id != sdk_entry_id
    ):
        raise ManagedToolStoreError(
            f"managed adapter SDK dependency differs from the workspace binding: "
            f"{entry.logical_name}"
        )

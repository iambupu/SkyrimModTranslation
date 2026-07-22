"""Stable public result contract shared by SMT CLI entry points."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, TypedDict

from file_utils import is_reparse_point, validate_regular_path_under
from project_paths import (
    WORKSPACE_MARKER,
    find_workspace_root,
    is_under,
    plugin_root,
    safe_file_name,
)
from smt_fingerprint import (
    FINGERPRINT_ALGORITHM,
    FinalizedModName,
    InputEntry,
    InputManifest,
    build_input_manifest,
    choose_workspace_name,
    composite_input_identity,
    derive_mod_name_candidate,
    finalize_mod_name,
    verify_imported_copy,
    verify_source_unchanged,
)
from smt_windows import (
    ManagedProcess,
    SmtLockTimeoutError,
    SmtProcessFileLock,
    documents_directory,
    local_app_data_directory,
)


SCHEMA_VERSION = 1

PublicOutcome: TypeAlias = Literal[
    "completed",
    "ready_for_manual_test",
    "needs_gui",
    "needs_agent_translation",
    "needs_user_input",
    "blocked",
]

EXIT_SUCCESS = 0
EXIT_INTERNAL_READ_OR_BUSY = 1
EXIT_SAFE_STOP = 3
EXIT_UNSUPPORTED_INPUT_OR_CAPABILITY = 4
EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE = 5
EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT = 6
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130


class ArtifactInfo(TypedDict):
    """A workspace artifact that supports a next action."""

    path: str
    exists: bool
    kind: str
    validated: bool | None
    validation_evidence: str | None


class NextAction(TypedDict):
    """The one public action a caller should take next."""

    kind: str
    summary: str
    artifacts: list[str]


@dataclass
class CliResult:
    """Schema v1 payload returned by every public SMT CLI command."""

    schema_version: int = SCHEMA_VERSION
    command: str = ""
    outcome: PublicOutcome | None = None
    exit_code: int = EXIT_INTERNAL_READ_OR_BUSY
    message: str = ""
    workspace: str | None = None
    mod_name: str | None = None
    game_id: str | None = None
    workflow_state: str | None = None
    state_snapshot: bool = False
    state_generated_at: str | None = None
    state_generated_at_timezone: str | None = None
    refreshed_by_this_command: bool = False
    busy: bool = False
    next_action: NextAction | None = None
    progress_card_path: str | None = None
    progress_card: str | None = None
    output_paths: dict[str, ArtifactInfo] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    diagnostic_log_path: str | None = None
    underlying_exit_codes: list[int] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of the fixed schema v1 payload."""

        return _validate_json_value(asdict(self))


def empty_result(command: str) -> CliResult:
    """Create an unpopulated result without emitting command output."""

    return CliResult(command=command)


def _validate_json_value(value: Any, path: str = "$") -> Any:
    """Reject values outside the JSON contract instead of coercing them."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        raise TypeError(f"{path}: pathlib.Path values are not allowed")
    if isinstance(value, dict):
        validated: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"{path}: dictionary keys must be str, got {type(key).__name__}"
                )
            validated[key] = _validate_json_value(item, f"{path}.{key}")
        return validated
    if isinstance(value, list):
        return [
            _validate_json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path}: unsupported JSON value {type(value).__name__}")


CLI_STATE_SCHEMA_VERSION = 1
SESSION_SCHEMA_VERSION = 1
WORKSPACE_MARKER_SCHEMA_VERSION = 2
WORKSPACE_KIND = "bethesda-mod-chs-translation-workspace"
CLI_STATE_DIRECTORY_NAME = "SkyrimModTranslation"
DEFAULT_WORKSPACE_DIRECTORY_NAME = "SkyrimModTranslationWorkspaces"
SESSION_RELATIVE_PATH = Path(".workflow") / "smt-session.json"
WORKSPACE_LOCK_RELATIVE_PATH = Path(".workflow") / "smt-operation.lock"
IMPORT_FAILURE_RELATIVE_PATH = Path(".workflow") / "smt-import-failure.json"
PARTIAL_IMPORT_PREFIX = ".smt-import-"
PARTIAL_IMPORT_SUFFIX = ".partial"


class WorkspaceConflictError(ValueError):
    """Raised when a workspace cannot be safely bound to the requested input."""


class CliStateError(ValueError):
    """Raised when the disposable CLI cache is malformed."""


class ImportTransactionError(RuntimeError):
    """Raised when a reserved workspace cannot complete its import transaction."""


class _ResolutionRetry(RuntimeError):
    """Internal signal that cache state changed after the first short snapshot."""


class _Lock(Protocol):
    def acquire(self) -> Any: ...

    def release(self) -> None: ...


LockFactory = Callable[..., _Lock]
WorkspaceInitializer = Callable[[Path, str, str], None]
FileCopier = Callable[[Path, Path], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _atomic_write_json_replace(
    path: Path,
    payload: Mapping[str, Any],
    *,
    allowed_root: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if allowed_root is not None:
        validate_regular_path_under(
            path.parent,
            allowed_root,
            kind="directory",
            label="atomic JSON parent",
        )
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceConflictError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceConflictError(f"{label} must be a JSON object: {path}")
    return payload


@dataclass(frozen=True)
class SmtSession:
    """Immutable identity record for one imported Mod in one workspace."""

    schema_version: int
    workspace_id: str
    mod_name: str
    game_id: str
    fingerprint_algorithm: str
    input_identity: str
    source_kind: str
    import_relative_path: str
    source_display_name: str = ""
    source_sha256: str = ""
    imported_sha256: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SESSION_SCHEMA_VERSION:
            raise WorkspaceConflictError(
                f"unsupported SMT session schema: {self.schema_version}"
            )
        try:
            uuid.UUID(self.workspace_id)
        except (ValueError, AttributeError) as exc:
            raise WorkspaceConflictError(
                "SMT session workspace_id is not a UUID"
            ) from exc
        if (
            safe_file_name(self.mod_name) != self.mod_name
            or _utf16_units(self.mod_name) > 80
        ):
            raise WorkspaceConflictError("SMT session mod_name is unsafe")
        if not self.game_id or self.source_kind not in {"directory", "zip", "7z"}:
            raise WorkspaceConflictError("SMT session game/source identity is missing")
        if self.fingerprint_algorithm != FINGERPRINT_ALGORITHM:
            raise WorkspaceConflictError(
                "SMT session fingerprint algorithm is unsupported"
            )
        expected_prefix = (
            f"{self.fingerprint_algorithm}:{self.game_id}:{self.source_kind}:"
        )
        if not self.input_identity.startswith(expected_prefix):
            raise WorkspaceConflictError(
                "SMT session composite identity is inconsistent"
            )
        identity_digest = self.input_identity[len(expected_prefix) :]
        source_digest = self.source_sha256 or identity_digest
        imported_digest = self.imported_sha256 or source_digest
        if not _is_sha256(source_digest) or imported_digest != source_digest:
            raise WorkspaceConflictError("SMT session source/import digest is invalid")
        if identity_digest != source_digest:
            raise WorkspaceConflictError(
                "SMT session digest does not match composite identity"
            )
        object.__setattr__(self, "source_sha256", source_digest)
        object.__setattr__(self, "imported_sha256", imported_digest)
        source_name = self.source_display_name or Path(self.import_relative_path).name
        if (
            Path(source_name).name != source_name
            or safe_file_name(source_name) != source_name
        ):
            raise WorkspaceConflictError("SMT session source display name is unsafe")
        object.__setattr__(self, "source_display_name", source_name)
        relative = Path(self.import_relative_path.replace("\\", "/"))
        extension = {"directory": "", "zip": ".zip", "7z": ".7z"}[self.source_kind]
        expected_import_name = f"{self.mod_name}{extension}"
        if (
            relative.is_absolute()
            or relative.parts[:1] != ("mod",)
            or len(relative.parts) != 2
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.name != safe_file_name(relative.name)
            or relative.name != expected_import_name
        ):
            raise WorkspaceConflictError(
                "SMT session import path must be one direct mod/ child"
            )
        if not self.created_at:
            object.__setattr__(self, "created_at", _utc_now())
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise WorkspaceConflictError(
                "SMT session created_at is not ISO-8601"
            ) from exc
        if created.tzinfo is None:
            raise WorkspaceConflictError(
                "SMT session created_at must include a timezone"
            )

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SmtSession:
        allowed = {
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
        unknown = set(payload) - allowed
        if unknown:
            raise WorkspaceConflictError(
                f"SMT session contains unknown immutable fields: {sorted(unknown)}"
            )
        try:
            game_id = str(payload["game_id"])
            source_kind = str(payload["source_kind"])
            algorithm = str(payload["fingerprint_algorithm"])
            source_sha256 = str(payload["source_sha256"])
            identity = str(
                payload.get(
                    "input_identity",
                    f"{algorithm}:{game_id}:{source_kind}:{source_sha256}",
                )
            )
            return cls(
                schema_version=int(payload["schema_version"]),
                workspace_id=str(payload["workspace_id"]),
                mod_name=str(payload["mod_name"]),
                game_id=game_id,
                fingerprint_algorithm=algorithm,
                input_identity=identity,
                source_kind=source_kind,
                source_display_name=str(payload.get("source_display_name", "")),
                source_sha256=source_sha256,
                import_relative_path=str(payload["import_relative_path"]),
                imported_sha256=str(payload["imported_sha256"]),
                created_at=str(payload["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceConflictError(f"invalid SMT session payload: {exc}") from exc


def create_session_no_replace(path: Path, session: SmtSession) -> None:
    """Atomically publish the first session and only validate later attempts."""

    path = _normalized_absolute(path)
    if path.exists():
        existing = SmtSession.from_payload(_read_json_object(path, label="SMT session"))
        if existing != session:
            raise WorkspaceConflictError("existing SMT session identity is immutable")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_regular_path_under(
        path.parent,
        path.parent.parent,
        kind="directory",
        label="SMT session parent",
    )
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    encoded = (
        json.dumps(session.to_payload(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                # Windows rename is an atomic no-replace move.  It avoids
                # requiring hard-link support from the workspace volume.
                os.rename(temporary, path)
            else:
                # Real commands are Windows-only, but static/contract tests
                # still need a no-replace publication primitive elsewhere.
                os.link(temporary, path)
        except FileExistsError:
            existing = SmtSession.from_payload(
                _read_json_object(path, label="SMT session")
            )
            if existing != session:
                raise WorkspaceConflictError(
                    "existing SMT session identity is immutable"
                )
        except OSError as exc:
            if path.exists():
                existing = SmtSession.from_payload(
                    _read_json_object(path, label="SMT session")
                )
                if existing != session:
                    raise WorkspaceConflictError(
                        "existing SMT session identity is immutable"
                    )
            else:
                raise WorkspaceConflictError(
                    f"could not atomically commit SMT session: {exc}"
                ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _empty_cli_state() -> dict[str, Any]:
    return {
        "schema_version": CLI_STATE_SCHEMA_VERSION,
        "last_workspace": None,
        "input_mappings": {},
        "reservations": {},
    }


@dataclass(frozen=True)
class CliStateStore:
    """Atomic, disposable cache under the Known Local AppData state directory."""

    root: Path

    @property
    def path(self) -> Path:
        return _normalized_absolute(self.root) / "cli-state.json"

    @property
    def lock_path(self) -> Path:
        return _normalized_absolute(self.root) / "cli-state.lock"

    @property
    def reservation_lock_root(self) -> Path:
        return _normalized_absolute(self.root) / "reservation-locks"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_cli_state()
        payload = _read_json_object(self.path, label="SMT CLI state")
        if payload.get("schema_version") != CLI_STATE_SCHEMA_VERSION:
            raise CliStateError("unsupported SMT CLI state schema")
        if set(payload) != {
            "schema_version",
            "last_workspace",
            "input_mappings",
            "reservations",
        }:
            raise CliStateError("SMT CLI state fields do not match schema v1")
        if payload["last_workspace"] is not None and not isinstance(
            payload["last_workspace"], str
        ):
            raise CliStateError("SMT CLI last workspace must be a string or null")
        if not isinstance(payload["input_mappings"], dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload["input_mappings"].items()
        ):
            raise CliStateError("SMT CLI mappings must be string pairs")
        if not isinstance(payload["reservations"], dict):
            raise CliStateError("SMT CLI reservations must be an object")
        for key, row in payload["reservations"].items():
            if not isinstance(key, str) or not isinstance(row, dict):
                raise CliStateError("SMT CLI reservation rows must be keyed objects")
            required = {
                "workspace_id",
                "path",
                "fingerprint_identity",
                "pid",
                "created_at",
            }
            if set(row) != required or row.get("workspace_id") != key:
                raise CliStateError("SMT CLI reservation row does not match schema v1")
            try:
                uuid.UUID(key)
            except ValueError as exc:
                raise CliStateError(
                    "SMT CLI reservation workspace_id is invalid"
                ) from exc
            if (
                not isinstance(row["path"], str)
                or not Path(row["path"]).is_absolute()
                or not isinstance(row["fingerprint_identity"], str)
                or not isinstance(row["pid"], int)
                or isinstance(row["pid"], bool)
                or row["pid"] < 0
                or not isinstance(row["created_at"], str)
            ):
                raise CliStateError("SMT CLI reservation values are invalid")
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        candidate = dict(payload)
        if candidate.get("schema_version") != CLI_STATE_SCHEMA_VERSION:
            raise CliStateError("refusing to write unsupported SMT CLI state schema")
        _atomic_write_json_replace(self.path, candidate, allowed_root=self.root)


@dataclass(frozen=True)
class RunRequest:
    source: Path
    game_id: str
    workspace: Path | None = None
    workspace_root: Path | None = None
    cwd: Path | None = None
    local_state_root: Path | None = None
    tool_setup: Literal["auto", "manual", "skip"] = "auto"
    timeout_seconds: float = 5.0
    initializer: WorkspaceInitializer | None = None
    copier: FileCopier = shutil.copyfile
    lock_factory: LockFactory = SmtProcessFileLock


@dataclass
class WorkspaceResolution:
    workspace: Path
    workspace_id: str
    input_identity: str
    finalized_mod_name: FinalizedModName
    game_id: str
    source_display_name: str
    state_store: CliStateStore
    is_new: bool
    owns_reservation: bool
    tool_setup: str
    timeout_seconds: float
    initializer: WorkspaceInitializer | None
    copier: FileCopier
    lock_factory: LockFactory
    reservation_lock: _Lock | None = field(default=None, repr=False)
    workspace_lock: _Lock | None = field(default=None, repr=False)

    def close(self) -> None:
        if self.workspace_lock is not None:
            self.workspace_lock.release()
            self.workspace_lock = None
        if self.reservation_lock is not None:
            self.reservation_lock.release()
            self.reservation_lock = None

    def __enter__(self) -> WorkspaceResolution:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _state_root(request: RunRequest) -> Path:
    if request.local_state_root is not None:
        return _normalized_absolute(request.local_state_root)
    return local_app_data_directory() / CLI_STATE_DIRECTORY_NAME


def _workspace_root(request: RunRequest) -> Path:
    if request.workspace_root is not None:
        return _normalized_absolute(request.workspace_root)
    return documents_directory() / DEFAULT_WORKSPACE_DIRECTORY_NAME


def _marker_game(workspace: Path) -> str:
    marker_path = workspace / WORKSPACE_MARKER
    try:
        validate_regular_path_under(
            marker_path,
            workspace,
            kind="file",
            label="workspace marker",
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(
            f"workspace marker is invalid: {workspace}: {exc}"
        ) from exc
    marker = _read_json_object(marker_path, label="workspace marker")
    if (
        marker.get("schema_version") != WORKSPACE_MARKER_SCHEMA_VERSION
        or marker.get("kind") != WORKSPACE_KIND
        or not isinstance(marker.get("game_id"), str)
    ):
        raise WorkspaceConflictError(
            f"workspace marker identity is invalid: {workspace}"
        )
    return str(marker["game_id"])


def _partial_imports(workspace: Path) -> tuple[Path, ...]:
    mod_root = workspace / "mod"
    try:
        validate_regular_path_under(
            mod_root,
            workspace,
            kind="directory",
            label="workspace Mod root",
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(f"workspace Mod root is invalid: {exc}") from exc
    try:
        return tuple(
            sorted(
                (
                    child
                    for child in mod_root.iterdir()
                    if child.name.startswith(PARTIAL_IMPORT_PREFIX)
                    and child.name.endswith(PARTIAL_IMPORT_SUFFIX)
                ),
                key=lambda path: path.name.casefold(),
            )
        )
    except OSError as exc:
        raise WorkspaceConflictError(
            f"cannot inspect workspace import transactions: {exc}"
        ) from exc


def validate_session(workspace: Path, identity: str | None = None) -> SmtSession:
    """Validate marker, immutable session, committed import, and transaction state."""

    workspace = _normalized_absolute(workspace)
    try:
        validate_regular_path_under(
            workspace,
            workspace,
            kind="directory",
            label="SMT workspace",
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(
            f"SMT workspace is invalid: {workspace}: {exc}"
        ) from exc
    if is_under(workspace, plugin_root()):
        raise WorkspaceConflictError(
            "SMT workspace cannot be the plugin repository or its child"
        )
    marker_game = _marker_game(workspace)
    session_path = workspace / SESSION_RELATIVE_PATH
    try:
        validate_regular_path_under(
            session_path,
            workspace,
            kind="file",
            label="SMT session",
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(
            f"SMT session is invalid: {workspace}: {exc}"
        ) from exc
    session = SmtSession.from_payload(
        _read_json_object(session_path, label="SMT session")
    )
    if session.game_id != marker_game:
        raise WorkspaceConflictError(
            "workspace marker and SMT session game identities differ"
        )
    if identity is not None and session.input_identity != identity:
        raise WorkspaceConflictError(
            "SMT session does not match the requested input identity"
        )
    partials = _partial_imports(workspace)
    if partials:
        raise WorkspaceConflictError(
            "workspace contains unfinished partial import transactions: "
            + ", ".join(str(path) for path in partials)
        )
    target = workspace / Path(session.import_relative_path)
    expected_kind = "directory" if session.source_kind == "directory" else "file"
    try:
        validate_regular_path_under(
            target,
            workspace / "mod",
            kind=expected_kind,
            label="SMT imported input",
        )
        expected = InputManifest(
            source_kind=session.source_kind,  # type: ignore[arg-type]
            entries=(),
            digest=session.imported_sha256,
            source_identity=None,
        )
        if session.source_kind == "directory":
            imported = build_input_manifest(target)
            if imported.digest != session.imported_sha256:
                raise WorkspaceConflictError(
                    "SMT imported directory digest does not match session"
                )
        else:
            verify_imported_copy(target, expected)
    except WorkspaceConflictError:
        raise
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(
            f"SMT imported copy/digest is invalid: {exc}"
        ) from exc
    return session


def detect_extra_mod_inputs(workspace: Path, session: SmtSession) -> tuple[str, ...]:
    """Report unregistered top-level Mod inputs without absorbing them."""

    mod_root = _normalized_absolute(workspace) / "mod"
    try:
        validate_regular_path_under(
            mod_root,
            _normalized_absolute(workspace),
            kind="directory",
            label="workspace Mod root",
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(f"workspace Mod root is invalid: {exc}") from exc
    expected_name = Path(session.import_relative_path).name.casefold()
    extras: list[str] = []
    try:
        children = list(mod_root.iterdir())
    except OSError as exc:
        raise WorkspaceConflictError(f"cannot inspect Mod input root: {exc}") from exc
    for child in children:
        if child.name.casefold() == expected_name or child.name == ".gitkeep":
            continue
        if child.name.startswith(PARTIAL_IMPORT_PREFIX):
            continue
        entry_stat = child.lstat()
        if child.is_symlink() or is_reparse_point(entry_stat):
            extras.append(f"mod/{child.name} (link/reparse)")
        else:
            extras.append(f"mod/{child.name}")
    return tuple(sorted(extras, key=str.casefold))


def exact_queue_arguments(session: SmtSession) -> tuple[str, ...]:
    return (
        "--mod-name",
        session.mod_name,
        "--source-path",
        session.import_relative_path,
        "--limit",
        "1",
    )


def resolve_command_workspace(
    explicit_workspace: Path | None,
    cwd: Path | None,
    state_store: CliStateStore,
) -> Path:
    """Resolve non-run commands by explicit, current, then last-active precedence."""

    if explicit_workspace is not None:
        candidate = _normalized_absolute(explicit_workspace)
        if is_under(candidate, plugin_root()):
            raise WorkspaceConflictError("workspace cannot be inside plugin source")
        _marker_game(candidate)
        return candidate
    current = find_workspace_root(_normalized_absolute(cwd or Path.cwd()))
    if current is not None:
        if is_under(current, plugin_root()):
            raise WorkspaceConflictError("workspace cannot be inside plugin source")
        _marker_game(current)
        return current
    state = state_store.read()
    last = state["last_workspace"]
    if not isinstance(last, str):
        raise WorkspaceConflictError("no selected or recently active SMT workspace")
    candidate = _normalized_absolute(Path(last))
    if is_under(candidate, plugin_root()):
        raise WorkspaceConflictError("workspace cannot be inside plugin source")
    _marker_game(candidate)
    return candidate


def _lock(
    factory: LockFactory,
    path: Path,
    timeout_seconds: float,
    *,
    command: str,
) -> _Lock:
    return factory(
        path,
        "exclusive",
        timeout_seconds,
        command=command,
    )


def _valid_matching_session(workspace: Path, identity: str) -> SmtSession | None:
    try:
        return validate_session(workspace, identity)
    except (OSError, ValueError):
        return None


def _acquire_existing_resolution(
    request: RunRequest,
    store: CliStateStore,
    workspace: Path,
    session: SmtSession,
    finalized: FinalizedModName,
    identity: str,
    *,
    reservation_lock: _Lock | None = None,
) -> WorkspaceResolution:
    workspace_lock = _lock(
        request.lock_factory,
        workspace / WORKSPACE_LOCK_RELATIVE_PATH,
        request.timeout_seconds,
        command="run",
    )
    workspace_lock.acquire()
    try:
        validated = validate_session(workspace, identity)
        if validated != session:
            raise WorkspaceConflictError(
                "SMT session changed while acquiring workspace lock"
            )
        with _lock(
            request.lock_factory,
            store.lock_path,
            request.timeout_seconds,
            command="run-state",
        ):
            state = store.read()
            state["input_mappings"][identity] = str(workspace)
            state["last_workspace"] = str(workspace)
            state["reservations"].pop(session.workspace_id, None)
            store.write(state)
    except BaseException:
        workspace_lock.release()
        raise
    del finalized
    existing_finalized = FinalizedModName(
        source_kind=session.source_kind,  # type: ignore[arg-type]
        value=session.mod_name,
        import_name=Path(session.import_relative_path).name,
        digest_suffix_applied=False,
        digest_prefix=None,
    )
    return WorkspaceResolution(
        workspace=workspace,
        workspace_id=session.workspace_id,
        input_identity=identity,
        finalized_mod_name=existing_finalized,
        game_id=request.game_id,
        source_display_name=session.source_display_name,
        state_store=store,
        is_new=False,
        owns_reservation=False,
        tool_setup=request.tool_setup,
        timeout_seconds=request.timeout_seconds,
        initializer=request.initializer,
        copier=request.copier,
        lock_factory=request.lock_factory,
        reservation_lock=reservation_lock,
        workspace_lock=workspace_lock,
    )


def _direct_session_matches(
    root: Path,
    identity: str,
) -> list[tuple[Path, SmtSession]]:
    if not root.is_dir():
        return []
    matches: list[tuple[Path, SmtSession]] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError as exc:
        raise WorkspaceConflictError(
            f"cannot scan workspace root: {root}: {exc}"
        ) from exc
    for child in children:
        try:
            session = validate_session(child, identity)
        except (OSError, ValueError):
            continue
        matches.append((child, session))
    return matches


def _occupied_workspace_names(root: Path, state: Mapping[str, Any]) -> set[str]:
    occupied: set[str] = set()
    if root.is_dir():
        try:
            occupied.update(path.name for path in root.iterdir())
        except OSError as exc:
            raise WorkspaceConflictError(
                f"cannot enumerate workspace names: {exc}"
            ) from exc
    for row in state["reservations"].values():
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            occupied.add(Path(row["path"]).name)
    return occupied


def _reservation_rows(
    identity: str, state: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for row in state["reservations"].values():
        if isinstance(row, dict) and row.get("fingerprint_identity") == identity:
            rows.append(dict(row))
    return tuple(rows)


def _next_reservation_row(
    identity: str,
    state: Mapping[str, Any],
    ignored_reservation_ids: frozenset[str],
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in _reservation_rows(identity, state)
            if str(row.get("workspace_id", "")) not in ignored_reservation_ids
        ),
        None,
    )


def _new_reservation(
    request: RunRequest,
    manifest: InputManifest,
    store: CliStateStore,
    root: Path,
    identity: str,
    finalized: FinalizedModName,
    *,
    explicit_path: Path | None,
    ignored_reservation_ids: frozenset[str] = frozenset(),
) -> WorkspaceResolution:
    global_lock = _lock(
        request.lock_factory,
        store.lock_path,
        request.timeout_seconds,
        command="run-state",
    )
    global_lock.acquire()
    reservation_lock: _Lock | None = None
    workspace_id = str(uuid.uuid4())
    try:
        state = store.read()
        if identity in state["input_mappings"]:
            raise _ResolutionRetry("input mapping appeared while reserving workspace")
        existing_reservation = _next_reservation_row(
            identity,
            state,
            ignored_reservation_ids,
        )
        if existing_reservation is not None:
            raise _ResolutionRetry(
                "input reservation appeared while reserving workspace"
            )
        if explicit_path is None:
            name = choose_workspace_name(
                finalized,
                manifest.digest,
                _occupied_workspace_names(root, state),
            )
            workspace = root / name
        else:
            workspace = explicit_path
        row = {
            "workspace_id": workspace_id,
            "path": str(workspace),
            "fingerprint_identity": identity,
            "pid": os.getpid(),
            "created_at": _utc_now(),
        }
        state["reservations"][workspace_id] = row
        store.write(state)
        reservation_lock = _lock(
            request.lock_factory,
            store.reservation_lock_root / f"{workspace_id}.lock",
            0,
            command="run-reservation",
        )
        try:
            reservation_lock.acquire()
        except BaseException:
            state = store.read()
            if state["reservations"].get(workspace_id) == row:
                state["reservations"].pop(workspace_id, None)
                store.write(state)
            raise
    finally:
        global_lock.release()
    return WorkspaceResolution(
        workspace=workspace,
        workspace_id=workspace_id,
        input_identity=identity,
        finalized_mod_name=finalized,
        game_id=request.game_id,
        source_display_name=Path(request.source).name,
        state_store=store,
        is_new=True,
        owns_reservation=True,
        tool_setup=request.tool_setup,
        timeout_seconds=request.timeout_seconds,
        initializer=request.initializer,
        copier=request.copier,
        lock_factory=request.lock_factory,
        reservation_lock=reservation_lock,
    )


def resolve_run_workspace(
    request: RunRequest,
    manifest: InputManifest,
    *,
    _ignored_reservation_ids: frozenset[str] = frozenset(),
    _retry_depth: int = 0,
) -> WorkspaceResolution:
    """Resolve or reserve exactly one workspace for a run input identity."""

    if request.tool_setup not in {"auto", "manual", "skip"}:
        raise ValueError("tool_setup must be auto, manual, or skip")
    if _retry_depth > 8:
        raise WorkspaceConflictError("workspace reservation state kept changing")
    source = _normalized_absolute(request.source)
    identity = composite_input_identity(request.game_id, manifest)
    finalized = finalize_mod_name(
        derive_mod_name_candidate(source),
        manifest.digest,
        source_kind=manifest.source_kind,
    )
    store = CliStateStore(_state_root(request))
    root = _workspace_root(request)
    if is_under(root, plugin_root()):
        raise WorkspaceConflictError("workspace root cannot be inside plugin source")
    explicit = (
        _normalized_absolute(request.workspace)
        if request.workspace is not None
        else None
    )

    if explicit is not None:
        if is_under(explicit, plugin_root()):
            raise WorkspaceConflictError(
                "explicit workspace cannot be inside plugin source"
            )
        if explicit.exists():
            if not explicit.is_dir():
                raise WorkspaceConflictError("explicit workspace is not a directory")
            if any(explicit.iterdir()):
                session = _valid_matching_session(explicit, identity)
                if session is None or session.game_id != request.game_id:
                    raise WorkspaceConflictError(
                        "explicit workspace marker/session/input identity conflicts with run"
                    )
                return _acquire_existing_resolution(
                    request,
                    store,
                    explicit,
                    session,
                    finalized,
                    identity,
                )
        with _lock(
            request.lock_factory,
            store.lock_path,
            request.timeout_seconds,
            command="run-state",
        ):
            explicit_state = store.read()
            mapped = explicit_state["input_mappings"].get(identity)
            reservation = _next_reservation_row(
                identity,
                explicit_state,
                _ignored_reservation_ids,
            )
        if isinstance(mapped, str):
            mapped_workspace = _normalized_absolute(Path(mapped))
            if mapped_workspace != explicit:
                raise WorkspaceConflictError(
                    "explicit workspace conflicts with the existing input mapping"
                )
            session = _valid_matching_session(mapped_workspace, identity)
            if session is None:
                raise WorkspaceConflictError("explicit workspace mapping is invalid")
            return _acquire_existing_resolution(
                request,
                store,
                mapped_workspace,
                session,
                finalized,
                identity,
            )
        if reservation is not None:
            reservation_workspace = _normalized_absolute(Path(str(reservation["path"])))
            if reservation_workspace != explicit:
                raise WorkspaceConflictError(
                    "explicit workspace conflicts with an in-progress input reservation"
                )
            reservation_id = str(reservation["workspace_id"])
            existing_lock = _lock(
                request.lock_factory,
                store.reservation_lock_root / f"{reservation_id}.lock",
                request.timeout_seconds,
                command="run-reservation-wait",
            )
            try:
                existing_lock.acquire()
            except SmtLockTimeoutError as exc:
                raise WorkspaceConflictError(
                    "explicit workspace is still being initialized"
                ) from exc
            session = _valid_matching_session(explicit, identity)
            if session is not None:
                return _acquire_existing_resolution(
                    request,
                    store,
                    explicit,
                    session,
                    finalized,
                    identity,
                    reservation_lock=existing_lock,
                )
            existing_lock.release()
            _ignored_reservation_ids = _ignored_reservation_ids | frozenset(
                {reservation_id}
            )
        try:
            return _new_reservation(
                request,
                manifest,
                store,
                root,
                identity,
                finalized,
                explicit_path=explicit,
                ignored_reservation_ids=_ignored_reservation_ids,
            )
        except _ResolutionRetry:
            return resolve_run_workspace(
                request,
                manifest,
                _ignored_reservation_ids=_ignored_reservation_ids,
                _retry_depth=_retry_depth + 1,
            )

    current = find_workspace_root(_normalized_absolute(request.cwd or Path.cwd()))
    if current is not None:
        session = _valid_matching_session(current, identity)
        if session is not None:
            return _acquire_existing_resolution(
                request,
                store,
                current,
                session,
                finalized,
                identity,
            )

    with _lock(
        request.lock_factory,
        store.lock_path,
        request.timeout_seconds,
        command="run-state",
    ):
        snapshot = store.read()
        mapped = snapshot["input_mappings"].get(identity)
        reservation = _next_reservation_row(
            identity,
            snapshot,
            _ignored_reservation_ids,
        )

    if isinstance(mapped, str):
        mapped_workspace = _normalized_absolute(Path(mapped))
        session = _valid_matching_session(mapped_workspace, identity)
        if session is not None:
            return _acquire_existing_resolution(
                request,
                store,
                mapped_workspace,
                session,
                finalized,
                identity,
            )
        with _lock(
            request.lock_factory,
            store.lock_path,
            request.timeout_seconds,
            command="run-state",
        ):
            state = store.read()
            if state["input_mappings"].get(identity) == mapped:
                state["input_mappings"].pop(identity, None)
                store.write(state)

    if reservation is not None:
        reservation_workspace = _normalized_absolute(
            Path(str(reservation.get("path", "")))
        )
        reservation_id = str(reservation.get("workspace_id", ""))
        if reservation_id:
            existing_lock = _lock(
                request.lock_factory,
                store.reservation_lock_root / f"{reservation_id}.lock",
                request.timeout_seconds,
                command="run-reservation-wait",
            )
            try:
                existing_lock.acquire()
            except SmtLockTimeoutError as exc:
                raise WorkspaceConflictError(
                    "workspace is still being initialized for this input"
                ) from exc
            session = _valid_matching_session(reservation_workspace, identity)
            if session is not None:
                return _acquire_existing_resolution(
                    request,
                    store,
                    reservation_workspace,
                    session,
                    finalized,
                    identity,
                    reservation_lock=existing_lock,
                )
            existing_lock.release()
            _ignored_reservation_ids = _ignored_reservation_ids | frozenset(
                {reservation_id}
            )

    matches = _direct_session_matches(root, identity)
    if len(matches) == 1:
        workspace, session = matches[0]
        return _acquire_existing_resolution(
            request,
            store,
            workspace,
            session,
            finalized,
            identity,
        )
    if len(matches) > 1:
        raise WorkspaceConflictError(
            "multiple workspaces match the same SMT input identity: "
            + ", ".join(str(workspace) for workspace, _session in matches)
        )
    try:
        return _new_reservation(
            request,
            manifest,
            store,
            root,
            identity,
            finalized,
            explicit_path=None,
            ignored_reservation_ids=_ignored_reservation_ids,
        )
    except _ResolutionRetry:
        return resolve_run_workspace(
            request,
            manifest,
            _ignored_reservation_ids=_ignored_reservation_ids,
            _retry_depth=_retry_depth + 1,
        )


def _entry_identity(path: Path) -> tuple[int, int, int, int]:
    entry_stat = path.lstat()
    if path.is_symlink() or is_reparse_point(entry_stat):
        raise ImportTransactionError(
            f"source entry became a link/reparse point: {path}"
        )
    return (
        int(entry_stat.st_dev),
        int(entry_stat.st_ino),
        int(entry_stat.st_size),
        int(entry_stat.st_mtime_ns),
    )


def _verify_bound_entry(source_root: Path, entry: InputEntry) -> Path:
    target = source_root.joinpath(*entry.relative_path.split("/"))
    if entry.identity is None:
        raise ImportTransactionError(
            f"input manifest entry has no bound identity: {entry.relative_path}"
        )
    expected = (
        entry.identity.device,
        entry.identity.inode,
        entry.identity.size,
        entry.identity.mtime_ns,
    )
    if _entry_identity(target) != expected:
        raise ImportTransactionError(
            f"source entry changed before copy: {entry.relative_path}"
        )
    mode = target.lstat().st_mode
    if entry.entry_type == "directory" and not stat.S_ISDIR(mode):
        raise ImportTransactionError(
            f"source directory changed type: {entry.relative_path}"
        )
    if entry.entry_type == "file" and not stat.S_ISREG(mode):
        raise ImportTransactionError(f"source file changed type: {entry.relative_path}")
    return target


def _copy_manifest_input(
    source: Path,
    staging: Path,
    manifest: InputManifest,
    copier: FileCopier,
) -> None:
    if manifest.source_kind != "directory":
        staging.parent.mkdir(parents=True, exist_ok=True)
        copier(source, staging)
        return
    staging.mkdir()
    for entry in manifest.entries:
        source_entry = _verify_bound_entry(source, entry)
        target_entry = staging.joinpath(*entry.relative_path.split("/"))
        if entry.entry_type == "directory":
            target_entry.mkdir(parents=True, exist_ok=False)
            continue
        target_entry.parent.mkdir(parents=True, exist_ok=True)
        copier(source_entry, target_entry)
        _verify_bound_entry(source, entry)


def _remove_owned_staging(path: Path) -> None:
    if not os.path.lexists(path):
        return
    entry_stat = path.lstat()
    if path.is_symlink() or is_reparse_point(entry_stat):
        path.unlink(missing_ok=True)
    elif stat.S_ISDIR(entry_stat.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _default_initializer(resolution: WorkspaceResolution) -> None:
    command = [
        sys.executable,
        str(plugin_root() / "scripts" / "init_workspace.py"),
        str(resolution.workspace),
        "--game",
        resolution.game_id,
        "--tool-setup",
        resolution.tool_setup,
    ]
    environment = dict(os.environ)
    environment.update(
        {
            "SKYRIM_CHS_WORKSPACE_ROOT": str(resolution.workspace),
            "SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root()),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    result = ManagedProcess().run(
        command,
        cwd=plugin_root(),
        env=environment,
        timeout_seconds=resolution.timeout_seconds,
        log_path=(
            resolution.state_store.root
            / "logs"
            / f"initialize-{resolution.workspace_id}.log"
        ),
        output_encoding="utf-8",
    )
    if result.exit_code != 0:
        raise ImportTransactionError(
            "workspace initializer failed with exit code "
            f"{result.exit_code}: {' | '.join(result.output_tail[-10:])}"
        )


def _acquire_workspace_after_initialization(resolution: WorkspaceResolution) -> None:
    if resolution.workspace_lock is not None:
        return
    workspace_lock = _lock(
        resolution.lock_factory,
        resolution.workspace / WORKSPACE_LOCK_RELATIVE_PATH,
        resolution.timeout_seconds,
        command="run",
    )
    workspace_lock.acquire()
    resolution.workspace_lock = workspace_lock


def _commit_resolution_mapping(
    resolution: WorkspaceResolution, session: SmtSession
) -> None:
    with _lock(
        resolution.lock_factory,
        resolution.state_store.lock_path,
        resolution.timeout_seconds,
        command="run-state",
    ):
        state = resolution.state_store.read()
        state["input_mappings"][session.input_identity] = str(resolution.workspace)
        state["last_workspace"] = str(resolution.workspace)
        state["reservations"].pop(resolution.workspace_id, None)
        resolution.state_store.write(state)


def _write_import_failure(resolution: WorkspaceResolution, exc: BaseException) -> None:
    if (
        not resolution.owns_reservation
        or not (resolution.workspace / ".workflow").is_dir()
    ):
        return
    validate_regular_path_under(
        resolution.workspace / ".workflow",
        resolution.workspace,
        kind="directory",
        label="SMT workflow directory",
    )
    _atomic_write_json_replace(
        resolution.workspace / IMPORT_FAILURE_RELATIVE_PATH,
        {
            "schema_version": 1,
            "workspace_id": resolution.workspace_id,
            "fingerprint_identity": resolution.input_identity,
            "failed_at": _utc_now(),
            "error_type": type(exc).__name__,
            "message": str(exc),
        },
        allowed_root=resolution.workspace,
    )


def import_input_transactionally(
    source: Path,
    resolution: WorkspaceResolution,
    manifest: InputManifest,
) -> SmtSession:
    """Initialize, copy, verify, commit, then publish session and cache mapping."""

    source = _normalized_absolute(source)
    if (
        composite_input_identity(resolution.game_id, manifest)
        != resolution.input_identity
    ):
        raise WorkspaceConflictError(
            "input manifest does not match workspace reservation"
        )
    if not resolution.is_new:
        return validate_session(resolution.workspace, resolution.input_identity)
    if not resolution.owns_reservation or resolution.reservation_lock is None:
        raise WorkspaceConflictError(
            "new workspace import requires an owned reservation"
        )

    staging: Path | None = None
    committed_target: Path | None = None
    session_created = False
    try:
        if resolution.initializer is None:
            _default_initializer(resolution)
        else:
            resolution.initializer(
                resolution.workspace,
                resolution.game_id,
                resolution.tool_setup,
            )
        if _marker_game(resolution.workspace) != resolution.game_id:
            raise WorkspaceConflictError(
                "initialized workspace game marker conflicts with run"
            )
        _acquire_workspace_after_initialization(resolution)
        mod_root = resolution.workspace / "mod"
        validate_regular_path_under(
            mod_root,
            resolution.workspace,
            kind="directory",
            label="workspace Mod root",
        )
        staging = (
            mod_root
            / f"{PARTIAL_IMPORT_PREFIX}{uuid.uuid4().hex}{PARTIAL_IMPORT_SUFFIX}"
        )
        target = mod_root / resolution.finalized_mod_name.import_name
        if os.path.lexists(target):
            raise WorkspaceConflictError(
                f"refusing to overwrite existing Mod input: {target}"
            )
        _copy_manifest_input(source, staging, manifest, resolution.copier)
        verify_imported_copy(staging, manifest)
        verify_source_unchanged(source, manifest)
        os.rename(staging, target)
        committed_target = target
        staging = None
        session = SmtSession(
            schema_version=SESSION_SCHEMA_VERSION,
            workspace_id=resolution.workspace_id,
            mod_name=resolution.finalized_mod_name.value,
            game_id=resolution.game_id,
            fingerprint_algorithm=FINGERPRINT_ALGORITHM,
            input_identity=resolution.input_identity,
            source_kind=manifest.source_kind,
            source_display_name=resolution.source_display_name,
            source_sha256=manifest.digest,
            import_relative_path=f"mod/{resolution.finalized_mod_name.import_name}",
            imported_sha256=manifest.digest,
            created_at=_utc_now(),
        )
        create_session_no_replace(resolution.workspace / SESSION_RELATIVE_PATH, session)
        session_created = True
        _commit_resolution_mapping(resolution, session)
        resolution.is_new = False
        resolution.owns_reservation = False
        return session
    except BaseException as exc:
        if staging is not None:
            _remove_owned_staging(staging)
        if committed_target is not None and not session_created:
            _remove_owned_staging(committed_target)
        try:
            _write_import_failure(resolution, exc)
        except (OSError, ValueError):
            # A derived diagnostic report must never replace the original
            # transaction failure or broaden cleanup beyond owned staging.
            pass
        raise

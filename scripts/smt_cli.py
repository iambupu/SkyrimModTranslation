"""Stable public result contract shared by SMT CLI entry points."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Protocol, TypeAlias, TypedDict

from file_utils import is_reparse_point, validate_regular_path_under
from agent_capabilities import KNOWN_AGENT_CAPABILITIES
from project_paths import (
    WORKSPACE_MARKER,
    final_mod_dir,
    find_workspace_root,
    intermediate_output_dir,
    is_under,
    localization_output_root,
    packaged_mod_path,
    plugin_root,
    safe_file_name,
)
from smt_fingerprint import (
    FINGERPRINT_ALGORITHM,
    FinalizedModName,
    InputChangedError,
    InputEntry,
    InputManifest,
    InputSafetyError,
    UnsupportedInputError,
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
    ManagedProcessEnvironmentError,
    ProcessResult,
    SmtLockTimeoutError,
    SmtProcessFileLock,
    documents_directory,
    local_app_data_directory,
)
from workflow_refresh import CORE_REFRESH_STEPS
from workflow_task_policy import (
    GLOBAL_RESOURCE,
    GUI_RESOURCE,
    dependencies_satisfied,
    resources_available,
    split_task_command,
    task_can_be_started,
)
from workflow_agent_log import append_workflow_agent_event


SCHEMA_VERSION = 1
MAX_DIAGNOSTIC_LINES = 200

PublicOutcome: TypeAlias = Literal[
    "completed",
    "ready_for_manual_test",
    "needs_gui",
    "needs_agent_translation",
    "needs_user_input",
    "blocked",
]


class _DiagnosticTail(list[str]):
    """A list-compatible diagnostic buffer that never retains over 200 lines."""

    def __init__(self, values: Sequence[str] = ()) -> None:
        super().__init__()
        self.extend(values)

    def append(self, value: str) -> None:
        if len(self) >= MAX_DIAGNOSTIC_LINES:
            del self[: len(self) - MAX_DIAGNOSTIC_LINES + 1]
        super().append(value)

    def extend(self, values: Iterable[str]) -> None:
        for value in values:
            self.append(value)


def _append_diagnostic(diagnostics: list[str], value: str) -> None:
    if len(diagnostics) >= MAX_DIAGNOSTIC_LINES:
        del diagnostics[: len(diagnostics) - MAX_DIAGNOSTIC_LINES + 1]
    diagnostics.append(value)


def _extend_diagnostics(diagnostics: list[str], values: Iterable[str]) -> None:
    for value in values:
        _append_diagnostic(diagnostics, value)

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

    def __post_init__(self) -> None:
        self.diagnostics = _DiagnosticTail(self.diagnostics)

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
PARTIAL_IMPORT_NAME_RE = re.compile(r"\A\.smt-import-[0-9a-fA-F]{32}\.partial\Z")


class WorkspaceConflictError(ValueError):
    """Raised when a workspace cannot be safely bound to the requested input."""


class CliStateError(ValueError):
    """Raised when the disposable CLI cache is malformed."""


class ImportTransactionError(RuntimeError):
    """Raised when a reserved workspace cannot complete its import transaction."""


class InputIdentityChangedError(InputChangedError, ImportTransactionError):
    """Raised when a manifest-bound source entry changes during import."""


class _ResolutionRetry(RuntimeError):
    """Internal signal that cache state changed after the first short snapshot."""


class _Lock(Protocol):
    def acquire(self) -> Any: ...

    def release(self) -> None: ...


def _raise_pending_release_signal(exceptions: Sequence[BaseException]) -> None:
    fatal = next(
        (
            exc
            for exc in exceptions
            if isinstance(exc, (SystemExit, GeneratorExit))
        ),
        None,
    )
    if fatal is not None:
        raise fatal
    interrupted = next(
        (exc for exc in exceptions if isinstance(exc, KeyboardInterrupt)),
        None,
    )
    if interrupted is not None:
        raise interrupted


LockFactory = Callable[..., _Lock]
WorkspaceInitializer = Callable[[Path, str, str], None]
FileCopier = Callable[[Path, Path], None]


class CommandRunner(Protocol):
    """The supervised, non-rendering subprocess boundary used by the CLI."""

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int | float,
        log_path: Path,
        output_encoding: str | None = None,
    ) -> ProcessResult: ...


@dataclass(frozen=True)
class WorkflowSnapshot:
    """One internally consistent view of the existing authoritative workflow."""

    workspace: Path
    marker: dict[str, Any]
    session: SmtSession
    workflow_state: dict[str, Any]
    workflow_tasks: dict[str, Any]
    progress_card: str
    policy: dict[str, Any]


@dataclass
class SmtServices:
    """Injectable process and policy services for workflow advancement."""

    runner: CommandRunner = field(default_factory=ManagedProcess)
    policy_path: Path | None = None
    max_steps: int = 16
    monotonic: Callable[[], float] = time.monotonic
    attempt_logger: Callable[..., None] = append_workflow_agent_event


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
        required = {
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
        unknown = set(payload) - required
        if unknown:
            raise WorkspaceConflictError(
                f"SMT session contains unknown immutable fields: {sorted(unknown)}"
            )
        missing = required - set(payload)
        if missing:
            raise WorkspaceConflictError(
                f"SMT session schema v1 is missing fields: {sorted(missing)}"
            )
        if type(payload["schema_version"]) is not int:
            raise WorkspaceConflictError(
                "SMT session schema_version has an invalid raw type"
            )
        string_fields = required - {"schema_version"}
        invalid_types = sorted(
            field_name
            for field_name in string_fields
            if type(payload[field_name]) is not str
        )
        if invalid_types:
            raise WorkspaceConflictError(
                "SMT session schema v1 fields have invalid raw types: "
                + ", ".join(invalid_types)
            )
        return cls(
            schema_version=payload["schema_version"],
            workspace_id=payload["workspace_id"],
            mod_name=payload["mod_name"],
            game_id=payload["game_id"],
            fingerprint_algorithm=payload["fingerprint_algorithm"],
            input_identity=payload["input_identity"],
            source_kind=payload["source_kind"],
            source_display_name=payload["source_display_name"],
            source_sha256=payload["source_sha256"],
            import_relative_path=payload["import_relative_path"],
            imported_sha256=payload["imported_sha256"],
            created_at=payload["created_at"],
        )


def create_session_no_replace(path: Path, session: SmtSession) -> None:
    """Atomically publish the first session and only validate later attempts."""

    path = _normalized_absolute(path)
    if os.path.lexists(path):
        existing = _read_validated_existing_session(path)
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
            existing = _read_validated_existing_session(path)
            if existing != session:
                raise WorkspaceConflictError(
                    "existing SMT session identity is immutable"
                )
        except OSError as exc:
            if os.path.lexists(path):
                existing = _read_validated_existing_session(path)
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


def _read_validated_existing_session(path: Path) -> SmtSession:
    try:
        validate_regular_path_under(
            path,
            path.parent.parent,
            kind="file",
            label="existing SMT session",
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(
            f"existing SMT session path is unsafe or has multiple hardlinks: {exc}"
        ) from exc
    return SmtSession.from_payload(_read_json_object(path, label="SMT session"))


def _empty_cli_state() -> dict[str, Any]:
    return {
        "schema_version": CLI_STATE_SCHEMA_VERSION,
        "last_workspace": None,
        "input_mappings": {},
        "reservations": {},
    }


def _validate_cli_state_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidate = dict(payload)
    if type(candidate.get("schema_version")) is not int or (
        candidate["schema_version"] != CLI_STATE_SCHEMA_VERSION
    ):
        raise CliStateError("unsupported SMT CLI state schema")
    if set(candidate) != {
        "schema_version",
        "last_workspace",
        "input_mappings",
        "reservations",
    }:
        raise CliStateError("SMT CLI state fields do not match schema v1")
    if candidate["last_workspace"] is not None and not isinstance(
        candidate["last_workspace"], str
    ):
        raise CliStateError("SMT CLI last workspace must be a string or null")
    if not isinstance(candidate["input_mappings"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in candidate["input_mappings"].items()
    ):
        raise CliStateError("SMT CLI mappings must be string pairs")
    if not isinstance(candidate["reservations"], dict):
        raise CliStateError("SMT CLI reservations must be an object")
    reservation_paths: set[str] = set()
    for key, row in candidate["reservations"].items():
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
            raise CliStateError("SMT CLI reservation workspace_id is invalid") from exc
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
        path_key = _workspace_path_key(row["path"])
        if path_key in reservation_paths:
            raise WorkspaceConflictError(
                "ambiguous SMT reservations contain a duplicate normalized workspace path"
            )
        reservation_paths.add(path_key)
    return candidate


@dataclass(frozen=True)
class CliStateLoad:
    state: dict[str, Any] | None
    diagnostic: str | None


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
        return _validate_cli_state_payload(payload)

    def load(self) -> CliStateLoad:
        try:
            return CliStateLoad(state=self.read(), diagnostic=None)
        except (CliStateError, WorkspaceConflictError) as exc:
            return CliStateLoad(state=None, diagnostic=str(exc))

    def write(self, payload: Mapping[str, Any]) -> None:
        candidate = _validate_cli_state_payload(payload)
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
    timeout_seconds: float = 1800.0
    lock_timeout_seconds: float = 5.0
    initializer: WorkspaceInitializer | None = None
    copier: FileCopier = shutil.copyfile
    lock_factory: LockFactory = SmtProcessFileLock


@dataclass(frozen=True)
class ResumeRequest:
    """Address and supervision options for the public ``resume`` command."""

    workspace: Path | None = None
    cwd: Path | None = None
    local_state_root: Path | None = None
    timeout_seconds: float = 1800.0
    lock_timeout_seconds: float = 5.0
    lock_factory: LockFactory = SmtProcessFileLock


@dataclass(frozen=True)
class StatusRequest:
    """Addressing and shared-lock options for a read-only status query."""

    workspace: Path | None = None
    cwd: Path | None = None
    local_state_root: Path | None = None
    lock_timeout_seconds: float = 2.0
    lock_factory: LockFactory = SmtProcessFileLock


@dataclass(frozen=True)
class DoctorRequest:
    """Read-only diagnostic scope; no field grants mutation authority."""

    workspace: Path | None = None
    cwd: Path | None = None
    workspace_root: Path | None = None
    local_state_root: Path | None = None
    lock_timeout_seconds: float = 0.5
    lock_factory: LockFactory = SmtProcessFileLock


@dataclass(frozen=True)
class OutputRequest:
    """Addressing and optional predefined open target for output queries."""

    workspace: Path | None = None
    cwd: Path | None = None
    local_state_root: Path | None = None
    open_target: str | None = None
    lock_timeout_seconds: float = 2.0
    lock_factory: LockFactory = SmtProcessFileLock


def _default_open_directory(path: Path) -> None:
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise ManagedProcessEnvironmentError(
            "opening an SMT output directory requires Windows"
        )
    os.startfile(path)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class ReadOnlyServices:
    """Injectable platform services used only by read-only public commands."""

    documents_provider: Callable[[], Path] = documents_directory
    local_app_data_provider: Callable[[], Path] = local_app_data_directory
    opener: Callable[[Path], None] = _default_open_directory


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
    lock_timeout_seconds: float
    initializer: WorkspaceInitializer | None
    copier: FileCopier
    lock_factory: LockFactory
    existing_session: SmtSession | None = None
    reservation_lock: _Lock | None = field(default=None, repr=False)
    workspace_lock: _Lock | None = field(default=None, repr=False)
    release_errors: list[str] = field(default_factory=list, repr=False)

    def _release_lock(self, attribute: str, label: str) -> BaseException | None:
        lock = getattr(self, attribute)
        setattr(self, attribute, None)
        if lock is None:
            return None
        try:
            lock.release()
        except BaseException as exc:
            self.release_errors.append(
                f"{label} lock release failed: {type(exc).__name__}: {exc}"
            )
            return exc
        return None

    def release_reservation(self) -> None:
        self.owns_reservation = False
        exception = self._release_lock("reservation_lock", "reservation")
        _raise_pending_release_signal((exception,) if exception is not None else ())

    def close(self) -> tuple[str, ...]:
        self.owns_reservation = False
        exceptions = tuple(
            exception
            for exception in (
                self._release_lock("workspace_lock", "workspace operation"),
                self._release_lock("reservation_lock", "reservation"),
            )
            if exception is not None
        )
        _raise_pending_release_signal(exceptions)
        return tuple(self.release_errors)

    def __enter__(self) -> WorkspaceResolution:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            self.close()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            if isinstance(exc, (SystemExit, GeneratorExit)):
                raise exc
            raise


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


def _is_transaction_staging_name(name: str) -> bool:
    return PARTIAL_IMPORT_NAME_RE.fullmatch(name) is not None


def _partial_imports(workspace: Path, source_kind: str) -> tuple[Path, ...]:
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
        partials = tuple(
            sorted(
                (
                    child
                    for child in mod_root.iterdir()
                    if _is_transaction_staging_name(child.name)
                ),
                key=lambda path: path.name.casefold(),
            )
        )
    except OSError as exc:
        raise WorkspaceConflictError(
            f"cannot inspect workspace import transactions: {exc}"
        ) from exc
    expected_kind = "directory" if source_kind == "directory" else "file"
    for partial in partials:
        try:
            validate_regular_path_under(
                partial,
                mod_root,
                kind=expected_kind,
                label="SMT partial import transaction",
            )
        except (OSError, ValueError) as exc:
            raise WorkspaceConflictError(
                f"SMT partial import transaction has an invalid type or path: {exc}"
            ) from exc
    return partials


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
    partials = _partial_imports(workspace, session.source_kind)
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
        entry_stat = child.lstat()
        if _is_transaction_staging_name(child.name):
            expects_directory = session.source_kind == "directory"
            valid_type = (
                stat.S_ISDIR(entry_stat.st_mode)
                if expects_directory
                else stat.S_ISREG(entry_stat.st_mode)
            )
            if (
                valid_type
                and not child.is_symlink()
                and not is_reparse_point(entry_stat)
                and (expects_directory or entry_stat.st_nlink == 1)
            ):
                continue
            extras.append(f"mod/{child.name} (invalid staging)")
            continue
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
    reservation_id: str | None = None,
    expected_reservation_signature: tuple[tuple[object, ...], ...] | None = None,
) -> WorkspaceResolution:
    if reservation_lock is not None:
        if reservation_id is None or session.workspace_id != reservation_id:
            reservation_lock.release()
            raise WorkspaceConflictError(
                "reservation workspace_id does not match the committed SMT session"
            )
    workspace_lock = _lock(
        request.lock_factory,
        workspace / WORKSPACE_LOCK_RELATIVE_PATH,
        request.lock_timeout_seconds,
        command="run",
    )
    try:
        workspace_lock.acquire()
    except BaseException:
        if reservation_lock is not None:
            reservation_lock.release()
        raise
    try:
        validated = validate_session(workspace, identity)
        if validated != session:
            raise WorkspaceConflictError(
                "SMT session changed while acquiring workspace lock"
            )
        with _lock(
            request.lock_factory,
            store.lock_path,
            request.lock_timeout_seconds,
            command="run-state",
        ):
            loaded = store.load()
            if loaded.state is not None:
                state = loaded.state
                if expected_reservation_signature is not None and (
                    _identity_reservation_signature(identity, state)
                    != expected_reservation_signature
                ):
                    raise _ResolutionRetry(
                        "input reservation state changed before mapping recovery"
                    )
                _reconcile_existing_workspace_reservation(
                    state,
                    workspace=workspace,
                    identity=identity,
                    session=session,
                )
                state["input_mappings"][identity] = str(workspace)
                state["last_workspace"] = str(workspace)
                store.write(state)
    except BaseException:
        workspace_lock.release()
        if reservation_lock is not None:
            reservation_lock.release()
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
        lock_timeout_seconds=request.lock_timeout_seconds,
        initializer=request.initializer,
        copier=request.copier,
        lock_factory=request.lock_factory,
        existing_session=session,
        reservation_lock=reservation_lock,
        workspace_lock=workspace_lock,
    )


def _reconcile_existing_workspace_reservation(
    state: dict[str, Any],
    *,
    workspace: Path,
    identity: str,
    session: SmtSession,
) -> None:
    """Fail closed on every reservation related to an existing workspace."""

    workspace_key = _workspace_path_key(workspace)
    related: list[tuple[str, dict[str, Any]]] = []
    for reservation_key, raw_row in state["reservations"].items():
        if not isinstance(reservation_key, str) or not isinstance(raw_row, dict):
            raise WorkspaceConflictError("SMT reservation state is malformed")
        reservation_path = raw_row.get("path")
        same_path = isinstance(reservation_path, str) and (
            _workspace_path_key(reservation_path) == workspace_key
        )
        if same_path or reservation_key == session.workspace_id:
            related.append((reservation_key, raw_row))

    if not related:
        return
    if len(related) != 1:
        raise WorkspaceConflictError(
            "ambiguous reservations are related to the existing workspace or session"
        )
    reservation_key, row = related[0]
    if (
        reservation_key != session.workspace_id
        or row.get("workspace_id") != session.workspace_id
        or row.get("fingerprint_identity") != identity
        or not isinstance(row.get("path"), str)
        or _workspace_path_key(row["path"]) != workspace_key
    ):
        raise WorkspaceConflictError(
            "reservation workspace_id, identity, or path does not match the existing session"
        )
    del state["reservations"][reservation_key]


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


@dataclass(frozen=True)
class _WorkspaceCandidate:
    workspace: Path
    session: SmtSession


def _add_workspace_candidate(
    candidates: dict[str, _WorkspaceCandidate],
    workspace: Path,
    session: SmtSession,
) -> None:
    key = _workspace_path_key(workspace)
    existing = candidates.get(key)
    candidate = _WorkspaceCandidate(workspace=workspace, session=session)
    if existing is not None and existing.session != session:
        raise WorkspaceConflictError(
            f"conflicting SMT sessions resolve to the same workspace path: {workspace}"
        )
    candidates[key] = candidate


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


def _identity_reservation_signature(
    identity: str,
    state: Mapping[str, Any],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                str(row["workspace_id"]),
                _workspace_path_key(str(row["path"])),
                str(row["fingerprint_identity"]),
                int(row["pid"]),
                str(row["created_at"]),
            )
            for row in _reservation_rows(identity, state)
        )
    )


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


def _workspace_path_key(path: Path | str) -> str:
    normalized = _normalized_absolute(Path(path))
    return unicodedata.normalize(
        "NFC",
        os.path.normcase(os.path.normpath(str(normalized))),
    )


def _reservations_for_workspace(
    state: Mapping[str, Any],
    workspace: Path,
) -> tuple[dict[str, Any], ...]:
    key = _workspace_path_key(workspace)
    return tuple(
        dict(row)
        for row in state["reservations"].values()
        if isinstance(row, dict)
        and isinstance(row.get("path"), str)
        and _workspace_path_key(row["path"]) == key
    )


def _reservation_for_workspace(
    state: Mapping[str, Any],
    workspace: Path,
) -> dict[str, Any] | None:
    return next(iter(_reservations_for_workspace(state, workspace)), None)


def _retry_resolution_after_state_change(
    request: RunRequest,
    manifest: InputManifest,
    store: CliStateStore,
    *,
    ignored_reservation_ids: frozenset[str],
    seen_state_fingerprints: frozenset[str],
    deadline: float,
) -> WorkspaceResolution:
    if time.monotonic() >= deadline:
        raise WorkspaceConflictError(
            "workspace reservation state kept changing until the resolution deadline"
        )
    with _lock(
        request.lock_factory,
        store.lock_path,
        request.lock_timeout_seconds,
        command="run-state",
    ):
        loaded = store.load()
    if loaded.state is None:
        raise CliStateError(
            "SMT CLI cache became invalid while resolving a workspace: "
            f"{loaded.diagnostic}"
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            loaded.state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if fingerprint in seen_state_fingerprints:
        raise WorkspaceConflictError(
            "workspace reservation resolution made no progress"
        )
    return resolve_run_workspace(
        request,
        manifest,
        _ignored_reservation_ids=ignored_reservation_ids,
        _seen_state_fingerprints=seen_state_fingerprints | {fingerprint},
        _resolution_deadline=deadline,
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
        request.lock_timeout_seconds,
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
        path_reservation = _reservation_for_workspace(state, workspace)
        if path_reservation is not None:
            if explicit_path is not None:
                raise WorkspaceConflictError(
                    "explicit workspace path is already reserved"
                )
            raise _ResolutionRetry("workspace path became reserved")
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
        lock_timeout_seconds=request.lock_timeout_seconds,
        initializer=request.initializer,
        copier=request.copier,
        lock_factory=request.lock_factory,
        existing_session=None,
        reservation_lock=reservation_lock,
    )


def resolve_run_workspace(
    request: RunRequest,
    manifest: InputManifest,
    *,
    _ignored_reservation_ids: frozenset[str] = frozenset(),
    _seen_state_fingerprints: frozenset[str] = frozenset(),
    _resolution_deadline: float | None = None,
) -> WorkspaceResolution:
    """Resolve or reserve exactly one workspace for a run input identity."""

    if request.tool_setup not in {"auto", "manual", "skip"}:
        raise ValueError("tool_setup must be auto, manual, or skip")
    if _resolution_deadline is None:
        _resolution_deadline = time.monotonic() + max(
            request.lock_timeout_seconds, 0.001
        )
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
        try:
            safe_leaf = safe_file_name(explicit.name)
        except ValueError as exc:
            raise WorkspaceConflictError(
                "explicit workspace leaf name is unsafe"
            ) from exc
        if safe_leaf != explicit.name or _utf16_units(explicit.name) > 80:
            raise WorkspaceConflictError(
                "explicit workspace leaf name must be safe and at most 80 UTF-16 units"
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
            request.lock_timeout_seconds,
            command="run-state",
        ):
            explicit_state = store.read()
            mapped = explicit_state["input_mappings"].get(identity)
            reservation = _next_reservation_row(
                identity,
                explicit_state,
                _ignored_reservation_ids,
            )
            path_reservations = _reservations_for_workspace(
                explicit_state,
                explicit,
            )
        if path_reservations and (
            reservation is None
            or any(
                row["workspace_id"] != reservation["workspace_id"]
                for row in path_reservations
            )
        ):
            raise WorkspaceConflictError(
                "explicit workspace path is reserved for another input identity"
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
                request.lock_timeout_seconds,
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
                    reservation_id=reservation_id,
                )
            existing_lock.release()
            raise WorkspaceConflictError(
                "explicit workspace has an unfinished reservation without a valid session"
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
            return _retry_resolution_after_state_change(
                request,
                manifest,
                store,
                ignored_reservation_ids=_ignored_reservation_ids,
                seen_state_fingerprints=_seen_state_fingerprints,
                deadline=_resolution_deadline,
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
        request.lock_timeout_seconds,
        command="run-state",
    ):
        loaded = store.load()
        snapshot = loaded.state
        mapped = (
            snapshot["input_mappings"].get(identity) if snapshot is not None else None
        )
        reservations = (
            tuple(
                row
                for row in _reservation_rows(identity, snapshot)
                if str(row.get("workspace_id", "")) not in _ignored_reservation_ids
            )
            if snapshot is not None
            else ()
        )
        reservation_signature = (
            _identity_reservation_signature(identity, snapshot)
            if snapshot is not None
            else None
        )

    stale_mapping: str | None = None
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
        stale_mapping = mapped

    candidates: dict[str, _WorkspaceCandidate] = {}
    for reservation in reservations:
        reservation_workspace = _normalized_absolute(
            Path(str(reservation.get("path", "")))
        )
        reservation_id = str(reservation.get("workspace_id", ""))
        if reservation_id:
            existing_lock = _lock(
                request.lock_factory,
                store.reservation_lock_root / f"{reservation_id}.lock",
                request.lock_timeout_seconds,
                command="run-reservation-wait",
            )
            try:
                existing_lock.acquire()
            except SmtLockTimeoutError as exc:
                raise WorkspaceConflictError(
                    "workspace is still being initialized for this input"
                ) from exc
            try:
                session = _valid_matching_session(reservation_workspace, identity)
            finally:
                existing_lock.release()
            if session is not None:
                if session.workspace_id != reservation_id:
                    raise WorkspaceConflictError(
                        "reservation workspace_id does not match the recovered SMT session"
                    )
                _add_workspace_candidate(
                    candidates,
                    reservation_workspace,
                    session,
                )
                continue
            _ignored_reservation_ids = _ignored_reservation_ids | frozenset(
                {reservation_id}
            )

    for workspace, session in _direct_session_matches(root, identity):
        _add_workspace_candidate(candidates, workspace, session)

    recovered = sorted(
        candidates.values(), key=lambda row: _workspace_path_key(row.workspace)
    )
    if len(recovered) > 1:
        raise WorkspaceConflictError(
            "multiple workspaces are valid recovery candidates for the same SMT input "
            "identity: " + ", ".join(str(row.workspace) for row in recovered)
        )
    if recovered:
        candidate = recovered[0]
        try:
            return _acquire_existing_resolution(
                request,
                store,
                candidate.workspace,
                candidate.session,
                finalized,
                identity,
                expected_reservation_signature=reservation_signature,
            )
        except _ResolutionRetry:
            return _retry_resolution_after_state_change(
                request,
                manifest,
                store,
                ignored_reservation_ids=_ignored_reservation_ids,
                seen_state_fingerprints=_seen_state_fingerprints,
                deadline=_resolution_deadline,
            )
    if snapshot is None:
        raise CliStateError(
            "SMT CLI cache is invalid; refusing to create a new workspace reservation: "
            f"{loaded.diagnostic}"
        )
    if stale_mapping is not None:
        with _lock(
            request.lock_factory,
            store.lock_path,
            request.lock_timeout_seconds,
            command="run-state",
        ):
            state = store.read()
            if state["input_mappings"].get(identity) == stale_mapping:
                state["input_mappings"].pop(identity, None)
                store.write(state)
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
        return _retry_resolution_after_state_change(
            request,
            manifest,
            store,
            ignored_reservation_ids=_ignored_reservation_ids,
            seen_state_fingerprints=_seen_state_fingerprints,
            deadline=_resolution_deadline,
        )


def _entry_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        entry_stat = path.lstat()
    except OSError as exc:
        raise InputIdentityChangedError(
            f"source entry disappeared or became unreadable: {path}: {exc}"
        ) from exc
    if path.is_symlink() or is_reparse_point(entry_stat):
        raise InputIdentityChangedError(
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
        raise InputIdentityChangedError(
            f"input manifest entry has no bound identity: {entry.relative_path}"
        )
    expected = (
        entry.identity.device,
        entry.identity.inode,
        entry.identity.size,
        entry.identity.mtime_ns,
    )
    if _entry_identity(target) != expected:
        raise InputIdentityChangedError(
            f"source entry changed before copy: {entry.relative_path}"
        )
    try:
        mode = target.lstat().st_mode
    except OSError as exc:
        raise InputIdentityChangedError(
            f"source entry changed before type verification: {entry.relative_path}"
        ) from exc
    if entry.entry_type == "directory" and not stat.S_ISDIR(mode):
        raise InputIdentityChangedError(
            f"source directory changed type: {entry.relative_path}"
        )
    if entry.entry_type == "file" and not stat.S_ISREG(mode):
        raise InputIdentityChangedError(
            f"source file changed type: {entry.relative_path}"
        )
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
        resolution.lock_timeout_seconds,
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
        resolution.lock_timeout_seconds,
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
        resolution.release_reservation()
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


WORKFLOW_STATE_RELATIVE_PATH = Path("qa") / "workflow_state.json"
WORKFLOW_TASKS_RELATIVE_PATH = Path("qa") / "workflow_tasks.json"
PROGRESS_CARD_RELATIVE_PATH = Path(".workflow") / "progress_card.md"
CLI_LOG_RELATIVE_PATH = Path(".workflow") / "smt-cli.log"
DEFAULT_MAX_SAME_BLOCKER_ATTEMPTS = 2


def _workflow_environment(workspace: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
            "SKYRIM_CHS_PLUGIN_ROOT": str(plugin_root()),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return environment


def _checked_plugin_script(script_name: str) -> Path:
    scripts_root = (plugin_root() / "scripts").resolve(strict=True)
    script = (scripts_root / script_name).resolve(strict=True)
    if not is_under(script, scripts_root) or script.suffix.casefold() != ".py":
        raise WorkspaceConflictError(
            f"workflow script is outside the plugin scripts directory: {script_name}"
        )
    return script


def refresh_authoritative_state(
    workspace: Path,
    runner: CommandRunner,
    timeout_seconds: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] | None = None,
    diagnostics: list[str] | None = None,
) -> list[int]:
    """Run CORE refresh under one deadline and retain bounded step diagnostics."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    workspace = _normalized_absolute(workspace)
    clock = monotonic or time.monotonic
    shared_deadline = (
        clock() + timeout_seconds if deadline is None else deadline
    )
    exit_codes: list[int] = []
    for step in CORE_REFRESH_STEPS:
        remaining = shared_deadline - clock()
        if remaining <= 0:
            exit_codes.append(EXIT_TIMEOUT)
            if diagnostics is not None:
                _append_diagnostic(
                    diagnostics,
                    f"canonical refresh {step.name} exit={EXIT_TIMEOUT}: shared deadline elapsed"
                )
            break
        step_timeout = max(1, min(timeout_seconds, math.ceil(remaining)))
        try:
            result = runner.run(
                [sys.executable, str(_checked_plugin_script(step.script)), *step.args],
                cwd=workspace,
                env=_workflow_environment(workspace),
                timeout_seconds=step_timeout,
                log_path=workspace / CLI_LOG_RELATIVE_PATH,
                output_encoding="utf-8",
            )
        except (ManagedProcessEnvironmentError, OSError, ValueError) as exc:
            exit_codes.append(EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE)
            if diagnostics is not None:
                _append_diagnostic(
                    diagnostics,
                    f"canonical refresh {step.name} exit={EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE}: {exc}"
                )
            break
        result_code = (
            EXIT_TIMEOUT
            if result.timed_out
            else EXIT_INTERRUPTED
            if result.interrupted
            else result.exit_code
        )
        exit_codes.append(result_code)
        if diagnostics is not None:
            _append_diagnostic(
                diagnostics, f"canonical refresh {step.name} exit={result_code}"
            )
            _extend_diagnostics(
                diagnostics,
                (
                    f"canonical refresh {step.name}: {line}"
                    for line in result.output_tail
                ),
            )
        if result_code != 0:
            break
    return exit_codes


def _read_authoritative_json(workspace: Path, relative_path: Path, label: str) -> dict[str, Any]:
    path = workspace / relative_path
    try:
        validate_regular_path_under(
            path,
            workspace,
            kind="file",
            label=label,
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(f"{label} is missing or unsafe: {path}: {exc}") from exc
    return _read_json_object(path, label=label)


def _read_progress_card(workspace: Path) -> str:
    path = workspace / PROGRESS_CARD_RELATIVE_PATH
    try:
        validate_regular_path_under(
            path,
            workspace,
            kind="file",
            label="SMT progress card",
        )
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkspaceConflictError(
            f"SMT progress card is missing or unreadable: {path}: {exc}"
        ) from exc


def read_workflow_snapshot(
    workspace: Path,
    *,
    expected_session: SmtSession | None = None,
    policy_path: Path | None = None,
) -> WorkflowSnapshot:
    """Read only the existing marker/session/state/tasks/card and policy."""

    workspace = _normalized_absolute(workspace)
    marker = _read_authoritative_json(
        workspace, Path(WORKSPACE_MARKER), "workspace marker"
    )
    marker_game = _marker_game(workspace)
    session_payload = _read_authoritative_json(
        workspace, SESSION_RELATIVE_PATH, "SMT session"
    )
    session = SmtSession.from_payload(session_payload)
    if marker_game != session.game_id:
        raise WorkspaceConflictError(
            "workspace marker and SMT session game identities differ"
        )
    if expected_session is not None and session != expected_session:
        raise WorkspaceConflictError(
            "authoritative SMT session changed during workflow advancement"
        )
    workflow_state = _read_authoritative_json(
        workspace, WORKFLOW_STATE_RELATIVE_PATH, "workflow state"
    )
    workflow_tasks = _read_authoritative_json(
        workspace, WORKFLOW_TASKS_RELATIVE_PATH, "workflow tasks"
    )
    selected_policy_path = (
        _normalized_absolute(policy_path)
        if policy_path is not None
        else plugin_root() / "config" / "workflow_policy.json"
    )
    policy = _read_json_object(selected_policy_path, label="workflow policy")
    return WorkflowSnapshot(
        workspace=workspace,
        marker=marker,
        session=session,
        workflow_state=workflow_state,
        workflow_tasks=workflow_tasks,
        progress_card=_read_progress_card(workspace),
        policy=policy,
    )


def _workflow_tasks(snapshot: WorkflowSnapshot) -> list[dict[str, Any]]:
    tasks = _validated_task_rows(snapshot)
    return tasks if tasks is not None else []


def _resource_lock_matches_mod(resource: str, mod_name: str) -> bool:
    if resource in {GLOBAL_RESOURCE, GUI_RESOURCE}:
        return True
    if resource == f"mod:{mod_name}":
        return True
    parts = resource.split(":", 2)
    return bool(
        len(parts) == 3
        and parts[0] in {"file", "resource"}
        and parts[1] == mod_name
        and parts[2].strip()
    )


def _validated_task_rows(
    snapshot: WorkflowSnapshot,
) -> list[dict[str, Any]] | None:
    """Validate the complete task payload before exposing any row to scheduling."""

    raw_tasks = snapshot.workflow_tasks.get("tasks")
    if not isinstance(raw_tasks, list):
        return None
    identifiers = [
        task.get("task_id")
        for task in raw_tasks
        if isinstance(task, dict)
    ]
    if (
        len(identifiers) != len(raw_tasks)
        or any(type(task_id) is not str or not task_id.strip() for task_id in identifiers)
        or len(set(identifiers)) != len(identifiers)
    ):
        return None
    validated: list[dict[str, Any]] = []
    required_fields = {
        "task_id",
        "mod",
        "stage",
        "kind",
        "status",
        "reason",
        "risk",
        "command",
        "executable",
        "can_run_parallel",
        "dependencies",
        "resource_locks",
        "evidence",
    }
    string_fields = (
        "task_id",
        "mod",
        "stage",
        "status",
        "reason",
        "risk",
        "command",
        "kind",
        "evidence",
    )
    for task in raw_tasks:
        if not isinstance(task, dict):
            return None
        dependencies = task.get("dependencies")
        resources = task.get("resource_locks")
        mod_name = str(task.get("mod", ""))
        capability = task.get("required_agent_capability", "")
        handoff = task.get("handoff_target", "")
        if (
            not required_fields.issubset(task)
            or any(type(task.get(field)) is not str for field in string_fields)
            or not mod_name.strip()
            or type(task.get("executable")) is not bool
            or type(task.get("can_run_parallel")) is not bool
            or not isinstance(dependencies, list)
            or any(type(item) is not str or not item.strip() for item in dependencies)
            or not isinstance(resources, list)
            or not resources
            or any(type(item) is not str or not item.strip() for item in resources)
            or any(
                not _resource_lock_matches_mod(item, mod_name)
                for item in resources
            )
            or type(capability) is not str
            or (capability and capability not in KNOWN_AGENT_CAPABILITIES)
            or type(handoff) is not str
            or handoff not in {"", "codex"}
            or (
                "supported" in task and type(task.get("supported")) is not bool
            )
            or any(
                field in task and type(task.get(field)) is not str
                for field in (
                    "error_code",
                    "capability_reason",
                    "claim_owner",
                    "lease_until",
                    "started_at",
                    "finished_at",
                )
            )
            or (
                "exit_code" in task
                and task.get("exit_code") is not None
                and type(task.get("exit_code")) is not int
            )
            or (
                "output_tail" in task
                and (
                    not isinstance(task.get("output_tail"), list)
                    or any(
                        type(item) is not str
                        for item in task.get("output_tail", [])
                    )
                )
            )
        ):
            return None
        validated.append(task)
    return validated


def select_exact_safe_task(
    snapshot: WorkflowSnapshot,
    mod_name: str,
    now: datetime,
) -> dict[str, object] | None:
    """Select one deterministic, pending, exact-Mod low-risk non-GUI task."""

    validated_tasks = _validated_task_rows(snapshot)
    if validated_tasks is None:
        return None
    payload = dict(snapshot.workflow_tasks)
    payload["tasks"] = validated_tasks
    eligible: list[dict[str, Any]] = []
    for task in validated_tasks:
        required_capability = str(task.get("required_agent_capability", ""))
        handoff_target = str(task.get("handoff_target", ""))
        if (
            str(task.get("mod", "")) != mod_name
            or not task_can_be_started(task, now)
            or task.get("executable") is not True
            or str(task.get("risk", "")) != "low"
            or not str(task.get("task_id", "")).strip()
            or not str(task.get("command", "")).strip()
            or bool(required_capability)
            or bool(handoff_target)
            or _task_is_gui(task)
            or not dependencies_satisfied(payload, task)
            or not resources_available(payload, task, now)
        ):
            continue
        eligible.append(task)
    if not eligible:
        return None
    reason_priority = {
        "chs_package_missing": 0,
        "provenance_missing": 1,
        "package_validation_not_clean": 2,
        "strict_gate_not_clean": 3,
        "refresh_translation_readiness_after_any_action": 8,
        "refresh_workflow_state_after_readiness": 9,
    }
    eligible.sort(
        key=lambda task: (
            task.get("can_run_parallel") is not True,
            reason_priority.get(str(task.get("reason", "")), 5),
            str(task.get("task_id", "")),
        )
    )
    return eligible[0]


def _state_rows(snapshot: WorkflowSnapshot) -> list[dict[str, Any]]:
    rows = snapshot.workflow_state.get("states", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _current_state_row(snapshot: WorkflowSnapshot, mod_name: str) -> dict[str, Any] | None:
    matches = [
        row for row in _state_rows(snapshot) if str(row.get("mod", "")) == mod_name
    ]
    return matches[0] if len(matches) == 1 else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _project_or_global_blockers(
    snapshot: WorkflowSnapshot, mod_name: str
) -> list[str]:
    blockers = [
        *_string_list(snapshot.workflow_state.get("blocking_checks")),
        *_string_list(snapshot.workflow_state.get("project_blocking_checks")),
    ]
    for row in _state_rows(snapshot):
        if str(row.get("mod", "")) == mod_name:
            continue
        row_blockers = _string_list(row.get("blocking_checks"))
        blockers.extend(row_blockers)
        row_state = str(row.get("state", ""))
        if row_state in {"blocked", "qa_failed", "needs_input"} and not row_blockers:
            blockers.append(f"mod:{row.get('mod', '')}:{row_state}")
    return sorted(set(blockers))


def _all_blockers(snapshot: WorkflowSnapshot, mod_name: str) -> list[str]:
    current = _current_state_row(snapshot, mod_name)
    return sorted(
        set(
            _project_or_global_blockers(snapshot, mod_name)
            + (_string_list(current.get("blocking_checks")) if current else [])
        )
    )


def _task_is_gui(task: Mapping[str, Any]) -> bool:
    resources = _string_list(task.get("resource_locks"))
    return (
        GUI_RESOURCE in resources
        or str(task.get("required_agent_capability", "")).strip() == GUI_RESOURCE
        or str(task.get("handoff_target", "")).strip().casefold() == "codex"
    )


def _task_text(task: Mapping[str, Any]) -> str:
    return " ".join(
        str(task.get(key, ""))
        for key in ("kind", "reason", "stage", "error_code", "capability_reason")
    ).casefold()


def _current_state_actions(
    snapshot: WorkflowSnapshot, mod_name: str
) -> list[dict[str, Any]]:
    current = _current_state_row(snapshot, mod_name) or {}
    actions = current.get("next_actions", [])
    if not isinstance(actions, list):
        return []
    return [action for action in actions if isinstance(action, dict)]


def _task_is_agent_work(task: Mapping[str, Any]) -> bool:
    text = _task_text(task)
    return any(
        token in text
        for token in (
            "agent_translation",
            "translation",
            "translate",
            "model",
            "semantic",
            "proofread",
            "review",
        )
    )


def _task_is_user_input(task: Mapping[str, Any]) -> bool:
    text = _task_text(task)
    return any(
        token in text
        for token in (
            "needs_input",
            "user_input",
            "choose",
            "selection",
            "terminology",
            "game_identity",
            "extra_mod",
        )
    )


def _blockers_need_user_input(blockers: Sequence[str]) -> bool:
    return any(
        token in blocker.casefold()
        for blocker in blockers
        for token in (
            "input",
            "identity",
            "choice",
            "choose",
            "terminology",
            "extra_mod",
        )
    )


def _recoverable_blocker_codes(policy: Mapping[str, Any]) -> set[str]:
    orchestration = policy.get("agent_orchestration_policy", {})
    if not isinstance(orchestration, dict):
        return set()
    return set(_string_list(orchestration.get("auto_repair_allowed")))


def _must_stop_blocker_codes(policy: Mapping[str, Any]) -> set[str]:
    orchestration = policy.get("agent_orchestration_policy", {})
    if not isinstance(orchestration, dict):
        return set()
    return set(_string_list(orchestration.get("must_stop_or_model_review")))


def _task_blocker_identity(
    snapshot: WorkflowSnapshot,
    mod_name: str,
    task: Mapping[str, Any],
) -> str | None:
    """Bind a task to exactly one current blocker, independent of list order."""

    current = _current_state_row(snapshot, mod_name) or {}
    blockers = set(_string_list(current.get("blocking_checks")))
    if not blockers:
        return ""
    candidates = {
        str(task.get(field, "")).strip()
        for field in ("blocker", "reason", "error_code", "evidence")
        if str(task.get(field, "")).strip()
    }
    matches = blockers & candidates
    if len(matches) != 1:
        return None
    return next(iter(matches))


def classify_outcome(
    snapshot: WorkflowSnapshot,
    mod_name: str,
    selected_task: dict[str, object] | None,
) -> PublicOutcome | None:
    """Project the existing state machine without creating a new state."""

    if _validated_task_rows(snapshot) is None:
        return "blocked"
    current = _current_state_row(snapshot, mod_name)
    if current is None:
        return "needs_user_input"
    project_state = str(snapshot.workflow_state.get("project_state", ""))
    current_state = str(current.get("state", ""))
    current_blockers = _string_list(current.get("blocking_checks"))
    global_blockers = _project_or_global_blockers(snapshot, mod_name)
    blockers = sorted(set([*current_blockers, *global_blockers]))
    if (
        project_state == "manual_tested"
        and current_state == "manual_tested"
        and not blockers
    ):
        return "completed"
    if (
        current_state == "ready_for_manual_test"
        and project_state in {"ready_for_manual_test", "manual_tested"}
        and not current_blockers
        and not global_blockers
    ):
        return "ready_for_manual_test"

    tasks = [
        task for task in _workflow_tasks(snapshot) if str(task.get("mod", "")) == mod_name
    ]
    state_actions = _current_state_actions(snapshot, mod_name)
    failed_tasks = [task for task in tasks if str(task.get("status", "")) == "failed"]
    high_risk_tasks = [
        task
        for task in tasks
        if str(task.get("status", "")) in {"pending", "pending_manual"}
        and str(task.get("risk", "")) not in {"", "low", "manual", "semantic"}
    ]
    high_risk_actions = [
        action
        for action in state_actions
        if str(action.get("risk", "")) not in {"", "low", "manual", "semantic"}
    ]
    unsupported_tasks = [
        task
        for task in tasks
        if task.get("supported") is False
        or "unsupported" in str(task.get("error_code", "")).casefold()
    ]
    recoverable = _recoverable_blocker_codes(snapshot.policy)
    must_stop = _must_stop_blocker_codes(snapshot.policy)
    matched_blocker = (
        _task_blocker_identity(snapshot, mod_name, selected_task)
        if selected_task is not None
        else None
    )
    task_recoverable_blockers: set[str] = set()
    if (
        matched_blocker is not None
        and all(
            blocker in recoverable and blocker not in must_stop
            for blocker in current_blockers
        )
    ):
        task_recoverable_blockers.update(current_blockers)
    blockers_that_preempt_safe_work = [
        *global_blockers,
        *(
            blocker
            for blocker in current_blockers
            if blocker not in task_recoverable_blockers
        ),
    ]
    effective_blockers = (
        blockers_that_preempt_safe_work if selected_task is not None else blockers
    )
    if (
        effective_blockers
        or failed_tasks
        or high_risk_tasks
        or high_risk_actions
        or unsupported_tasks
    ):
        if _blockers_need_user_input(effective_blockers) or any(
            _task_is_user_input(task)
            for task in [*failed_tasks, *high_risk_tasks, *high_risk_actions]
        ):
            return "needs_user_input"
        return "blocked"
    if current_state in {"qa_failed", "blocked"} and selected_task is None:
        return "blocked"
    if selected_task is not None:
        return None
    manual_tasks = [
        task
        for task in tasks
        if str(task.get("status", "")) in {"pending", "pending_manual"}
    ]
    manual_actions = [*manual_tasks, *state_actions]
    if any(_task_is_gui(task) for task in manual_actions):
        return "needs_gui"
    if any(_task_is_agent_work(task) for task in manual_actions) or (
        current_state == "candidates_extracted" and not manual_actions
    ):
        return "needs_agent_translation"
    if current_state == "needs_input" or any(
        _task_is_user_input(task) for task in manual_actions
    ):
        return "needs_user_input"
    return "blocked"


def state_digest(snapshot: WorkflowSnapshot, mod_name: str) -> str:
    """Hash only stable workflow progress evidence, never generic retry_count."""

    current = _current_state_row(snapshot, mod_name) or {}
    tasks = []
    for task in _workflow_tasks(snapshot):
        if str(task.get("mod", "")) != mod_name:
            continue
        status = str(task.get("status", ""))
        if status not in {"pending", "pending_manual", "running", "failed"}:
            continue
        tasks.append(
            {
                "task_id": str(task.get("task_id", "")),
                "status": status,
                "kind": str(task.get("kind", "")),
                "evidence": task.get("evidence", ""),
            }
        )
    tasks.sort(key=lambda item: (item["task_id"], item["status"]))
    next_actions = current.get("next_actions", [])
    action_types = []
    if isinstance(next_actions, list):
        action_types = sorted(
            str(action.get("type", ""))
            for action in next_actions
            if isinstance(action, dict)
        )
    digest_input = {
        "project_state": str(snapshot.workflow_state.get("project_state", "")),
        "current_mod_state": str(current.get("state", "")),
        "blocking_checks": _all_blockers(snapshot, mod_name),
        "tasks": tasks,
        "next_action_types": action_types,
        "evidence": current.get("evidence", {}),
    }
    encoded = json.dumps(
        digest_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task_evidence_key(task: Mapping[str, Any]) -> tuple[str, str]:
    return str(task.get("task_id", "")), str(task.get("evidence", ""))


def _current_blocker_evidence(
    snapshot: WorkflowSnapshot, mod_name: str, task: Mapping[str, Any]
) -> tuple[str, str] | None:
    if _project_or_global_blockers(snapshot, mod_name):
        return None
    blocker = _task_blocker_identity(snapshot, mod_name, task)
    if blocker is None:
        return None
    return blocker, str(task.get("evidence", ""))


def _max_blocker_attempts(policy: Mapping[str, Any]) -> int:
    orchestration = policy.get("agent_orchestration_policy", {})
    if not isinstance(orchestration, dict):
        return DEFAULT_MAX_SAME_BLOCKER_ATTEMPTS
    raw = orchestration.get(
        "max_same_blocker_attempts", DEFAULT_MAX_SAME_BLOCKER_ATTEMPTS
    )
    if type(raw) is not int or raw < 1:
        return DEFAULT_MAX_SAME_BLOCKER_ATTEMPTS
    return raw


def _cross_command_attempt_unchanged(
    snapshot: WorkflowSnapshot,
    mod_name: str,
    task: Mapping[str, Any],
    digest: str,
) -> bool:
    current = _current_state_row(snapshot, mod_name) or {}
    attempt = current.get("last_attempt", {})
    if not isinstance(attempt, dict):
        return False
    required_proof = {
        "command",
        "evidence",
        "status",
        "state_digest",
        "blocker",
    }
    if not required_proof.issubset(attempt):
        return False
    command = str(attempt.get("command", ""))
    evidence = str(attempt.get("evidence", ""))
    status = str(attempt.get("status", "")).casefold()
    if status == "skipped":
        return False
    previous_digest = str(attempt.get("state_digest", ""))
    previous_blocker = str(attempt.get("blocker", ""))
    blocker_identity = _current_blocker_evidence(snapshot, mod_name, task)
    if blocker_identity is None:
        return False
    blocker, task_evidence = blocker_identity
    return bool(
        command
        and command == str(task.get("command", ""))
        and evidence == task_evidence
        and status in {"failed", "blocked"}
        and _is_sha256(previous_digest)
        and previous_digest == digest
        and previous_blocker == blocker
    )


def _next_action_for_outcome(
    snapshot: WorkflowSnapshot,
    mod_name: str,
    outcome: PublicOutcome | None,
) -> NextAction | None:
    if outcome in {None, "completed"}:
        return None
    current_tasks = [
        task
        for task in _workflow_tasks(snapshot)
        if str(task.get("mod", "")) == mod_name
        and str(task.get("status", "")) in {"pending", "pending_manual", "failed"}
    ]
    chosen: dict[str, Any] | None = None
    predicates: dict[str, Callable[[Mapping[str, Any]], bool]] = {
        "needs_gui": _task_is_gui,
        "needs_agent_translation": _task_is_agent_work,
        "needs_user_input": _task_is_user_input,
    }
    predicate = predicates.get(outcome)
    if predicate is not None:
        chosen = next((task for task in current_tasks if predicate(task)), None)
    if chosen is None and current_tasks:
        chosen = sorted(current_tasks, key=lambda task: str(task.get("task_id", "")))[0]
    evidence = str(chosen.get("evidence", "")) if chosen else ""
    summary = str(chosen.get("reason", "")) if chosen else outcome.replace("_", " ")
    return {
        "kind": outcome,
        "summary": summary or outcome.replace("_", " "),
        "artifacts": [evidence] if evidence else [],
    }


def _snapshot_result(
    snapshot: WorkflowSnapshot,
    outcome: PublicOutcome | None,
    *,
    exit_code: int,
    diagnostics: Sequence[str],
    underlying_exit_codes: Sequence[int],
) -> CliResult:
    current = _current_state_row(snapshot, snapshot.session.mod_name) or {}
    return CliResult(
        command="resume",
        outcome=outcome,
        exit_code=exit_code,
        message=(outcome or "no-op").replace("_", " "),
        workspace=str(snapshot.workspace),
        mod_name=snapshot.session.mod_name,
        game_id=snapshot.session.game_id,
        workflow_state=str(current.get("state", "")) or None,
        state_snapshot=True,
        state_generated_at=str(snapshot.workflow_state.get("generated_at", "")) or None,
        state_generated_at_timezone=None,
        refreshed_by_this_command=True,
        next_action=_next_action_for_outcome(
            snapshot, snapshot.session.mod_name, outcome
        ),
        progress_card_path=PROGRESS_CARD_RELATIVE_PATH.as_posix(),
        progress_card=snapshot.progress_card,
        diagnostics=list(diagnostics),
        diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
        underlying_exit_codes=list(underlying_exit_codes),
    )


def _stable_outcome_exit_code(
    snapshot: WorkflowSnapshot, mod_name: str, outcome: PublicOutcome
) -> int:
    if outcome in {"completed", "ready_for_manual_test"}:
        return EXIT_SUCCESS
    if outcome != "blocked":
        return EXIT_SAFE_STOP
    signals = [*_all_blockers(snapshot, mod_name)]
    for task in _workflow_tasks(snapshot):
        if str(task.get("mod", "")) != mod_name:
            continue
        signals.extend(
            str(task.get(key, ""))
            for key in (
                "error_code",
                "capability_reason",
                "reason",
            )
        )
        if task.get("supported") is False:
            signals.append("unsupported")
    normalized = " ".join(signals).casefold()
    if any(
        token in normalized
        for token in ("unsupported", "not_supported", "capability:", "profile:")
    ):
        return EXIT_UNSUPPORTED_INPUT_OR_CAPABILITY
    if any(
        token in normalized
        for token in (
            "tool_unavailable",
            "tool_missing",
            "environment_unavailable",
            "decoder_missing",
            "sdk_missing",
        )
    ):
        return EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
    return EXIT_SAFE_STOP


def _safe_stop(
    snapshot: WorkflowSnapshot,
    diagnostics: Sequence[str],
    underlying_exit_codes: Sequence[int],
) -> CliResult:
    return _snapshot_result(
        snapshot,
        "blocked",
        exit_code=EXIT_SAFE_STOP,
        diagnostics=diagnostics,
        underlying_exit_codes=underlying_exit_codes,
    )


def _blocked_projection_is_only_no_action(
    snapshot: WorkflowSnapshot, mod_name: str
) -> bool:
    current = _current_state_row(snapshot, mod_name) or {}
    if str(current.get("state", "")) in {
        "blocked",
        "qa_failed",
        "needs_input",
        "candidates_extracted",
    }:
        return False
    if _all_blockers(snapshot, mod_name):
        return False
    return not any(
        str(task.get("mod", "")) == mod_name
        and str(task.get("status", "")) in {"pending", "pending_manual", "failed"}
        for task in _workflow_tasks(snapshot)
    )


def advance_workflow(
    workspace: Path,
    session: SmtSession,
    services: SmtServices,
    timeout_seconds: int | float,
) -> CliResult:
    """Refresh, classify, and execute only exact safe tasks until stable."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if services.max_steps < 1:
        raise ValueError("max_steps must be positive")
    workspace = _normalized_absolute(workspace)
    deadline = services.monotonic() + timeout_seconds
    timeout_limit = max(1, math.floor(timeout_seconds))
    diagnostics = _DiagnosticTail()
    underlying: list[int] = []
    attempted_tasks: set[tuple[str, str]] = set()
    blocker_attempts: dict[tuple[str, str], int] = {}
    last_snapshot: WorkflowSnapshot | None = None

    def remaining_seconds() -> int:
        remaining = deadline - services.monotonic()
        if remaining <= 0:
            return 0
        return max(1, min(timeout_limit, math.ceil(remaining)))

    try:
        for _step_index in range(services.max_steps):
            remaining = remaining_seconds()
            if remaining == 0:
                if last_snapshot is None:
                    return CliResult(
                        command="resume",
                        exit_code=EXIT_TIMEOUT,
                        message="workflow advancement timed out",
                        diagnostics=diagnostics,
                        underlying_exit_codes=underlying,
                    )
                return _snapshot_result(
                    last_snapshot,
                    "blocked",
                    exit_code=EXIT_TIMEOUT,
                    diagnostics=[*diagnostics, "workflow advancement timed out"],
                    underlying_exit_codes=underlying,
                )
            refresh_codes = refresh_authoritative_state(
                workspace,
                services.runner,
                remaining,
                deadline=deadline,
                monotonic=services.monotonic,
                diagnostics=diagnostics,
            )
            underlying.extend(refresh_codes)
            if any(code == EXIT_TIMEOUT for code in refresh_codes):
                return CliResult(
                    command="resume",
                    exit_code=EXIT_TIMEOUT,
                    message="canonical refresh timed out",
                    workspace=str(workspace),
                    mod_name=session.mod_name,
                    game_id=session.game_id,
                    diagnostics=diagnostics,
                    diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
                    underlying_exit_codes=underlying,
                )
            if any(code == EXIT_INTERRUPTED for code in refresh_codes):
                return CliResult(
                    command="resume",
                    exit_code=EXIT_INTERRUPTED,
                    message="canonical refresh interrupted",
                    workspace=str(workspace),
                    mod_name=session.mod_name,
                    game_id=session.game_id,
                    diagnostics=diagnostics,
                    diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
                    underlying_exit_codes=underlying,
                )
            if any(
                code == EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
                for code in refresh_codes
            ):
                return CliResult(
                    command="resume",
                    exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                    message="canonical refresh environment is unavailable",
                    workspace=str(workspace),
                    mod_name=session.mod_name,
                    game_id=session.game_id,
                    diagnostics=diagnostics,
                    diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
                    underlying_exit_codes=underlying,
                )
            if any(code != 0 for code in refresh_codes):
                return CliResult(
                    command="resume",
                    exit_code=EXIT_INTERNAL_READ_OR_BUSY,
                    message="canonical refresh failed",
                    workspace=str(workspace),
                    mod_name=session.mod_name,
                    game_id=session.game_id,
                    diagnostics=[
                        *diagnostics,
                        f"canonical refresh exit codes: {refresh_codes}",
                    ],
                    diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
                    underlying_exit_codes=underlying,
                )
            if remaining_seconds() == 0:
                return CliResult(
                    command="resume",
                    exit_code=EXIT_TIMEOUT,
                    message="workflow advancement timed out after canonical refresh",
                    workspace=str(workspace),
                    mod_name=session.mod_name,
                    game_id=session.game_id,
                    diagnostics=[
                        *diagnostics,
                        "total workflow deadline elapsed during canonical refresh",
                    ],
                    diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
                    underlying_exit_codes=underlying,
                )
            snapshot = read_workflow_snapshot(
                workspace,
                expected_session=session,
                policy_path=services.policy_path,
            )
            last_snapshot = snapshot
            selected = select_exact_safe_task(
                snapshot, session.mod_name, datetime.now()
            )
            outcome = classify_outcome(snapshot, session.mod_name, selected)
            if outcome is not None:
                return _snapshot_result(
                    snapshot,
                    outcome,
                    exit_code=_stable_outcome_exit_code(
                        snapshot, session.mod_name, outcome
                    ),
                    diagnostics=diagnostics,
                    underlying_exit_codes=underlying,
                )
            if selected is None:
                return _safe_stop(
                    snapshot,
                    [*diagnostics, "no exact low-risk task is available"],
                    underlying,
                )

            task_key = _task_evidence_key(selected)
            if task_key in attempted_tasks:
                return _safe_stop(
                    snapshot,
                    [
                        *diagnostics,
                        f"task/evidence already attempted: {task_key[0]} {task_key[1]}",
                    ],
                    underlying,
                )
            before_digest = state_digest(snapshot, session.mod_name)
            if _cross_command_attempt_unchanged(
                snapshot, session.mod_name, selected, before_digest
            ):
                return _safe_stop(
                    snapshot,
                    [
                        *diagnostics,
                        f"last_attempt failed/blocked with unchanged state: {task_key[0]}",
                    ],
                    underlying,
                )
            blocker_key = _current_blocker_evidence(
                snapshot, session.mod_name, selected
            )
            if blocker_key is None:
                return _safe_stop(
                    snapshot,
                    [
                        *diagnostics,
                        f"task {task_key[0]} cannot be bound to one current blocker",
                    ],
                    underlying,
                )
            blocker_attempts[blocker_key] = blocker_attempts.get(blocker_key, 0) + 1
            if blocker_attempts[blocker_key] > _max_blocker_attempts(snapshot.policy):
                return _safe_stop(
                    snapshot,
                    [
                        *diagnostics,
                        "same blocker/evidence reached the policy attempt limit: "
                        f"{blocker_key[0]} {blocker_key[1]}",
                    ],
                    underlying,
                )
            attempted_tasks.add(task_key)
            remaining = remaining_seconds()
            if remaining == 0:
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_TIMEOUT,
                    diagnostics=[*diagnostics, "workflow advancement timed out"],
                    underlying_exit_codes=underlying,
                )
            current = _current_state_row(snapshot, session.mod_name) or {}
            try:
                services.attempt_logger(
                    root=workspace,
                    mod_name=session.mod_name,
                    state=str(current.get("state", "")),
                    event="smt_command",
                    action=str(selected.get("command", "")),
                    command=str(selected.get("command", "")),
                    status="started",
                    evidence=str(selected.get("evidence", "")),
                    task_id=str(selected.get("task_id", "")),
                    state_digest=before_digest,
                    blocker=blocker_key[0],
                    details="SMT exact safe task started",
                )
            except (OSError, ValueError, RuntimeError) as exc:
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                    diagnostics=[
                        *diagnostics,
                        f"could not persist SMT task attempt: {exc}",
                    ],
                    underlying_exit_codes=underlying,
                )
            try:
                resume_script = _checked_plugin_script("resume_workflow.py")
                process_result = services.runner.run(
                    [
                        sys.executable,
                        str(resume_script),
                        "--mode",
                        "safe",
                        "--mod-name",
                        session.mod_name,
                        "--task-id",
                        str(selected.get("task_id", "")),
                        "--include-serial",
                        "--timeout-seconds",
                        str(remaining),
                    ],
                    cwd=workspace,
                    env=_workflow_environment(workspace),
                    timeout_seconds=remaining,
                    log_path=workspace / CLI_LOG_RELATIVE_PATH,
                    output_encoding="utf-8",
                )
            except (ManagedProcessEnvironmentError, OSError, ValueError) as exc:
                try:
                    services.attempt_logger(
                        root=workspace,
                        mod_name=session.mod_name,
                        state=str(current.get("state", "")),
                        event="smt_command",
                        action=str(selected.get("command", "")),
                        command=str(selected.get("command", "")),
                        status="failed",
                        evidence=str(selected.get("evidence", "")),
                        task_id=str(selected.get("task_id", "")),
                        state_digest=before_digest,
                        blocker=blocker_key[0],
                        details=f"SMT managed process unavailable: {exc}",
                    )
                except (OSError, ValueError, RuntimeError):
                    pass
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                    diagnostics=[*diagnostics, str(exc)],
                    underlying_exit_codes=underlying,
                )
            underlying.append(process_result.exit_code)
            diagnostics.extend(process_result.output_tail)
            if process_result.timed_out or process_result.exit_code == EXIT_TIMEOUT:
                try:
                    services.attempt_logger(
                        root=workspace,
                        mod_name=session.mod_name,
                        state=str(current.get("state", "")),
                        event="smt_command",
                        action=str(selected.get("command", "")),
                        command=str(selected.get("command", "")),
                        status="failed",
                        evidence=str(selected.get("evidence", "")),
                        task_id=str(selected.get("task_id", "")),
                        state_digest=before_digest,
                        blocker=blocker_key[0],
                        details="SMT exact task timed out",
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    diagnostics.append(f"could not persist timeout attempt: {exc}")
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_TIMEOUT,
                    diagnostics=diagnostics,
                    underlying_exit_codes=underlying,
                )
            if process_result.interrupted or process_result.exit_code == EXIT_INTERRUPTED:
                try:
                    services.attempt_logger(
                        root=workspace,
                        mod_name=session.mod_name,
                        state=str(current.get("state", "")),
                        event="smt_command",
                        action=str(selected.get("command", "")),
                        command=str(selected.get("command", "")),
                        status="failed",
                        evidence=str(selected.get("evidence", "")),
                        task_id=str(selected.get("task_id", "")),
                        state_digest=before_digest,
                        blocker=blocker_key[0],
                        details="SMT exact task interrupted",
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    diagnostics.append(f"could not persist interrupted attempt: {exc}")
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_INTERRUPTED,
                    diagnostics=diagnostics,
                    underlying_exit_codes=underlying,
                )

            def persist_post_refresh_failure(details: str) -> None:
                try:
                    services.attempt_logger(
                        root=workspace,
                        mod_name=session.mod_name,
                        state=str(current.get("state", "")),
                        event="smt_command",
                        action=str(selected.get("command", "")),
                        command=str(selected.get("command", "")),
                        status="blocked",
                        evidence=str(selected.get("evidence", "")),
                        task_id=str(selected.get("task_id", "")),
                        state_digest=before_digest,
                        blocker=blocker_key[0],
                        details=details,
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    diagnostics.append(
                        f"could not persist post-refresh failure attempt: {exc}"
                    )

            remaining = remaining_seconds()
            if remaining == 0:
                persist_post_refresh_failure(
                    "deadline elapsed before post-resume canonical refresh"
                )
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_TIMEOUT,
                    diagnostics=[
                        *diagnostics,
                        "deadline elapsed before post-resume canonical refresh",
                    ],
                    underlying_exit_codes=underlying,
                )
            post_refresh_codes = refresh_authoritative_state(
                workspace,
                services.runner,
                remaining,
                deadline=deadline,
                monotonic=services.monotonic,
                diagnostics=diagnostics,
            )
            underlying.extend(post_refresh_codes)
            if any(code == EXIT_TIMEOUT for code in post_refresh_codes):
                persist_post_refresh_failure(
                    "post-resume canonical refresh timed out"
                )
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_TIMEOUT,
                    diagnostics=[
                        *diagnostics,
                        "post-resume canonical refresh timed out",
                    ],
                    underlying_exit_codes=underlying,
                )
            if any(code == EXIT_INTERRUPTED for code in post_refresh_codes):
                persist_post_refresh_failure(
                    "post-resume canonical refresh interrupted"
                )
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_INTERRUPTED,
                    diagnostics=[
                        *diagnostics,
                        "post-resume canonical refresh interrupted",
                    ],
                    underlying_exit_codes=underlying,
                )
            if any(
                code == EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
                for code in post_refresh_codes
            ):
                persist_post_refresh_failure(
                    "post-resume canonical refresh environment unavailable"
                )
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                    diagnostics=[
                        *diagnostics,
                        "post-resume canonical refresh environment is unavailable",
                    ],
                    underlying_exit_codes=underlying,
                )
            if any(code != 0 for code in post_refresh_codes):
                persist_post_refresh_failure(
                    f"post-resume canonical refresh failed: {post_refresh_codes}"
                )
                return _snapshot_result(
                    snapshot,
                    "blocked",
                    exit_code=EXIT_INTERNAL_READ_OR_BUSY,
                    diagnostics=[
                        *diagnostics,
                        f"post-resume canonical refresh exit codes: {post_refresh_codes}",
                    ],
                    underlying_exit_codes=underlying,
                )

            after = read_workflow_snapshot(
                workspace,
                expected_session=session,
                policy_path=services.policy_path,
            )
            last_snapshot = after
            after_selected = select_exact_safe_task(
                after, session.mod_name, datetime.now()
            )
            after_outcome = classify_outcome(
                after, session.mod_name, after_selected
            )
            after_digest = state_digest(after, session.mod_name)
            after_blocker_identity = _current_blocker_evidence(
                after, session.mod_name, selected
            )
            final_blocker = (
                after_blocker_identity[0]
                if after_blocker_identity is not None
                else blocker_key[0]
            )
            if process_result.exit_code == 2:
                final_status = "skipped"
            elif process_result.exit_code != 0:
                final_status = "failed"
            elif before_digest == after_digest:
                final_status = "blocked"
            else:
                final_status = "passed"
            try:
                services.attempt_logger(
                    root=workspace,
                    mod_name=session.mod_name,
                    state=str(
                        (_current_state_row(after, session.mod_name) or {}).get(
                            "state", ""
                        )
                    ),
                    event="smt_command",
                    action=str(selected.get("command", "")),
                    command=str(selected.get("command", "")),
                    status=final_status,
                    evidence=str(selected.get("evidence", "")),
                    task_id=str(selected.get("task_id", "")),
                    state_digest=after_digest,
                    blocker=final_blocker,
                    details=(
                        "SMT exact task no_task exit=2"
                        if process_result.exit_code == 2
                        else f"SMT exact task exit={process_result.exit_code}"
                    ),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                return _snapshot_result(
                    after,
                    "blocked",
                    exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                    diagnostics=[
                        *diagnostics,
                        f"could not persist completed SMT task attempt: {exc}",
                    ],
                    underlying_exit_codes=underlying,
                )
            if process_result.exit_code == 2 and after_outcome is not None:
                if (
                    after_outcome == "blocked"
                    and _blocked_projection_is_only_no_action(
                        after, session.mod_name
                    )
                ):
                    return _snapshot_result(
                        after,
                        None,
                        exit_code=EXIT_SUCCESS,
                        diagnostics=[
                            *diagnostics,
                            "resume_workflow.py returned internal no-task code 2; public no-op",
                        ],
                        underlying_exit_codes=underlying,
                    )
                return _snapshot_result(
                    after,
                    after_outcome,
                    exit_code=_stable_outcome_exit_code(
                        after, session.mod_name, after_outcome
                    ),
                    diagnostics=diagnostics,
                    underlying_exit_codes=underlying,
                )
            if before_digest == after_digest:
                return _safe_stop(
                    after,
                    [
                        *diagnostics,
                        f"no workflow progress after task {task_key[0]} evidence {task_key[1]}",
                    ],
                    underlying,
                )
            if after_outcome is not None:
                return _snapshot_result(
                    after,
                    after_outcome,
                    exit_code=_stable_outcome_exit_code(
                        after, session.mod_name, after_outcome
                    ),
                    diagnostics=diagnostics,
                    underlying_exit_codes=underlying,
                )

        if last_snapshot is None:
            return CliResult(
                command="resume",
                outcome="blocked",
                exit_code=EXIT_SAFE_STOP,
                message="maximum workflow steps reached",
                diagnostics=[*diagnostics, "maximum workflow steps reached"],
                underlying_exit_codes=underlying,
            )
        return _safe_stop(
            last_snapshot,
            [*diagnostics, "maximum workflow steps reached"],
            underlying,
        )
    except KeyboardInterrupt:
        if last_snapshot is not None:
            return _snapshot_result(
                last_snapshot,
                "blocked",
                exit_code=EXIT_INTERRUPTED,
                diagnostics=[*diagnostics, "workflow advancement interrupted"],
                underlying_exit_codes=underlying,
            )
        return CliResult(
            command="resume",
            exit_code=EXIT_INTERRUPTED,
            message="workflow advancement interrupted",
            workspace=str(workspace),
            mod_name=session.mod_name,
            game_id=session.game_id,
            diagnostics=diagnostics,
            underlying_exit_codes=underlying,
        )
    except WorkspaceConflictError as exc:
        return CliResult(
            command="resume",
            exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
            message="authoritative workspace snapshot is invalid",
            workspace=str(workspace),
            mod_name=session.mod_name,
            game_id=session.game_id,
            diagnostics=[*diagnostics, str(exc)],
            diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
            underlying_exit_codes=underlying,
        )
    except ManagedProcessEnvironmentError as exc:
        return CliResult(
            command="resume",
            exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
            message="managed workflow process is unavailable",
            workspace=str(workspace),
            mod_name=session.mod_name,
            game_id=session.game_id,
            diagnostics=[*diagnostics, str(exc)],
            diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
            underlying_exit_codes=underlying,
        )
    except ValueError as exc:
        return CliResult(
            command="resume",
            exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
            message="workflow process configuration is unavailable",
            workspace=str(workspace),
            mod_name=session.mod_name,
            game_id=session.game_id,
            diagnostics=[*diagnostics, str(exc)],
            diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
            underlying_exit_codes=underlying,
        )
    except OSError as exc:
        return CliResult(
            command="resume",
            exit_code=EXIT_INTERNAL_READ_OR_BUSY,
            message="workflow snapshot could not be read",
            workspace=str(workspace),
            mod_name=session.mod_name,
            game_id=session.game_id,
            diagnostics=[*diagnostics, str(exc)],
            diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
            underlying_exit_codes=underlying,
        )


class _InternalProcessFailure(RuntimeError):
    """Carry one supervised child result across transaction callbacks."""

    def __init__(self, stage: str, result: ProcessResult) -> None:
        self.stage = stage
        self.result = result
        super().__init__(f"{stage} failed with exit code {result.exit_code}")


def _remaining_timeout(deadline: float, monotonic: Callable[[], float]) -> float:
    return max(0.0, deadline - monotonic())


def _process_failure_exit(result: ProcessResult, *, environment: bool) -> int:
    if result.timed_out or result.exit_code == EXIT_TIMEOUT:
        return EXIT_TIMEOUT
    if result.interrupted or result.exit_code == EXIT_INTERRUPTED:
        return EXIT_INTERRUPTED
    if environment:
        return EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
    return EXIT_INTERNAL_READ_OR_BUSY


def _command_failure_result(
    command: str,
    *,
    workspace: Path | None,
    session: SmtSession | None,
    message: str,
    exit_code: int,
    diagnostics: Sequence[str] = (),
    underlying_exit_codes: Sequence[int] = (),
) -> CliResult:
    return CliResult(
        command=command,
        outcome="blocked" if exit_code in {EXIT_SAFE_STOP, EXIT_TIMEOUT} else None,
        exit_code=exit_code,
        message=message,
        workspace=str(workspace) if workspace is not None else None,
        mod_name=session.mod_name if session is not None else None,
        game_id=session.game_id if session is not None else None,
        diagnostics=list(diagnostics),
        diagnostic_log_path=(
            CLI_LOG_RELATIVE_PATH.as_posix() if workspace is not None else None
        ),
        underlying_exit_codes=list(underlying_exit_codes),
    )


def _extra_input_result(
    command: str,
    workspace: Path,
    session: SmtSession,
    extras: Sequence[str],
    *,
    diagnostics: Sequence[str] = (),
    underlying_exit_codes: Sequence[int] = (),
) -> CliResult:
    extra_list = list(extras)
    return CliResult(
        command=command,
        outcome="needs_user_input",
        exit_code=EXIT_SAFE_STOP,
        message="workspace contains unregistered additional Mod input",
        workspace=str(workspace),
        mod_name=session.mod_name,
        game_id=session.game_id,
        next_action={
            "kind": "user_input",
            "summary": "移走额外 Mod 输入，或为它创建独立工作区",
            "artifacts": extra_list,
        },
        details=extra_list,
        diagnostics=list(diagnostics),
        diagnostic_log_path=CLI_LOG_RELATIVE_PATH.as_posix(),
        underlying_exit_codes=list(underlying_exit_codes),
    )


def _nested_scalar_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _nested_scalar_strings(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            yield from _nested_scalar_strings(item)


def _normalized_windows_relative(value: str) -> str:
    candidate = PureWindowsPath(value.strip())
    return unicodedata.normalize("NFC", candidate.as_posix()).casefold()


def _extra_relative_paths(extras: Sequence[str]) -> set[str]:
    paths: set[str] = set()
    for extra in extras:
        relative = extra.split(" (", 1)[0].replace("\\", "/")
        candidate = PureWindowsPath(relative)
        if len(candidate.parts) >= 2 and candidate.parts[0].casefold() == "mod":
            paths.add(_normalized_windows_relative(relative))
    return paths


_STRUCTURED_EXTRA_INPUT_CODES = {
    "extra_input",
    "extra_inputs",
    "extra_mod",
    "extra_mod_input",
    "extra_mod_inputs",
    "multiple_input",
    "multiple_inputs",
    "multiple_mod",
    "multiple_mods",
    "multiple_mod_input",
    "multiple_mod_inputs",
    "unregistered_input",
    "unregistered_inputs",
    "unregistered_mod",
    "unregistered_mod_input",
    "unregistered_mod_inputs",
}


def _structured_extra_input_marker(value: object) -> bool:
    for text in _nested_scalar_strings(value):
        code = re.sub(r"[-\s]+", "_", text.strip().casefold())
        if code in _STRUCTURED_EXTRA_INPUT_CODES:
            return True
    return False


def _named_structured_values(value: object) -> Iterable[object]:
    structured_keys = {
        "blocker",
        "blockers",
        "blocking_checks",
        "capability_reason",
        "error_code",
        "reason",
        "stop_conditions",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in structured_keys:
                yield item
            yield from _named_structured_values(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            yield from _named_structured_values(item)


def extra_inputs_affect_authoritative_state(
    snapshot: WorkflowSnapshot,
    session: SmtSession,
    extras: Sequence[str],
) -> bool:
    """Return true only when authoritative workflow evidence absorbed extras."""

    if not extras:
        return False
    ignored_lanes = {"", "project", "global", "_project", "__project__"}
    current_lane = unicodedata.normalize("NFC", session.mod_name).casefold()
    for row in _state_rows(snapshot):
        lane = str(row.get("mod", "")).strip()
        lane_key = unicodedata.normalize("NFC", lane).casefold()
        if lane_key not in ignored_lanes and lane_key != current_lane:
            return True
    for task in _workflow_tasks(snapshot):
        lane = str(task.get("mod", "")).strip()
        lane_key = unicodedata.normalize("NFC", lane).casefold()
        if lane_key not in ignored_lanes and lane_key != current_lane:
            return True

    payloads: tuple[object, ...] = (
        snapshot.workflow_state,
        snapshot.workflow_tasks,
    )
    if any(
        _structured_extra_input_marker(value)
        for payload in payloads
        for value in _named_structured_values(payload)
    ):
        return True

    extra_paths = _extra_relative_paths(extras)
    if not extra_paths:
        return False
    if any(
        _normalized_windows_relative(text) in extra_paths
        for payload in payloads
        for text in _nested_scalar_strings(payload)
        if text.strip()
    ):
        return True
    for task in _workflow_tasks(snapshot):
        command = task.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        try:
            arguments = split_task_command(command, strict=True)
        except ValueError:
            continue
        if any(
            _normalized_windows_relative(argument) in extra_paths
            for argument in arguments
        ):
            return True
    return False


def _extra_input_diagnostics(extras: Sequence[str]) -> list[str]:
    return [
        "unregistered additional Mod input detected; current session remains "
        "exactly filtered: " + ", ".join(extras),
        "run `python scripts/smt.py doctor` to inspect additional Mod inputs; "
        "move each extra input or create a separate workspace for it",
    ]


def _record_process_result(
    result: ProcessResult,
    diagnostics: list[str],
    underlying_exit_codes: list[int],
) -> None:
    underlying_exit_codes.append(result.exit_code)
    _extend_diagnostics(diagnostics, result.output_tail)


def _run_internal_script(
    services: SmtServices,
    script_name: str,
    arguments: Sequence[str],
    *,
    workspace: Path,
    timeout_seconds: float,
    log_path: Path | None = None,
    environment_workspace: Path | None = None,
) -> ProcessResult:
    if timeout_seconds <= 0:
        return ProcessResult(exit_code=EXIT_TIMEOUT, output_tail=(), timed_out=True)
    script = _checked_plugin_script(script_name)
    try:
        return services.runner.run(
            [sys.executable, str(script), *arguments],
            cwd=workspace,
            env=_workflow_environment(environment_workspace or workspace),
            timeout_seconds=timeout_seconds,
            log_path=log_path or workspace / CLI_LOG_RELATIVE_PATH,
            output_encoding="utf-8",
        )
    except (ManagedProcessEnvironmentError, OSError, ValueError) as exc:
        raise ManagedProcessEnvironmentError(
            f"managed {script_name} process is unavailable: {exc}"
        ) from exc


def _prepare_reused_workspace_tools(
    request: RunRequest,
    resolution: WorkspaceResolution,
    services: SmtServices,
    *,
    timeout_seconds: float,
) -> ProcessResult | None:
    """Apply the requested idempotent tool policy to a reused workspace."""

    if request.tool_setup == "skip":
        return None
    # ``setup_workspace_tools.py --mode manual`` performs detection/reporting
    # only. Its auto mode verifies pinned manifests before preparing only
    # missing, damaged, or mismatched controlled tools.
    return _run_internal_script(
        services,
        "setup_workspace_tools.py",
        ["--mode", request.tool_setup],
        workspace=resolution.workspace,
        timeout_seconds=timeout_seconds,
    )


def _merge_advance_result(
    result: CliResult,
    *,
    command: str,
    diagnostics: Sequence[str],
    underlying_exit_codes: Sequence[int],
) -> CliResult:
    result.command = command
    result.underlying_exit_codes = [
        *underlying_exit_codes,
        *result.underlying_exit_codes,
    ]
    result.diagnostics = _DiagnosticTail([*diagnostics, *result.diagnostics])
    return result


def _apply_lock_release_errors(
    result: CliResult,
    errors: Sequence[str],
) -> CliResult:
    if not errors:
        return result
    _extend_diagnostics(result.diagnostics, errors)
    if result.exit_code == EXIT_SUCCESS:
        result.exit_code = EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
        result.outcome = None
        result.message = "SMT process lock could not be released cleanly"
    return result


def _finish_resolution_result(
    result: CliResult,
    resolution: WorkspaceResolution | None,
) -> CliResult:
    if resolution is None:
        return result
    return _apply_lock_release_errors(result, resolution.close())


def run_command(
    request: RunRequest,
    services: SmtServices | None = None,
) -> CliResult:
    """Safely import one input, prepare its exact queue lane, then advance it."""

    services = services or SmtServices()
    deadline = services.monotonic() + max(0.0, request.timeout_seconds)
    diagnostics: list[str] = _DiagnosticTail()
    underlying: list[int] = []
    resolution: WorkspaceResolution | None = None
    session: SmtSession | None = None
    try:
        manifest = build_input_manifest(_normalized_absolute(request.source))
        resolution = resolve_run_workspace(request, manifest)
        with resolution:
            if resolution.is_new and resolution.initializer is None:

                def initialize_with_managed_runner(
                    workspace: Path,
                    game_id: str,
                    tool_setup: str,
                ) -> None:
                    result = _run_internal_script(
                        services,
                        "init_workspace.py",
                        [
                            str(workspace),
                            "--game",
                            game_id,
                            "--tool-setup",
                            tool_setup,
                        ],
                        workspace=plugin_root(),
                        timeout_seconds=_remaining_timeout(
                            deadline, services.monotonic
                        ),
                        log_path=(
                            resolution.state_store.root
                            / "logs"
                            / f"initialize-{resolution.workspace_id}.log"
                        ),
                        environment_workspace=resolution.workspace,
                    )
                    _record_process_result(result, diagnostics, underlying)
                    if result.exit_code != 0:
                        raise _InternalProcessFailure("workspace initialization", result)

                resolution.initializer = initialize_with_managed_runner

            was_new = resolution.is_new
            if not was_new:
                tool_result = _prepare_reused_workspace_tools(
                    request,
                    resolution,
                    services,
                    timeout_seconds=_remaining_timeout(deadline, services.monotonic),
                )
                if tool_result is not None:
                    _record_process_result(tool_result, diagnostics, underlying)
                    if tool_result.exit_code != 0:
                        return _finish_resolution_result(
                            _command_failure_result(
                                "run",
                                workspace=resolution.workspace,
                                session=resolution.existing_session,
                                message="workspace tool verification or preparation failed",
                                exit_code=_process_failure_exit(
                                    tool_result, environment=True
                                ),
                                diagnostics=diagnostics,
                                underlying_exit_codes=underlying,
                            ),
                            resolution,
                        )
                verify_source_unchanged(
                    _normalized_absolute(request.source),
                    manifest,
                )
            session = import_input_transactionally(
                _normalized_absolute(request.source),
                resolution,
                manifest,
            )
            if resolution.release_errors:
                return _finish_resolution_result(
                    _command_failure_result(
                        "run",
                        workspace=resolution.workspace,
                        session=session,
                        message="reservation lock could not be released after commit",
                        exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                        diagnostics=diagnostics,
                        underlying_exit_codes=underlying,
                    ),
                    resolution,
                )

            extras = detect_extra_mod_inputs(resolution.workspace, session)
            if extras:
                _extend_diagnostics(diagnostics, _extra_input_diagnostics(extras))

            queue_result = _run_internal_script(
                services,
                "run_translation_queue.py",
                ["--mode", "prepare", *exact_queue_arguments(session)],
                workspace=resolution.workspace,
                timeout_seconds=_remaining_timeout(deadline, services.monotonic),
            )
            _record_process_result(queue_result, diagnostics, underlying)
            if queue_result.exit_code != 0:
                return _finish_resolution_result(
                    _command_failure_result(
                        "run",
                        workspace=resolution.workspace,
                        session=session,
                        message="exact queue preparation failed",
                        exit_code=_process_failure_exit(
                            queue_result, environment=False
                        ),
                        diagnostics=diagnostics,
                        underlying_exit_codes=underlying,
                    ),
                    resolution,
                )

            refresh_remaining = _remaining_timeout(deadline, services.monotonic)
            if refresh_remaining <= 0:
                return _finish_resolution_result(
                    _command_failure_result(
                        "run",
                        workspace=resolution.workspace,
                        session=session,
                        message="SMT run timed out before canonical refresh",
                        exit_code=EXIT_TIMEOUT,
                        diagnostics=diagnostics,
                        underlying_exit_codes=underlying,
                    ),
                    resolution,
                )
            refresh_codes = refresh_authoritative_state(
                resolution.workspace,
                services.runner,
                refresh_remaining,
                deadline=deadline,
                monotonic=services.monotonic,
                diagnostics=diagnostics,
            )
            underlying.extend(refresh_codes)
            if any(code != 0 for code in refresh_codes):
                if EXIT_TIMEOUT in refresh_codes:
                    exit_code = EXIT_TIMEOUT
                elif EXIT_INTERRUPTED in refresh_codes:
                    exit_code = EXIT_INTERRUPTED
                elif EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE in refresh_codes:
                    exit_code = EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
                else:
                    exit_code = EXIT_INTERNAL_READ_OR_BUSY
                return _finish_resolution_result(
                    _command_failure_result(
                        "run",
                        workspace=resolution.workspace,
                        session=session,
                        message="canonical workflow refresh failed",
                        exit_code=exit_code,
                        diagnostics=diagnostics,
                        underlying_exit_codes=underlying,
                    ),
                    resolution,
                )

            if extras:
                snapshot = read_workflow_snapshot(
                    resolution.workspace,
                    expected_session=session,
                    policy_path=services.policy_path,
                )
                if extra_inputs_affect_authoritative_state(
                    snapshot, session, extras
                ):
                    return _finish_resolution_result(
                        _extra_input_result(
                            "run",
                            resolution.workspace,
                            session,
                            extras,
                            diagnostics=diagnostics,
                            underlying_exit_codes=underlying,
                        ),
                        resolution,
                    )

            remaining = _remaining_timeout(deadline, services.monotonic)
            if remaining <= 0:
                return _finish_resolution_result(
                    _command_failure_result(
                        "run",
                        workspace=resolution.workspace,
                        session=session,
                        message="SMT run timed out",
                        exit_code=EXIT_TIMEOUT,
                        diagnostics=diagnostics,
                        underlying_exit_codes=underlying,
                    ),
                    resolution,
                )
            return _finish_resolution_result(
                _merge_advance_result(
                    advance_workflow(
                        resolution.workspace,
                        session,
                        services,
                        remaining,
                    ),
                    command="run",
                    diagnostics=diagnostics,
                    underlying_exit_codes=underlying,
                ),
                resolution,
            )
    except (UnsupportedInputError, InputSafetyError) as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="input format or safety policy is unsupported",
                exit_code=EXIT_UNSUPPORTED_INPUT_OR_CAPABILITY,
                diagnostics=[*diagnostics, str(exc)],
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except InputChangedError as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="input changed during verification",
                exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
                diagnostics=[*diagnostics, str(exc)],
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except _InternalProcessFailure as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message=str(exc),
                exit_code=_process_failure_exit(exc.result, environment=True),
                diagnostics=diagnostics,
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except SmtLockTimeoutError as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="workspace is busy or reserved by another SMT command",
                exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
                diagnostics=[*diagnostics, str(exc)],
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except WorkspaceConflictError as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="workspace, marker, or session identity conflicts with run",
                exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
                diagnostics=[*diagnostics, str(exc)],
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except (ManagedProcessEnvironmentError, ImportTransactionError) as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="required tool or managed process is unavailable",
                exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                diagnostics=[*diagnostics, str(exc)],
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except KeyboardInterrupt:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="SMT run was interrupted",
                exit_code=EXIT_INTERRUPTED,
                diagnostics=diagnostics,
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except OSError as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="SMT run environment or workspace I/O is unavailable",
                exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                diagnostics=[*diagnostics, str(exc)],
                underlying_exit_codes=underlying,
            ),
            resolution,
        )
    except (CliStateError, ValueError) as exc:
        return _finish_resolution_result(
            _command_failure_result(
                "run",
                workspace=resolution.workspace if resolution is not None else None,
                session=session,
                message="SMT run could not read or update its controlled workspace",
                exit_code=EXIT_INTERNAL_READ_OR_BUSY,
                diagnostics=[*diagnostics, str(exc)],
                underlying_exit_codes=underlying,
            ),
            resolution,
        )


def _resume_state_store(request: ResumeRequest) -> CliStateStore:
    root = (
        _normalized_absolute(request.local_state_root)
        if request.local_state_root is not None
        else local_app_data_directory() / CLI_STATE_DIRECTORY_NAME
    )
    return CliStateStore(root)


def _resume_under_operation_lock(
    *,
    workspace: Path,
    session: SmtSession,
    services: SmtServices,
    deadline: float,
    diagnostics: list[str],
    underlying: list[int],
) -> CliResult:
    session = validate_session(workspace, session.input_identity)
    extras = detect_extra_mod_inputs(workspace, session)
    if extras:
        _extend_diagnostics(diagnostics, _extra_input_diagnostics(extras))
        refresh_remaining = _remaining_timeout(deadline, services.monotonic)
        if refresh_remaining <= 0:
            return _command_failure_result(
                "resume",
                workspace=workspace,
                session=session,
                message="SMT resume timed out before authoritative refresh",
                exit_code=EXIT_TIMEOUT,
                diagnostics=diagnostics,
                underlying_exit_codes=underlying,
            )
        refresh_codes = refresh_authoritative_state(
            workspace,
            services.runner,
            refresh_remaining,
            deadline=deadline,
            monotonic=services.monotonic,
            diagnostics=diagnostics,
        )
        underlying.extend(refresh_codes)
        if any(code != 0 for code in refresh_codes):
            if EXIT_TIMEOUT in refresh_codes:
                exit_code = EXIT_TIMEOUT
            elif EXIT_INTERRUPTED in refresh_codes:
                exit_code = EXIT_INTERRUPTED
            elif EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE in refresh_codes:
                exit_code = EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
            else:
                exit_code = EXIT_INTERNAL_READ_OR_BUSY
            return _command_failure_result(
                "resume",
                workspace=workspace,
                session=session,
                message="authoritative extra-input refresh failed",
                exit_code=exit_code,
                diagnostics=diagnostics,
                underlying_exit_codes=underlying,
            )
        snapshot = read_workflow_snapshot(
            workspace,
            expected_session=session,
            policy_path=services.policy_path,
        )
        if extra_inputs_affect_authoritative_state(snapshot, session, extras):
            return _extra_input_result(
                "resume",
                workspace,
                session,
                extras,
                diagnostics=diagnostics,
                underlying_exit_codes=underlying,
            )
    remaining = _remaining_timeout(deadline, services.monotonic)
    if remaining <= 0:
        return _command_failure_result(
            "resume",
            workspace=workspace,
            session=session,
            message="SMT resume timed out",
            exit_code=EXIT_TIMEOUT,
            diagnostics=diagnostics,
            underlying_exit_codes=underlying,
        )
    return _merge_advance_result(
        advance_workflow(workspace, session, services, remaining),
        command="resume",
        diagnostics=diagnostics,
        underlying_exit_codes=underlying,
    )


def resume_command(
    request: ResumeRequest,
    services: SmtServices | None = None,
) -> CliResult:
    """Validate one immutable session and enter the shared advancement loop."""

    services = services or SmtServices()
    workspace: Path | None = None
    session: SmtSession | None = None
    operation_lock: _Lock | None = None
    diagnostics: list[str] = _DiagnosticTail()
    underlying: list[int] = []
    release_errors: list[str] = []
    release_exceptions: list[BaseException] = []
    result: CliResult
    deadline = services.monotonic() + max(0.0, request.timeout_seconds)
    try:
        workspace = resolve_command_workspace(
            request.workspace,
            request.cwd,
            _resume_state_store(request),
        )
        session = validate_session(workspace)
        operation_lock = _lock(
            request.lock_factory,
            workspace / WORKSPACE_LOCK_RELATIVE_PATH,
            request.lock_timeout_seconds,
            command="resume",
        )
        operation_lock.acquire()
        result = _resume_under_operation_lock(
            workspace=workspace,
            session=session,
            services=services,
            deadline=deadline,
            diagnostics=diagnostics,
            underlying=underlying,
        )
    except SmtLockTimeoutError as exc:
        result = _command_failure_result(
            "resume",
            workspace=workspace,
            session=session,
            message="workspace is busy with another SMT operation",
            exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
            diagnostics=[*diagnostics, str(exc)],
            underlying_exit_codes=underlying,
        )
    except WorkspaceConflictError as exc:
        result = _command_failure_result(
            "resume",
            workspace=workspace,
            session=session,
            message="workspace, marker, or session identity conflicts with resume",
            exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
            diagnostics=[*diagnostics, str(exc)],
            underlying_exit_codes=underlying,
        )
    except ManagedProcessEnvironmentError as exc:
        result = _command_failure_result(
            "resume",
            workspace=workspace,
            session=session,
            message="required tool or managed process is unavailable",
            exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
            diagnostics=[*diagnostics, str(exc)],
            underlying_exit_codes=underlying,
        )
    except KeyboardInterrupt:
        result = _command_failure_result(
            "resume",
            workspace=workspace,
            session=session,
            message="SMT resume was interrupted",
            exit_code=EXIT_INTERRUPTED,
            diagnostics=diagnostics,
            underlying_exit_codes=underlying,
        )
    except (CliStateError, OSError, ValueError) as exc:
        result = _command_failure_result(
            "resume",
            workspace=workspace,
            session=session,
            message="SMT resume could not read its controlled workspace",
            exit_code=EXIT_INTERNAL_READ_OR_BUSY,
            diagnostics=[*diagnostics, str(exc)],
            underlying_exit_codes=underlying,
        )
    finally:
        if operation_lock is not None:
            try:
                operation_lock.release()
            except BaseException as exc:
                release_errors.append(
                    "workspace operation lock release failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                release_exceptions.append(exc)
    _raise_pending_release_signal(
        tuple(
            exc
            for exc in release_exceptions
            if not isinstance(exc, KeyboardInterrupt)
        )
    )
    if any(isinstance(exc, KeyboardInterrupt) for exc in release_exceptions):
        result.exit_code = EXIT_INTERRUPTED
        result.outcome = None
        result.message = "SMT resume was interrupted while releasing its operation lock"
    return _apply_lock_release_errors(result, release_errors)


def _readonly_state_store(
    local_state_root: Path | None,
    services: ReadOnlyServices,
) -> CliStateStore:
    root = (
        _normalized_absolute(local_state_root)
        if local_state_root is not None
        else services.local_app_data_provider() / CLI_STATE_DIRECTORY_NAME
    )
    return CliStateStore(root)


def _readonly_workspace_candidates(
    explicit_workspace: Path | None,
    cwd: Path | None,
    state_store: CliStateStore,
) -> tuple[Path, ...]:
    """Return precedence candidates; validity is decided only under a lock."""

    if explicit_workspace is not None:
        candidate = _normalized_absolute(explicit_workspace)
        if is_under(candidate, plugin_root()):
            raise WorkspaceConflictError(
                "workspace cannot be inside plugin source"
            )
        return (candidate,)
    candidates: list[Path] = []
    current = find_workspace_root(_normalized_absolute(cwd or Path.cwd()))
    if current is not None and not is_under(current, plugin_root()):
        candidates.append(_normalized_absolute(current))
    state = state_store.read()
    last = state.get("last_workspace")
    if isinstance(last, str):
        last_path = _normalized_absolute(Path(last))
        if not is_under(last_path, plugin_root()):
            candidates.append(last_path)
    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique.setdefault(_workspace_path_key(candidate), candidate)
    if not unique:
        raise WorkspaceConflictError(
            "no selected or recently active SMT workspace"
        )
    return tuple(unique.values())


def _shared_lock(
    factory: LockFactory,
    workspace: Path,
    timeout_seconds: float,
    *,
    command: str,
) -> _Lock:
    lock_path = workspace / WORKSPACE_LOCK_RELATIVE_PATH
    try:
        validate_regular_path_under(
            lock_path,
            workspace,
            kind="file",
            label="SMT shared operation lock",
        )
    except (OSError, ValueError) as exc:
        raise WorkspaceConflictError(
            f"workspace shared operation lock is missing or unsafe: {exc}"
        ) from exc
    return factory(
        lock_path,
        "shared",
        timeout_seconds,
        command=command,
    )


def _release_candidate_lock(lock: _Lock | None) -> None:
    if lock is None:
        return
    try:
        lock.release()
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as exc:
        raise ManagedProcessEnvironmentError(
            "workspace shared lock could not be released while validating "
            f"a candidate: {type(exc).__name__}: {exc}"
        ) from exc


def _acquire_valid_readonly_workspace(
    explicit_workspace: Path | None,
    cwd: Path | None,
    state_store: CliStateStore,
    lock_factory: LockFactory,
    timeout_seconds: float,
    *,
    command: str,
) -> tuple[Path, SmtSession, _Lock]:
    """Choose the first candidate whose complete session validates under lock."""

    candidates = _readonly_workspace_candidates(
        explicit_workspace,
        cwd,
        state_store,
    )
    invalid: list[str] = []
    for candidate in candidates:
        lock: _Lock | None = None
        acquired = False
        try:
            lock = _shared_lock(
                lock_factory,
                candidate,
                timeout_seconds,
                command=command,
            )
            lock.acquire()
            acquired = True
            session = validate_session(candidate)
            return candidate, session, lock
        except SmtLockTimeoutError:
            raise
        except ManagedProcessEnvironmentError:
            if acquired:
                _release_candidate_lock(lock)
            raise
        except (WorkspaceConflictError, OSError, ValueError) as exc:
            if acquired:
                _release_candidate_lock(lock)
            invalid.append(f"{candidate}: {exc}")
            if explicit_workspace is not None:
                break
    raise WorkspaceConflictError(
        "no valid SMT workspace candidate: " + " | ".join(invalid)
    )


def _release_readonly_lock(
    result: CliResult,
    lock: _Lock | None,
) -> CliResult:
    if lock is None:
        return result
    try:
        lock.release()
    except KeyboardInterrupt as exc:
        _append_diagnostic(
            result.diagnostics,
            f"workspace shared lock release failed: {type(exc).__name__}",
        )
        result.exit_code = EXIT_INTERRUPTED
        result.outcome = None
        result.message = (
            f"SMT {result.command} was interrupted while releasing its shared lock"
        )
    except (SystemExit, GeneratorExit):
        raise
    except BaseException as exc:
        result = _apply_lock_release_errors(
            result,
            (
                "workspace shared lock release failed: "
                f"{type(exc).__name__}: {exc}",
            ),
        )
    return result


def _readonly_snapshot_result(
    command: Literal["status", "output"],
    snapshot: WorkflowSnapshot,
) -> CliResult:
    selected = select_exact_safe_task(
        snapshot,
        snapshot.session.mod_name,
        datetime.now(timezone.utc),
    )
    outcome = classify_outcome(snapshot, snapshot.session.mod_name, selected)
    result = _snapshot_result(
        snapshot,
        outcome,
        exit_code=EXIT_SUCCESS,
        diagnostics=(),
        underlying_exit_codes=(),
    )
    result.command = command
    result.exit_code = EXIT_SUCCESS
    result.message = (
        f"SMT {command} read the latest authoritative snapshot"
    )
    result.state_snapshot = True
    result.refreshed_by_this_command = False
    return result


def status_command(
    request: StatusRequest,
    services: ReadOnlyServices | None = None,
) -> CliResult:
    """Read one consistent existing workflow snapshot without refreshing it."""

    services = services or ReadOnlyServices()
    workspace: Path | None = None
    session: SmtSession | None = None
    lock: _Lock | None = None
    try:
        store = _readonly_state_store(request.local_state_root, services)
        if request.workspace is not None:
            workspace = _normalized_absolute(request.workspace)
        workspace, session, lock = _acquire_valid_readonly_workspace(
            request.workspace,
            request.cwd,
            store,
            request.lock_factory,
            request.lock_timeout_seconds,
            command="status",
        )
        try:
            snapshot = read_workflow_snapshot(
                workspace,
                expected_session=session,
            )
        except WorkspaceConflictError as exc:
            return _release_readonly_lock(
                _command_failure_result(
                    "status",
                    workspace=workspace,
                    session=session,
                    message="SMT status could not read the authoritative snapshot",
                    exit_code=EXIT_INTERNAL_READ_OR_BUSY,
                    diagnostics=(str(exc),),
                ),
                lock,
            )
        return _release_readonly_lock(
            _readonly_snapshot_result("status", snapshot),
            lock,
        )
    except SmtLockTimeoutError as exc:
        result = _command_failure_result(
            "status",
            workspace=workspace,
            session=session,
            message="workspace is being updated; retry status later",
            exit_code=EXIT_INTERNAL_READ_OR_BUSY,
            diagnostics=(str(exc),),
        )
        result.busy = True
        return result
    except WorkspaceConflictError as exc:
        return _release_readonly_lock(
            _command_failure_result(
                "status",
                workspace=workspace,
                session=session,
                message="workspace, marker, or session identity is invalid",
                exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
                diagnostics=(str(exc),),
            ),
            lock,
        )
    except ManagedProcessEnvironmentError as exc:
        return _release_readonly_lock(
            _command_failure_result(
                "status",
                workspace=workspace,
                session=session,
                message="Windows shared-lock support is unavailable",
                exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                diagnostics=(str(exc),),
            ),
            lock,
        )
    except KeyboardInterrupt:
        return _release_readonly_lock(
            _command_failure_result(
                "status",
                workspace=workspace,
                session=session,
                message="SMT status was interrupted",
                exit_code=EXIT_INTERRUPTED,
            ),
            lock,
        )
    except (CliStateError, OSError, ValueError) as exc:
        return _release_readonly_lock(
            _command_failure_result(
                "status",
                workspace=workspace,
                session=session,
                message="SMT status could not read its selected workspace",
                exit_code=EXIT_INTERNAL_READ_OR_BUSY,
                diagnostics=(str(exc),),
            ),
            lock,
        )


def _doctor_default_workspace_candidates(
    request: DoctorRequest,
    services: ReadOnlyServices,
) -> tuple[Path, ...]:
    root = (
        _normalized_absolute(request.workspace_root)
        if request.workspace_root is not None
        else services.documents_provider() / DEFAULT_WORKSPACE_DIRECTORY_NAME
    )
    if not root.exists():
        return ()
    validate_regular_path_under(
        root,
        root,
        kind="directory",
        label="default SMT workspace root",
    )
    candidates: list[Path] = []
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda row: row.name.casefold()):
            path = Path(entry.path)
            entry_stat = path.lstat()
            if path.is_symlink() or is_reparse_point(entry_stat):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                candidates.append(path)
    return tuple(candidates)


def _doctor_mapping_candidates(
    cache_state: Mapping[str, Any] | None,
) -> tuple[Path, ...]:
    if cache_state is None:
        return ()
    mappings = cache_state.get("input_mappings", {})
    if not isinstance(mappings, dict):
        return ()
    candidates: dict[str, Path] = {}
    for mapped_value in mappings.values():
        if not isinstance(mapped_value, str):
            continue
        mapped = _normalized_absolute(Path(mapped_value))
        if mapped.is_dir() and not is_under(mapped, plugin_root()):
            candidates.setdefault(_workspace_path_key(mapped), mapped)
    return tuple(candidates.values())


def _doctor_cache_diagnostics(store: CliStateStore) -> tuple[list[str], dict[str, Any] | None]:
    diagnostics: list[str] = []
    loaded = store.load()
    if loaded.diagnostic is not None:
        diagnostics.append(f"CLI cache unreadable: {loaded.diagnostic}")
        return diagnostics, None
    state = loaded.state or _empty_cli_state()
    for identity, workspace_value in sorted(state["input_mappings"].items()):
        mapped = _normalized_absolute(Path(workspace_value))
        if not mapped.is_dir():
            diagnostics.append(
                f"stale input mapping: {identity} -> {mapped}"
            )
    for workspace_id, row in sorted(state["reservations"].items()):
        diagnostics.append(
            "reservation pending: "
            f"{workspace_id} -> {row.get('path', '')}"
        )
    return diagnostics, state


def _doctor_tool_config(workspace: Path, diagnostics: list[str]) -> None:
    path = workspace / "config" / "tools.local.json"
    if not os.path.lexists(path):
        diagnostics.append(f"tool configuration missing: {path}")
        return
    try:
        validate_regular_path_under(
            path,
            workspace,
            kind="file",
            label="SMT tool configuration",
        )
        _read_json_object(path, label="SMT tool configuration")
    except (OSError, ValueError) as exc:
        diagnostics.append(f"tool configuration unreadable: {path}: {exc}")


def _doctor_platform_details(
    services: ReadOnlyServices,
    result: CliResult,
) -> None:
    documents = _normalized_absolute(services.documents_provider())
    local_app_data = _normalized_absolute(services.local_app_data_provider())
    result.details.extend(
        (
            f"Windows Documents Known Folder: {documents}",
            f"Windows Local AppData Known Folder: {local_app_data}",
        )
    )
    manifest_path = plugin_root() / ".codex-plugin" / "plugin.json"
    try:
        manifest = _read_json_object(manifest_path, label="plugin manifest")
        version = manifest.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("plugin manifest version is missing")
        result.details.append(f"Plugin version: {version}")
    except (OSError, ValueError) as exc:
        _append_diagnostic(
            result.diagnostics,
            f"plugin version check failed: {manifest_path}: {exc}",
        )


def _doctor_record_valid_workspace(
    result: CliResult,
    workspace: Path,
    session: SmtSession,
    cache_state: Mapping[str, Any] | None,
    *,
    publish_selection: bool = False,
) -> None:
    if publish_selection:
        result.workspace = str(workspace)
        result.mod_name = session.mod_name
        result.game_id = session.game_id
    result.details.append(f"Workspace candidate: {workspace}")
    result.details.append(
        f"Registered session: {session.mod_name} ({session.game_id})"
    )
    if cache_state is not None:
        for identity, mapped_value in cache_state["input_mappings"].items():
            if _workspace_path_key(mapped_value) != _workspace_path_key(workspace):
                continue
            if identity != session.input_identity:
                _append_diagnostic(
                    result.diagnostics,
                    "input mapping identity does not match session: "
                    f"{identity} -> {workspace}",
                )
    extras = detect_extra_mod_inputs(workspace, session)
    _extend_diagnostics(
        result.diagnostics,
        (f"unregistered Mod input: {row}" for row in extras),
    )
    _doctor_tool_config(workspace, result.diagnostics)


def _doctor_inspect_exact_candidate(
    result: CliResult,
    workspace: Path,
    store: CliStateStore,
    request: DoctorRequest,
    cache_state: Mapping[str, Any] | None,
    *,
    publish_selection: bool = False,
) -> tuple[bool, bool]:
    """Inspect one exact path; return ``(valid, busy)`` without fallback."""

    lock: _Lock | None = None
    result.details.append(f"Workspace candidate: {workspace}")
    try:
        selected, session, lock = _acquire_valid_readonly_workspace(
            workspace,
            None,
            store,
            request.lock_factory,
            request.lock_timeout_seconds,
            command="doctor",
        )
        result.details.pop()
        _doctor_record_valid_workspace(
            result,
            selected,
            session,
            cache_state,
            publish_selection=publish_selection,
        )
        return True, False
    except SmtLockTimeoutError as exc:
        result.busy = True
        _append_diagnostic(
            result.diagnostics,
            f"workspace busy, skipped without reading: {workspace}: {exc}",
        )
        return False, True
    except (WorkspaceConflictError, OSError, ValueError) as exc:
        _append_diagnostic(
            result.diagnostics,
            f"unregistered or invalid workspace: {workspace}: {exc}",
        )
        return False, False
    finally:
        if lock is not None:
            _release_readonly_lock(result, lock)


def doctor_command(
    request: DoctorRequest,
    services: ReadOnlyServices | None = None,
) -> CliResult:
    """Diagnose cache and direct workspaces without installing or repairing."""

    services = services or ReadOnlyServices()
    result = CliResult(
        command="doctor",
        exit_code=EXIT_SUCCESS,
        message="SMT doctor completed read-only diagnostics",
        state_snapshot=True,
        refreshed_by_this_command=False,
        details=[
            f"Python: {sys.version.split()[0]}",
            f"Plugin root: {plugin_root()}",
        ],
    )
    try:
        _doctor_platform_details(services, result)
        store = _readonly_state_store(request.local_state_root, services)
        cache_diagnostics, cache_state = _doctor_cache_diagnostics(store)
        _extend_diagnostics(result.diagnostics, cache_diagnostics)
        inspected: set[str] = set()
        selection_busy = False
        if request.workspace is not None:
            workspace = _normalized_absolute(request.workspace)
            inspected.add(_workspace_path_key(workspace))
            _doctor_inspect_exact_candidate(
                result,
                workspace,
                store,
                request,
                cache_state,
                publish_selection=True,
            )
        else:
            lock: _Lock | None = None
            try:
                workspace, session, lock = _acquire_valid_readonly_workspace(
                    None,
                    request.cwd,
                    store,
                    request.lock_factory,
                    request.lock_timeout_seconds,
                    command="doctor",
                )
                inspected.add(_workspace_path_key(workspace))
                _doctor_record_valid_workspace(
                    result,
                    workspace,
                    session,
                    cache_state,
                    publish_selection=True,
                )
            except SmtLockTimeoutError as exc:
                result.busy = True
                selection_busy = True
                _append_diagnostic(
                    result.diagnostics,
                    "selected workspace candidate is busy; no precedence fallback: "
                    f"{exc}",
                )
            except (WorkspaceConflictError, OSError, ValueError) as exc:
                _append_diagnostic(
                    result.diagnostics,
                    f"cwd/last candidates are not valid workspaces: {exc}",
                )
            finally:
                if lock is not None:
                    result = _release_readonly_lock(result, lock)
            if not inspected and not selection_busy:
                for workspace in _doctor_default_workspace_candidates(
                    request, services
                ):
                    inspected.add(_workspace_path_key(workspace))
                    _doctor_inspect_exact_candidate(
                        result,
                        workspace,
                        store,
                        request,
                        cache_state,
                    )
            if not selection_busy:
                for workspace in _doctor_mapping_candidates(cache_state):
                    key = _workspace_path_key(workspace)
                    if key in inspected:
                        continue
                    inspected.add(key)
                    _doctor_inspect_exact_candidate(
                        result,
                        workspace,
                        store,
                        request,
                        cache_state,
                    )
        return result
    except ManagedProcessEnvironmentError as exc:
        result.exit_code = EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
        result.outcome = None
        result.message = "Windows diagnostic path or lock services are unavailable"
        _append_diagnostic(result.diagnostics, str(exc))
        return result
    except KeyboardInterrupt:
        result.exit_code = EXIT_INTERRUPTED
        result.outcome = None
        result.message = "SMT doctor was interrupted"
        return result
    except (CliStateError, OSError, ValueError) as exc:
        result.exit_code = EXIT_INTERNAL_READ_OR_BUSY
        result.outcome = None
        result.message = "SMT doctor could not inspect its requested scope"
        _append_diagnostic(result.diagnostics, str(exc))
        return result


def _artifact_info(
    path: Path,
    *,
    kind: Literal["directory", "file"],
    workspace: Path,
    validated: bool | None = None,
    validation_evidence: str | None = None,
) -> ArtifactInfo:
    exists = os.path.lexists(path)
    path_valid = False
    if exists:
        try:
            validate_regular_path_under(
                path,
                workspace,
                kind=kind,
                label="SMT output artifact",
            )
            path_valid = True
        except (OSError, ValueError):
            path_valid = False
    return {
        "path": str(path),
        "exists": exists,
        "kind": kind,
        "validated": (
            False if exists and not path_valid else validated
        ),
        "validation_evidence": validation_evidence,
    }


def _output_artifacts(
    workspace: Path,
    session: SmtSession,
    *,
    can_test: bool,
    manual_tested: bool,
) -> dict[str, ArtifactInfo]:
    mod_name = session.mod_name
    final_path = final_mod_dir(workspace, mod_name)
    package_path = packaged_mod_path(workspace, mod_name)
    strict_path = workspace / "qa" / f"{mod_name}.non_gui_qa_gates.md"
    manual_plan = workspace / "qa" / "manual_game_test_plan.md"
    manual_validation = (
        workspace / "qa" / "manual_game_test_results_validation.json"
    )
    provenance = final_path / "meta" / "provenance.jsonl"
    return {
        "root": _artifact_info(
            workspace,
            kind="directory",
            workspace=workspace,
            validated=True,
        ),
        "final_mod": _artifact_info(
            final_path,
            kind="directory",
            workspace=workspace,
            validated=can_test if final_path.exists() else None,
            validation_evidence=str(strict_path),
        ),
        "intermediate": _artifact_info(
            intermediate_output_dir(workspace, mod_name),
            kind="directory",
            workspace=workspace,
        ),
        "package": _artifact_info(
            package_path,
            kind="file",
            workspace=workspace,
            validated=can_test if package_path.exists() else None,
            validation_evidence=str(strict_path),
        ),
        "package_directory": _artifact_info(
            localization_output_root(workspace, mod_name),
            kind="directory",
            workspace=workspace,
        ),
        "strict_qa": _artifact_info(
            strict_path,
            kind="file",
            workspace=workspace,
            validated=can_test if strict_path.exists() else None,
            validation_evidence=str(strict_path),
        ),
        "manual_test_plan": _artifact_info(
            manual_plan,
            kind="file",
            workspace=workspace,
        ),
        "manual_test_validation": _artifact_info(
            manual_validation,
            kind="file",
            workspace=workspace,
            validated=manual_tested if manual_validation.exists() else None,
            validation_evidence=str(manual_validation),
        ),
        "provenance": _artifact_info(
            provenance,
            kind="file",
            workspace=workspace,
            validated=can_test if provenance.exists() else None,
            validation_evidence=str(strict_path),
        ),
    }


def _open_target_path(
    target: str,
    workspace: Path,
    session: SmtSession,
) -> Path:
    targets = {
        "root": workspace,
        "final-mod": final_mod_dir(workspace, session.mod_name),
        "intermediate": intermediate_output_dir(workspace, session.mod_name),
        "package-directory": packaged_mod_path(
            workspace, session.mod_name
        ).parent,
    }
    if target not in targets:
        raise ValueError(
            "output --open target must be root, final-mod, intermediate, or package-directory"
        )
    return targets[target]


def _validated_directory_identity(path: Path, workspace: Path) -> tuple[int, ...]:
    validate_regular_path_under(
        path,
        workspace,
        kind="directory",
        label="SMT output open target",
    )
    entry_stat = path.lstat()
    return (
        int(entry_stat.st_dev),
        int(entry_stat.st_ino),
        int(entry_stat.st_mode),
        int(entry_stat.st_size),
        int(entry_stat.st_mtime_ns),
        int(getattr(entry_stat, "st_file_attributes", 0)),
    )


def output_command(
    request: OutputRequest,
    services: ReadOnlyServices | None = None,
) -> CliResult:
    """Describe current-session artifacts and optionally open one safe directory."""

    services = services or ReadOnlyServices()
    workspace: Path | None = None
    session: SmtSession | None = None
    lock: _Lock | None = None
    open_path: Path | None = None
    open_identity: tuple[int, ...] | None = None
    result: CliResult
    try:
        store = _readonly_state_store(request.local_state_root, services)
        if request.workspace is not None:
            workspace = _normalized_absolute(request.workspace)
        workspace, session, lock = _acquire_valid_readonly_workspace(
            request.workspace,
            request.cwd,
            store,
            request.lock_factory,
            request.lock_timeout_seconds,
            command="output",
        )
        try:
            snapshot = read_workflow_snapshot(
                workspace,
                expected_session=session,
            )
        except WorkspaceConflictError as exc:
            result = _command_failure_result(
                "output",
                workspace=workspace,
                session=session,
                message="SMT output could not read the authoritative snapshot",
                exit_code=EXIT_INTERNAL_READ_OR_BUSY,
                diagnostics=(str(exc),),
            )
        else:
            result = _readonly_snapshot_result("output", snapshot)
            current = _current_state_row(snapshot, session.mod_name) or {}
            current_state = str(current.get("state", ""))
            project_state = str(snapshot.workflow_state.get("project_state", ""))
            can_test = (
                current_state in {"ready_for_manual_test", "manual_tested"}
                and project_state in {"ready_for_manual_test", "manual_tested"}
                and not _all_blockers(snapshot, session.mod_name)
            )
            manual_tested = (
                current_state == "manual_tested"
                and project_state == "manual_tested"
                and not _all_blockers(snapshot, session.mod_name)
            )
            result.output_paths = _output_artifacts(
                workspace,
                session,
                can_test=can_test,
                manual_tested=manual_tested,
            )
            result.details.extend(
                (
                    "可以进入人工游戏测试：" + ("是" if can_test else "否"),
                    "人工游戏测试已验证：" + ("是" if manual_tested else "否"),
                )
            )
            if request.open_target is not None:
                try:
                    open_path = _open_target_path(
                        request.open_target,
                        workspace,
                        session,
                    )
                    open_identity = _validated_directory_identity(
                        open_path,
                        workspace,
                    )
                except (OSError, ValueError) as exc:
                    result.exit_code = EXIT_INTERNAL_READ_OR_BUSY
                    result.outcome = None
                    result.message = "requested output directory cannot be opened safely"
                    _append_diagnostic(result.diagnostics, str(exc))
                    open_path = None
                    open_identity = None
        result = _release_readonly_lock(result, lock)
        lock = None
        if (
            result.exit_code == EXIT_SUCCESS
            and open_path is not None
            and open_identity is not None
        ):
            try:
                if _validated_directory_identity(open_path, workspace) != open_identity:
                    raise ValueError(
                        "output open target changed after shared-lock validation"
                    )
                services.opener(open_path)
            except ManagedProcessEnvironmentError as exc:
                result.exit_code = EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE
                result.outcome = None
                result.message = "Windows directory opening is unavailable"
                _append_diagnostic(result.diagnostics, str(exc))
            except (OSError, ValueError) as exc:
                result.exit_code = EXIT_INTERNAL_READ_OR_BUSY
                result.outcome = None
                result.message = "requested output directory changed or could not be opened"
                _append_diagnostic(result.diagnostics, str(exc))
        return result
    except SmtLockTimeoutError as exc:
        result = _command_failure_result(
            "output",
            workspace=workspace,
            session=session,
            message="workspace is being updated; retry output later",
            exit_code=EXIT_INTERNAL_READ_OR_BUSY,
            diagnostics=(str(exc),),
        )
        result.busy = True
        return result
    except WorkspaceConflictError as exc:
        return _release_readonly_lock(
            _command_failure_result(
                "output",
                workspace=workspace,
                session=session,
                message="workspace, marker, or session identity is invalid",
                exit_code=EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
                diagnostics=(str(exc),),
            ),
            lock,
        )
    except ManagedProcessEnvironmentError as exc:
        return _release_readonly_lock(
            _command_failure_result(
                "output",
                workspace=workspace,
                session=session,
                message="Windows shared-lock support is unavailable",
                exit_code=EXIT_TOOL_OR_ENVIRONMENT_UNAVAILABLE,
                diagnostics=(str(exc),),
            ),
            lock,
        )
    except KeyboardInterrupt:
        return _release_readonly_lock(
            _command_failure_result(
                "output",
                workspace=workspace,
                session=session,
                message="SMT output was interrupted",
                exit_code=EXIT_INTERRUPTED,
            ),
            lock,
        )
    except (CliStateError, OSError, ValueError) as exc:
        return _release_readonly_lock(
            _command_failure_result(
                "output",
                workspace=workspace,
                session=session,
                message="SMT output could not read its selected workspace",
                exit_code=EXIT_INTERNAL_READ_OR_BUSY,
                diagnostics=(str(exc),),
            ),
            lock,
        )

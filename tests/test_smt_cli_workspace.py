from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import multiprocessing
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import smt_fingerprint  # noqa: E402
import smt_cli  # noqa: E402
import smt_windows  # noqa: E402
from game_context import load_game_profile  # noqa: E402
from smt_fingerprint import (  # noqa: E402
    FileIdentity,
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
from smt_windows import (  # noqa: E402
    ManagedProcess,
    ManagedProcessEnvironmentError,
    ManagedProcessTimeoutError,
    SmtLockTimeoutError,
    SmtProcessFileLock,
    get_documents_path,
    get_local_app_data_path,
    start_managed_process,
)
from smt_cli import (  # noqa: E402
    CliStateError,
    CliStateStore,
    RunRequest,
    SmtSession,
    WorkspaceConflictError,
    create_session_no_replace,
    detect_extra_mod_inputs,
    exact_queue_arguments,
    import_input_transactionally,
    resolve_command_workspace,
    resolve_run_workspace,
    validate_session,
)


@pytest.fixture
def safe_tmp_path() -> Path:
    with tempfile.TemporaryDirectory(prefix=".pytest-smt-", dir=ROOT) as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def workspace_tmp_path() -> Path:
    with tempfile.TemporaryDirectory(
        prefix="pytest-smt-workspace-",
        dir=ROOT.parent,
    ) as temp_dir:
        yield Path(temp_dir)


def _write_workspace_marker(workspace: Path, game_id: str = "skyrim-se") -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "game_id": game_id,
            }
        ),
        encoding="utf-8",
    )
    (workspace / ".workflow").mkdir(exist_ok=True)
    (workspace / "mod").mkdir(exist_ok=True)


def _spawn_reservation_worker(
    source_text: str,
    state_root_text: str,
    workspace_root_text: str,
    result_queue: object,
    *,
    overlap_barrier: object | None = None,
    entered_event: object | None = None,
    release_event: object | None = None,
    ready_before_resolve_event: object | None = None,
    reservation_acquire_started_event: object | None = None,
    result_emitted_event: object | None = None,
) -> None:
    """Spawn-safe worker using only the real reservation/session layer and a stub init."""

    source = Path(source_text)
    state_root = Path(state_root_text)
    workspace_root = Path(workspace_root_text)
    initialization: list[float] = []

    class _SignalingLock:
        def __init__(self, lock: SmtProcessFileLock, signal: object) -> None:
            self.lock = lock
            self.signal = signal

        def acquire(self) -> "_SignalingLock":
            if self.lock.timeout_seconds <= 0:
                raise AssertionError("observed reservation acquisition must be blocking")
            probe = SmtProcessFileLock(
                self.lock.path,
                self.lock.mode,
                0,
                command="spawn-contention-probe",
            )
            try:
                probe.acquire()
            except SmtLockTimeoutError:
                pass
            else:
                probe.release()
                raise AssertionError("same-identity reservation was not contended")
            self.signal.set()  # type: ignore[attr-defined]
            self.lock.acquire()
            return self

        def release(self) -> None:
            self.lock.release()

        def __enter__(self) -> "_SignalingLock":
            return self.acquire()

        def __exit__(self, *_args: object) -> None:
            self.release()

    def lock_factory(
        path: Path,
        mode: str,
        timeout_seconds: float,
        *,
        command: str | None = None,
    ) -> object:
        lock = SmtProcessFileLock(path, mode, timeout_seconds, command=command)  # type: ignore[arg-type]
        if (
            reservation_acquire_started_event is not None
            and path.name not in {"cli-state.lock", "smt-operation.lock"}
        ):
            return _SignalingLock(lock, reservation_acquire_started_event)
        return lock

    def emit(payload: dict[str, object]) -> None:
        if result_emitted_event is not None:
            result_emitted_event.set()  # type: ignore[attr-defined]
        result_queue.put(payload)  # type: ignore[attr-defined]

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        if tool_setup != "skip":
            raise AssertionError("spawn integration must not prepare external tools")
        initialization.append(time.monotonic())
        if entered_event is not None:
            entered_event.set()  # type: ignore[attr-defined]
        if overlap_barrier is not None:
            overlap_barrier.wait(timeout=10)  # type: ignore[attr-defined]
            time.sleep(0.15)
        if release_event is not None and not release_event.wait(timeout=10):  # type: ignore[attr-defined]
            raise TimeoutError("parent did not release stub initializer")
        _write_workspace_marker(workspace, game_id)
        initialization.append(time.monotonic())

    resolution = None
    try:
        manifest = build_input_manifest(source)
        if ready_before_resolve_event is not None:
            ready_before_resolve_event.set()  # type: ignore[attr-defined]
        resolution = resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                tool_setup="skip",
                cwd=source.parent,
                local_state_root=state_root,
                workspace_root=workspace_root,
                initializer=initializer,
                lock_factory=lock_factory,  # type: ignore[arg-type]
                lock_timeout_seconds=10,
                timeout_seconds=20,
            ),
            manifest,
        )
        created = resolution.is_new
        session = import_input_transactionally(source, resolution, manifest)
        emit(
            {
                "status": "committed",
                "created": created,
                "workspace": str(resolution.workspace),
                "workspace_id": session.workspace_id,
                "identity": session.input_identity,
                "init_start": initialization[0] if initialization else None,
                "init_end": initialization[1] if len(initialization) > 1 else None,
            }
        )
    except SmtLockTimeoutError as exc:
        emit(
            {
                "status": "timeout",
                "exit_code": smt_cli.EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT,
                "error": str(exc),
            }
        )
    except BaseException as exc:
        emit(
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        raise
    finally:
        if resolution is not None:
            resolution.close()


def _session_for(
    workspace: Path,
    manifest: InputManifest,
    *,
    mod_name: str = "Example",
    game_id: str = "skyrim-se",
    workspace_id: str = "11111111-1111-4111-8111-111111111111",
) -> SmtSession:
    import_name = (
        mod_name
        if manifest.source_kind == "directory"
        else f"{mod_name}.{manifest.source_kind}"
    )
    return SmtSession(
        schema_version=1,
        workspace_id=workspace_id,
        mod_name=mod_name,
        game_id=game_id,
        fingerprint_algorithm="smt-input-v1",
        input_identity=composite_input_identity(game_id, manifest),
        source_kind=manifest.source_kind,
        source_display_name=import_name,
        source_sha256=manifest.digest,
        import_relative_path=f"mod/{import_name}",
        imported_sha256=manifest.digest,
        created_at="2026-07-22T00:00:00+00:00",
    )


class _RecordingLock:
    def __init__(
        self, factory: "_RecordingLockFactory", name: str, timeout: float
    ) -> None:
        self.factory = factory
        self.name = name
        self.timeout = timeout
        self.acquired = False

    def acquire(self) -> "_RecordingLock":
        assert not (
            self.name != "global" and self.timeout > 0 and "global" in self.factory.held
        ), "blocking lower-level lock acquired while global lock is held"
        self.factory.events.append(
            ("acquire", self.name, self.timeout, tuple(self.factory.held))
        )
        self.factory.held.append(self.name)
        self.acquired = True
        return self

    def release(self) -> None:
        if not self.acquired:
            return
        self.factory.events.append(
            ("release", self.name, self.timeout, tuple(self.factory.held))
        )
        self.factory.held.remove(self.name)
        self.acquired = False

    def __enter__(self) -> "_RecordingLock":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


class _RecordingLockFactory:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, float, tuple[str, ...]]] = []
        self.held: list[str] = []

    def __call__(
        self,
        path: Path,
        mode: str,
        timeout_seconds: float,
        *,
        command: str | None = None,
    ) -> _RecordingLock:
        del mode, command
        if path.name == "cli-state.lock":
            name = "global"
        elif path.name == "smt-operation.lock":
            name = "workspace"
        else:
            name = "reservation"
        return _RecordingLock(self, name, timeout_seconds)


class _ThreadLock:
    def __init__(
        self,
        factory: "_ThreadLockFactory",
        path: Path,
        timeout: float,
    ) -> None:
        self.factory = factory
        self.path = path
        self.timeout = timeout
        self.underlying = factory.lock_for(path)
        self.acquired = False

    def acquire(self) -> "_ThreadLock":
        held = self.factory.held_by_thread.setdefault(threading.get_ident(), set())
        assert not (
            self.path.name != "cli-state.lock"
            and self.timeout > 0
            and "cli-state.lock" in held
        )
        self.factory.events.append(
            (threading.get_ident(), "acquire-start", self.path.name, tuple(held))
        )
        acquired = self.underlying.acquire(timeout=self.timeout)
        if not acquired:
            raise SmtLockTimeoutError(f"timed out: {self.path}")
        held.add(self.path.name)
        self.acquired = True
        self.factory.events.append(
            (threading.get_ident(), "acquired", self.path.name, tuple(held))
        )
        return self

    def release(self) -> None:
        if not self.acquired:
            return
        self.factory.held_by_thread[threading.get_ident()].remove(self.path.name)
        self.underlying.release()
        self.acquired = False

    def __enter__(self) -> "_ThreadLock":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


class _ThreadLockFactory:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}
        self.held_by_thread: dict[int, set[str]] = {}
        self.events: list[tuple[int, str, str, tuple[str, ...]]] = []

    def lock_for(self, path: Path) -> threading.Lock:
        key = (
            smt_windows.windows_path_key(path)
            if os.name == "nt"
            else os.path.normcase(os.path.abspath(path))
        )
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def __call__(
        self,
        path: Path,
        mode: str,
        timeout_seconds: float,
        *,
        command: str | None = None,
    ) -> _ThreadLock:
        del mode, command
        return _ThreadLock(self, path, timeout_seconds)


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace aliases are platform-specific")
@pytest.mark.parametrize("namespace", ["\\\\?\\", "\\\\.\\"])
def test_thread_lock_factory_uses_one_key_for_windows_namespace_alias(
    safe_tmp_path: Path,
    namespace: str,
) -> None:
    factory = _ThreadLockFactory()
    lock_path = safe_tmp_path / "state" / "cli-state.lock"
    namespaced_path = Path(namespace + str(lock_path))

    before = factory.lock_for(lock_path)
    after = factory.lock_for(namespaced_path)

    assert after is before
    assert factory.lock_for(lock_path.with_name("other.lock")) is not before


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace aliases are platform-specific")
@pytest.mark.parametrize("namespace", ["\\\\?\\", "\\\\.\\"])
def test_workspace_path_key_uses_one_key_for_windows_namespace_alias(
    safe_tmp_path: Path,
    namespace: str,
) -> None:
    workspace = safe_tmp_path / "Workspace"
    namespaced = Path(namespace + str(workspace))

    assert smt_cli._workspace_path_key(workspace) == smt_cli._workspace_path_key(
        namespaced
    )
    assert smt_windows.windows_path_key(workspace) == smt_windows.windows_path_key(
        namespaced
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace aliases are platform-specific")
@pytest.mark.parametrize(
    "namespaced",
    [
        r"\\?\UNC\server\share\Workspace",
        r"\\.\UNC\server\share\Workspace",
    ],
)
def test_windows_path_key_uses_one_key_for_unc_namespace_alias(
    namespaced: str,
) -> None:
    ordinary = r"\\server\share\Workspace"

    assert smt_windows.windows_path_key(namespaced) == smt_windows.windows_path_key(
        ordinary
    )
    assert smt_cli._workspace_path_key(namespaced) == smt_cli._workspace_path_key(
        ordinary
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace aliases are platform-specific")
@pytest.mark.parametrize(
    "unsafe",
    [
        r"\\.\PhysicalDrive0",
        r"\\?\GLOBALROOT\Device\HarddiskVolume1",
        r"\\?\Volume{00000000-0000-0000-0000-000000000000}\Workspace",
    ],
)
def test_windows_path_key_rejects_unmappable_device_namespace(unsafe: str) -> None:
    with pytest.raises(ManagedProcessEnvironmentError, match="device namespace"):
        smt_windows.windows_path_key(unsafe)
    with pytest.raises(ManagedProcessEnvironmentError, match="device namespace"):
        smt_cli._workspace_path_key(unsafe)


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace aliases are platform-specific")
def test_windows_path_key_keeps_distinct_paths_distinct() -> None:
    assert smt_windows.windows_path_key(r"D:\Workspace\One") != (
        smt_windows.windows_path_key(r"D:\Workspace\Two")
    )
    assert smt_windows.windows_path_key(r"\\server\share\One") != (
        smt_windows.windows_path_key(r"\\server\share\Two")
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace aliases are platform-specific")
@pytest.mark.parametrize("namespace", ["\\\\?\\", "\\\\.\\"])
def test_namespace_alias_cannot_reserve_one_physical_workspace_twice(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    namespace: str,
) -> None:
    first_source = safe_tmp_path / "First.zip"
    second_source = safe_tmp_path / "Second.zip"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    physical_workspace = workspace_tmp_path / "SharedWorkspace"
    namespaced_workspace = Path(namespace + str(physical_workspace))
    state_root = safe_tmp_path / "state"
    factory = _RecordingLockFactory()

    first = resolve_run_workspace(
        RunRequest(
            source=first_source,
            game_id="skyrim-se",
            workspace=physical_workspace,
            local_state_root=state_root,
            tool_setup="skip",
            lock_factory=factory,
        ),
        build_input_manifest(first_source),
    )
    try:
        with pytest.raises(WorkspaceConflictError, match="reserved|workspace"):
            resolve_run_workspace(
                RunRequest(
                    source=second_source,
                    game_id="skyrim-se",
                    workspace=namespaced_workspace,
                    local_state_root=state_root,
                    tool_setup="skip",
                    lock_factory=factory,
                ),
                build_input_manifest(second_source),
            )
        state = CliStateStore(state_root).read()
        assert len(state["reservations"]) == 1
    finally:
        first.close()


def test_cli_state_cache_is_atomic_schema_v1_and_non_authoritative(
    safe_tmp_path: Path,
) -> None:
    state_root = safe_tmp_path / "LocalState"
    store = CliStateStore(state_root)

    empty = store.read()
    assert empty == {
        "schema_version": 1,
        "last_workspace": None,
        "input_mappings": {},
        "reservations": {},
    }

    payload = dict(empty)
    payload["last_workspace"] = str(safe_tmp_path / "missing")
    payload["input_mappings"] = {
        "smt-input-v1:skyrim-se:zip:" + "0" * 64: str(safe_tmp_path / "missing")
    }
    store.write(payload)

    assert store.path == state_root / "cli-state.json"
    assert store.read() == payload
    assert not list(state_root.glob("*.tmp"))


def test_invalid_cli_cache_does_not_block_default_root_session_reuse(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    state_root = safe_tmp_path / "state"
    store = CliStateStore(state_root)
    store.path.parent.mkdir(parents=True)
    invalid_cache = b'{"schema_version": 1, "input_mappings": {}}\n'
    store.path.write_bytes(invalid_cache)
    workspace_root = workspace_tmp_path / "workspaces"
    workspace = workspace_root / "Example"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=safe_tmp_path,
            local_state_root=state_root,
            workspace_root=workspace_root,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        assert not resolution.is_new
        assert resolution.workspace == workspace
        assert resolution.workspace_id == session.workspace_id
        assert store.path.read_bytes() == invalid_cache
    finally:
        resolution.close()


def test_invalid_cli_cache_without_session_cannot_create_or_overwrite_state(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    state_root = safe_tmp_path / "state"
    store = CliStateStore(state_root)
    store.path.parent.mkdir(parents=True)
    invalid_cache = b'{"schema_version": 1, "reservations": []}\n'
    store.path.write_bytes(invalid_cache)
    workspace_root = workspace_tmp_path / "workspaces"

    with pytest.raises(
        (CliStateError, WorkspaceConflictError), match="cache|state|schema"
    ):
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                cwd=safe_tmp_path,
                local_state_root=state_root,
                workspace_root=workspace_root,
                lock_factory=_RecordingLockFactory(),
            ),
            manifest,
        )

    assert store.path.read_bytes() == invalid_cache
    assert not workspace_root.exists()


def test_cli_state_write_rejects_every_payload_read_would_reject(
    safe_tmp_path: Path,
) -> None:
    store = CliStateStore(safe_tmp_path / "state")
    invalid = {
        "schema_version": 1,
        "last_workspace": None,
        "input_mappings": {},
    }

    with pytest.raises((CliStateError, WorkspaceConflictError), match="state|schema"):
        store.write(invalid)

    assert not store.path.exists()


def test_session_is_created_no_replace_and_second_run_only_validates(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / "Workspace"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    path = workspace / ".workflow" / "smt-session.json"

    create_session_no_replace(path, session)
    original = path.read_bytes()
    create_session_no_replace(path, session)

    assert path.read_bytes() == original
    assert validate_session(workspace, session.input_identity) == session
    changed = _session_for(
        workspace,
        manifest,
        mod_name="Changed",
        workspace_id="22222222-2222-4222-8222-222222222222",
    )
    with pytest.raises(WorkspaceConflictError):
        create_session_no_replace(path, changed)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "missing_field",
    [
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
    ],
)
def test_session_schema_v1_requires_every_field_without_migration(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    missing_field: str,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    payload = _session_for(
        workspace_tmp_path / "Workspace",
        build_input_manifest(source),
    ).to_payload()
    del payload[missing_field]

    with pytest.raises(WorkspaceConflictError, match="schema|field|payload"):
        SmtSession.from_payload(payload)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("schema_version", True),
        ("workspace_id", 1),
        ("mod_name", ["Example"]),
        ("game_id", 1),
        ("fingerprint_algorithm", 1),
        ("input_identity", 1),
        ("source_kind", 1),
        ("source_display_name", 1),
        ("source_sha256", 1),
        ("import_relative_path", 1),
        ("imported_sha256", 1),
        ("created_at", 1),
    ],
)
def test_session_schema_v1_rejects_raw_type_coercion(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    field_name: str,
    replacement: object,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    payload = _session_for(
        workspace_tmp_path / "Workspace",
        build_input_manifest(source),
    ).to_payload()
    payload[field_name] = replacement

    with pytest.raises(WorkspaceConflictError, match="schema|type|payload"):
        SmtSession.from_payload(payload)


def test_session_schema_v1_rejects_unknown_fields(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    payload = _session_for(
        workspace_tmp_path / "Workspace",
        build_input_manifest(source),
    ).to_payload()
    payload["migration_hint"] = "legacy"

    with pytest.raises(WorkspaceConflictError, match="unknown|schema|field"):
        SmtSession.from_payload(payload)


def test_existing_hardlinked_session_is_rejected_before_json_comparison(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / "Workspace-hardlinked-session"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    path = workspace / ".workflow" / "smt-session.json"
    create_session_no_replace(path, session)
    alias = workspace / ".workflow" / "session-alias.json"
    os.link(path, alias)

    try:
        with pytest.raises(WorkspaceConflictError, match="hardlink|multiple"):
            create_session_no_replace(path, session)
    finally:
        alias.unlink()

    assert path.read_bytes()


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("workspace_id", "22222222-2222-4222-8222-222222222222"),
        ("mod_name", "Changed"),
        ("game_id", "fallout4"),
        ("fingerprint_algorithm", "smt-input-v2"),
        ("import_relative_path", "mod/Other.zip"),
    ],
)
def test_session_identity_field_tampering_is_rejected_without_migration(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    field_name: str,
    replacement: str,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / f"Workspace-{field_name}"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    path = workspace / ".workflow" / "smt-session.json"
    create_session_no_replace(path, session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field_name] = replacement
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WorkspaceConflictError):
        create_session_no_replace(path, session)

    assert json.loads(path.read_text(encoding="utf-8"))[field_name] == replacement


def test_validate_session_rejects_marker_import_and_transaction_conflicts(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / "Workspace"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)

    partial = workspace / "mod" / ".smt-import-11111111111141118111111111111111.partial"
    partial.write_bytes(b"partial")
    with pytest.raises(WorkspaceConflictError, match="partial"):
        validate_session(workspace, session.input_identity)
    partial.unlink()

    (workspace / "mod" / "Example.zip").write_bytes(b"changed")
    with pytest.raises(WorkspaceConflictError, match="digest|copy"):
        validate_session(workspace, session.input_identity)


def test_explicit_non_workspace_and_identity_mismatch_are_conflicts(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    state_root = safe_tmp_path / "state"
    non_workspace = workspace_tmp_path / "occupied"
    non_workspace.mkdir()
    (non_workspace / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceConflictError):
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                workspace=non_workspace,
                local_state_root=state_root,
                workspace_root=workspace_tmp_path / "workspaces",
            ),
            manifest,
        )

    assert (non_workspace / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_two_input_identities_cannot_reserve_the_same_explicit_workspace(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    first_source = safe_tmp_path / "First.zip"
    second_source = safe_tmp_path / "Second.zip"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    first_manifest = build_input_manifest(first_source)
    second_manifest = build_input_manifest(second_source)
    explicit = workspace_tmp_path / "Explicit"
    locks = _RecordingLockFactory()
    first = resolve_run_workspace(
        RunRequest(
            source=first_source,
            game_id="skyrim-se",
            workspace=explicit,
            local_state_root=safe_tmp_path / "state",
            workspace_root=workspace_tmp_path / "workspaces",
            lock_factory=locks,
        ),
        first_manifest,
    )
    try:
        with pytest.raises(
            WorkspaceConflictError, match="reservation|reserved|workspace"
        ):
            resolve_run_workspace(
                RunRequest(
                    source=second_source,
                    game_id="skyrim-se",
                    workspace=explicit,
                    local_state_root=safe_tmp_path / "state",
                    workspace_root=workspace_tmp_path / "workspaces",
                    lock_factory=locks,
                ),
                second_manifest,
            )
        state = CliStateStore(safe_tmp_path / "state").read()
        assert len(state["reservations"]) == 1
    finally:
        first.close()


def test_explicit_workspace_leaf_must_be_safe_and_at_most_80_utf16_units(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    too_long = workspace_tmp_path / ("A" * 81)

    with pytest.raises(WorkspaceConflictError, match="80|name"):
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                workspace=too_long,
                local_state_root=safe_tmp_path / "state",
                workspace_root=workspace_tmp_path / "workspaces",
                lock_factory=_RecordingLockFactory(),
            ),
            manifest,
        )

    assert not too_long.exists()


def test_run_ignores_mismatching_cwd_workspace_and_reserves_new_path(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    first_source = safe_tmp_path / "First.zip"
    first_source.write_bytes(b"first")
    first_manifest = build_input_manifest(first_source)
    old_workspace = workspace_tmp_path / "Old"
    _write_workspace_marker(old_workspace)
    shutil.copy2(first_source, old_workspace / "mod" / "First.zip")
    create_session_no_replace(
        old_workspace / ".workflow" / "smt-session.json",
        _session_for(old_workspace, first_manifest, mod_name="First"),
    )

    source = safe_tmp_path / "Second.zip"
    source.write_bytes(b"second")
    manifest = build_input_manifest(source)
    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=old_workspace / "work",
            local_state_root=safe_tmp_path / "state",
            workspace_root=workspace_tmp_path / "workspaces",
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        assert resolution.is_new
        assert resolution.workspace != old_workspace
        assert resolution.workspace.name == "Second"
    finally:
        resolution.close()


def test_other_commands_resolve_explicit_then_cwd_then_last_active(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    explicit = workspace_tmp_path / "Explicit"
    cwd_workspace = workspace_tmp_path / "Current"
    last = workspace_tmp_path / "Last"
    for workspace in (explicit, cwd_workspace, last):
        _write_workspace_marker(workspace)
    store = CliStateStore(safe_tmp_path / "state")
    state = store.read()
    state["last_workspace"] = str(last)
    store.write(state)

    assert resolve_command_workspace(explicit, cwd_workspace, store) == explicit
    assert (
        resolve_command_workspace(None, cwd_workspace / "mod", store) == cwd_workspace
    )
    assert resolve_command_workspace(None, safe_tmp_path, store) == last


def test_direct_scan_multiple_matching_sessions_requires_cache_tiebreaker(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    workspace_root = workspace_tmp_path / "workspaces"
    for index in (1, 2):
        workspace = workspace_root / f"Example-{index}"
        _write_workspace_marker(workspace)
        shutil.copy2(source, workspace / "mod" / "Example.zip")
        create_session_no_replace(
            workspace / ".workflow" / "smt-session.json",
            _session_for(
                workspace,
                manifest,
                workspace_id=f"{index}{index}{index}{index}{index}{index}{index}{index}-1111-4111-8111-111111111111",
            ),
        )

    with pytest.raises(WorkspaceConflictError) as conflict:
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                cwd=safe_tmp_path,
                local_state_root=safe_tmp_path / "state",
                workspace_root=workspace_root,
                lock_factory=_RecordingLockFactory(),
            ),
            manifest,
        )

    assert all(
        str(workspace_root / f"Example-{index}") in str(conflict.value)
        for index in (1, 2)
    )


@pytest.mark.parametrize("reservation_order", [(0, 1), (1, 0)])
def test_multiple_reservation_session_candidates_conflict_independent_of_json_order(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    reservation_order: tuple[int, int],
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    workspace_root = workspace_tmp_path / "workspaces"
    candidate_rows: list[tuple[str, Path, dict[str, object]]] = []
    for index, reservation_id in enumerate(
        (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        )
    ):
        workspace = workspace_root / f"Candidate-{index + 1}"
        _write_workspace_marker(workspace)
        shutil.copy2(source, workspace / "mod" / "Example.zip")
        session = _session_for(
            workspace,
            manifest,
            workspace_id=reservation_id,
        )
        create_session_no_replace(
            workspace / ".workflow" / "smt-session.json",
            session,
        )
        candidate_rows.append(
            (
                reservation_id,
                workspace,
                {
                    "workspace_id": reservation_id,
                    "path": str(workspace),
                    "fingerprint_identity": identity,
                    "pid": 999,
                    "created_at": "2026-07-22T00:00:00+00:00",
                },
            )
        )
    store = CliStateStore(safe_tmp_path / "state")
    state = store.read()
    state["reservations"] = {
        candidate_rows[index][0]: candidate_rows[index][2]
        for index in reservation_order
    }
    store.write(state)
    expected_state = store.read()

    with pytest.raises(WorkspaceConflictError, match="multiple|candidate") as conflict:
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                cwd=safe_tmp_path,
                local_state_root=safe_tmp_path / "state",
                workspace_root=workspace_root,
                lock_factory=_RecordingLockFactory(),
            ),
            manifest,
        )

    assert all(
        str(workspace) in str(conflict.value) for _, workspace, _ in candidate_rows
    )
    assert store.read() == expected_state


def test_one_reservation_session_candidate_is_reconfirmed_and_registered(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    workspace_root = workspace_tmp_path / "workspaces"
    workspace = workspace_root / "Recovered"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    store = CliStateStore(safe_tmp_path / "state")
    state = store.read()
    state["reservations"] = {
        session.workspace_id: {
            "workspace_id": session.workspace_id,
            "path": str(workspace),
            "fingerprint_identity": identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    }
    store.write(state)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=safe_tmp_path,
            local_state_root=safe_tmp_path / "state",
            workspace_root=workspace_root,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        assert resolution.workspace == workspace
        assert resolution.workspace_id == session.workspace_id
        assert not resolution.is_new
        persisted = store.read()
        assert persisted["input_mappings"] == {identity: str(workspace)}
        assert persisted["reservations"] == {}
    finally:
        resolution.close()


def test_reservation_lock_order_never_blocks_lower_lock_while_global_is_held(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    locks = _RecordingLockFactory()

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=safe_tmp_path,
            local_state_root=safe_tmp_path / "state",
            workspace_root=workspace_tmp_path / "workspaces",
            lock_factory=locks,
        ),
        manifest,
    )
    try:
        non_global_acquires = [
            event
            for event in locks.events
            if event[0] == "acquire" and event[1] != "global"
        ]
        assert non_global_acquires
        assert all(
            event[2] == 0 or "global" not in event[3] for event in non_global_acquires
        )
    finally:
        resolution.close()


def test_lock_timeout_is_short_and_independent_from_initializer_timeout(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    locks = _RecordingLockFactory()
    operation_timeouts: list[float] = []

    class _InitializerProcess:
        def run(
            self,
            command: list[str],
            **kwargs: object,
        ) -> SimpleNamespace:
            operation_timeouts.append(float(kwargs["timeout_seconds"]))
            _write_workspace_marker(Path(command[2]), "skyrim-se")
            return SimpleNamespace(exit_code=0, output_tail=())

    monkeypatch.setattr(smt_cli, "ManagedProcess", _InitializerProcess)
    defaults = RunRequest(source=source, game_id="skyrim-se")
    assert defaults.timeout_seconds == 1800
    assert defaults.lock_timeout_seconds == 5
    request = RunRequest(
        source=source,
        game_id="skyrim-se",
        cwd=safe_tmp_path,
        local_state_root=safe_tmp_path / "state",
        workspace_root=workspace_tmp_path / "workspaces",
        timeout_seconds=321,
        lock_timeout_seconds=0.25,
        lock_factory=locks,
    )

    resolution = resolve_run_workspace(request, manifest)
    try:
        import_input_transactionally(source, resolution, manifest)
    finally:
        resolution.close()

    assert operation_timeouts == [321]
    assert {
        event[2] for event in locks.events if event[0] == "acquire" and event[2] > 0
    } == {0.25}


def test_same_input_waits_for_existing_reservation_then_reuses_committed_session(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Same.zip"
    source.write_bytes(b"same input")
    manifest = build_input_manifest(source)
    locks = _ThreadLockFactory()
    initializer_entered = threading.Event()
    allow_initializer = threading.Event()
    results: list[tuple[str, bool, Path]] = []
    errors: list[BaseException] = []

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        initializer_entered.set()
        assert allow_initializer.wait(timeout=5)
        _write_workspace_marker(workspace, game_id)

    request = RunRequest(
        source=source,
        game_id="skyrim-se",
        tool_setup="skip",
        cwd=safe_tmp_path,
        local_state_root=safe_tmp_path / "state",
        workspace_root=workspace_tmp_path / "workspaces",
        initializer=initializer,
        lock_factory=locks,
        timeout_seconds=3,
    )

    def first_runner() -> None:
        resolution = None
        try:
            resolution = resolve_run_workspace(request, manifest)
            session = import_input_transactionally(source, resolution, manifest)
            results.append(
                (session.workspace_id, resolution.is_new, resolution.workspace)
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            if resolution is not None:
                resolution.close()

    def second_runner() -> None:
        resolution = None
        try:
            resolution = resolve_run_workspace(request, manifest)
            results.append(
                (resolution.workspace_id, resolution.is_new, resolution.workspace)
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            if resolution is not None:
                resolution.close()

    first = threading.Thread(target=first_runner)
    second = threading.Thread(target=second_runner)
    first.start()
    assert initializer_entered.wait(timeout=5)
    second.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(event[1:3] == ("acquire-start", "11111111") for event in locks.events):
            break
        if (
            len(
                [
                    event
                    for event in locks.events
                    if event[1] == "acquire-start" and event[2].endswith(".lock")
                ]
            )
            >= 4
        ):
            break
        time.sleep(0.01)
    assert second.is_alive(), (
        "second run should be waiting for the existing reservation"
    )
    allow_initializer.set()
    first.join(timeout=6)
    second.join(timeout=6)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert results[0][0] == results[1][0]
    assert results[0][2] == results[1][2]
    assert [row[1] for row in results].count(False) == 2
    wait_events = [
        event
        for event in locks.events
        if event[1] == "acquire-start"
        and event[2] not in {"cli-state.lock", "smt-operation.lock"}
    ]
    assert any("cli-state.lock" not in event[3] for event in wait_events)


def test_different_input_initializers_can_overlap_outside_global_lock(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    sources = [safe_tmp_path / "First.zip", safe_tmp_path / "Second.zip"]
    for index, source in enumerate(sources):
        source.write_bytes(f"input-{index}".encode())
    manifests = [build_input_manifest(source) for source in sources]
    locks = _ThreadLockFactory()
    both_entered = threading.Event()
    entered_guard = threading.Lock()
    entered = 0
    errors: list[BaseException] = []
    workspaces: list[Path] = []

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        nonlocal entered
        del tool_setup
        with entered_guard:
            entered += 1
            if entered == 2:
                both_entered.set()
        assert both_entered.wait(timeout=5)
        _write_workspace_marker(workspace, game_id)

    def runner(source: Path, manifest: InputManifest) -> None:
        resolution = None
        try:
            resolution = resolve_run_workspace(
                RunRequest(
                    source=source,
                    game_id="skyrim-se",
                    tool_setup="skip",
                    cwd=safe_tmp_path,
                    local_state_root=safe_tmp_path / "state",
                    workspace_root=workspace_tmp_path / "workspaces",
                    initializer=initializer,
                    lock_factory=locks,
                    timeout_seconds=3,
                ),
                manifest,
            )
            import_input_transactionally(source, resolution, manifest)
            workspaces.append(resolution.workspace)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if resolution is not None:
                resolution.close()

    threads = [
        threading.Thread(target=runner, args=(source, manifest))
        for source, manifest in zip(sources, manifests, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=7)

    assert all(not thread.is_alive() for thread in threads)
    if errors:
        raise ExceptionGroup("different-input runners failed", errors)
    assert entered == 2
    assert len(set(workspaces)) == 2


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx integration requires Windows")
def test_spawned_different_identities_overlap_initialization_and_keep_valid_state(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    sources = [safe_tmp_path / "SpawnFirst.zip", safe_tmp_path / "SpawnSecond.zip"]
    for index, source in enumerate(sources):
        source.write_bytes(f"spawn-input-{index}".encode())
    state_root = safe_tmp_path / "spawn-state"
    workspace_root = workspace_tmp_path / "spawn-workspaces"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_spawn_reservation_worker,
            args=(str(source), str(state_root), str(workspace_root), results),
            kwargs={"overlap_barrier": barrier},
        )
        for source in sources
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    stuck = [process for process in processes if process.is_alive()]
    for process in stuck:
        process.terminate()
        process.join(timeout=5)
    assert stuck == []
    assert [process.exitcode for process in processes] == [0, 0]
    rows = [results.get(timeout=2) for _process in processes]
    assert {row["status"] for row in rows} == {"committed"}
    starts = [float(row["init_start"]) for row in rows]
    ends = [float(row["init_end"]) for row in rows]
    assert max(starts) < min(ends), "different-identity initializer intervals must overlap"
    assert len({row["workspace"] for row in rows}) == 2
    assert len({row["identity"] for row in rows}) == 2

    state = CliStateStore(state_root).read()
    assert len(state["input_mappings"]) == 2
    assert state["reservations"] == {}
    session_files = sorted(workspace_root.glob("*/.workflow/smt-session.json"))
    assert len(session_files) == 2
    assert all(json.loads(path.read_text(encoding="utf-8")) for path in session_files)


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx integration requires Windows")
def test_spawned_same_identity_waits_then_reuses_one_committed_session(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "SpawnSame.zip"
    source.write_bytes(b"same spawn input")
    state_root = safe_tmp_path / "same-state"
    workspace_root = workspace_tmp_path / "same-workspaces"
    context = multiprocessing.get_context("spawn")
    first_initializer_entered = context.Event()
    release = context.Event()
    second_ready_before_resolve = context.Event()
    second_reservation_acquire_started = context.Event()
    second_initializer_entered = context.Event()
    second_result_emitted = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_spawn_reservation_worker,
        args=(str(source), str(state_root), str(workspace_root), results),
        kwargs={
            "entered_event": first_initializer_entered,
            "release_event": release,
        },
    )
    second = context.Process(
        target=_spawn_reservation_worker,
        args=(str(source), str(state_root), str(workspace_root), results),
        kwargs={
            "ready_before_resolve_event": second_ready_before_resolve,
            "reservation_acquire_started_event": second_reservation_acquire_started,
            "entered_event": second_initializer_entered,
            "result_emitted_event": second_result_emitted,
        },
    )

    first.start()
    if not first_initializer_entered.wait(timeout=10):
        release.set()
        first.join(timeout=5)
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)
        pytest.fail("first initializer did not acquire its reservation")
    second.start()
    second_ready = second_ready_before_resolve.wait(timeout=10)
    second_competed = second_reservation_acquire_started.wait(timeout=10)
    second_waited = (
        second.is_alive()
        and not second_initializer_entered.is_set()
        and not second_result_emitted.is_set()
    )
    release.set()
    first.join(timeout=20)
    second.join(timeout=20)

    stuck = [process for process in (first, second) if process.is_alive()]
    for process in stuck:
        process.terminate()
        process.join(timeout=5)
    assert second_ready, "second runner did not reach resolve_run_workspace"
    assert second_competed, "second runner did not attempt to acquire the reservation"
    assert second_waited, "same-identity runner must wait outside the global lock"
    assert stuck == []
    assert first.exitcode == 0 and second.exitcode == 0
    rows = [results.get(timeout=2), results.get(timeout=2)]
    assert {row["status"] for row in rows} <= {"committed", "timeout"}
    committed = [row for row in rows if row["status"] == "committed"]
    timed_out = [row for row in rows if row["status"] == "timeout"]
    assert committed
    assert all(
        row["exit_code"] == smt_cli.EXIT_WORKSPACE_SESSION_OR_MARKER_CONFLICT
        for row in timed_out
    )
    if len(committed) == 2:
        assert {row["workspace_id"] for row in committed} == {
            committed[0]["workspace_id"]
        }
        assert {row["identity"] for row in committed} == {committed[0]["identity"]}
        assert sorted(row["created"] for row in committed) == [False, True]

    state = CliStateStore(state_root).read()
    assert len(state["input_mappings"]) == 1
    assert state["reservations"] == {}
    session_files = sorted(workspace_root.glob("*/.workflow/smt-session.json"))
    assert len(session_files) == 1
    session_payload = json.loads(session_files[0].read_text(encoding="utf-8"))
    assert session_payload["input_identity"] in state["input_mappings"]


@pytest.mark.parametrize("source_kind", ["archive", "directory"])
def test_transactional_import_uses_a_safe_portable_fallback_for_static_validation(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    source_kind: str,
) -> None:
    if source_kind == "archive":
        source = safe_tmp_path / "Example.zip"
        source.write_bytes(b"portable archive")
    else:
        source = safe_tmp_path / "Example"
        source.mkdir()
        (source / "payload.txt").write_text("portable directory", encoding="utf-8")
    manifest = build_input_manifest(source)

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        assert tool_setup == "skip"
        _write_workspace_marker(workspace, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace_tmp_path / f"portable-{source_kind}",
            local_state_root=safe_tmp_path / f"state-{source_kind}",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    smt_windows._win32_bindings.cache_clear()
    monkeypatch.setattr(smt_windows, "_IS_WINDOWS", False)
    try:
        session = import_input_transactionally(source, resolution, manifest)
        target = resolution.workspace / session.import_relative_path
        if source_kind == "archive":
            assert target.read_bytes() == b"portable archive"
        else:
            assert (target / "payload.txt").read_text(encoding="utf-8") == (
                "portable directory"
            )
        verify_imported_copy(target, manifest)
        verify_source_unchanged(source, manifest, load_game_profile("skyrim-se"))
    finally:
        resolution.close()
        smt_windows._win32_bindings.cache_clear()


def test_portable_archive_import_exclusively_rejects_a_prepositioned_hardlink(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"portable archive")
    manifest = build_input_manifest(source)
    victim = workspace_tmp_path / "victim.bin"
    victim.write_bytes(b"do not replace")
    workspace = workspace_tmp_path / "portable-hardlink"
    fixed_hex = "b" * 32

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        assert tool_setup == "skip"
        _write_workspace_marker(target, game_id)
        os.link(
            victim,
            target
            / "mod"
            / f"{smt_cli.PARTIAL_IMPORT_PREFIX}{fixed_hex}{smt_cli.PARTIAL_IMPORT_SUFFIX}",
        )

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / "portable-hardlink-state",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    smt_windows._win32_bindings.cache_clear()
    monkeypatch.setattr(smt_windows, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        smt_cli.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=fixed_hex),
    )
    try:
        with pytest.raises((OSError, ValueError, smt_cli.ImportTransactionError)):
            import_input_transactionally(source, resolution, manifest)
        assert victim.read_bytes() == b"do not replace"
    finally:
        resolution.close()
        smt_windows._win32_bindings.cache_clear()


def test_publish_path_no_replace_preserves_an_existing_target(
    safe_tmp_path: Path,
) -> None:
    staging = safe_tmp_path / "staging.bin"
    target = safe_tmp_path / "target.bin"
    staging.write_bytes(b"trusted staging")
    target.write_bytes(b"racing target")

    with pytest.raises(FileExistsError):
        smt_windows.publish_path_no_replace(staging, target)

    assert staging.read_bytes() == b"trusted staging"
    assert target.read_bytes() == b"racing target"


@pytest.mark.parametrize(
    ("failure_errno", "expected_error"),
    [
        (errno.EEXIST, FileExistsError),
        (errno.ENOSYS, ManagedProcessEnvironmentError),
    ],
)
def test_posix_publish_path_no_replace_has_a_fail_closed_errno_contract(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    failure_errno: int,
    expected_error: type[BaseException],
) -> None:
    calls: list[tuple[object, ...]] = []

    def failing_renameat2(*args: object) -> int:
        calls.append(args)
        ctypes.set_errno(failure_errno)
        return -1

    monkeypatch.setattr(smt_windows, "_USE_WINDOWS_RENAME", False, raising=False)
    monkeypatch.setattr(
        smt_windows,
        "_renameat2_function",
        lambda: failing_renameat2,
        raising=False,
    )
    staging = safe_tmp_path / "staging.bin"
    target = safe_tmp_path / "target.bin"
    staging.write_bytes(b"trusted staging")

    with pytest.raises(expected_error):
        smt_windows.publish_path_no_replace(staging, target)

    assert len(calls) == 1
    assert staging.read_bytes() == b"trusted staging"
    assert not target.exists()


def test_transaction_publish_never_replaces_target_created_after_precheck(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "PublishRace.zip"
    source.write_bytes(b"trusted archive")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / "publish-race"

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        assert tool_setup == "skip"
        _write_workspace_marker(target, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / "publish-race-state",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    target = workspace / "mod" / source.name
    real_verify_bound_import = smt_cli._verify_bound_import
    staging_verifications = 0

    def create_target_after_final_staging_verification(
        binding: object,
        selected_path: Path,
        expected_manifest: InputManifest,
    ) -> None:
        nonlocal staging_verifications
        real_verify_bound_import(binding, selected_path, expected_manifest)  # type: ignore[arg-type]
        if selected_path.name.startswith(smt_cli.PARTIAL_IMPORT_PREFIX):
            staging_verifications += 1
            if staging_verifications == 2:
                target.write_bytes(b"racing target")

    monkeypatch.setattr(
        smt_cli,
        "_verify_bound_import",
        create_target_after_final_staging_verification,
    )
    try:
        with pytest.raises(FileExistsError):
            import_input_transactionally(source, resolution, manifest)
        assert staging_verifications == 2
        assert target.read_bytes() == b"racing target"
        assert not (workspace / ".workflow" / "smt-session.json").exists()
        assert resolution.state_store.read()["input_mappings"] == {}
    finally:
        resolution.close()


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize(
    "attack_kind",
    ["archive-replace", "directory-overwrite", "directory-replace"],
)
def test_transaction_never_publishes_staging_mutated_after_initial_verification(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    portable: bool,
    attack_kind: str,
) -> None:
    if attack_kind == "archive-replace":
        source = safe_tmp_path / f"Example-{portable}.zip"
        source.write_bytes(b"trusted archive")
    else:
        source = safe_tmp_path / f"Example-{portable}"
        source.mkdir()
        (source / "payload.txt").write_bytes(b"trusted directory")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / f"{attack_kind}-{portable}"

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        assert tool_setup == "skip"
        _write_workspace_marker(target, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / f"state-{attack_kind}-{portable}",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    real_verify_source_unchanged = smt_cli.verify_source_unchanged

    def mutate_staging_before_source_verification_returns(
        source_path: Path,
        expected_manifest: InputManifest,
        context: object,
    ) -> None:
        real_verify_source_unchanged(source_path, expected_manifest, context)  # type: ignore[arg-type]
        staging_rows = list((workspace / "mod").glob(".smt-import-*.partial"))
        assert len(staging_rows) == 1
        staging = staging_rows[0]
        if attack_kind == "archive-replace":
            staging.unlink()
            staging.write_bytes(b"attacker archive")
            return
        leaf = staging / "payload.txt"
        if attack_kind == "directory-replace":
            leaf.unlink()
            leaf.write_bytes(b"attacker replacement")
        else:
            leaf.write_bytes(b"attacker overwrite")

    smt_windows._win32_bindings.cache_clear()
    if portable:
        monkeypatch.setattr(smt_windows, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        smt_cli,
        "verify_source_unchanged",
        mutate_staging_before_source_verification_returns,
    )
    try:
        with pytest.raises((PermissionError, smt_cli.ImportTransactionError)):
            import_input_transactionally(source, resolution, manifest)
        assert not (workspace / ".workflow" / "smt-session.json").exists()
        assert CliStateStore(resolution.state_store.root).read()["input_mappings"] == {}
        committed = workspace / "mod" / source.name
        assert not committed.exists()
    finally:
        resolution.close()
        smt_windows._win32_bindings.cache_clear()


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("attack_kind", ["overwrite", "replace"])
def test_transaction_rejects_directory_leaf_mutation_in_publish_rebind_gap(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    portable: bool,
    attack_kind: str,
) -> None:
    source = safe_tmp_path / f"Gap-{portable}-{attack_kind}"
    source.mkdir()
    (source / "payload.txt").write_bytes(b"trusted directory")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / f"gap-{portable}-{attack_kind}"

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        assert tool_setup == "skip"
        _write_workspace_marker(target, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / f"gap-state-{portable}-{attack_kind}",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    real_binding = smt_cli.PinnedImportTree
    binding_count = 0

    def attacking_binding(*args: object, **kwargs: object) -> object:
        nonlocal binding_count
        binding_count += 1
        if binding_count == 2:
            leaf = workspace / "mod" / source.name / "payload.txt"
            if attack_kind == "replace":
                leaf.unlink()
            leaf.write_bytes(b"attacker in publish gap")
        return real_binding(*args, **kwargs)  # type: ignore[arg-type]

    smt_windows._win32_bindings.cache_clear()
    if portable:
        monkeypatch.setattr(smt_windows, "_IS_WINDOWS", False)
    monkeypatch.setattr(smt_cli, "PinnedImportTree", attacking_binding)
    try:
        with pytest.raises(smt_cli.ImportTransactionError):
            import_input_transactionally(source, resolution, manifest)
        assert binding_count == 2
        assert not (workspace / ".workflow" / "smt-session.json").exists()
        assert not (workspace / "mod" / source.name).exists()
    finally:
        resolution.close()
        smt_windows._win32_bindings.cache_clear()


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("extra_kind", ["file", "empty-directory"])
def test_transaction_rejects_directory_entries_added_at_final_hash_tail(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    portable: bool,
    extra_kind: str,
) -> None:
    source = safe_tmp_path / f"Final-{portable}-{extra_kind}"
    source.mkdir()
    (source / "payload.txt").write_bytes(b"trusted directory")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / f"final-{portable}-{extra_kind}"

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        assert tool_setup == "skip"
        _write_workspace_marker(target, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / f"final-state-{portable}-{extra_kind}",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    target = workspace / "mod" / source.name
    real_verify_imported_copy = smt_cli.verify_imported_copy
    injected = False

    def inject_after_final_content_hash(
        selected_target: Path,
        expected_manifest: InputManifest,
    ) -> None:
        nonlocal injected
        real_verify_imported_copy(selected_target, expected_manifest)
        session_path = workspace / ".workflow" / "smt-session.json"
        mapping = resolution.state_store.read()["input_mappings"]
        if (
            not injected
            and selected_target == target
            and session_path.is_file()
            and mapping.get(resolution.input_identity) == str(workspace)
        ):
            injected = True
            if extra_kind == "file":
                (target / "extra.txt").write_bytes(b"attacker extra file")
            else:
                (target / "newdir").mkdir()

    smt_windows._win32_bindings.cache_clear()
    if portable:
        monkeypatch.setattr(smt_windows, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        smt_cli,
        "verify_imported_copy",
        inject_after_final_content_hash,
    )
    try:
        with pytest.raises(smt_cli.ImportTransactionError):
            import_input_transactionally(source, resolution, manifest)
        assert injected
        assert not (workspace / ".workflow" / "smt-session.json").exists()
        assert resolution.state_store.read()["input_mappings"] == {}
        assert not target.exists()
    finally:
        resolution.close()
        smt_windows._win32_bindings.cache_clear()


@pytest.mark.parametrize("concurrent_last_workspace_update", [False, True])
def test_final_import_failure_restores_previous_last_workspace_without_lost_update(
    monkeypatch: pytest.MonkeyPatch,
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    concurrent_last_workspace_update: bool,
) -> None:
    source = safe_tmp_path / f"Rollback-{concurrent_last_workspace_update}"
    source.mkdir()
    (source / "payload.txt").write_bytes(b"trusted directory")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / f"rollback-{concurrent_last_workspace_update}"
    previous_workspace = workspace_tmp_path / "PreviousWorkspace"
    concurrent_workspace = workspace_tmp_path / "ConcurrentWorkspace"
    state_root = safe_tmp_path / f"rollback-state-{concurrent_last_workspace_update}"
    store = CliStateStore(state_root)
    initial_state = store.read()
    initial_state["last_workspace"] = str(previous_workspace)
    store.write(initial_state)

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        assert tool_setup == "skip"
        _write_workspace_marker(target, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=state_root,
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    target = workspace / "mod" / source.name
    real_verify_imported_copy = smt_cli.verify_imported_copy
    injected = False

    def fail_after_mapping_commit(
        selected_target: Path,
        expected_manifest: InputManifest,
    ) -> None:
        nonlocal injected
        real_verify_imported_copy(selected_target, expected_manifest)
        state = store.read()
        if (
            not injected
            and state["input_mappings"].get(resolution.input_identity)
            == str(workspace)
        ):
            injected = True
            if concurrent_last_workspace_update:
                state["last_workspace"] = str(concurrent_workspace)
                store.write(state)
            (target / "extra.txt").write_bytes(b"force final verification failure")

    monkeypatch.setattr(smt_cli, "verify_imported_copy", fail_after_mapping_commit)
    try:
        with pytest.raises(smt_cli.ImportTransactionError):
            import_input_transactionally(source, resolution, manifest)
        assert injected
        final_state = store.read()
        expected_last = (
            concurrent_workspace
            if concurrent_last_workspace_update
            else previous_workspace
        )
        assert final_state["last_workspace"] == str(expected_last)
        assert final_state["input_mappings"] == {}
        assert not (workspace / ".workflow" / "smt-session.json").exists()
        assert not target.exists()
    finally:
        resolution.close()


def test_transactional_directory_import_uses_manifest_and_commits_session_then_mapping(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "DirectoryMod"
    (source / "Empty").mkdir(parents=True)
    (source / "Interface").mkdir()
    (source / "Interface" / "menu.txt").write_text("hello", encoding="utf-8")
    manifest = build_input_manifest(source)
    locks = _RecordingLockFactory()

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        assert game_id == "skyrim-se"
        assert tool_setup == "skip"
        _write_workspace_marker(workspace, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            tool_setup="skip",
            cwd=safe_tmp_path,
            local_state_root=safe_tmp_path / "state",
            workspace_root=workspace_tmp_path / "workspaces",
            initializer=initializer,
            lock_factory=locks,
        ),
        manifest,
    )
    try:
        session = import_input_transactionally(source, resolution, manifest)
        target = resolution.workspace / session.import_relative_path
        assert target.is_dir()
        assert (target / "Empty").is_dir()
        assert (target / "Interface" / "menu.txt").read_text(
            encoding="utf-8"
        ) == "hello"
        assert validate_session(resolution.workspace, session.input_identity) == session
        state = CliStateStore(safe_tmp_path / "state").read()
        assert state["input_mappings"][session.input_identity] == str(
            resolution.workspace
        )
        assert state["reservations"] == {}
        assert not list((resolution.workspace / "mod").glob(".smt-import-*.partial"))
    finally:
        resolution.close()


def test_transaction_failure_removes_only_owned_staging_and_writes_owned_report(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "DirectoryMod"
    source.mkdir()
    (source / "one.txt").write_text("one", encoding="utf-8")
    (source / "two.txt").write_text("two", encoding="utf-8")
    manifest = build_input_manifest(source)
    copied = 0

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        _write_workspace_marker(workspace, game_id)
        (workspace / "keep.txt").write_text("keep", encoding="utf-8")

    def failing_copier(source_file: Path, target_file: object) -> None:
        nonlocal copied
        copied += 1
        if copied == 2:
            raise OSError("forced copy failure")
        with source_file.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, target_file)  # type: ignore[arg-type]

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            tool_setup="skip",
            cwd=safe_tmp_path,
            local_state_root=safe_tmp_path / "state",
            workspace_root=workspace_tmp_path / "workspaces",
            initializer=initializer,
            copier=failing_copier,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        with pytest.raises(OSError, match="forced copy failure"):
            import_input_transactionally(source, resolution, manifest)
        assert resolution.workspace.is_dir()
        assert (resolution.workspace / "keep.txt").is_file()
        assert not list((resolution.workspace / "mod").glob(".smt-import-*.partial"))
        assert not (resolution.workspace / ".workflow" / "smt-session.json").exists()
        assert (
            resolution.workspace / ".workflow" / "smt-import-failure.json"
        ).is_file()
        assert CliStateStore(safe_tmp_path / "state").read()["input_mappings"] == {}
    finally:
        resolution.close()


def test_initializer_failure_before_workspace_creation_does_not_create_failure_path(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    explicit_workspace = workspace_tmp_path / "must-not-be-created"

    def failing_initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        del workspace, game_id, tool_setup
        raise RuntimeError("initializer failed before creating workspace")

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=explicit_workspace,
            local_state_root=safe_tmp_path / "state",
            workspace_root=workspace_tmp_path / "workspaces",
            initializer=failing_initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        with pytest.raises(RuntimeError, match="before creating workspace"):
            import_input_transactionally(source, resolution, manifest)
        assert not explicit_workspace.exists()
    finally:
        resolution.close()


def test_archive_import_rejects_a_prepositioned_hardlink_before_writing(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive bytes")
    manifest = build_input_manifest(source)
    victim = workspace_tmp_path / "victim.bin"
    victim.write_bytes(b"do not replace")
    workspace = workspace_tmp_path / "Workspace-hardlink-import"
    fixed_hex = "a" * 32

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        _write_workspace_marker(target, game_id)
        os.link(
            victim,
            target
            / "mod"
            / f"{smt_cli.PARTIAL_IMPORT_PREFIX}{fixed_hex}{smt_cli.PARTIAL_IMPORT_SUFFIX}",
        )

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / "state",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    monkeypatch.setattr(
        smt_cli.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=fixed_hex),
    )
    try:
        with pytest.raises((OSError, ValueError, smt_cli.ImportTransactionError)):
            import_input_transactionally(source, resolution, manifest)
        assert victim.read_bytes() == b"do not replace"
    finally:
        resolution.close()


def test_directory_import_does_not_write_through_a_racing_reparse_parent(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "DirectoryMod"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "visible.txt").write_text("translated", encoding="utf-8")
    manifest = build_input_manifest(source)
    outside = workspace_tmp_path / "outside"
    outside.mkdir()
    workspace = workspace_tmp_path / "Workspace-directory-race"
    fixed_hex = "b" * 32
    partial = (
        workspace
        / "mod"
        / f"{smt_cli.PARTIAL_IMPORT_PREFIX}{fixed_hex}{smt_cli.PARTIAL_IMPORT_SUFFIX}"
    )
    copier_ready = threading.Event()
    race_done = threading.Event()
    race_errors: list[OSError] = []

    def synchronized_copier(source_file: Path, destination: object) -> None:
        copier_ready.set()
        assert race_done.wait(timeout=5)
        if hasattr(destination, "write"):
            with source_file.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, destination)  # type: ignore[arg-type]
        else:
            shutil.copyfile(source_file, destination)  # type: ignore[arg-type]

    def race_parent() -> None:
        assert copier_ready.wait(timeout=5)
        nested = partial / "nested"
        try:
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            race_errors.append(exc)
        finally:
            race_done.set()

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        _write_workspace_marker(target, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / "state-directory-race",
            tool_setup="skip",
            initializer=initializer,
            copier=synchronized_copier,  # type: ignore[arg-type]
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    monkeypatch.setattr(
        smt_cli.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=fixed_hex),
    )
    racer = threading.Thread(target=race_parent, daemon=True)
    racer.start()
    try:
        try:
            import_input_transactionally(source, resolution, manifest)
        except (OSError, ValueError, smt_cli.ImportTransactionError):
            pass
        racer.join(timeout=5)
        assert not racer.is_alive()
        assert not (outside / "visible.txt").exists()
        assert race_errors, "the pinned destination parent must reject replacement"
    finally:
        resolution.close()


def test_renamed_identical_archive_reuses_original_immutable_session_name(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    original = safe_tmp_path / "Original.zip"
    original.write_bytes(b"same archive bytes")
    original_manifest = build_input_manifest(original)
    state_root = safe_tmp_path / "state"

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        _write_workspace_marker(workspace, game_id)

    first = resolve_run_workspace(
        RunRequest(
            source=original,
            game_id="skyrim-se",
            tool_setup="skip",
            local_state_root=state_root,
            workspace_root=workspace_tmp_path / "workspaces",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        original_manifest,
    )
    try:
        original_session = import_input_transactionally(
            original, first, original_manifest
        )
        original_workspace = first.workspace
    finally:
        first.close()

    renamed = safe_tmp_path / "Renamed.zip"
    shutil.copyfile(original, renamed)
    renamed_manifest = build_input_manifest(renamed)
    second = resolve_run_workspace(
        RunRequest(
            source=renamed,
            game_id="skyrim-se",
            local_state_root=state_root,
            workspace_root=workspace_tmp_path / "workspaces",
            lock_factory=_RecordingLockFactory(),
        ),
        renamed_manifest,
    )
    try:
        reused = import_input_transactionally(renamed, second, renamed_manifest)
        assert second.workspace == original_workspace
        assert second.finalized_mod_name.value == "Original"
        assert reused == original_session
        session_payload = json.loads(
            (second.workspace / ".workflow" / "smt-session.json").read_text(
                encoding="utf-8"
            )
        )
        assert str(original) not in json.dumps(session_payload)
        assert str(renamed) not in json.dumps(session_payload)
    finally:
        second.close()


def test_committed_session_without_mapping_is_recovered_and_reservation_removed(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    state_root = safe_tmp_path / "state"
    workspace_root = workspace_tmp_path / "workspaces"
    workspace = workspace_root / "Example"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {
        session.workspace_id: {
            "workspace_id": session.workspace_id,
            "path": str(workspace),
            "fingerprint_identity": session.input_identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    }
    store.write(state)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=safe_tmp_path,
            local_state_root=state_root,
            workspace_root=workspace_root,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        assert not resolution.is_new
        assert resolution.workspace == workspace
        recovered = store.read()
        assert recovered["input_mappings"][session.input_identity] == str(workspace)
        assert recovered["reservations"] == {}
    finally:
        resolution.close()


def test_reservation_workspace_id_must_match_recovered_session_workspace_id(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    state_root = safe_tmp_path / "state"
    workspace = workspace_tmp_path / "workspaces" / "Example"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(
        workspace,
        manifest,
        workspace_id="22222222-2222-4222-8222-222222222222",
    )
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    reservation_id = "11111111-1111-4111-8111-111111111111"
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {
        reservation_id: {
            "workspace_id": reservation_id,
            "path": str(workspace),
            "fingerprint_identity": session.input_identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    }
    store.write(state)

    with pytest.raises(WorkspaceConflictError, match="workspace_id|reservation"):
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                local_state_root=state_root,
                workspace_root=workspace_tmp_path / "workspaces",
                cwd=safe_tmp_path,
                lock_factory=_RecordingLockFactory(),
            ),
            manifest,
        )

    persisted = store.read()
    assert persisted["reservations"].keys() == {reservation_id}
    assert persisted["input_mappings"] == {}


@pytest.mark.parametrize(
    "resolution_source",
    ["cwd", "mapping", "scan", "explicit"],
)
def test_every_existing_workspace_resolution_reconciles_related_reservations(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    resolution_source: str,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    state_root = safe_tmp_path / "state"
    workspace_root = workspace_tmp_path / "workspaces"
    workspace = workspace_root / "Example"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(
        workspace,
        manifest,
        workspace_id="22222222-2222-4222-8222-222222222222",
    )
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    reservation_id = "11111111-1111-4111-8111-111111111111"
    reservation_identity = (
        "smt-input-v1:fallout4:zip:" + manifest.digest
        if resolution_source == "scan"
        else identity
    )
    reservation = {
        "workspace_id": reservation_id,
        "path": str(workspace),
        "fingerprint_identity": reservation_identity,
        "pid": 999,
        "created_at": "2026-07-22T00:00:00+00:00",
    }
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {reservation_id: reservation}
    if resolution_source == "mapping":
        state["input_mappings"] = {identity: str(workspace)}
    store.write(state)
    request = RunRequest(
        source=source,
        game_id="skyrim-se",
        workspace=workspace if resolution_source == "explicit" else None,
        cwd=workspace if resolution_source == "cwd" else safe_tmp_path,
        local_state_root=state_root,
        workspace_root=workspace_root,
        lock_factory=_RecordingLockFactory(),
    )

    with pytest.raises(
        WorkspaceConflictError, match="reservation|workspace_id|ambiguous"
    ):
        resolve_run_workspace(request, manifest)

    persisted = store.read()
    assert persisted["reservations"] == {reservation_id: reservation}
    if resolution_source != "mapping":
        assert persisted["input_mappings"] == {}


def test_existing_workspace_deletes_only_the_uniquely_reconciled_reservation(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    state_root = safe_tmp_path / "state"
    workspace = workspace_tmp_path / "workspaces" / "Example"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    reservation = {
        "workspace_id": session.workspace_id,
        "path": str(workspace),
        "fingerprint_identity": identity,
        "pid": 999,
        "created_at": "2026-07-22T00:00:00+00:00",
    }
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {session.workspace_id: reservation}
    store.write(state)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=workspace,
            local_state_root=state_root,
            workspace_root=workspace_tmp_path / "workspaces",
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        persisted = store.read()
        assert persisted["reservations"] == {}
        assert persisted["input_mappings"] == {identity: str(workspace)}
    finally:
        resolution.close()


def test_existing_workspace_preserves_same_identity_reservation_at_other_path(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    workspace = workspace_tmp_path / "workspaces" / "Example"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    other_id = "22222222-2222-4222-8222-222222222222"
    state_root = safe_tmp_path / "state"
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {
        session.workspace_id: {
            "workspace_id": session.workspace_id,
            "path": str(workspace),
            "fingerprint_identity": identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        },
        other_id: {
            "workspace_id": other_id,
            "path": str(workspace_tmp_path / "abandoned"),
            "fingerprint_identity": identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        },
    }
    store.write(state)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=workspace,
            local_state_root=state_root,
            workspace_root=workspace_tmp_path / "workspaces",
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        persisted = store.read()
        assert persisted["reservations"] == {
            other_id: {
                "workspace_id": other_id,
                "path": str(workspace_tmp_path / "abandoned"),
                "fingerprint_identity": identity,
                "pid": 999,
                "created_at": "2026-07-22T00:00:00+00:00",
            }
        }
        assert persisted["input_mappings"] == {identity: str(workspace)}
    finally:
        resolution.close()


def test_unfinished_reservation_without_session_is_preserved_and_new_name_allocated(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    state_root = safe_tmp_path / "state"
    workspace_root = workspace_tmp_path / "workspaces"
    abandoned = workspace_root / "Example"
    _write_workspace_marker(abandoned)
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {
        "11111111-1111-4111-8111-111111111111": {
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "path": str(abandoned),
            "fingerprint_identity": identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    }
    store.write(state)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=safe_tmp_path,
            local_state_root=state_root,
            workspace_root=workspace_root,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        assert resolution.is_new
        assert resolution.workspace != abandoned
        assert resolution.workspace.name.startswith("Example-")
        assert len(store.read()["reservations"]) == 2
    finally:
        resolution.close()


def test_many_historical_reservations_do_not_hit_a_fixed_retry_limit(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    state_root = safe_tmp_path / "state"
    workspace_root = workspace_tmp_path / "workspaces"
    reservations: dict[str, dict[str, object]] = {}
    for index in range(12):
        reservation_id = f"{index + 1:08d}-1111-4111-8111-{index + 1:012d}"
        abandoned = workspace_root / ("Example" if index == 0 else f"Old-{index}")
        _write_workspace_marker(abandoned)
        reservations[reservation_id] = {
            "workspace_id": reservation_id,
            "path": str(abandoned),
            "fingerprint_identity": identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = reservations
    store.write(state)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            cwd=safe_tmp_path,
            local_state_root=state_root,
            workspace_root=workspace_root,
            lock_factory=_RecordingLockFactory(),
        ),
        manifest,
    )
    try:
        assert resolution.is_new
        assert resolution.workspace.name.startswith("Example-")
        assert store.read()["reservations"] == {
            **reservations,
            resolution.workspace_id: {
                "workspace_id": resolution.workspace_id,
                "path": str(resolution.workspace),
                "fingerprint_identity": identity,
                "pid": os.getpid(),
                "created_at": store.read()["reservations"][resolution.workspace_id][
                    "created_at"
                ],
            },
        }
    finally:
        resolution.close()


def test_abandoned_same_identity_reservation_does_not_block_committed_workspace_reuse(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    state_root = safe_tmp_path / "state"
    workspace_root = workspace_tmp_path / "workspaces"
    abandoned = workspace_root / "Example"
    _write_workspace_marker(abandoned)
    abandoned_id = "11111111-1111-4111-8111-111111111111"
    abandoned_reservation = {
        "workspace_id": abandoned_id,
        "path": str(abandoned),
        "fingerprint_identity": identity,
        "pid": 999,
        "created_at": "2026-07-22T00:00:00+00:00",
    }
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {abandoned_id: abandoned_reservation}
    store.write(state)

    def initializer(workspace: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        _write_workspace_marker(workspace, game_id)

    request = RunRequest(
        source=source,
        game_id="skyrim-se",
        tool_setup="skip",
        cwd=safe_tmp_path,
        local_state_root=state_root,
        workspace_root=workspace_root,
        initializer=initializer,
        lock_factory=_RecordingLockFactory(),
    )
    first = resolve_run_workspace(request, manifest)
    try:
        assert first.is_new
        assert first.workspace != abandoned
        assert first.workspace.name.startswith("Example-")
        session = import_input_transactionally(source, first, manifest)
        committed_workspace = first.workspace
    finally:
        first.close()

    committed_state = store.read()
    assert committed_state["input_mappings"] == {identity: str(committed_workspace)}
    assert committed_state["reservations"] == {abandoned_id: abandoned_reservation}

    third = resolve_run_workspace(request, manifest)
    try:
        assert not third.is_new
        assert third.workspace == committed_workspace
        assert third.workspace_id == session.workspace_id
        assert store.read()["reservations"] == {abandoned_id: abandoned_reservation}
    finally:
        third.close()


def test_unfinished_reservation_on_explicit_workspace_is_a_conflict_not_reused(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    identity = composite_input_identity("skyrim-se", manifest)
    state_root = safe_tmp_path / "state"
    explicit = workspace_tmp_path / "Explicit"
    reservation_id = "11111111-1111-4111-8111-111111111111"
    store = CliStateStore(state_root)
    state = store.read()
    state["reservations"] = {
        reservation_id: {
            "workspace_id": reservation_id,
            "path": str(explicit),
            "fingerprint_identity": identity,
            "pid": 999,
            "created_at": "2026-07-22T00:00:00+00:00",
        }
    }
    store.write(state)

    with pytest.raises(WorkspaceConflictError, match="unfinished|reservation|session"):
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                workspace=explicit,
                local_state_root=state_root,
                workspace_root=workspace_tmp_path / "workspaces",
                lock_factory=_RecordingLockFactory(),
            ),
            manifest,
        )

    assert store.read()["reservations"].keys() == {reservation_id}


def test_extra_mod_inputs_are_reported_but_exact_queue_filter_stays_bound_to_session(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / "Workspace"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    (workspace / "mod" / "OtherMod.zip").write_bytes(b"unregistered")
    (workspace / "mod" / ".smt-import-OtherMod.zip").write_bytes(b"unregistered")
    (workspace / "mod" / ".smt-import-crashed.partial").write_bytes(b"unregistered")
    true_staging = (
        workspace / "mod" / ".smt-import-11111111111141118111111111111111.partial"
    )
    true_staging.write_bytes(b"owned staging")

    assert detect_extra_mod_inputs(workspace, session) == (
        "mod/.smt-import-crashed.partial",
        "mod/.smt-import-OtherMod.zip",
        "mod/OtherMod.zip",
    )
    assert exact_queue_arguments(session) == (
        "--mod-name",
        "Example",
        "--source-path",
        "mod/Example.zip",
        "--limit",
        "1",
    )


def test_strict_partial_name_with_wrong_source_type_is_not_silently_ignored(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    manifest = build_input_manifest(source)
    workspace = workspace_tmp_path / "Workspace-invalid-staging"
    _write_workspace_marker(workspace)
    shutil.copy2(source, workspace / "mod" / "Example.zip")
    session = _session_for(workspace, manifest)
    create_session_no_replace(workspace / ".workflow" / "smt-session.json", session)
    invalid_staging = (
        workspace / "mod" / ".smt-import-11111111111141118111111111111111.partial"
    )
    invalid_staging.mkdir()

    assert detect_extra_mod_inputs(workspace, session) == (
        f"mod/{invalid_staging.name} (invalid staging)",
    )
    with pytest.raises(WorkspaceConflictError, match="partial|type"):
        validate_session(workspace, session.input_identity)


def _directory_contract(entries: tuple[InputEntry, ...]) -> str:
    payload = bytearray(b"SMT-INPUT-DIR\x00")
    payload.extend(struct.pack(">H", 1))
    payload.extend(struct.pack(">Q", len(entries)))
    for entry in entries:
        relative_bytes = entry.relative_path.encode("utf-8")
        payload.extend(b"\x01" if entry.entry_type == "directory" else b"\x02")
        payload.extend(struct.pack(">I", len(relative_bytes)))
        payload.extend(relative_bytes)
        if entry.entry_type == "file":
            payload.extend(struct.pack(">Q", entry.size))
            payload.extend(bytes.fromhex(entry.sha256 or ""))
    return hashlib.sha256(payload).hexdigest()


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_directory_manifest_is_stable_and_includes_empty_directory(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example"
    (source / "Empty").mkdir(parents=True)
    (source / "Interface").mkdir()
    (source / "Interface" / "menu.txt").write_text("hello", encoding="utf-8")

    first = build_input_manifest(source)
    second = build_input_manifest(source)

    assert first == second
    assert first.source_kind == "directory"
    assert first.source_identity is not None
    assert isinstance(first.entries, tuple)
    assert [row.relative_path for row in first.entries] == [
        "Empty",
        "Interface",
        "Interface/menu.txt",
    ]
    assert first.entries[0].size == 0
    assert first.entries[0].sha256 is None
    assert first.entries[0].identity is not None
    assert first.entries[2].identity is not None
    assert first.digest == _directory_contract(first.entries)


def test_manifest_is_frozen_and_converts_entries_to_tuple() -> None:
    manifest = InputManifest(
        source_kind="directory",
        entries=[],
        digest="0" * 64,
        source_identity=None,
    )

    assert manifest.entries == ()
    with pytest.raises(FrozenInstanceError):
        manifest.digest = "1" * 64  # type: ignore[misc]


def test_file_identity_has_the_frozen_public_field_contract() -> None:
    identity = FileIdentity(device=1, inode=2, size=3, mtime_ns=4)

    assert [field.name for field in fields(FileIdentity)] == [
        "device",
        "inode",
        "size",
        "mtime_ns",
    ]
    assert identity == FileIdentity(device=1, inode=2, size=3, mtime_ns=4)
    with pytest.raises(FrozenInstanceError):
        identity.device = 5  # type: ignore[misc]


def test_composite_identity_includes_game_and_source_kind(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example"
    source.mkdir()
    manifest = build_input_manifest(source)

    skyrim = composite_input_identity("skyrim-se", manifest)
    fallout = composite_input_identity("fallout4", manifest)

    assert skyrim == f"smt-input-v1:skyrim-se:directory:{manifest.digest}"
    assert fallout == f"smt-input-v1:fallout4:directory:{manifest.digest}"
    assert skyrim != fallout


def test_directory_paths_are_nfc_posix_and_sorted_by_utf8_bytes(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Unicode"
    source.mkdir()
    decomposed = "e\u0301"
    (source / decomposed).mkdir()
    (source / "z").mkdir()
    (source / decomposed / "a.txt").write_text("a", encoding="utf-8")

    manifest = build_input_manifest(source)

    expected_composed = unicodedata.normalize("NFC", decomposed)
    assert [entry.relative_path for entry in manifest.entries] == [
        "z",
        expected_composed,
        f"{expected_composed}/a.txt",
    ]
    assert [
        entry.relative_path.encode("utf-8") for entry in manifest.entries
    ] == sorted(entry.relative_path.encode("utf-8") for entry in manifest.entries)


def test_casefold_collision_is_rejected_when_filesystem_can_construct_it(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Collision"
    source.mkdir()
    (source / "Straße").mkdir()
    (source / "STRASSE").mkdir(exist_ok=True)
    names = {entry.name for entry in source.iterdir()}
    if names != {"Straße", "STRASSE"}:
        pytest.skip("filesystem cannot construct both casefold-colliding paths")

    with pytest.raises(InputSafetyError, match="case-insensitive path collision"):
        build_input_manifest(source)


@pytest.mark.parametrize(
    "suffix", [".rar", ".esp", ".esm", ".esl", ".bsa", ".ba2", ".txt"]
)
def test_unsupported_top_level_file_types_are_rejected(
    safe_tmp_path: Path,
    suffix: str,
) -> None:
    source = safe_tmp_path / f"Example{suffix}"
    source.write_bytes(b"fixture")

    with pytest.raises(UnsupportedInputError):
        build_input_manifest(source)


@pytest.mark.parametrize("marker", ["SteamLibrary", "ModOrganizer", "Vortex"])
def test_generic_risky_locations_are_rejected_before_reading(
    safe_tmp_path: Path,
    marker: str,
) -> None:
    source = safe_tmp_path / marker / "Example.zip"
    source.parent.mkdir()
    source.write_bytes(b"fixture")

    with pytest.raises(InputSafetyError, match="forbidden"):
        build_input_manifest(source)


def test_profile_specific_risky_location_is_rejected_before_reading(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Fallout 4" / "Data" / "Example.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture")

    with pytest.raises(InputSafetyError, match="Fallout 4"):
        build_input_manifest(source, context=load_game_profile("fallout4"))


@pytest.mark.parametrize("use_explicit_workspace", [False, True])
def test_new_workspace_rejects_profile_specific_risky_destination_before_reservation(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
    use_explicit_workspace: bool,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    risky_root = workspace_tmp_path / "Fallout 4" / "Data"
    state_root = safe_tmp_path / "state"
    request = RunRequest(
        source=source,
        game_id="fallout4",
        workspace=(risky_root / "Workspace") if use_explicit_workspace else None,
        workspace_root=risky_root,
        local_state_root=state_root,
        tool_setup="skip",
        lock_factory=_RecordingLockFactory(),
    )

    with pytest.raises(WorkspaceConflictError, match="Fallout 4|forbidden|risky"):
        resolve_run_workspace(request, build_input_manifest(source))

    assert CliStateStore(state_root).read()["reservations"] == {}
    assert not risky_root.exists()
    assert source.read_bytes() == b"archive"


def test_new_workspace_cannot_be_created_inside_directory_source(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = workspace_tmp_path / "DirectoryMod"
    source.mkdir()
    (source / "plugin.esp").write_bytes(b"plugin")
    workspace = source / "SMT-Workspace"
    state_root = safe_tmp_path / "state"

    with pytest.raises(WorkspaceConflictError, match="overlap|source"):
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                workspace=workspace,
                local_state_root=state_root,
                tool_setup="skip",
                lock_factory=_RecordingLockFactory(),
            ),
            build_input_manifest(source),
        )

    assert not workspace.exists()
    assert (source / "plugin.esp").read_bytes() == b"plugin"
    assert CliStateStore(state_root).read()["reservations"] == {}


def test_new_workspace_rejects_an_existing_reparse_ancestor(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    outside = workspace_tmp_path / "outside"
    outside.mkdir()
    alias = workspace_tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")
    workspace = alias / "Workspace"
    state_root = safe_tmp_path / "state"

    with pytest.raises(WorkspaceConflictError, match="reparse|symlink|junction"):
        resolve_run_workspace(
            RunRequest(
                source=source,
                game_id="skyrim-se",
                workspace=workspace,
                local_state_root=state_root,
                tool_setup="skip",
                lock_factory=_RecordingLockFactory(),
            ),
            build_input_manifest(source),
        )

    assert not (outside / "Workspace").exists()
    assert CliStateStore(state_root).read()["reservations"] == {}


def test_workspace_ancestor_swap_after_reservation_is_blocked_before_initializer(
    safe_tmp_path: Path,
    workspace_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"archive")
    parent = workspace_tmp_path / "candidate-parent"
    parent.mkdir()
    workspace = parent / "Workspace"
    outside = workspace_tmp_path / "outside"
    outside.mkdir()
    initializer_calls: list[Path] = []

    def initializer(target: Path, game_id: str, tool_setup: str) -> None:
        del tool_setup
        initializer_calls.append(target)
        _write_workspace_marker(target, game_id)

    resolution = resolve_run_workspace(
        RunRequest(
            source=source,
            game_id="skyrim-se",
            workspace=workspace,
            local_state_root=safe_tmp_path / "state-parent-swap",
            tool_setup="skip",
            initializer=initializer,
            lock_factory=_RecordingLockFactory(),
        ),
        build_input_manifest(source),
    )
    saved_parent = workspace_tmp_path / "saved-parent"
    parent.rename(saved_parent)
    try:
        parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        saved_parent.rename(parent)
        resolution.close()
        pytest.skip(f"directory symlink creation is unavailable: {exc}")
    try:
        with pytest.raises((WorkspaceConflictError, OSError, ValueError)):
            import_input_transactionally(source, resolution, build_input_manifest(source))
        assert initializer_calls == []
        assert not (outside / "Workspace").exists()
        assert source.read_bytes() == b"archive"
    finally:
        resolution.close()
        parent.unlink()
        saved_parent.rename(parent)


def test_top_level_symlink_is_rejected_without_following_target(
    safe_tmp_path: Path,
) -> None:
    target = safe_tmp_path / "real.zip"
    target.write_bytes(b"archive bytes")
    source = safe_tmp_path / "linked.zip"
    try:
        source.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="symlink|reparse"):
        build_input_manifest(source)

    assert target.read_bytes() == b"archive bytes"


def test_symlink_in_directory_is_rejected_when_supported(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Symlink"
    source.mkdir()
    target = source / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = source / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="symlink|reparse"):
        build_input_manifest(source)


def test_directory_replacement_after_discovery_is_rejected_before_acceptance(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "DirectoryRace"
    child = source / "child"
    child.mkdir(parents=True)
    outside = safe_tmp_path / "outside-empty"
    outside.mkdir()
    real_scandir = os.scandir
    replaced = False

    def replacing_scandir(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]):
        nonlocal replaced
        if Path(path) == child and not replaced:
            replaced = True
            child.rmdir()
            try:
                child.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(
                    f"directory symlink race construction is unavailable: {exc}"
                )
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", replacing_scandir)
    try:
        with pytest.raises(InputSafetyError, match="changed|symlink|reparse"):
            build_input_manifest(source)
    finally:
        if child.is_symlink():
            child.unlink()

    assert replaced


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-specific")
def test_directory_junction_is_rejected_when_supported(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "DirectoryReparse"
    source.mkdir()
    target = safe_tmp_path / "outside-directory"
    target.mkdir()
    (target / "secret.txt").write_text("must not be read", encoding="utf-8")
    junction = source / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"NTFS junction creation is unavailable: {result.stderr or result.stdout}"
        )
    try:
        with pytest.raises(InputSafetyError, match="junction|reparse"):
            build_input_manifest(source)
        assert (target / "secret.txt").read_text(encoding="utf-8") == "must not be read"
    finally:
        os.rmdir(junction)


def test_multiple_hardlinks_in_directory_are_rejected(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Hardlinks"
    source.mkdir()
    original = source / "one.txt"
    original.write_text("same inode", encoding="utf-8")
    try:
        os.link(original, source / "two.txt")
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="hardlinks"):
        build_input_manifest(source)


def test_archive_with_multiple_hardlinks_is_rejected(safe_tmp_path: Path) -> None:
    original = safe_tmp_path / "original.zip"
    original.write_bytes(b"archive")
    linked = safe_tmp_path / "linked.zip"
    try:
        os.link(original, linked)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="hardlinks"):
        build_input_manifest(linked)


def test_non_regular_entry_is_rejected(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Special"
    source.mkdir()
    special_path = source / "non-regular"
    if hasattr(os, "mkfifo"):
        os.mkfifo(special_path)
        with pytest.raises(InputSafetyError, match="non-regular"):
            build_input_manifest(source)
        return
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("neither FIFO nor AF_UNIX filesystem sockets are available")

    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            unix_socket.bind(str(special_path))
        except OSError as exc:
            pytest.skip(f"AF_UNIX filesystem socket creation is unavailable: {exc}")
        with pytest.raises(InputSafetyError, match="non-regular"):
            build_input_manifest(source)
    finally:
        unix_socket.close()
        special_path.unlink(missing_ok=True)


def test_hashing_detects_file_change_during_read(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "Changing"
    source.mkdir()
    file_path = source / "large.bin"
    original_size = 1024 * 1024 + 1
    file_path.write_bytes(b"A" * original_size)
    real_reader = smt_fingerprint._read_file_chunks
    changed = False

    def changing_reader(path: Path):
        nonlocal changed
        for chunk in real_reader(path):
            yield chunk
            if not changed:
                changed = True
                path.write_bytes(b"B" * original_size)

    monkeypatch.setattr(smt_fingerprint, "_read_file_chunks", changing_reader)

    with pytest.raises(InputChangedError, match="changed while hashing"):
        build_input_manifest(source)


def test_archive_hashing_detects_change_during_read(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "Changing.zip"
    original_size = 1024 * 1024 + 1
    source.write_bytes(b"A" * original_size)
    real_reader = smt_fingerprint._read_file_chunks
    changed = False

    def changing_reader(path: Path):
        nonlocal changed
        for chunk in real_reader(path):
            yield chunk
            if not changed:
                changed = True
                path.write_bytes(b"B" * original_size)

    monkeypatch.setattr(smt_fingerprint, "_read_file_chunks", changing_reader)

    with pytest.raises(InputChangedError, match="changed while hashing"):
        build_input_manifest(source)


@pytest.mark.parametrize("change", ["add", "delete", "rename", "type"])
def test_source_rebuild_detects_tree_changes(safe_tmp_path: Path, change: str) -> None:
    source = safe_tmp_path / f"Tree-{change}"
    source.mkdir()
    original = source / "A.txt"
    original.write_text("A", encoding="utf-8")
    manifest = build_input_manifest(source)

    if change == "add":
        (source / "B.txt").write_text("B", encoding="utf-8")
    elif change == "delete":
        original.unlink()
    elif change == "rename":
        original.rename(source / "Renamed.txt")
    else:
        original.unlink()
        original.mkdir()

    with pytest.raises(InputChangedError, match="source input changed"):
        verify_source_unchanged(source, manifest)


def test_source_rebuild_detects_same_length_overwrite_with_restored_mtime(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Overwrite"
    source.mkdir()
    target = source / "A.txt"
    target.write_bytes(b"AAAA")
    manifest = build_input_manifest(source)
    original_stat = target.stat()

    target.write_bytes(b"BBBB")
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert target.stat().st_size == original_stat.st_size
    assert target.stat().st_mtime_ns == original_stat.st_mtime_ns
    with pytest.raises(InputChangedError, match="source input changed"):
        verify_source_unchanged(source, manifest)


def test_verify_imported_directory_rebuilds_manifest(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Source"
    source.mkdir()
    (source / "Empty").mkdir()
    (source / "A.txt").write_text("A", encoding="utf-8")
    manifest = build_input_manifest(source)
    target = safe_tmp_path / ".smt-import.partial"
    shutil.copytree(source, target)

    verify_imported_copy(target, manifest)
    (target / "A.txt").write_text("B", encoding="utf-8")

    with pytest.raises(InputChangedError, match="imported copy changed"):
        verify_imported_copy(target, manifest)


def test_zip_and_suffixless_imported_copy_are_hashed_as_archive(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.ZIP"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Data/file.txt", "hello")

    manifest = build_input_manifest(source)
    target = safe_tmp_path / ".smt-import-123.partial"
    shutil.copyfile(source, target)

    assert manifest.source_kind == "zip"
    assert manifest.entries == ()
    assert manifest.digest == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest.source_identity is not None
    verify_imported_copy(target, manifest)

    target.write_bytes(b"not the same archive")
    with pytest.raises(InputChangedError, match="imported copy changed"):
        verify_imported_copy(target, manifest)


def test_7z_archive_is_supported(safe_tmp_path: Path) -> None:
    py7zr = pytest.importorskip("py7zr")
    payload = safe_tmp_path / "payload.txt"
    payload.write_text("hello", encoding="utf-8")
    source = safe_tmp_path / "Example.7z"
    with py7zr.SevenZipFile(source, "w") as archive:
        archive.write(payload, arcname="payload.txt")

    manifest = build_input_manifest(source)

    assert manifest.source_kind == "7z"
    assert manifest.digest == hashlib.sha256(source.read_bytes()).hexdigest()


def test_archive_source_verification_rehashes_content(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"AAAA")
    manifest = build_input_manifest(source)
    original_stat = source.stat()

    source.write_bytes(b"BBBB")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    with pytest.raises(InputChangedError, match="source input changed"):
        verify_source_unchanged(source, manifest)


def test_mod_and_workspace_names_are_safe_deterministic_and_utf16_bounded() -> None:
    assert derive_mod_name_candidate(Path("A<B>:C?.zip")) == "A_B__C_"
    example_candidate = derive_mod_name_candidate(Path("Example.7z"))
    assert example_candidate == "Example"
    dragon = "\U0001f409"
    emoji_candidate = derive_mod_name_candidate(Path(f"{dragon * 50}.zip"))
    assert emoji_candidate == dragon * 50
    assert _utf16_units(emoji_candidate) == 100

    digest = "0123456789abcdef"
    example_mod_name = finalize_mod_name(example_candidate, digest, source_kind="7z")
    assert example_mod_name == FinalizedModName(
        source_kind="7z",
        value="Example",
        import_name="Example.7z",
        digest_suffix_applied=False,
        digest_prefix=None,
    )
    occupied = {"example", "Example-01234567", "EXAMPLE-01234567-2"}
    assert (
        choose_workspace_name(example_mod_name, digest, occupied)
        == "Example-01234567-3"
    )

    emoji_mod_name = finalize_mod_name(emoji_candidate, digest, source_kind="zip")
    first_workspace = choose_workspace_name(emoji_mod_name, digest, ())
    second_workspace = choose_workspace_name(emoji_mod_name, digest, {first_workspace})
    third_workspace = choose_workspace_name(
        emoji_mod_name,
        digest,
        {first_workspace, second_workspace},
    )
    assert emoji_mod_name.digest_suffix_applied
    assert emoji_mod_name.digest_prefix == "01234567"
    assert emoji_mod_name.value.endswith("-01234567")
    assert first_workspace == emoji_mod_name.value
    assert second_workspace.endswith("-01234567-2")
    assert third_workspace.endswith("-01234567-3")
    assert all(
        _utf16_units(name) <= 80
        for name in (
            emoji_mod_name.value,
            first_workspace,
            second_workspace,
            third_workspace,
        )
    )
    assert emoji_mod_name.import_name == f"{emoji_mod_name.value}.zip"
    assert _utf16_units(emoji_mod_name.import_name) <= 80
    assert (Path("mod") / emoji_mod_name.import_name).name == emoji_mod_name.import_name
    with pytest.raises(FrozenInstanceError):
        emoji_mod_name.value = "changed"  # type: ignore[misc]

    with pytest.raises(ValueError, match="finalized"):
        choose_workspace_name(emoji_candidate, digest, ())


def test_exact_80_unit_workspace_name_is_preserved_when_unoccupied() -> None:
    exact_candidate = "A" * 80
    digest = "01234567" + "0" * 56
    exact_mod_name = finalize_mod_name(exact_candidate, digest, source_kind="directory")

    assert exact_mod_name.value == exact_candidate
    assert exact_mod_name.import_name == exact_candidate
    assert not exact_mod_name.digest_suffix_applied
    assert choose_workspace_name(exact_mod_name, digest, ()) == exact_candidate


def test_natural_digest_suffix_is_not_mistaken_for_truncation_metadata() -> None:
    digest = "01234567" + "0" * 56
    natural_name = finalize_mod_name(
        "Example-01234567",
        digest,
        source_kind="directory",
    )

    assert natural_name.value == "Example-01234567"
    assert not natural_name.digest_suffix_applied
    first_collision = choose_workspace_name(natural_name, digest, {"Example-01234567"})
    assert first_collision == "Example-01234567-01234567"
    assert (
        choose_workspace_name(
            natural_name,
            digest,
            {"Example-01234567", first_collision},
        )
        == "Example-01234567-01234567-2"
    )


def test_truncated_mod_names_use_digest_to_avoid_workspace_name_aliasing() -> None:
    shared_prefix = "A" * 80
    first_display_name = f"{shared_prefix}X"
    second_display_name = f"{shared_prefix}Y"
    first_candidate = derive_mod_name_candidate(Path(f"{first_display_name}.zip"))
    second_candidate = derive_mod_name_candidate(Path(f"{second_display_name}.zip"))

    assert first_candidate == first_display_name
    assert second_candidate == second_display_name
    first_mod_name = finalize_mod_name(
        first_candidate,
        "11111111" + "0" * 56,
        source_kind="zip",
    )
    second_mod_name = finalize_mod_name(
        second_candidate,
        "22222222" + "0" * 56,
        source_kind="zip",
    )
    first_workspace = choose_workspace_name(first_mod_name, "11111111" + "0" * 56, ())
    second_workspace = choose_workspace_name(second_mod_name, "22222222" + "0" * 56, ())
    assert first_mod_name.value == f"{'A' * 67}-11111111"
    assert second_mod_name.value == f"{'A' * 67}-22222222"
    assert first_mod_name.import_name == f"{first_mod_name.value}.zip"
    assert second_mod_name.import_name == f"{second_mod_name.value}.zip"
    assert first_mod_name.digest_suffix_applied
    assert second_mod_name.digest_suffix_applied
    assert first_workspace == first_mod_name.value
    assert second_workspace == second_mod_name.value
    assert first_mod_name.value != second_mod_name.value
    assert _utf16_units(first_mod_name.value) == 76
    assert _utf16_units(second_mod_name.value) == 76
    assert _utf16_units(first_mod_name.import_name) == 80
    assert _utf16_units(second_mod_name.import_name) == 80


@pytest.mark.parametrize(
    ("source_kind", "extension", "expected_value_units"),
    [("zip", ".zip", 76), ("7z", ".7z", 77)],
)
def test_archive_finalization_reserves_extension_within_80_utf16_units(
    source_kind: str,
    extension: str,
    expected_value_units: int,
) -> None:
    digest = "01234567" + "0" * 56
    finalized = finalize_mod_name(
        "A" * 80,
        digest,
        source_kind=source_kind,  # type: ignore[arg-type]
    )

    assert finalized.source_kind == source_kind
    assert finalized.digest_suffix_applied
    assert finalized.value.endswith("-01234567")
    assert finalized.import_name == f"{finalized.value}{extension}"
    assert _utf16_units(finalized.value) == expected_value_units
    assert _utf16_units(finalized.import_name) == 80

    with pytest.raises(ValueError, match="source kind"):
        finalize_mod_name("Example", digest, source_kind="rar")  # type: ignore[arg-type]


def test_smt_windows_imports_without_loading_win32_bindings() -> None:
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "import smt_windows; "
        "print(smt_windows._win32_bindings.cache_info().currsize)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def test_known_folder_calls_fail_closed_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smt_windows._win32_bindings.cache_clear()
    monkeypatch.setattr(smt_windows, "_IS_WINDOWS", False)

    with pytest.raises(ManagedProcessEnvironmentError, match="Windows"):
        get_documents_path()
    with pytest.raises(ManagedProcessEnvironmentError, match="Windows"):
        get_local_app_data_path()
    smt_windows._win32_bindings.cache_clear()


def test_known_folder_failure_always_frees_returned_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freed: list[object] = []

    class FailingShell32:
        @staticmethod
        def SHGetKnownFolderPath(*_args: object) -> int:
            return -1

    class RecordingOle32:
        @staticmethod
        def CoTaskMemFree(pointer: object) -> None:
            freed.append(pointer)

    monkeypatch.setattr(
        smt_windows,
        "_win32_bindings",
        lambda: SimpleNamespace(shell32=FailingShell32(), ole32=RecordingOle32()),
    )

    with pytest.raises(ManagedProcessEnvironmentError, match="Known Folder"):
        smt_windows._known_folder_path("FDD39AD0-238F-46AF-ADB4-6C85480369C7")

    assert len(freed) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Known Folder API is required")
def test_known_folder_api_returns_absolute_windows_paths() -> None:
    documents = get_documents_path()
    local_app_data = get_local_app_data_path()

    assert smt_windows.documents_directory() == documents
    assert smt_windows.local_app_data_directory() == local_app_data
    assert documents.is_absolute()
    assert local_app_data.is_absolute()
    assert documents != local_app_data


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_shared_locks_can_coexist_and_preserve_metadata(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "shared.lock"
    with SmtProcessFileLock(
        lock_path,
        "exclusive",
        timeout_seconds=1.0,
        command="run",
    ):
        pass

    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == os.getpid()
    assert metadata["command"] == "run"

    with SmtProcessFileLock(lock_path, "shared", timeout_seconds=1.0):
        with SmtProcessFileLock(lock_path, "shared", timeout_seconds=1.0):
            assert json.loads(lock_path.read_text(encoding="utf-8")) == metadata


def _run_lock_probe(
    lock_path: Path,
    *,
    exclusive: bool,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    mode = "exclusive" if exclusive else "shared"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "from smt_windows import SmtLockTimeoutError, SmtProcessFileLock; "
        "lock=None; "
        "\ntry:\n"
        f" lock=SmtProcessFileLock({str(lock_path)!r}, {mode!r}, timeout_seconds={timeout!r}); "
        " lock.acquire(); print('acquired')\n"
        "except SmtLockTimeoutError:\n print('timeout'); raise SystemExit(42)\n"
        "finally:\n"
        " if lock is not None: lock.release()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_real_process_shared_and_exclusive_contention(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "cross-process.lock"
    with SmtProcessFileLock(lock_path, "shared", timeout_seconds=1.0):
        shared = _run_lock_probe(lock_path, exclusive=False, timeout=0.5)
        assert shared.returncode == 0, shared.stderr
        assert shared.stdout.strip() == "acquired"

    with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
        exclusive = _run_lock_probe(lock_path, exclusive=True, timeout=0.1)
        assert exclusive.returncode == 42, exclusive.stderr
        assert exclusive.stdout.strip() == "timeout"
        shared = _run_lock_probe(lock_path, exclusive=False, timeout=0.1)
        assert shared.returncode == 42, shared.stderr
        assert shared.stdout.strip() == "timeout"


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_is_released_by_kernel_when_owner_process_exits(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "abandoned-handle.lock"
    ready_path = safe_tmp_path / "owner.ready"
    code = (
        "import pathlib, sys, time; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "from smt_windows import SmtProcessFileLock; "
        f"lock=SmtProcessFileLock({str(lock_path)!r}, 'exclusive', timeout_seconds=1); "
        "lock.acquire(); "
        f"pathlib.Path({str(ready_path)!r}).write_text('ready'); "
        "time.sleep(60)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists()
        owner.kill()
        owner.wait(timeout=5)

        with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
            assert lock_path.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_exclusive_lock_times_out_then_can_be_reacquired(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "exclusive.lock"
    with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
        with pytest.raises(SmtLockTimeoutError):
            with SmtProcessFileLock(
                lock_path,
                "exclusive",
                timeout_seconds=0.1,
            ):
                raise AssertionError("contended exclusive lock must not be acquired")

    assert lock_path.exists()
    with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
        pass


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_rejects_a_symlink_without_modifying_its_target(
    safe_tmp_path: Path,
) -> None:
    victim = safe_tmp_path / "victim.txt"
    victim.write_bytes(b"do not replace")
    lock_path = safe_tmp_path / "linked.lock"
    try:
        lock_path.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"file symlink creation is unavailable: {exc}")

    lock = SmtProcessFileLock(
        lock_path,
        "exclusive",
        timeout_seconds=1.0,
        command="run",
    )
    try:
        with pytest.raises((ManagedProcessEnvironmentError, OSError, ValueError)):
            lock.acquire()
    finally:
        lock.release()

    assert victim.read_bytes() == b"do not replace"


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_rejects_a_hardlink_without_modifying_its_target(
    safe_tmp_path: Path,
) -> None:
    victim = safe_tmp_path / "victim.txt"
    victim.write_bytes(b"do not replace")
    lock_path = safe_tmp_path / "hardlinked.lock"
    try:
        os.link(victim, lock_path)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    lock = SmtProcessFileLock(
        lock_path,
        "exclusive",
        timeout_seconds=1.0,
        command="run",
    )
    try:
        with pytest.raises((ManagedProcessEnvironmentError, OSError, ValueError)):
            lock.acquire()
    finally:
        lock.release()

    assert victim.read_bytes() == b"do not replace"


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_rejects_a_reparse_parent_before_creating_an_external_lock(
    safe_tmp_path: Path,
) -> None:
    outside = safe_tmp_path / "outside"
    outside.mkdir()
    alias = safe_tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    lock = SmtProcessFileLock(
        alias / "operation.lock",
        "exclusive",
        timeout_seconds=1.0,
        command="run",
    )
    try:
        with pytest.raises((ManagedProcessEnvironmentError, OSError, ValueError)):
            lock.acquire()
    finally:
        lock.release()

    assert not (outside / "operation.lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_rejects_a_reparse_ancestor_before_creating_external_parents(
    safe_tmp_path: Path,
) -> None:
    outside = safe_tmp_path / "outside"
    outside.mkdir()
    alias = safe_tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    lock = SmtProcessFileLock(
        alias / "new-parent" / "operation.lock",
        "exclusive",
        timeout_seconds=1.0,
        command="run",
    )
    try:
        with pytest.raises((ManagedProcessEnvironmentError, OSError, ValueError)):
            lock.acquire()
    finally:
        lock.release()

    assert not (outside / "new-parent").exists()


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_different_lock_files_do_not_block_each_other(
    safe_tmp_path: Path,
) -> None:
    first_path = safe_tmp_path / "first.lock"
    second_path = safe_tmp_path / "second.lock"
    with SmtProcessFileLock(first_path, "exclusive", timeout_seconds=1.0):
        second = _run_lock_probe(second_path, exclusive=True, timeout=0.1)
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == "acquired"
        assert first_path.exists()
        assert second_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_can_redirect_stdout_and_stderr(
    safe_tmp_path: Path,
) -> None:
    process = start_managed_process(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
        ],
        cwd=safe_tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    stdout, stderr = process.communicate(timeout_seconds=5)

    assert process.returncode == 0
    assert stdout.strip() == "stdout-line"
    assert stderr.strip() == "stderr-line"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_runner_logs_all_output_and_keeps_only_200_tail_lines(
    safe_tmp_path: Path,
) -> None:
    log_path = safe_tmp_path / "smt-cli.log"
    code = "\n".join(
        [
            "import sys",
            "for value in range(250):",
            "    stream = sys.stderr if value % 2 else sys.stdout",
            "    print(f'line-{value}', file=stream, flush=True)",
        ]
    )

    result = ManagedProcess().run(
        [sys.executable, "-c", code],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        log_path=log_path,
    )

    logged_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert result.exit_code == 0
    assert not result.timed_out
    assert not result.interrupted
    assert len(logged_lines) == 250
    assert len(result.output_tail) == 200
    assert result.output_tail == tuple(logged_lines[-200:])


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_log_is_incremental_before_child_exits(
    safe_tmp_path: Path,
) -> None:
    log_path = safe_tmp_path / "live.log"
    results: list[smt_windows.ProcessResult] = []
    errors: list[BaseException] = []
    finished = threading.Event()

    def run_child() -> None:
        try:
            results.append(
                ManagedProcess().run(
                    [
                        sys.executable,
                        "-c",
                        "import time; print('ready', flush=True); time.sleep(3); print('done', flush=True)",
                    ],
                    cwd=safe_tmp_path,
                    env=os.environ.copy(),
                    timeout_seconds=5,
                    log_path=log_path,
                    output_encoding="utf-8",
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run_child)
    worker.start()
    observed_while_running = False
    deadline = time.monotonic() + 1.5
    try:
        while time.monotonic() < deadline:
            if log_path.exists() and "ready" in log_path.read_text(encoding="utf-8"):
                observed_while_running = not finished.is_set()
                break
            time.sleep(0.02)
        assert observed_while_running
    finally:
        worker.join(timeout=6)

    assert not worker.is_alive()
    assert errors == []
    assert results[0].exit_code == 0
    assert results[0].output_tail == ("ready", "done")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_rejects_explicit_empty_output_encoding_before_spawn(
    safe_tmp_path: Path,
) -> None:
    marker = safe_tmp_path / "must-not-run.txt"

    with pytest.raises(ValueError, match="output encoding"):
        ManagedProcess().run(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ],
            cwd=safe_tmp_path,
            env=os.environ.copy(),
            timeout_seconds=5,
            log_path=safe_tmp_path / "empty-encoding.log",
            output_encoding="",
        )

    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_default_uses_system_cp936_without_utf8_guessing(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = safe_tmp_path / "cp936-default.log"
    monkeypatch.setattr(smt_windows.locale, "getencoding", lambda: "cp936")
    child_code = (
        "import sys; "
        "sys.stdout.buffer.write(bytes([0xC2, 0xA1, 0x0A])); "
        "sys.stdout.buffer.flush()"
    )

    result = ManagedProcess().run(
        [sys.executable, "-c", child_code],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        log_path=log_path,
    )

    assert result.exit_code == 0
    assert result.output_tail == ("隆",)
    assert log_path.read_text(encoding="utf-8").splitlines() == ["隆"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_explicit_utf8_decodes_chinese(
    safe_tmp_path: Path,
) -> None:
    log_path = safe_tmp_path / "explicit-utf8.log"
    text = "中文输出"
    child_code = (
        "import sys; "
        f"sys.stdout.buffer.write({text!r}.encode('utf-8') + b'\\n'); "
        "sys.stdout.buffer.flush()"
    )

    result = ManagedProcess().run(
        [sys.executable, "-c", child_code],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        log_path=log_path,
        output_encoding="utf-8",
    )

    assert result.exit_code == 0
    assert result.output_tail == (text,)
    assert log_path.read_text(encoding="utf-8").splitlines() == [text]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_utf16le_decodes_before_splitting_lines(
    safe_tmp_path: Path,
) -> None:
    log_path = safe_tmp_path / "explicit-utf16le.log"
    child_code = (
        "import sys; "
        "sys.stdout.buffer.write('中\\n'.encode('utf-16le')); "
        "sys.stdout.buffer.flush()"
    )

    result = ManagedProcess().run(
        [sys.executable, "-c", child_code],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        log_path=log_path,
        output_encoding="utf-16le",
    )

    assert result.exit_code == 0
    assert result.output_tail == ("中",)
    assert "�" not in log_path.read_text(encoding="utf-8")
    assert log_path.read_text(encoding="utf-8").splitlines() == ["中"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_incremental_decoder_handles_multibyte_chunk_boundary(
    safe_tmp_path: Path,
) -> None:
    log_path = safe_tmp_path / "chunk-boundary.log"
    prefix_length = smt_windows._OUTPUT_READ_CHUNK_SIZE - 1
    child_code = (
        "import sys; "
        f"sys.stdout.buffer.write(b'A' * {prefix_length} + '中\\n'.encode('utf-8')); "
        "sys.stdout.buffer.flush()"
    )

    result = ManagedProcess().run(
        [sys.executable, "-c", child_code],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        log_path=log_path,
        output_encoding="utf-8",
    )

    expected = "A" * prefix_length + "中"
    assert result.exit_code == 0
    assert result.output_tail == (expected,)
    assert log_path.read_text(encoding="utf-8").splitlines() == [expected]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_runner_replaces_undecodable_diagnostics(
    safe_tmp_path: Path,
) -> None:
    log_path = safe_tmp_path / "replacement.log"
    child_code = (
        "import sys; "
        "sys.stdout.buffer.write(bytes([255, 255, 10])); "
        "sys.stdout.buffer.flush()"
    )

    result = ManagedProcess().run(
        [sys.executable, "-c", child_code],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        log_path=log_path,
        output_encoding="ascii",
    )

    assert result.exit_code == 0
    assert result.output_tail == ("��",)
    assert log_path.read_text(encoding="utf-8").splitlines() == ["��"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_reader_start_failure_cleans_entire_tree_and_handles(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = safe_tmp_path / "reader-failure.pids"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(pid_path)!r}).write_text(f'{{__import__(\"os\").getpid()}} {{child.pid}}'); "
        "time.sleep(60)"
    )
    real_start_managed_process = smt_windows.start_managed_process
    captured: list[smt_windows.SmtManagedProcess] = []

    def recording_start(
        *args: object, **kwargs: object
    ) -> smt_windows.SmtManagedProcess:
        process = real_start_managed_process(*args, **kwargs)
        captured.append(process)
        return process

    class FailingReaderThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            deadline = time.monotonic() + 5.0
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert pid_path.exists()
            raise RuntimeError("forced reader start failure")

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(smt_windows, "start_managed_process", recording_start)
    monkeypatch.setattr(smt_windows.threading, "Thread", FailingReaderThread)

    with pytest.raises(RuntimeError, match="forced reader start failure"):
        ManagedProcess().run(
            [sys.executable, "-c", parent_code],
            cwd=safe_tmp_path,
            env=os.environ.copy(),
            timeout_seconds=5,
            log_path=safe_tmp_path / "reader-failure.log",
        )

    parent_pid, child_pid = map(
        int,
        pid_path.read_text(encoding="utf-8").split(),
    )
    assert not smt_windows.is_process_running(parent_pid)
    assert not smt_windows.is_process_running(child_pid)
    assert captured[0]._job_handle is None
    assert captured[0]._process._handle.closed  # type: ignore[attr-defined]
    assert captured[0].stdout is not None and captured[0].stdout.closed
    captured[0].close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_runner_projects_timeout_as_124(
    safe_tmp_path: Path,
) -> None:
    result = ManagedProcess().run(
        [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(60)",
        ],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=0.2,
        log_path=safe_tmp_path / "timeout.log",
    )

    assert result.exit_code == 124
    assert result.timed_out
    assert not result.interrupted
    assert result.output_tail == ("started",)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_timeout_terminates_descendant_tree(
    safe_tmp_path: Path,
) -> None:
    child_pid_path = safe_tmp_path / "child.pid"
    grandchild_code = "import time; time.sleep(60)"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    process = start_managed_process(
        [sys.executable, "-c", parent_code],
        cwd=safe_tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    with pytest.raises(ManagedProcessTimeoutError):
        process.communicate(timeout_seconds=0.5)

    assert child_pid_path.exists()
    descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert not smt_windows.is_process_running(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_interrupt_closes_descendant_tree(
    safe_tmp_path: Path,
) -> None:
    child_pid_path = safe_tmp_path / "interrupt-child.pid"
    grandchild_code = "import time; time.sleep(60)"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    process = start_managed_process(
        [sys.executable, "-c", parent_code],
        cwd=safe_tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    deadline = time.monotonic() + 5.0
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid_path.exists()
    descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))

    process.interrupt_tree(grace_seconds=0.1)

    assert process.poll() is not None
    assert not smt_windows.is_process_running(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended processes are required")
def test_job_assignment_failure_never_executes_child_body(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = safe_tmp_path / "must-not-exist.txt"
    monkeypatch.setattr(
        smt_windows,
        "_assign_process_to_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("forced assignment failure")
        ),
    )

    with pytest.raises(ManagedProcessEnvironmentError, match="assign"):
        start_managed_process(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ],
            cwd=safe_tmp_path,
        )

    assert not marker.exists()

"""Simple project-local lock used by workflow writers."""

import atexit
import ctypes
from ctypes import wintypes
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path


LOCK_TOKEN_ENV = "SKYRIM_TRANSLATION_WORKFLOW_LOCK_TOKEN"
LOCK_PATH_ENV = "SKYRIM_TRANSLATION_WORKFLOW_LOCK_PATH"
RESOURCE_LOCKS_ENV = "SKYRIM_TRANSLATION_RESOURCE_LOCKS"
INVALID_LOCK_GRACE_SECONDS = 30.0


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            return error != 87
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def lock_is_stale(path: Path) -> bool:
    payload = read_lock_payload(path)
    try:
        pid = int(payload.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid:
        return not process_is_alive(pid)
    try:
        age_seconds = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return False
    return age_seconds >= INVALID_LOCK_GRACE_SECONDS


def remove_stale_lock(path: Path) -> bool:
    if not path.is_file() or not lock_is_stale(path):
        return False
    before = read_lock_payload(path)
    before_token = str(before.get("token", ""))
    current = read_lock_payload(path)
    if before_token and str(current.get("token", "")) != before_token:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def safe_lock_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "unnamed"


def _resource_lock_registry() -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(os.environ.get(RESOURCE_LOCKS_ENV, "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    registry: dict[str, dict[str, str]] = {}
    for resource, entry in payload.items():
        if not isinstance(resource, str) or not isinstance(entry, dict):
            continue
        path = entry.get("path")
        token = entry.get("token")
        if isinstance(path, str) and path and isinstance(token, str) and token:
            registry[resource] = {"path": path, "token": token}
    return registry


class ResourceLock:
    """Project-local file lock for a named resource.

    These locks are intentionally independent from WorkflowLock. They allow the
    task scheduler to serialize a single Mod or shared resource while leaving
    unrelated Mod work free to run in parallel.
    """

    def __init__(self, project_root: Path, resource: str, owner: str):
        self.project_root = project_root.resolve(strict=False)
        self.resource = resource
        self.owner = owner
        self.path = self.project_root / "work" / "locks" / f"{safe_lock_name(resource)}.lock"
        self.token = str(uuid.uuid4())
        self.acquired = False
        self.reentrant = False

    def acquire(self, *, timeout_seconds: float = 0.0, poll_seconds: float = 0.05) -> "ResourceLock":
        inherited = _resource_lock_registry().get(self.resource)
        if inherited is not None:
            inherited_path = Path(inherited["path"]).resolve(strict=False)
            payload = read_lock_payload(self.path)
            if (
                inherited_path == self.path.resolve(strict=False)
                and payload.get("resource") == self.resource
                and payload.get("token") == inherited["token"]
            ):
                self.reentrant = True
                return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "owner": self.owner,
            "resource": self.resource,
            "pid": os.getpid(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "token": self.token,
        }
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        poll_interval = max(0.01, poll_seconds)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if remove_stale_lock(self.path):
                    continue
                if time.monotonic() >= deadline:
                    detail = ""
                    if self.path.is_file():
                        try:
                            detail = self.path.read_text(encoding="utf-8-sig").strip()
                        except OSError:
                            detail = "unable to read existing resource lock file"
                    raise RuntimeError(f"Resource lock is already held: {self.path}. {detail}") from exc
                time.sleep(poll_interval)

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        self.acquired = True
        atexit.register(self.release)
        return self

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                except json.JSONDecodeError:
                    data = {}
                if data.get("token") == self.token:
                    self.path.unlink()
        finally:
            self.acquired = False


def resource_lock_environment(locks: tuple[ResourceLock, ...] | list[ResourceLock]) -> str:
    """Serialize only the locks owned by one child task for verified reentry."""
    registry: dict[str, dict[str, str]] = {}
    for lock in locks:
        if not lock.acquired:
            raise ValueError(
                f"Cannot delegate an unowned resource lock: {lock.resource}"
            )
        registry[lock.resource] = {
            "path": str(lock.path.resolve(strict=False)),
            "token": lock.token,
        }
    return json.dumps(registry, ensure_ascii=False, sort_keys=True)


class WorkflowLock:
    def __init__(self, project_root: Path, owner: str):
        self.project_root = project_root.resolve(strict=False)
        self.owner = owner
        self.path = self.project_root / "work" / ".workflow.lock"
        self.token = str(uuid.uuid4())
        self.acquired = False
        self.reentrant = False

    def acquire(self) -> "WorkflowLock":
        if os.environ.get(LOCK_TOKEN_ENV):
            self.reentrant = True
            return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "owner": self.owner,
            "pid": os.getpid(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "token": self.token,
        }
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if remove_stale_lock(self.path):
                    continue
                detail = ""
                if self.path.is_file():
                    try:
                        detail = self.path.read_text(encoding="utf-8-sig").strip()
                    except OSError:
                        detail = "unable to read existing lock file"
                raise RuntimeError(f"Workflow lock is already held: {self.path}. {detail}") from exc

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        os.environ[LOCK_TOKEN_ENV] = self.token
        os.environ[LOCK_PATH_ENV] = str(self.path)
        self.acquired = True
        atexit.register(self.release)
        return self

    def release(self) -> None:
        if not self.acquired or self.reentrant:
            return
        try:
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                except json.JSONDecodeError:
                    data = {}
                if data.get("token") == self.token:
                    self.path.unlink()
        finally:
            if os.environ.get(LOCK_TOKEN_ENV) == self.token:
                os.environ.pop(LOCK_TOKEN_ENV, None)
                os.environ.pop(LOCK_PATH_ENV, None)
            self.acquired = False

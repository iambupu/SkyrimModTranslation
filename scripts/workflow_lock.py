"""Simple project-local lock used by workflow writers."""

import atexit
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path


LOCK_TOKEN_ENV = "SKYRIM_TRANSLATION_WORKFLOW_LOCK_TOKEN"
LOCK_PATH_ENV = "SKYRIM_TRANSLATION_WORKFLOW_LOCK_PATH"


def safe_lock_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "unnamed"


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

    def acquire(self, *, timeout_seconds: float = 0.0, poll_seconds: float = 0.05) -> "ResourceLock":
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
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
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

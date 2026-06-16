import atexit
import json
import os
import uuid
from datetime import datetime
from pathlib import Path


LOCK_TOKEN_ENV = "SKYRIM_TRANSLATION_WORKFLOW_LOCK_TOKEN"
LOCK_PATH_ENV = "SKYRIM_TRANSLATION_WORKFLOW_LOCK_PATH"


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

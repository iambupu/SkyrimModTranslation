"""Local JSONL trace helpers for Skyrim translation workflow scripts.

Trace files are developer diagnostics. They are not user progress and do not
replace workflow state, QA gates, or provenance evidence.
"""

from __future__ import annotations

import argparse
import atexit
import contextvars
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from model_review_contract import read_jsonl_objects
from project_paths import is_under, project_root, resolve_project_path
from report_utils import markdown_cell
from report_utils import utc_now


RUN_ID_ENV = "SKYRIM_CHS_RUN_ID"
TRACE_CHILD_ENV = "SKYRIM_CHS_TRACE_CHILD"
_current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("skyrim_chs_run_id", default="")
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("skyrim_chs_parent_span_id", default=None)
_current_root: contextvars.ContextVar[str] = contextvars.ContextVar("skyrim_chs_trace_root", default="")
_run_start_time: contextvars.ContextVar[str] = contextvars.ContextVar("skyrim_chs_run_start_time", default="")
_run_start_monotonic: contextvars.ContextVar[float] = contextvars.ContextVar("skyrim_chs_run_start_monotonic", default=0.0)
_atexit_registered = False
_run_finished = False



def new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def trace_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    configured = _current_root.get()
    if configured:
        return Path(configured)
    return project_root()


def trace_path(root: Path | None = None) -> Path:
    actual_root = trace_root(root)
    path = resolve_project_path(actual_root, "traces/latest.jsonl", must_exist=False)
    if not is_under(path, actual_root):
        raise ValueError("Trace path escaped project root.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def safe_artifact_path(root: Path, value: str | Path) -> str:
    text = str(value).strip()
    if not text:
        return ""
    try:
        path = resolve_project_path(root, text, must_exist=False)
    except ValueError:
        return ""
    try:
        return str(path.resolve(strict=False).relative_to(root.resolve(strict=False)))
    except ValueError:
        return ""


def safe_artifacts(root: Path, values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        artifact = safe_artifact_path(root, value)
        if artifact and artifact not in result:
            result.append(artifact)
    return result


def latest_trace_run_id(root: Path) -> str:
    for row in read_jsonl_objects(trace_path(root)):
        run_id = str(row.get("run_id", "")).strip()
        if run_id:
            return run_id
    return ""


def current_run_id(root: Path | None = None, *, reuse_latest: bool = False) -> str:
    value = _current_run_id.get() or os.environ.get(RUN_ID_ENV, "").strip()
    if value:
        return value
    if reuse_latest:
        existing = latest_trace_run_id(trace_root(root))
        if existing:
            _current_run_id.set(existing)
            os.environ[RUN_ID_ENV] = existing
            return existing
    value = new_run_id()
    _current_run_id.set(value)
    os.environ[RUN_ID_ENV] = value
    return value


def has_active_run() -> bool:
    return bool(_current_run_id.get() or os.environ.get(RUN_ID_ENV, "").strip())


def ensure_trace_run(root: Path | None = None) -> None:
    env_run_id = os.environ.get(RUN_ID_ENV, "").strip()
    if not has_active_run():
        start_trace_run(root)
        return
    if env_run_id and os.environ.get(TRACE_CHILD_ENV, "").strip() == "1":
        actual_root = trace_root(root)
        if latest_trace_run_id(actual_root) != env_run_id:
            start_trace_run(actual_root)


def start_trace_run(root: Path | None = None, run_id: str = "", *, reset: bool = True, finish_at_exit: bool = True) -> str:
    global _atexit_registered, _run_finished
    actual_root = trace_root(root)
    env_run_id = os.environ.get(RUN_ID_ENV, "").strip()
    resolved_run_id = run_id.strip() or env_run_id or new_run_id()
    append_parent_trace = bool(env_run_id and not run_id.strip() and os.environ.get(TRACE_CHILD_ENV, "").strip() == "1")
    existing_run_id = latest_trace_run_id(actual_root) if append_parent_trace else ""
    _current_run_id.set(resolved_run_id)
    _current_root.set(str(actual_root))
    _run_start_time.set(utc_now())
    _run_start_monotonic.set(time.perf_counter())
    _run_finished = False
    os.environ[RUN_ID_ENV] = resolved_run_id
    path = trace_path(actual_root)
    parent_trace_exists = append_parent_trace and existing_run_id == resolved_run_id
    if reset and not parent_trace_exists:
        path.write_text("", encoding="utf-8")
    if not parent_trace_exists:
        write_trace_record(
            {
                "run_id": resolved_run_id,
                "span_id": "run",
                "parent_span_id": None,
                "name": "workflow.run.start",
                "stage": "started",
                "status": "running",
                "start_time": utc_now(),
                "end_time": "",
                "duration_ms": 0,
                "attributes": {},
                "artifacts": [],
                "errors": [],
            },
            actual_root,
        )
    if finish_at_exit and not parent_trace_exists and not _atexit_registered:
        atexit.register(finish_trace_run)
        _atexit_registered = True
    return resolved_run_id


def finish_trace_run() -> None:
    global _run_finished
    if _run_finished:
        return
    run_id = _current_run_id.get()
    root_text = _current_root.get()
    started = _run_start_time.get()
    start_monotonic = _run_start_monotonic.get()
    if not run_id or not root_text or not started or start_monotonic <= 0:
        return
    root = Path(root_text)
    duration_ms = int((time.perf_counter() - start_monotonic) * 1000)
    status = "ok"
    try:
        statuses = [str(row.get("status", "")).strip() for row in read_trace_rows(root)]
        if "error" in statuses:
            status = "error"
        elif "blocked" in statuses:
            status = "blocked"
        elif "warning" in statuses:
            status = "warning"
        write_trace_record(
            {
                "run_id": run_id,
                "span_id": "run",
                "parent_span_id": None,
                "name": "workflow.run",
                "stage": "finished",
                "status": status,
                "start_time": started,
                "end_time": utc_now(),
                "duration_ms": max(0, duration_ms),
                "attributes": {},
                "artifacts": ["traces/latest.jsonl"],
                "errors": [],
            },
            root,
        )
        generate_trace_summary(root)
        _run_finished = True
    except Exception:
        return


def write_trace_record(record: dict[str, Any], root: Path | None = None) -> None:
    path = trace_path(root)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


@dataclass
class TraceSpan:
    name: str
    stage: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    status_on_success: str = "ok"
    root: Path | None = None
    span_id: str = field(default_factory=lambda: f"s{uuid.uuid4().hex[:12]}")
    parent_span_id: str | None = None
    start_time_text: str = ""
    start_monotonic: float = 0.0
    errors: list[str] = field(default_factory=list)
    _token: contextvars.Token[str | None] | None = None

    def __enter__(self) -> "TraceSpan":
        ensure_trace_run(self.root)
        self.parent_span_id = _current_span_id.get()
        self.start_time_text = utc_now()
        self.start_monotonic = time.perf_counter()
        self._token = _current_span_id.set(self.span_id)
        write_trace_record(self._record("running", end_time="", duration_ms=0), self.root)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        status = self.status_on_success
        if exc is not None:
            status = "error"
            self.error(str(exc))
        duration_ms = int((time.perf_counter() - self.start_monotonic) * 1000)
        write_trace_record(self._record(status, end_time=utc_now(), duration_ms=duration_ms), self.root)
        if self._token is not None:
            _current_span_id.reset(self._token)
        generate_trace_summary(trace_root(self.root))
        return False

    def add_artifact(self, artifact: str | Path) -> None:
        value = str(artifact)
        if value and value not in self.artifacts:
            self.artifacts.append(value)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def error(self, message: str) -> None:
        if message and message not in self.errors:
            self.errors.append(message)

    def _record(self, status: str, *, end_time: str, duration_ms: int) -> dict[str, Any]:
        root = trace_root(self.root)
        return {
            "run_id": current_run_id(root),
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "stage": self.stage,
            "status": status,
            "start_time": self.start_time_text,
            "end_time": end_time,
            "duration_ms": duration_ms,
            "attributes": self.attributes,
            "artifacts": safe_artifacts(root, self.artifacts),
            "errors": self.errors,
        }


def trace_span(
    name: str,
    *,
    stage: str = "",
    attributes: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
    status_on_success: str = "ok",
    root: Path | None = None,
) -> TraceSpan:
    return TraceSpan(
        name=name,
        stage=stage,
        attributes=dict(attributes or {}),
        artifacts=list(artifacts or []),
        status_on_success=status_on_success,
        root=root,
    )


def read_trace_rows(root: Path) -> list[dict[str, Any]]:
    return read_jsonl_objects(trace_path(root))



def generate_trace_summary(root: Path | None = None) -> Path:
    actual_root = trace_root(root)
    rows = [row for row in read_trace_rows(actual_root) if str(row.get("end_time", "")).strip()]
    path = resolve_project_path(actual_root, "traces/trace_summary.md", must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Trace Summary",
        "",
        "| Span | Stage | Status | Duration | Artifacts | Errors |",
        "|---|---|---|---:|---|---|",
    ]
    if not rows:
        lines.append("| | | | 0 ms | | No completed spans recorded. |")
    else:
        for row in rows:
            artifacts = row.get("artifacts", [])
            errors = row.get("errors", [])
            artifact_text = ", ".join(str(item) for item in artifacts) if isinstance(artifacts, list) else ""
            error_text = ", ".join(str(item) for item in errors) if isinstance(errors, list) else ""
            lines.append(
                f"| {markdown_cell(row.get('name', ''))} | {markdown_cell(row.get('stage', ''))} | "
                f"{markdown_cell(row.get('status', ''))} | {int(row.get('duration_ms', 0) or 0)} ms | "
                f"{markdown_cell(artifact_text)} | {markdown_cell(error_text)} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage local Bethesda Mod CHS workflow trace files.")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start a new trace file")
    start.add_argument("--run-id", default="")

    span = sub.add_parser("span", help="append one completed span record")
    span.add_argument("--name", required=True)
    span.add_argument("--stage", default="")
    span.add_argument("--status", default="ok", choices=["running", "ok", "warning", "error", "blocked", "skipped"])
    span.add_argument("--duration-ms", type=int, default=0)
    span.add_argument("--artifact", action="append", default=[])
    span.add_argument("--error", action="append", default=[])

    sub.add_parser("summary", help="write traces/trace_summary.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = project_root()
    if args.command == "start":
        run_id = start_trace_run(root, args.run_id, reset=True, finish_at_exit=False)
        print(f"Trace started: {run_id}")
        print("Trace file: traces/latest.jsonl")
        return 0
    if args.command == "span":
        now = utc_now()
        record = {
            "run_id": current_run_id(root, reuse_latest=True),
            "span_id": f"s{uuid.uuid4().hex[:12]}",
            "parent_span_id": None,
            "name": args.name,
            "stage": args.stage,
            "status": args.status,
            "start_time": now,
            "end_time": now,
            "duration_ms": max(0, args.duration_ms),
            "attributes": {},
            "artifacts": safe_artifacts(root, [str(item) for item in args.artifact]),
            "errors": [str(item) for item in args.error],
        }
        write_trace_record(record, root)
    summary = generate_trace_summary(root)
    print(f"Trace summary written to: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

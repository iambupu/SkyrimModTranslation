"""Record model task workload without estimating hidden or provider context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from file_utils import create_regular_directory_under, validate_regular_path_under
from project_paths import project_root, relative_posix_path, resolve_project_path
from workflow_lock import ResourceLock


SCHEMA_VERSION = 1
MODEL_STAGES = (
    "translation",
    "initial_review",
    "final_text_review",
    "final_binary_review",
    "model_recovery",
)
STATUSES = ("completed", "failed", "cancelled")
STAGE_LABELS = {
    "translation": "正文翻译",
    "initial_review": "初次模型审查",
    "final_text_review": "最终文本审查",
    "final_binary_review": "最终二进制复核",
    "model_recovery": "模型恢复",
}
PENDING_RELATIVE_DIR = Path(".workflow") / "model_usage_pending"
RETIRED_RELATIVE_DIR = Path(".workflow") / "model_usage_retired"
LOG_RELATIVE_PATH = Path("qa") / "model_usage.jsonl"
LOG_RESOURCE = "qa:model-usage-log"
USAGE_ID_RE = re.compile(r"\Amodel-[A-Za-z0-9][A-Za-z0-9._-]{0,126}\Z")
_MODEL_USAGE_PROCESS_LOCK = threading.RLock()


class ModelUsageError(ValueError):
    """Raised for invalid model usage input or unavailable pending evidence."""


class ModelUsageUnavailable(ModelUsageError):
    """Raised when auxiliary usage persistence is temporarily unavailable."""


@dataclass(frozen=True)
class RecordResult:
    recorded: bool
    already_recorded: bool
    usage_id: str
    warnings: tuple[str, ...] = ()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def default_usage_id(stage: str) -> str:
    short_stage = stage.replace("_", "-")
    return f"model-{short_stage}-{uuid.uuid4().hex[:8]}"


def _require_nonnegative_optional(value: int | None, field: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{field} must be a non-negative integer or null")


def _validate_usage_id(usage_id: str) -> str:
    value = usage_id.strip()
    if USAGE_ID_RE.fullmatch(value) is None:
        raise ModelUsageError(f"invalid usage_id: {usage_id!r}")
    return value


def _pending_dir(root: Path) -> Path:
    return root.resolve(strict=False) / PENDING_RELATIVE_DIR


def _pending_path(root: Path, usage_id: str) -> Path:
    return _pending_dir(root) / f"{_validate_usage_id(usage_id)}.json"


def _retired_dir(root: Path) -> Path:
    return root.resolve(strict=False) / RETIRED_RELATIVE_DIR


def _retired_path(root: Path, usage_id: str) -> Path:
    return _retired_dir(root) / f"{_validate_usage_id(usage_id)}.json"


def _log_path(root: Path) -> Path:
    return root.resolve(strict=False) / LOG_RELATIVE_PATH


def _safe_directory(
    root: Path,
    path: Path,
    *,
    label: str,
    create: bool,
) -> Path:
    try:
        if create:
            return create_regular_directory_under(path, root, label=label)
        return validate_regular_path_under(
            path,
            root,
            kind="directory",
            label=label,
        )
    except (OSError, ValueError) as exc:
        raise ModelUsageError(str(exc)) from exc


def _safe_file(root: Path, path: Path, *, label: str) -> Path:
    try:
        return validate_regular_path_under(
            path,
            root,
            kind="file",
            label=label,
        )
    except (OSError, ValueError) as exc:
        raise ModelUsageError(str(exc)) from exc


def _valid_pending_payload(
    payload: object,
    *,
    expected_usage_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    usage_id = payload.get("usage_id")
    if not isinstance(usage_id, str) or USAGE_ID_RE.fullmatch(usage_id) is None:
        return None
    if expected_usage_id is not None and usage_id != expected_usage_id:
        return None
    required_strings = (
        "created_at",
        "mod_name",
        "task_id",
        "stage",
        "input_path",
        "input_sha256",
    )
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if any(
        not isinstance(payload.get(field), str) or not str(payload.get(field)).strip()
        for field in required_strings
    ):
        return None
    if payload.get("stage") not in MODEL_STAGES:
        return None
    input_sha256 = str(payload.get("input_sha256", ""))
    if re.fullmatch(r"[0-9a-f]{64}", input_sha256) is None:
        return None
    for field in ("input_bytes", "input_characters"):
        value = payload.get(field)
        if type(value) is not int or value < 0:
            return None
    input_paths = payload.get("input_paths")
    if input_paths is not None and (
        not isinstance(input_paths, list)
        or not input_paths
        or any(
            not isinstance(value, str) or not value.strip()
            for value in input_paths
        )
    ):
        return None
    review_groups = payload.get("review_groups")
    if review_groups is not None and (
        type(review_groups) is not int or review_groups < 0
    ):
        return None
    workflow_blocking = payload.get("workflow_blocking", True)
    if type(workflow_blocking) is not bool:
        return None
    return payload


def _valid_usage_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    required_strings = (
        "timestamp",
        "usage_id",
        "mod_name",
        "task_id",
        "stage",
        "status",
        "tool",
        "input_sha256",
    )
    if any(
        not isinstance(payload.get(field), str) or not str(payload.get(field)).strip()
        for field in required_strings
    ):
        return None
    usage_id = str(payload["usage_id"])
    if USAGE_ID_RE.fullmatch(usage_id) is None:
        return None
    if payload.get("stage") not in MODEL_STAGES or payload.get("status") not in STATUSES:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", str(payload.get("input_sha256", ""))) is None:
        return None
    for field in ("input_bytes", "input_characters"):
        value = payload.get(field)
        if type(value) is not int or value < 0:
            return None
    for field in (
        "output_bytes",
        "review_groups",
        "input_tokens",
        "output_tokens",
    ):
        value = payload.get(field)
        if value is not None and (type(value) is not int or value < 0):
            return None
    if payload.get("status") == "completed" and payload.get("output_bytes") is None:
        return None
    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        return None
    measurement = payload.get("token_measurement")
    input_tokens = payload.get("input_tokens")
    output_tokens = payload.get("output_tokens")
    if measurement is None:
        if input_tokens is not None or output_tokens is not None:
            return None
    elif measurement == "provider_reported":
        if type(input_tokens) is not int or type(output_tokens) is not int:
            return None
    else:
        return None
    return payload


def read_pending(root: Path, usage_id: str) -> dict[str, Any]:
    path = _pending_path(root, usage_id)
    try:
        path = _safe_file(root, path, label="Model usage pending")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModelUsageError(f"pending does not exist for usage_id: {usage_id}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ModelUsageError) as exc:
        raise ModelUsageError(f"pending is unreadable for usage_id {usage_id}: {exc}") from exc
    validated = _valid_pending_payload(payload, expected_usage_id=usage_id)
    if validated is None:
        raise ModelUsageError(f"pending is invalid for usage_id: {usage_id}")
    return validated


def read_pending_records(root: Path) -> tuple[list[dict[str, Any]], int]:
    directory = _pending_dir(root)
    if not os.path.lexists(directory):
        return [], 0
    directory = _safe_directory(
        root,
        directory,
        label="Model usage pending directory",
        create=False,
    )
    rows: list[dict[str, Any]] = []
    damaged = 0
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name.casefold()):
        if USAGE_ID_RE.fullmatch(path.stem) is None:
            damaged += 1
            continue
        try:
            path = _safe_file(root, path, label="Model usage pending")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            damaged += 1
            continue
        validated = _valid_pending_payload(payload, expected_usage_id=path.stem)
        if validated is None:
            damaged += 1
            continue
        rows.append(validated)
    return rows, damaged


def _write_pending_atomic(path: Path, payload: dict[str, Any]) -> None:
    root = path.parents[2]
    _safe_directory(
        root,
        path.parent,
        label="Model usage pending directory",
        create=True,
    )
    if os.path.lexists(path):
        _safe_file(root, path, label="Model usage pending")
        raise FileExistsError(path)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_state_atomic(path: Path, payload: dict[str, Any]) -> None:
    root = path.parents[2]
    _safe_directory(
        root,
        path.parent,
        label="Model usage state directory",
        create=True,
    )
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _retire_pending_from_workflow(root: Path, usage_id: str) -> None:
    path = _retired_path(root, usage_id)
    if os.path.lexists(path):
        _safe_file(root, path, label="Model usage retired tombstone")
        return
    payload = {
        "schema_version": SCHEMA_VERSION,
        "usage_id": usage_id,
        "retired_at": utc_timestamp(),
    }
    try:
        _write_state_atomic(path, payload)
    except (OSError, ModelUsageError) as exc:
        raise ModelUsageUnavailable(
            f"model usage pending could not be retired after persistence failure: {exc}"
        ) from exc


def retired_usage_ids(root: Path) -> set[str]:
    directory = _retired_dir(root)
    if not os.path.lexists(directory):
        return set()
    directory = _safe_directory(
        root,
        directory,
        label="Model usage retired directory",
        create=False,
    )
    retired: set[str] = set()
    for path in directory.glob("*.json"):
        if USAGE_ID_RE.fullmatch(path.stem) is None:
            continue
        path = _safe_file(root, path, label="Model usage retired tombstone")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("usage_id") != path.stem:
            continue
        retired.add(path.stem)
    return retired


def _measure_input_packets(
    root: Path,
    *,
    input_path: str | Path,
    input_paths: Iterable[str | Path] | None,
) -> tuple[list[str], str, int, int]:
    primary = resolve_project_path(root, input_path, must_exist=True)
    raw_input_paths = list(input_paths) if input_paths is not None else [input_path]
    if not raw_input_paths:
        raise ValueError("input_paths cannot be empty")
    packets: list[Path] = []
    raw_packets: list[bytes] = []
    relative_packets: list[str] = []
    for value in raw_input_paths:
        packet = resolve_project_path(root, value, must_exist=True)
        packet = validate_regular_path_under(
            packet,
            root,
            kind="file",
            label="Model usage input packet",
        )
        raw = packet.read_bytes()
        packets.append(packet)
        raw_packets.append(raw)
        relative_packets.append(relative_posix_path(root, packet))
    if primary not in packets:
        raise ValueError("input_path must be included in input_paths")
    primary_raw = raw_packets[packets.index(primary)]
    try:
        characters = len(primary_raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"model usage input packet is not UTF-8: {input_path}"
        ) from exc
    if len(raw_packets) == 1:
        digest = hashlib.sha256(raw_packets[0]).hexdigest()
    else:
        aggregate = hashlib.sha256()
        for relative, raw in zip(relative_packets, raw_packets, strict=True):
            encoded_path = relative.encode("utf-8")
            aggregate.update(len(encoded_path).to_bytes(8, "big"))
            aggregate.update(encoded_path)
            aggregate.update(len(raw).to_bytes(8, "big"))
            aggregate.update(raw)
        digest = aggregate.hexdigest()
    return relative_packets, digest, sum(len(raw) for raw in raw_packets), characters


def create_pending(
    root: Path,
    *,
    mod_name: str,
    task_id: str,
    stage: str,
    input_path: str | Path,
    input_paths: Iterable[str | Path] | None = None,
    review_groups: int | None = None,
    usage_id_factory: Callable[[], str] | None = None,
    warning_sink: Callable[[str], None] | None = None,
    reuse_existing: bool = True,
    _suppress_completed: bool = False,
) -> str | None:
    """Issue a model attempt, reusing only an identical unfinished attempt."""

    root = root.resolve(strict=True)
    if stage not in MODEL_STAGES:
        raise ValueError(f"stage must be one of {', '.join(MODEL_STAGES)}")
    if not mod_name.strip():
        raise ValueError("mod_name cannot be empty")
    if not task_id.strip():
        raise ValueError("task_id cannot be empty")
    _require_nonnegative_optional(review_groups, "review_groups")
    relative_packets, digest, input_bytes, characters = _measure_input_packets(
        root,
        input_path=input_path,
        input_paths=input_paths,
    )
    primary_relative = relative_posix_path(
        root, resolve_project_path(root, input_path, must_exist=True)
    )

    with _MODEL_USAGE_PROCESS_LOCK:
        lock = ResourceLock(root, LOG_RESOURCE, f"model-usage-create:{task_id}")
        try:
            lock.acquire(timeout_seconds=2.0)
        except RuntimeError as exc:
            warning = f"model usage pending was not written: {exc}"
            (
                warning_sink
                or (lambda message: print(f"WARNING: {message}", file=sys.stderr))
            )(warning)
            return None
        try:
            try:
                pending_rows, _ = read_pending_records(root)
                retired = retired_usage_ids(root)
                usage_rows, _ = read_usage_log(root)
            except ModelUsageError as exc:
                warning = f"model usage state unavailable: {exc}"
                (
                    warning_sink
                    or (
                        lambda message: print(
                            f"WARNING: {message}", file=sys.stderr
                        )
                    )
                )(warning)
                return None
            confirmed_usage_ids = {
                str(row.get("usage_id", "")) for row in usage_rows
            }
            identity_matching = [
                row
                for row in pending_rows
                if row.get("task_id") == task_id
                and row.get("stage") == stage
                and row.get("input_sha256") == digest
            ]
            matching = [
                row
                for row in identity_matching
                if row.get("usage_id") not in retired
                and row.get("usage_id") not in confirmed_usage_ids
            ]
            obsolete = [
                row
                for row in pending_rows
                if row.get("task_id") == task_id
                and row.get("stage") == stage
                and row.get("input_sha256") != digest
                and row.get("usage_id") not in retired
                and row.get("usage_id") not in confirmed_usage_ids
            ]
            for row in obsolete:
                obsolete_usage_id = str(row.get("usage_id", ""))
                _retire_pending_from_workflow(root, obsolete_usage_id)
                retired.add(obsolete_usage_id)
            if reuse_existing and matching:
                return str(
                    min(matching, key=lambda row: str(row.get("created_at", ""))).get(
                        "usage_id", ""
                    )
                )
            if _suppress_completed:
                if any(
                    row.get("usage_id") in retired
                    for row in identity_matching
                ):
                    return None
                completed_matching = [
                    row
                    for row in usage_rows
                    if row.get("status") == "completed"
                    and row.get("task_id") == task_id
                    and row.get("stage") == stage
                    and row.get("input_sha256") == digest
                ]
                if completed_matching:
                    return None

            make_usage_id = usage_id_factory or (lambda: default_usage_id(stage))
            for _attempt in range(10):
                usage_id = _validate_usage_id(make_usage_id())
                path = _pending_path(root, usage_id)
                if (
                    path.exists()
                    or usage_id in retired
                    or usage_id in confirmed_usage_ids
                ):
                    continue
                payload = {
                    "schema_version": SCHEMA_VERSION,
                    "usage_id": usage_id,
                    "created_at": utc_timestamp(),
                    "mod_name": mod_name.strip(),
                    "task_id": task_id.strip(),
                    "stage": stage,
                    "input_path": primary_relative,
                    "input_paths": relative_packets,
                    "input_sha256": digest,
                    "input_bytes": input_bytes,
                    "input_characters": characters,
                    "review_groups": review_groups,
                    "workflow_blocking": False,
                }
                _write_pending_atomic(path, payload)
                return usage_id
            raise OSError("could not allocate a unique model usage_id")
        except (OSError, ModelUsageError) as exc:
            warning = f"model usage pending was not written: {exc}"
            (
                warning_sink
                or (lambda message: print(f"WARNING: {message}", file=sys.stderr))
            )(warning)
            return None
        finally:
            lock.release()


def ensure_packet_pending(
    root: Path,
    *,
    mod_name: str,
    task_id: str,
    stage: str,
    input_path: str | Path,
    input_paths: Iterable[str | Path] | None = None,
    review_groups: int | None = None,
    usage_id_factory: Callable[[], str] | None = None,
    warning_sink: Callable[[str], None] | None = None,
) -> str | None:
    """Ensure packet generation has one attempt without reissuing completed work."""

    return create_pending(
        root,
        mod_name=mod_name,
        task_id=task_id,
        stage=stage,
        input_path=input_path,
        input_paths=input_paths,
        review_groups=review_groups,
        usage_id_factory=usage_id_factory,
        warning_sink=warning_sink,
        reuse_existing=True,
        _suppress_completed=True,
    )


def read_usage_log(root: Path) -> tuple[list[dict[str, Any]], int]:
    path = _log_path(root)
    if not os.path.lexists(path):
        return [], 0
    path = _safe_file(root, path, label="Model usage log")
    rows: list[dict[str, Any]] = []
    damaged = 0
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise ModelUsageError(f"model usage log is unreadable: {exc}") from exc
    for raw_line in lines:
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            damaged += 1
            continue
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            damaged += 1
            continue
        validated = _valid_usage_payload(payload)
        if validated is None:
            damaged += 1
            continue
        rows.append(validated)
    return rows, damaged


def _existing_usage_id(root: Path, usage_id: str) -> bool:
    rows, _ = read_usage_log(root)
    return any(row.get("usage_id") == usage_id for row in rows)


def _delete_pending_if_present(root: Path, usage_id: str) -> None:
    path = _pending_path(root, usage_id)
    if not os.path.lexists(path):
        return
    try:
        _safe_file(root, path, label="Model usage pending").unlink()
    except FileNotFoundError:
        pass


def _cleanup_pending_warnings(root: Path, usage_id: str) -> tuple[str, ...]:
    try:
        _delete_pending_if_present(root, usage_id)
    except (OSError, ModelUsageError) as exc:
        return (
            "model usage was recorded but pending cleanup failed: "
            f"{exc}",
        )
    return ()


def _append_usage_row(root: Path, payload: dict[str, Any]) -> None:
    path = _log_path(root)
    _safe_directory(
        root,
        path.parent,
        label="Model usage log directory",
        create=True,
    )
    if os.path.lexists(path):
        _safe_file(root, path, label="Model usage log")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        _safe_file(root, path, label="Model usage log")
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def record_usage(
    root: Path,
    *,
    usage_id: str,
    status: str,
    output_path: str | Path | None,
    tool: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    lock_timeout_seconds: float = 2.0,
) -> RecordResult:
    """Confirm one pending execution in an idempotent append-only log."""

    root = root.resolve(strict=True)
    usage_id = _validate_usage_id(usage_id)
    if status not in STATUSES:
        raise ModelUsageError(f"status must be one of {', '.join(STATUSES)}")
    if not tool.strip():
        raise ModelUsageError("tool cannot be empty")
    if (input_tokens is None) != (output_tokens is None):
        raise ModelUsageError("input_tokens and output_tokens must be provided together")
    _require_nonnegative_optional(input_tokens, "input_tokens")
    _require_nonnegative_optional(output_tokens, "output_tokens")

    lock = ResourceLock(root, LOG_RESOURCE, f"model-usage:{usage_id}")
    try:
        lock.acquire(timeout_seconds=max(0.0, lock_timeout_seconds))
    except RuntimeError as exc:
        _retire_pending_from_workflow(root, usage_id)
        raise ModelUsageUnavailable(
            f"model usage log lock unavailable: {exc}"
        ) from exc
    try:
        if _existing_usage_id(root, usage_id):
            warnings = _cleanup_pending_warnings(root, usage_id)
            return RecordResult(
                recorded=False,
                already_recorded=True,
                usage_id=usage_id,
                warnings=warnings,
            )
        pending = read_pending(root, usage_id)
        output_bytes: int | None = None
        if status == "completed":
            if output_path is None or not str(output_path).strip():
                raise ModelUsageError("completed status requires an output file")
            try:
                output = resolve_project_path(root, output_path, must_exist=True)
                output = validate_regular_path_under(
                    output,
                    root,
                    kind="file",
                    label="Model usage output",
                )
            except (OSError, ValueError) as exc:
                raise ModelUsageError(
                    f"completed output is unavailable: {output_path}"
                ) from exc
            output_bytes = output.stat().st_size
            try:
                _paths, current_digest, _bytes, _characters = _measure_input_packets(
                    root,
                    input_path=str(pending["input_path"]),
                    input_paths=pending.get("input_paths"),
                )
            except (OSError, ValueError, ModelUsageError):
                warnings = [
                    "model usage input identity changed or became unavailable; "
                    "pending was retired without recording"
                ]
                try:
                    _retire_pending_from_workflow(root, usage_id)
                except (OSError, ModelUsageError) as retire_exc:
                    warnings.append(
                        f"model usage pending retirement failed: {retire_exc}"
                    )
                return RecordResult(
                    recorded=False,
                    already_recorded=False,
                    usage_id=usage_id,
                    warnings=tuple(warnings),
                )
            if current_digest != pending["input_sha256"]:
                warnings = [
                    "model usage input identity changed; "
                    "pending was retired without recording"
                ]
                try:
                    _retire_pending_from_workflow(root, usage_id)
                except (OSError, ModelUsageError) as exc:
                    warnings.append(
                        f"model usage pending retirement failed: {exc}"
                    )
                return RecordResult(
                    recorded=False,
                    already_recorded=False,
                    usage_id=usage_id,
                    warnings=tuple(warnings),
                )
        provider_reported = input_tokens is not None
        row = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_timestamp(),
            "usage_id": usage_id,
            "mod_name": pending["mod_name"],
            "task_id": pending["task_id"],
            "stage": pending["stage"],
            "status": status,
            "tool": tool.strip(),
            "model": model.strip() if isinstance(model, str) and model.strip() else None,
            "input_sha256": pending["input_sha256"],
            "input_paths": pending.get("input_paths", [pending["input_path"]]),
            "input_bytes": pending["input_bytes"],
            "input_characters": pending["input_characters"],
            "output_bytes": output_bytes,
            "review_groups": pending["review_groups"],
            "token_measurement": "provider_reported" if provider_reported else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        try:
            _append_usage_row(root, row)
        except (OSError, ModelUsageError) as exc:
            _retire_pending_from_workflow(root, usage_id)
            raise ModelUsageUnavailable(
                f"model usage log write failed: {exc}"
            ) from exc
        warnings = _cleanup_pending_warnings(root, usage_id)
        return RecordResult(
            recorded=True,
            already_recorded=False,
            usage_id=usage_id,
            warnings=warnings,
        )
    finally:
        lock.release()


def confirmed_usage_ids(root: Path) -> set[str]:
    rows, _ = read_usage_log(root)
    return {
        str(row.get("usage_id"))
        for row in rows
        if isinstance(row.get("usage_id"), str)
    }


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    amount = value / 1024
    if amount.is_integer():
        return f"{int(amount)} KB"
    return f"{amount:.1f} KB"


def summarize_usage(root: Path, *, mod_name: str | None = None) -> str:
    rows, damaged_lines = read_usage_log(root)
    if mod_name:
        rows = [row for row in rows if row.get("mod_name") == mod_name]
    pending_rows, damaged_pending = read_pending_records(root)
    inactive_usage_ids = {
        str(row.get("usage_id"))
        for row in rows
        if isinstance(row.get("usage_id"), str)
    } | retired_usage_ids(root)
    pending_rows = [
        row
        for row in pending_rows
        if str(row.get("usage_id", "")) not in inactive_usage_ids
    ]
    if mod_name:
        pending_rows = [
            row for row in pending_rows if row.get("mod_name") == mod_name
        ]

    title = f"{mod_name or '全部 Mod'} 模型工作负载"
    lines = [title, ""]
    for stage in MODEL_STAGES:
        stage_rows = [row for row in rows if row.get("stage") == stage]
        if not stage_rows:
            continue
        statuses = Counter(str(row.get("status", "")) for row in stage_rows)
        input_bytes = sum(
            value
            for row in stage_rows
            if type(value := row.get("input_bytes")) is int and value >= 0
        )
        output_bytes = sum(
            value
            for row in stage_rows
            if type(value := row.get("output_bytes")) is int and value >= 0
        )
        review_values = [
            value
            for row in stage_rows
            if type(value := row.get("review_groups")) is int and value >= 0
        ]
        lines.extend(
            [
                f"{STAGE_LABELS[stage]}：",
                f"  执行尝试：{len(stage_rows)}",
                f"  完成：{statuses['completed']}",
                f"  失败：{statuses['failed']}",
            ]
        )
        if statuses["cancelled"]:
            lines.append(f"  取消：{statuses['cancelled']}")
        lines.append(f"  输入任务包：{_format_bytes(input_bytes)}")
        if any(row.get("output_bytes") is not None for row in stage_rows):
            lines.append(f"  输出产物：{_format_bytes(output_bytes)}")
        if review_values:
            lines.append(f"  审查组：{sum(review_values)}")
        lines.append("")

    duplicate_counts = Counter(
        (
            str(row.get("task_id", "")),
            str(row.get("stage", "")),
            str(row.get("input_sha256", "")),
        )
        for row in rows
        if row.get("task_id") and row.get("stage") and row.get("input_sha256")
    )
    duplicate_total = sum(max(0, count - 1) for count in duplicate_counts.values())
    lines.extend(
        [
            "疑似重复提交：",
            f"  {duplicate_total} 次相同 task_id、stage 和输入 SHA256 的额外执行",
            "",
        ]
    )

    token_rows = [
        row
        for row in rows
        if row.get("token_measurement") == "provider_reported"
        and type(row.get("input_tokens")) is int
        and type(row.get("output_tokens")) is int
    ]
    lines.extend(
        [
            "工具上报 Token：",
            f"  覆盖：{len(token_rows)} / {len(rows)} 次执行",
            f"  输入 Token：{sum(int(row['input_tokens']) for row in token_rows)}",
            f"  输出 Token：{sum(int(row['output_tokens']) for row in token_rows)}",
            "",
            "待记录任务：",
            f"  {len(pending_rows)} 个 pending 尚未生成日志",
        ]
    )
    if damaged_lines:
        lines.append(f"损坏日志行：{damaged_lines}")
    if damaged_pending:
        lines.append(f"损坏 pending：{damaged_pending}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record and summarize model task workload."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record", help="Confirm one pending model task.")
    record.add_argument("--usage-id", required=True)
    record.add_argument("--status", required=True, choices=STATUSES)
    record.add_argument("--output")
    record.add_argument("--tool", required=True)
    record.add_argument("--model")
    record.add_argument("--input-tokens", type=int)
    record.add_argument("--output-tokens", type=int)
    summary = subparsers.add_parser("summary", help="Print a workload summary.")
    summary.add_argument("--mod-name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = project_root()
    try:
        if args.command == "record":
            result = record_usage(
                root,
                usage_id=args.usage_id,
                status=args.status,
                output_path=args.output,
                tool=args.tool,
                model=args.model,
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
            )
            for warning in result.warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
            if result.already_recorded:
                print(f"Model usage already recorded: {result.usage_id}")
            elif result.recorded:
                print(f"Model usage recorded: {result.usage_id}")
            else:
                print(f"Model usage not recorded: {result.usage_id}")
            return 0
        print(summarize_usage(root, mod_name=args.mod_name))
        return 0
    except ModelUsageUnavailable as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
        return 0
    except (ModelUsageError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

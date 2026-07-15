"""Write an agent-neutral handoff view.

This is intentionally separate from write_codex_handoff.py so the existing
Codex hot path keeps its current cost and output contract.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from project_paths import is_under, project_root, resolve_project_path
from agent_capabilities import (
    action_for_agent,
    capability_config_fingerprint,
    config_validation_errors,
    load_agent_capabilities,
)
from write_codex_handoff import build_handoff, markdown_cell
from game_context import game_display_label_from_metadata, game_metadata_mismatches
from route_translation_task import current_game_context
from report_utils import utc_now as utc_now_text


CHECKPOINT_NEXT_READ_SET = [
    ".skyrim-chs-workspace.json",
    ".workflow/progress_card.json",
    ".workflow/progress_card.md",
    "qa/agent_handoff.json",
    "qa/codex_handoff.json",
    "qa/workflow_state.json",
    "qa/workflow_tasks.json",
    "qa/translation_readiness.json",
    "qa/workflow_health.json",
]
CHECKPOINT_STALE_WATCH_PATHS = [
    ".skyrim-chs-workspace.json",
    "mod",
    "source",
    "translated",
    "work",
    "out",
    "qa/workflow_state.json",
    "qa/workflow_tasks.json",
    "qa/translation_readiness.json",
    "qa/workflow_health.json",
    "qa/codex_handoff.json",
]
CHECKPOINT_REPORT_REFS = [
    ".workflow/progress_card.md",
    "qa/workflow_state.md",
    "qa/workflow_tasks.md",
    "qa/translation_readiness.md",
    "qa/workflow_health.md",
    "qa/codex_handoff.md",
]
SNAPSHOT_POLICY_VERSION = 2
SNAPSHOT_SMALL_FILE_MAX_BYTES = 256 * 1024
SNAPSHOT_LARGE_FILE_SAMPLE_BYTES = 64 * 1024
SNAPSHOT_DEFAULT_MAX_ENTRIES = 10_000
SNAPSHOT_DEFAULT_MAX_READ_BYTES = 32 * 1024 * 1024
CHECKPOINT_MAX_EVIDENCE_REFS = 64
CHECKPOINT_MAX_TOTAL_READ_BYTES = 32 * 1024 * 1024
SNAPSHOT_CREDENTIAL_PREFIX = "v1:"
SNAPSHOT_CREDENTIAL_MAX_AGE_NS = 30 * 1_000_000_000


def normalize_project_ref(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or candidate.root or ".." in candidate.parts:
        return ""
    return text.strip("/")


def normalized_project_path(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()


def validate_agent_handoff_outputs(root: Path, json_path: Path, report_path: Path) -> None:
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(json_path, qa_root) or not is_under(report_path, qa_root):
        raise ValueError("Agent handoff outputs must be under qa/.")
    if normalized_project_path(root, json_path) != "qa/agent_handoff.json":
        raise ValueError("Agent handoff JSON output must be qa/agent_handoff.json.")
    if normalized_project_path(root, report_path) != "qa/agent_handoff.md":
        raise ValueError("Agent handoff report output must be qa/agent_handoff.md.")

def stat_mtime_utc(file_stat: os.stat_result) -> str:
    return (
        datetime.fromtimestamp(file_stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def is_reparse_entry(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(file_stat.st_mode) or bool(
        getattr(file_stat, "st_file_attributes", 0) & reparse_flag
    )


def snapshot_policy(*, max_entries: int, max_read_bytes: int) -> dict[str, object]:
    return {
        "version": SNAPSHOT_POLICY_VERSION,
        "small_file_full_hash_max_bytes": SNAPSHOT_SMALL_FILE_MAX_BYTES,
        "large_file_fingerprint": "sampled_sha256",
        "large_file_sample_bytes": SNAPSHOT_LARGE_FILE_SAMPLE_BYTES,
        "large_file_sample_positions": ["start", "middle", "end"],
        "max_entries": max_entries,
        "max_read_bytes": max_read_bytes,
        "limit_behavior": "fail_closed",
        "reparse_behavior": "reject_without_following",
    }


def sample_offsets(size: int) -> list[int]:
    sample_size = min(SNAPSHOT_LARGE_FILE_SAMPLE_BYTES, size)
    return list(
        dict.fromkeys(
            (
                0,
                max(0, (size - sample_size) // 2),
                max(0, size - sample_size),
            )
        )
    )


def file_content_fingerprint(
    path: Path,
    size: int,
    *,
    available_read_bytes: int,
) -> dict[str, object] | None:
    if size <= SNAPSHOT_SMALL_FILE_MAX_BYTES:
        required_read_bytes = size
        mode = "full_sha256"
        offsets = [0]
    else:
        offsets = sample_offsets(size)
        required_read_bytes = sum(
            min(SNAPSHOT_LARGE_FILE_SAMPLE_BYTES, size - offset) for offset in offsets
        )
        mode = "sampled_sha256"
    if required_read_bytes > available_read_bytes:
        return None

    samples: list[dict[str, object]] = []
    content_digest = hashlib.sha256()
    read_bytes = 0
    with path.open("rb") as handle:
        for offset in offsets:
            length = (
                size
                if mode == "full_sha256"
                else min(SNAPSHOT_LARGE_FILE_SAMPLE_BYTES, size - offset)
            )
            handle.seek(offset)
            data = handle.read(length)
            if len(data) != length:
                raise OSError(f"short read while fingerprinting {path}")
            sample_sha256 = hashlib.sha256(data).hexdigest()
            samples.append(
                {
                    "offset": offset,
                    "length": length,
                    "sha256": sample_sha256,
                }
            )
            content_digest.update(data)
            read_bytes += len(data)
    fingerprint_source = {
        "mode": mode,
        "size": size,
        "samples": samples,
    }
    return {
        "fingerprint_mode": mode,
        "fingerprint": hashlib.sha256(
            json.dumps(
                fingerprint_source,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "content_sha256": content_digest.hexdigest() if mode == "full_sha256" else "",
        "samples": samples,
        "read_bytes": read_bytes,
    }


def unsafe_snapshot(
    snapshot: dict[str, object],
    unsafe_entry: str,
    *,
    scanned_entries: int = 0,
    observed_entries: int | None = None,
    read_bytes: int = 0,
) -> dict[str, object]:
    snapshot.update(
        {
            "exists": True,
            "kind": "unsafe_entry",
            "unsafe_entry": unsafe_entry,
            "scanned_entries": scanned_entries,
            "observed_entries": (
                scanned_entries if observed_entries is None else observed_entries
            ),
            "read_bytes": read_bytes,
            "complete": False,
            "snapshot_status": "unsafe",
            "limit_reason": "reparse_entry",
            "truncated": True,
        }
    )
    return snapshot


def fail_closed_snapshot(
    snapshot: dict[str, object],
    reason: str,
    *,
    status: str,
    kind: str | None = None,
    scanned_entries: int | None = None,
    observed_entries: int | None = None,
    read_bytes: int | None = None,
) -> dict[str, object]:
    updates: dict[str, object] = {
        "complete": False,
        "snapshot_status": status,
        "limit_reason": reason,
        "truncated": True,
    }
    if kind is not None:
        updates["kind"] = kind
    if scanned_entries is not None:
        updates["scanned_entries"] = scanned_entries
    if observed_entries is not None:
        updates["observed_entries"] = observed_entries
    if read_bytes is not None:
        updates["read_bytes"] = read_bytes
    snapshot.update(updates)
    return snapshot


def path_snapshot(
    root: Path,
    rel_path: str,
    *,
    max_entries: int | None = None,
    max_read_bytes: int | None = None,
) -> dict[str, object]:
    entry_limit = SNAPSHOT_DEFAULT_MAX_ENTRIES if max_entries is None else max(0, max_entries)
    read_limit = (
        SNAPSHOT_DEFAULT_MAX_READ_BYTES
        if max_read_bytes is None
        else max(0, max_read_bytes)
    )
    policy = snapshot_policy(max_entries=entry_limit, max_read_bytes=read_limit)

    def new_snapshot(path_value: str, *, kind: str = "missing") -> dict[str, object]:
        return {
            "path": path_value,
            "exists": False,
            "kind": kind,
            "mtime_utc": "",
            "latest_mtime_utc": "",
            "mtime_ns": 0,
            "latest_mtime_ns": 0,
            "size": 0,
            "fingerprint": "",
            "fingerprint_mode": "none",
            "content_sha256": "",
            "samples": [],
            "unsafe_entry": "",
            "scanned_entries": 0,
            "observed_entries": 0,
            "read_bytes": 0,
            "max_entries": entry_limit,
            "max_read_bytes": read_limit,
            "complete": True,
            "snapshot_status": "complete",
            "limit_reason": "",
            "truncated": False,
            "snapshot_policy": policy,
        }

    normalized = normalize_project_ref(rel_path)
    if not normalized:
        return fail_closed_snapshot(
            new_snapshot(str(rel_path), kind="unsafe_path"),
            "unsafe_path",
            status="unsafe",
        )
    lexical_root = Path(os.path.abspath(root))
    path = lexical_root / normalized
    snapshot = new_snapshot(normalized)

    current = lexical_root
    components = [(lexical_root, ".")]
    for part in Path(normalized).parts:
        current = current / part
        components.append((current, current.relative_to(lexical_root).as_posix()))
    target_stat: os.stat_result | None = None
    for component, component_ref in components:
        try:
            component_stat = component.lstat()
        except FileNotFoundError:
            return snapshot
        except OSError:
            return fail_closed_snapshot(snapshot, "path_lstat_failed", status="io_error")
        if is_reparse_entry(component_stat):
            return unsafe_snapshot(snapshot, component_ref)
        if component == path:
            target_stat = component_stat

    if target_stat is None:
        return fail_closed_snapshot(snapshot, "target_stat_missing", status="io_error")
    snapshot["exists"] = True

    if stat.S_ISREG(target_stat.st_mode):
        try:
            content = file_content_fingerprint(
                path,
                target_stat.st_size,
                available_read_bytes=read_limit,
            )
            if content is None:
                return fail_closed_snapshot(
                    snapshot,
                    "read_budget_exceeded",
                    status="limit_exceeded",
                    kind="file",
                )
            after_stat = path.lstat()
        except OSError:
            return fail_closed_snapshot(snapshot, "file_fingerprint_failed", status="io_error", kind="file")
        if is_reparse_entry(after_stat) or any(
            getattr(target_stat, field, None) != getattr(after_stat, field, None)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        ):
            return unsafe_snapshot(
                snapshot,
                normalized,
                read_bytes=int(content["read_bytes"]),
            )
        current_mtime = stat_mtime_utc(target_stat)
        current_mtime_ns = target_stat.st_mtime_ns
        current_size = target_stat.st_size
        snapshot.update(
            {
                "kind": "file",
                "mtime_utc": current_mtime,
                "latest_mtime_utc": current_mtime,
                "mtime_ns": current_mtime_ns,
                "latest_mtime_ns": current_mtime_ns,
                "size": current_size,
                "fingerprint_mode": content["fingerprint_mode"],
                "content_sha256": content["content_sha256"],
                "samples": content["samples"],
                "read_bytes": content["read_bytes"],
                "fingerprint": hashlib.sha256(
                    (
                        f"file:{current_size}:{current_mtime_ns}:"
                        f"{content['fingerprint_mode']}:{content['fingerprint']}"
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
        return snapshot
    if not stat.S_ISDIR(target_stat.st_mode):
        current_mtime = stat_mtime_utc(target_stat)
        current_mtime_ns = target_stat.st_mtime_ns
        current_size = target_stat.st_size
        snapshot.update(
            {
                "kind": "other",
                "mtime_utc": current_mtime,
                "latest_mtime_utc": current_mtime,
                "mtime_ns": current_mtime_ns,
                "latest_mtime_ns": current_mtime_ns,
                "size": current_size,
                "fingerprint": hashlib.sha256(
                    f"other:{current_size}:{current_mtime_ns}".encode("utf-8")
                ).hexdigest(),
            }
        )
        return snapshot

    root_stat = target_stat
    root_mtime_utc = stat_mtime_utc(root_stat)
    latest = root_mtime_utc
    latest_ns = root_stat.st_mtime_ns
    scanned = 0
    observed = 0
    read_bytes = 0
    fingerprint = hashlib.sha256()
    pending: list[tuple[Path, str]] = [(path, "")]
    try:
        while pending:
            directory, prefix = pending.pop()
            remaining_entries = entry_limit - scanned
            with os.scandir(directory) as entries:
                children = []
                for child in entries:
                    observed += 1
                    children.append(child)
                    if len(children) > remaining_entries:
                        return fail_closed_snapshot(
                            snapshot,
                            "max_entries_exceeded",
                            status="limit_exceeded",
                            kind="directory",
                            scanned_entries=scanned,
                            observed_entries=observed,
                            read_bytes=read_bytes,
                        )
            children.sort(key=lambda entry: entry.name)
            child_directories: list[tuple[Path, str]] = []
            for child in children:
                scanned += 1
                child_path = Path(child.path)
                child_relative = f"{prefix}/{child.name}".strip("/")
                child_stat = child_path.lstat()
                if is_reparse_entry(child_stat):
                    return unsafe_snapshot(
                        snapshot,
                        child_relative,
                        scanned_entries=scanned,
                        observed_entries=observed,
                        read_bytes=read_bytes,
                    )
                child_mtime_ns = child_stat.st_mtime_ns
                child_size = child_stat.st_size
                content_sha256 = ""
                content_mode = "none"
                if stat.S_ISDIR(child_stat.st_mode):
                    child_kind = "directory"
                    child_directories.append((child_path, child_relative))
                elif stat.S_ISREG(child_stat.st_mode):
                    child_kind = "file"
                    content = file_content_fingerprint(
                        child_path,
                        child_size,
                        available_read_bytes=read_limit - read_bytes,
                    )
                    if content is None:
                        return fail_closed_snapshot(
                            snapshot,
                            "read_budget_exceeded",
                            status="limit_exceeded",
                            kind="directory",
                            scanned_entries=scanned - 1,
                            observed_entries=observed,
                            read_bytes=read_bytes,
                        )
                    content_sha256 = str(content["fingerprint"])
                    content_mode = str(content["fingerprint_mode"])
                    read_bytes += int(content["read_bytes"])
                    after_stat = child_path.lstat()
                    if is_reparse_entry(after_stat) or any(
                        getattr(child_stat, field, None) != getattr(after_stat, field, None)
                        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
                    ):
                        return unsafe_snapshot(
                            snapshot,
                            child_relative,
                            scanned_entries=scanned,
                            observed_entries=observed,
                            read_bytes=read_bytes,
                        )
                else:
                    child_kind = "other"
                    content_mode = "none"
                child_mtime = stat_mtime_utc(child_stat)
                if child_mtime and child_mtime > latest:
                    latest = child_mtime
                if child_mtime_ns > latest_ns:
                    latest_ns = child_mtime_ns
                fingerprint_row = (
                    f"{child_relative}\0{child_kind}\0{child_size}\0"
                    f"{child_mtime_ns}\0{content_mode}\0{content_sha256}\n"
                )
                fingerprint.update(fingerprint_row.encode("utf-8"))
            pending.extend(reversed(child_directories))
    except OSError:
        return fail_closed_snapshot(
            snapshot,
            "directory_scan_failed",
            status="io_error",
            kind="directory",
            scanned_entries=scanned,
            observed_entries=observed,
            read_bytes=read_bytes,
        )
    snapshot.update(
        {
            "kind": "directory",
            "mtime_utc": root_mtime_utc,
            "latest_mtime_utc": latest,
            "mtime_ns": root_stat.st_mtime_ns,
            "latest_mtime_ns": latest_ns,
            "size": root_stat.st_size,
            "fingerprint": fingerprint.hexdigest(),
            "fingerprint_mode": "directory_manifest_sha256",
            "scanned_entries": scanned,
            "observed_entries": observed,
            "read_bytes": read_bytes,
        }
    )
    return snapshot


def checkpoint_budget_not_scanned_snapshot(
    rel_path: str,
    *,
    remaining_read_bytes: int,
) -> dict[str, object]:
    return {
        "path": normalize_project_ref(rel_path) or str(rel_path),
        "exists": False,
        "exists_known": False,
        "kind": "not_scanned",
        "mtime_utc": "",
        "latest_mtime_utc": "",
        "mtime_ns": 0,
        "latest_mtime_ns": 0,
        "size": 0,
        "fingerprint": "",
        "fingerprint_mode": "none",
        "content_sha256": "",
        "samples": [],
        "unsafe_entry": "",
        "scanned_entries": 0,
        "observed_entries": 0,
        "read_bytes": 0,
        "max_entries": SNAPSHOT_DEFAULT_MAX_ENTRIES,
        "max_read_bytes": remaining_read_bytes,
        "complete": False,
        "snapshot_status": "limit_exceeded",
        "limit_reason": "checkpoint_read_budget_exhausted",
        "truncated": True,
        "snapshot_policy": snapshot_policy(
            max_entries=SNAPSHOT_DEFAULT_MAX_ENTRIES,
            max_read_bytes=remaining_read_bytes,
        ),
        "checkpoint_budget_before_bytes": remaining_read_bytes,
        "checkpoint_budget_after_bytes": remaining_read_bytes,
    }


def snapshots_with_shared_read_budget(
    root: Path,
    paths: list[str],
    *,
    total_read_bytes: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    budget = min(max(0, total_read_bytes), CHECKPOINT_MAX_TOTAL_READ_BYTES)
    remaining = budget
    exhausted = False
    snapshots: list[dict[str, object]] = []
    for path in paths:
        if exhausted:
            snapshots.append(
                checkpoint_budget_not_scanned_snapshot(
                    path,
                    remaining_read_bytes=remaining,
                )
            )
            continue
        before = remaining
        snapshot = path_snapshot(root, path, max_read_bytes=remaining)
        used = min(max(0, int(snapshot.get("read_bytes", 0))), remaining)
        remaining -= used
        snapshot["checkpoint_budget_before_bytes"] = before
        snapshot["checkpoint_budget_after_bytes"] = remaining
        snapshots.append(snapshot)
        if snapshot.get("limit_reason") == "read_budget_exceeded":
            exhausted = True
    return snapshots, {
        "read_budget_bytes": budget,
        "read_bytes": budget - remaining,
        "read_budget_remaining_bytes": remaining,
        "budget_exhausted": exhausted,
    }


def unique_strings(values: list[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip().replace("\\", "/").strip("/")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def unique_project_refs(values: list[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = normalize_project_ref(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def checkpoint_actions(payload: dict[str, object]) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for row in payload.get("blocking_mods", []):
        if not isinstance(row, dict):
            continue
        action = row.get("safe_next_action", {})
        if (
            not isinstance(action, dict)
            or not str(action.get("command", "")).strip()
            or action.get("agent_capability_satisfied") is False
        ):
            continue
        actions.append(
            {
                "mod": str(row.get("mod", "")),
                "task_id": str(row.get("task_id", "")),
                "command": str(action.get("command", "")),
                "type": str(action.get("type", "")),
                "risk": str(action.get("risk", "")),
                "can_run_parallel": bool(row.get("can_run_parallel", False)),
                "resource_locks": row.get("resource_locks", []) if isinstance(row.get("resource_locks", []), list) else [],
                "must_read_evidence": unique_project_refs(row.get("must_read_evidence", []))
                if isinstance(row.get("must_read_evidence", []), list)
                else [],
            }
        )
    return actions


def adapt_handoff_for_agent(
    payload: dict[str, object],
    config: dict[str, object],
    agent: str,
) -> None:
    payload["target_agent"] = agent
    payload["agent_capabilities_sha256"] = capability_config_fingerprint(config)
    safe_actions: list[dict[str, object]] = []
    for row in payload.get("blocking_mods", []):
        if not isinstance(row, dict):
            continue
        action = row.get("safe_next_action", {})
        if not isinstance(action, dict):
            continue
        adapted = action_for_agent(action, config, agent)
        row["safe_next_action"] = adapted
        required = str(adapted.get("required_agent_capability", "")).strip()
        if required:
            row["required_agent_capability"] = required
            row["agent_capability_satisfied"] = bool(
                adapted.get("agent_capability_satisfied", False)
            )
        if adapted.get("agent_capability_satisfied") is False:
            row["agent_action_status"] = "blocked"
            row["error_code"] = "agent_capability_missing"
            row["handoff_target"] = str(adapted.get("handoff_target", "codex"))
            row["primary_blocker"] = f"agent_capability_missing:{required}"
            row["can_run_parallel"] = False
            continue
        if str(adapted.get("command", "")).strip() and adapted.get("allowed", True):
            safe_actions.append(adapted)
    payload["safe_next_actions"] = safe_actions


def select_evidence_refs(
    payload: dict[str, object],
    *,
    max_refs: int = CHECKPOINT_MAX_EVIDENCE_REFS,
) -> tuple[list[str], dict[str, object]]:
    limit = min(max(0, max_refs), CHECKPOINT_MAX_EVIDENCE_REFS)
    selected: list[str] = []
    seen: set[str] = set()
    observed = 0
    complete = True

    def add(value: object) -> bool:
        nonlocal observed, complete
        normalized = normalize_project_ref(value)
        if not normalized or normalized in seen:
            return True
        seen.add(normalized)
        observed += 1
        if len(selected) >= limit:
            complete = False
            return False
        selected.append(normalized)
        return True

    for value in CHECKPOINT_REPORT_REFS:
        if not add(value):
            break
    if complete:
        for row in payload.get("blocking_mods", []):
            if not isinstance(row, dict):
                continue
            evidence = row.get("must_read_evidence", [])
            if not isinstance(evidence, list):
                continue
            for value in evidence:
                if not add(value):
                    break
            if not complete:
                break
    return selected, {
        "limit": limit,
        "selected_count": len(selected),
        "observed_count_at_least": observed,
        "complete": complete,
        "limit_behavior": "fail_closed",
    }


def evidence_refs(payload: dict[str, object]) -> list[str]:
    refs, _summary = select_evidence_refs(payload)
    return refs


def canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def handoff_payload_sha256(payload: dict[str, object]) -> str:
    return canonical_json_sha256(
        {key: value for key, value in payload.items() if key != "resume_checkpoint"}
    )


def checkpoint_id_for(checkpoint: dict[str, object]) -> str:
    checkpoint_content = {
        key: value for key, value in checkpoint.items() if key != "checkpoint_id"
    }
    return canonical_json_sha256(checkpoint_content)[:16]


def checkpoint_credential_for_payload(payload: dict[str, object]) -> str:
    checkpoint = payload.get("resume_checkpoint", {})
    if not isinstance(checkpoint, dict):
        return ""
    source = {
        "checkpoint_id": checkpoint.get("checkpoint_id", ""),
        "handoff_payload_sha256": checkpoint.get("handoff_payload_sha256", ""),
        "generated_at_epoch_ns": checkpoint.get("generated_at_epoch_ns", 0),
        "target_agent": checkpoint.get("target_agent", ""),
    }
    return SNAPSHOT_CREDENTIAL_PREFIX + canonical_json_sha256(source)


def build_resume_checkpoint(
    root: Path,
    payload: dict[str, object],
    *,
    max_evidence_refs: int = CHECKPOINT_MAX_EVIDENCE_REFS,
    max_total_read_bytes: int = CHECKPOINT_MAX_TOTAL_READ_BYTES,
) -> dict[str, object]:
    refs, evidence_ref_summary = select_evidence_refs(
        payload,
        max_refs=max_evidence_refs,
    )
    ordered_paths = [*CHECKPOINT_STALE_WATCH_PATHS, *refs]
    ordered_snapshots, budget_summary = snapshots_with_shared_read_budget(
        root,
        ordered_paths,
        total_read_bytes=max_total_read_bytes,
    )
    watch_count = len(CHECKPOINT_STALE_WATCH_PATHS)
    watch_snapshots = ordered_snapshots[:watch_count]
    artifact_snapshots = [
        {"path": path, "snapshot": snapshot}
        for path, snapshot in zip(refs, ordered_snapshots[watch_count:], strict=True)
    ]
    generated_at_epoch_ns = time.time_ns()
    generated_at_utc = utc_now_text()
    blocking_mods = [row for row in payload.get("blocking_mods", []) if isinstance(row, dict)]
    last_stages = unique_strings([row.get("last_success_stage", "") for row in blocking_mods])
    all_snapshots = [
        *(row["snapshot"] for row in artifact_snapshots),
        *watch_snapshots,
    ]
    incomplete_reasons = {
        str(row.get("limit_reason", ""))
        for row in all_snapshots
        if not bool(row.get("complete", False)) and str(row.get("limit_reason", ""))
    }
    if not bool(evidence_ref_summary["complete"]):
        incomplete_reasons.add("evidence_ref_limit_exceeded")
    checkpoint_policy = snapshot_policy(
        max_entries=SNAPSHOT_DEFAULT_MAX_ENTRIES,
        max_read_bytes=int(budget_summary["read_budget_bytes"]),
    )
    checkpoint_policy.update(
        {
            "checkpoint_max_total_read_bytes": int(budget_summary["read_budget_bytes"]),
            "max_evidence_refs": int(evidence_ref_summary["limit"]),
            "snapshot_order": ["watch", "artifact"],
        }
    )
    checkpoint: dict[str, object] = {
        "schema_version": 2,
        "checkpoint_id": "",
        "generated_at_utc": generated_at_utc,
        "generated_at_epoch_ns": generated_at_epoch_ns,
        "purpose": "low-context resume index; authoritative state remains workflow_state.json and workflow_tasks.json",
        "project_state": payload.get("project_state", ""),
        "readiness_overall_status": payload.get("readiness_overall_status", ""),
        "last_successful_stage": last_stages[0] if last_stages else "",
        "last_successful_stages": last_stages,
        "target_agent": payload.get("target_agent", ""),
        "agent_capabilities_sha256": payload.get("agent_capabilities_sha256", ""),
        "handoff_payload_sha256": handoff_payload_sha256(payload),
        "snapshot_policy": checkpoint_policy,
        "evidence_ref_summary": evidence_ref_summary,
        "snapshot_summary": {
            "artifact_count": len(artifact_snapshots),
            "watch_count": len(watch_snapshots),
            "complete": (
                bool(evidence_ref_summary["complete"])
                and all(bool(row.get("complete", False)) for row in all_snapshots)
            ),
            "read_bytes": int(budget_summary["read_bytes"]),
            "read_budget_bytes": int(budget_summary["read_budget_bytes"]),
            "read_budget_remaining_bytes": int(
                budget_summary["read_budget_remaining_bytes"]
            ),
            "budget_exhausted": bool(budget_summary["budget_exhausted"]),
            "snapshot_order": ["watch", "artifact"],
            "incomplete_reasons": sorted(incomplete_reasons),
            "limit_behavior": "fail_closed",
        },
        "credential_policy": {
            "version": 1,
            "transport": "single_child_process_environment",
            "scope": "next_export_only",
            "max_age_seconds": SNAPSHOT_CREDENTIAL_MAX_AGE_NS // 1_000_000_000,
            "independent_export_behavior": "full_snapshot_scan",
        },
        "next_read_set": CHECKPOINT_NEXT_READ_SET,
        "next_actions": checkpoint_actions(payload),
        "artifact_refs": artifact_snapshots,
        "stale_if_newer_than": {
            "checkpoint_generated_at_utc": generated_at_utc,
            "checkpoint_generated_at_epoch_ns": generated_at_epoch_ns,
            "rule": "Compare stored and current watched-path snapshots before trusting this checkpoint.",
            "watch": watch_snapshots,
        },
    }
    checkpoint["checkpoint_id"] = checkpoint_id_for(checkpoint)
    return checkpoint


def evaluate_resume_checkpoint(
    root: Path,
    checkpoint: dict[str, object],
    *,
    verify_current_snapshots: bool = True,
) -> dict[str, object]:
    reasons: list[dict[str, object]] = []
    if checkpoint.get("schema_version") != 2:
        reasons.append({"path": "", "reason": "unsupported_checkpoint_schema"})
    stored_checkpoint_id = str(checkpoint.get("checkpoint_id", "")).strip()
    expected_checkpoint_id = checkpoint_id_for(checkpoint)
    if not stored_checkpoint_id or stored_checkpoint_id != expected_checkpoint_id:
        reasons.append(
            {
                "path": "qa/agent_handoff.json",
                "reason": "checkpoint_id_mismatch",
                "stored": stored_checkpoint_id,
                "expected": expected_checkpoint_id,
            }
        )
    snapshot_summary = checkpoint.get("snapshot_summary", {})
    if not isinstance(snapshot_summary, dict) or snapshot_summary.get("complete") is not True:
        reasons.append(
            {
                "path": "qa/agent_handoff.json",
                "reason": "checkpoint_snapshot_incomplete",
                "incomplete_reasons": (
                    snapshot_summary.get("incomplete_reasons", [])
                    if isinstance(snapshot_summary, dict)
                    else []
                ),
            }
        )
    stale_rule = checkpoint.get("stale_if_newer_than", {})
    if not isinstance(stale_rule, dict):
        stale_rule = {}
    watch = stale_rule.get("watch", [])
    if not isinstance(watch, list) or not watch:
        reasons.append({"path": "", "reason": "missing_watch_snapshots"})
        watch = []

    actual_watch_paths = [
        normalize_project_ref(row.get("path", ""))
        for row in watch
        if isinstance(row, dict) and normalize_project_ref(row.get("path", ""))
    ]
    expected_watch_paths = set(CHECKPOINT_STALE_WATCH_PATHS)
    for path in sorted(expected_watch_paths - set(actual_watch_paths)):
        reasons.append({"path": path, "reason": "missing_required_watch_path"})
    for path in sorted(set(actual_watch_paths) - expected_watch_paths):
        reasons.append({"path": path, "reason": "unexpected_watch_path"})
    if len(actual_watch_paths) != len(set(actual_watch_paths)):
        reasons.append({"path": "", "reason": "duplicate_watch_path"})

    generated_ns = checkpoint.get("generated_at_epoch_ns", stale_rule.get("checkpoint_generated_at_epoch_ns", 0))
    try:
        generated_epoch_ns = int(generated_ns)
    except (TypeError, ValueError):
        generated_epoch_ns = 0
    if generated_epoch_ns <= 0:
        reasons.append({"path": "", "reason": "missing_generated_at_epoch_ns"})

    checkpoint_policy = checkpoint.get("snapshot_policy", {})
    if not isinstance(checkpoint_policy, dict):
        checkpoint_policy = {}
    raw_read_budget = checkpoint_policy.get(
        "checkpoint_max_total_read_bytes",
        checkpoint_policy.get("max_read_bytes", CHECKPOINT_MAX_TOTAL_READ_BYTES),
    )
    try:
        configured_read_budget = int(raw_read_budget)
    except (TypeError, ValueError):
        configured_read_budget = -1
    if not 0 <= configured_read_budget <= CHECKPOINT_MAX_TOTAL_READ_BYTES:
        reasons.append(
            {
                "path": "qa/agent_handoff.json",
                "reason": "invalid_checkpoint_read_budget",
                "actual": raw_read_budget,
                "maximum": CHECKPOINT_MAX_TOTAL_READ_BYTES,
            }
        )
        configured_read_budget = CHECKPOINT_MAX_TOTAL_READ_BYTES
    verification_remaining = configured_read_budget
    verification_read_bytes = 0
    verification_budget_exhausted = False

    for stored in watch:
        if not isinstance(stored, dict):
            reasons.append({"path": "", "reason": "invalid_watch_snapshot"})
            continue
        path = normalize_project_ref(stored.get("path", ""))
        if not path:
            reasons.append({"path": str(stored.get("path", "")), "reason": "unsafe_watch_path"})
            continue
        if bool(stored.get("truncated", False)) or stored.get("complete") is not True:
            reasons.append(
                {
                    "path": path,
                    "reason": "stored_snapshot_truncated",
                    "snapshot_status": stored.get("snapshot_status", ""),
                    "limit_reason": stored.get("limit_reason", ""),
                }
            )
            continue
        if not verify_current_snapshots:
            continue

        if verification_budget_exhausted:
            reasons.append(
                {
                    "path": path,
                    "reason": "current_snapshot_not_scanned",
                    "snapshot_status": "limit_exceeded",
                    "limit_reason": "checkpoint_read_budget_exhausted",
                }
            )
            continue
        before = verification_remaining
        current = path_snapshot(
            root,
            path,
            max_read_bytes=verification_remaining,
        )
        used = min(
            max(0, int(current.get("read_bytes", 0))),
            verification_remaining,
        )
        verification_remaining -= used
        verification_read_bytes += used
        current["checkpoint_budget_before_bytes"] = before
        current["checkpoint_budget_after_bytes"] = verification_remaining
        if bool(current.get("truncated", False)) or current.get("complete") is not True:
            reasons.append(
                {
                    "path": path,
                    "reason": "current_snapshot_truncated",
                    "snapshot_status": current.get("snapshot_status", ""),
                    "limit_reason": current.get("limit_reason", ""),
                }
            )
            if current.get("limit_reason") == "read_budget_exceeded":
                verification_budget_exhausted = True
            continue

        changed_fields: list[str] = []
        for field in (
            "exists",
            "kind",
            "latest_mtime_ns",
            "scanned_entries",
            "size",
            "fingerprint",
            "content_sha256",
            "unsafe_entry",
            "fingerprint_mode",
            "snapshot_policy",
            "complete",
            "snapshot_status",
            "limit_reason",
        ):
            if stored.get(field) != current.get(field):
                changed_fields.append(field)
        if changed_fields:
            reasons.append(
                {
                    "path": path,
                    "reason": "snapshot_changed",
                    "changed_fields": changed_fields,
                    "stored": {field: stored.get(field) for field in changed_fields},
                    "current": {field: current.get(field) for field in changed_fields},
                }
            )

    return {
        "checkpoint_id": str(checkpoint.get("checkpoint_id", "")),
        "checked_at_utc": utc_now_text(),
        "fresh": not reasons,
        "status": "fresh" if not reasons else "stale",
        "generated_at_epoch_ns": generated_epoch_ns,
        "watch_count": len(watch),
        "verification_read_budget_bytes": configured_read_budget,
        "verification_read_bytes": verification_read_bytes,
        "verification_read_budget_remaining_bytes": verification_remaining,
        "verification_budget_exhausted": verification_budget_exhausted,
        "verification_mode": (
            "full_snapshot_scan"
            if verify_current_snapshots
            else "same_chain_checkpoint_credential"
        ),
        "reasons": reasons,
    }


def evaluate_agent_handoff_freshness(
    root: Path,
    payload: dict[str, object],
    *,
    expected_agent: str = "",
    checkpoint_credential: str = "",
) -> dict[str, object]:
    checkpoint = payload.get("resume_checkpoint", {})
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    expected_credential = checkpoint_credential_for_payload(payload)
    credential_supplied = bool(checkpoint_credential)
    credential_matches = credential_supplied and hmac.compare_digest(
        checkpoint_credential,
        expected_credential,
    )
    try:
        checkpoint_age_ns = time.time_ns() - int(checkpoint.get("generated_at_epoch_ns", 0))
    except (TypeError, ValueError):
        checkpoint_age_ns = SNAPSHOT_CREDENTIAL_MAX_AGE_NS + 1
    credential_fresh = 0 <= checkpoint_age_ns <= SNAPSHOT_CREDENTIAL_MAX_AGE_NS
    result = evaluate_resume_checkpoint(
        root,
        checkpoint,
        verify_current_snapshots=not credential_supplied,
    )

    def add_stale_reason(reason: dict[str, object]) -> None:
        reasons = result.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = []
        reasons.append(reason)
        result["reasons"] = reasons
        result["fresh"] = False
        result["status"] = "stale"

    if credential_supplied and not credential_matches:
        add_stale_reason(
            {
                "path": "qa/agent_handoff.json",
                "reason": "checkpoint_credential_mismatch",
            }
        )
    elif credential_supplied and not credential_fresh:
        add_stale_reason(
            {
                "path": "qa/agent_handoff.json",
                "reason": "checkpoint_credential_expired",
                "age_ns": checkpoint_age_ns,
                "max_age_ns": SNAPSHOT_CREDENTIAL_MAX_AGE_NS,
            }
        )

    stored_payload_sha256 = str(checkpoint.get("handoff_payload_sha256", "")).strip()
    current_payload_sha256 = handoff_payload_sha256(payload)
    if not stored_payload_sha256 or stored_payload_sha256 != current_payload_sha256:
        add_stale_reason(
            {
                "path": "qa/agent_handoff.json",
                "reason": "handoff_payload_mismatch",
                "stored": stored_payload_sha256,
                "expected": current_payload_sha256,
            }
        )

    context = current_game_context(root)
    mismatches = game_metadata_mismatches(payload, context)
    if mismatches:
        add_stale_reason(
            {
                "path": ".skyrim-chs-workspace.json",
                "reason": "game_metadata_mismatch",
                "mismatches": mismatches,
            }
        )
    target_agent = str(payload.get("target_agent", "")).strip()
    checkpoint_target_agent = str(checkpoint.get("target_agent", "")).strip()
    if target_agent != checkpoint_target_agent:
        add_stale_reason(
            {
                "path": "qa/agent_handoff.json",
                "reason": "checkpoint_target_agent_mismatch",
                "payload_target_agent": target_agent,
                "checkpoint_target_agent": checkpoint_target_agent,
            }
        )
    if expected_agent and target_agent != expected_agent:
        add_stale_reason(
            {
                "path": "qa/agent_handoff.json",
                "reason": "target_agent_mismatch",
                "expected": expected_agent,
                "actual": target_agent,
            }
        )
    if expected_agent and checkpoint_target_agent != expected_agent:
        add_stale_reason(
            {
                "path": "qa/agent_handoff.json",
                "reason": "checkpoint_target_agent_mismatch",
                "expected": expected_agent,
                "actual": checkpoint_target_agent,
            }
        )
    payload_fingerprint = str(payload.get("agent_capabilities_sha256", "")).strip()
    checkpoint_fingerprint = str(
        checkpoint.get("agent_capabilities_sha256", "")
    ).strip()
    if payload_fingerprint != checkpoint_fingerprint:
        add_stale_reason(
            {
                "path": "qa/agent_handoff.json",
                "reason": "agent_capabilities_binding_mismatch",
                "payload": payload_fingerprint,
                "checkpoint": checkpoint_fingerprint,
            }
        )
    try:
        current_fingerprint = capability_config_fingerprint(load_agent_capabilities())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        current_fingerprint = ""
        capability_reason: dict[str, object] = {
            "path": "config/agent_capabilities.example.json",
            "reason": "agent_capabilities_unavailable",
            "detail": str(exc),
        }
    else:
        capability_reason = {
            "path": "config/agent_capabilities.example.json",
            "reason": "agent_capabilities_changed",
            "stored": checkpoint_fingerprint,
            "current": current_fingerprint,
        }
    if not checkpoint_fingerprint or checkpoint_fingerprint != current_fingerprint:
        add_stale_reason(capability_reason)
    return result


def write_agent_reports(root: Path, payload: dict[str, object], json_path: Path, report_path: Path) -> None:
    payload["handoff_kind"] = "agent"
    payload["read_order"] = [
        "qa/agent_handoff.json",
        "qa/codex_handoff.json",
        "qa/workflow_state.json",
        "qa/workflow_tasks.json",
        "qa/translation_readiness.json",
    ]
    payload["plugin_source_read_order"] = [
        "config/workflow_policy.json",
        "config/agent_capabilities.example.json",
    ]
    payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    health = payload.get("workflow_health", {}) if isinstance(payload.get("workflow_health"), dict) else {}
    task_info = payload.get("task_summary", {}) if isinstance(payload.get("task_summary"), dict) else {}
    checkpoint = payload.get("resume_checkpoint", {}) if isinstance(payload.get("resume_checkpoint"), dict) else {}
    checkpoint_actions_count = len(checkpoint.get("next_actions", [])) if isinstance(checkpoint.get("next_actions", []), list) else 0
    snapshot_summary = checkpoint.get("snapshot_summary", {}) if isinstance(checkpoint.get("snapshot_summary"), dict) else {}
    checkpoint_policy = checkpoint.get("snapshot_policy", {}) if isinstance(checkpoint.get("snapshot_policy"), dict) else {}
    lines = [
        "# Agent Handoff",
        "",
        f"- Game: {game_display_label_from_metadata(payload)}",
        f"- Support level: {payload.get('support_level', '')}",
        f"- Target agent: {payload.get('target_agent', '')}",
        f"- Generated at: {payload.get('generated_at', '')}",
        f"- Project state: {payload.get('project_state', '')}",
        f"- Readiness: {payload.get('readiness_overall_status', '')}",
        f"- Workflow health: {health.get('verdict', '')} / Blocking: {health.get('blocking_issues', 0)}",
        f"- Pending executable tasks: {task_info.get('pending_executable', 0)}",
        f"- Parallel-safe tasks: {task_info.get('parallel_safe', 0)}",
        f"- Resume checkpoint: {checkpoint.get('checkpoint_id', '')} / Next actions: {checkpoint_actions_count}",
        (
            "- Snapshot policy: "
            f"full SHA256 <= {checkpoint_policy.get('small_file_full_hash_max_bytes', 0)} bytes; "
            f"larger files use {checkpoint_policy.get('large_file_sample_positions', [])} samples of "
            f"{checkpoint_policy.get('large_file_sample_bytes', 0)} bytes; "
            f"max entries {checkpoint_policy.get('max_entries', 0)}; "
            f"max read bytes {checkpoint_policy.get('max_read_bytes', 0)}; limits fail closed."
        ),
        (
            f"- Snapshot coverage: complete={snapshot_summary.get('complete', False)} / "
            f"read_bytes={snapshot_summary.get('read_bytes', 0)}"
        ),
        "- GUI boundary: GUI, Computer Use, pywinauto, and desktop automation are Codex-only.",
        "- Plugin source files: `config/workflow_policy.json`, `config/agent_capabilities.example.json`.",
        "- Resume checkpoint is an index only; refresh the state chain if watched artifacts changed after it was generated.",
        "",
        "## Blocking Mods",
        "",
        "| Mod | State | Primary blocker | Safe next action | Task |",
        "|---|---|---|---|---|",
    ]
    for row in payload.get("blocking_mods", []):
        if not isinstance(row, dict):
            continue
        action = row.get("safe_next_action", {}) if isinstance(row.get("safe_next_action"), dict) else {}
        lines.append(
            f"| {markdown_cell(row.get('mod', ''))} | {markdown_cell(row.get('state', ''))} | "
            f"{markdown_cell(row.get('primary_blocker', ''))} | {markdown_cell(action.get('command', ''))} | "
            f"{markdown_cell(row.get('task_id', ''))} |"
        )
    lines.extend(
        [
            "",
            "## Resume Checkpoint",
            "",
            f"- Checkpoint id: `{checkpoint.get('checkpoint_id', '')}`",
            f"- Generated UTC: `{checkpoint.get('generated_at_utc', '')}`",
            f"- Last successful stage: `{checkpoint.get('last_successful_stage', '')}`",
            f"- Next read set: {', '.join(f'`{item}`' for item in checkpoint.get('next_read_set', []) if isinstance(item, str))}",
            "- Stale rule: refresh readiness/state/tasks/handoff if any watched path is newer than the checkpoint.",
            "",
            "## Adapter Rules",
            "",
            "- Codex may handle GUI-only handoffs through controlled project rules.",
            "- opencode and Claude Code are full non-GUI adapters.",
            "- Non-GUI adapters must return `blocked` with `handoff_target=codex` for GUI-only tasks.",
            "- This handoff is a read view; it does not run commands or mark QA complete.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write qa/agent_handoff.json and qa/agent_handoff.md.")
    parser.add_argument("--workflow-state-path", default="qa/workflow_state.json")
    parser.add_argument("--readiness-json-path", default="qa/translation_readiness.json")
    parser.add_argument("--workflow-health-path", default="qa/workflow_health.json")
    parser.add_argument("--workflow-tasks-path", default="qa/workflow_tasks.json")
    parser.add_argument("--json-output-path", default="qa/agent_handoff.json")
    parser.add_argument("--report-output-path", default="qa/agent_handoff.md")
    parser.add_argument(
        "--agent",
        choices=("opencode", "claude-code"),
        default="opencode",
        help="Non-GUI top-level adapter that will consume this explicit handoff.",
    )
    parser.add_argument(
        "--check-freshness",
        action="store_true",
        help="Check the existing resume checkpoint against current watched paths without rewriting reports.",
    )
    args = parser.parse_args()

    root = project_root()
    state_path = resolve_project_path(root, args.workflow_state_path, must_exist=False)
    readiness_path = resolve_project_path(root, args.readiness_json_path, must_exist=False)
    health_path = resolve_project_path(root, args.workflow_health_path, must_exist=False)
    tasks_path = resolve_project_path(root, args.workflow_tasks_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    validate_agent_handoff_outputs(root, json_path, report_path)

    if args.check_freshness:
        if not json_path.is_file():
            print(f"ERROR: agent handoff does not exist: {json_path}")
            return 1
        try:
            existing_payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read agent handoff: {exc}")
            return 1
        if not isinstance(existing_payload, dict):
            print("ERROR: agent handoff must contain a JSON object")
            return 1
        result = evaluate_agent_handoff_freshness(
            root,
            existing_payload,
            expected_agent=args.agent,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["fresh"] else 2

    payload, issues = build_handoff(root, state_path, readiness_path, health_path, tasks_path)
    capabilities = load_agent_capabilities()
    capability_errors = config_validation_errors(capabilities)
    if capability_errors:
        print("ERROR: invalid agent capability config: " + "; ".join(capability_errors))
        return 1
    adapt_handoff_for_agent(payload, capabilities, args.agent)
    write_agent_reports(root, payload, json_path, report_path)
    print(f"AGENT_HANDOFF_CREDENTIAL={checkpoint_credential_for_payload(payload)}")
    blocking = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    print(f"Agent handoff JSON written to: {json_path}")
    print(f"Agent handoff report written to: {report_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())

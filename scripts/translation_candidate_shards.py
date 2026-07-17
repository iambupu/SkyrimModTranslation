"""Create bounded model-context shards from the complete candidate JSONL."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from audit_mod_scale import default_scale_config_path, load_scale_config
from file_utils import sha256_file, validate_regular_path_under
from project_paths import is_under, project_root, relative_path, resolve_project_path, safe_file_name
from report_utils import utc_now
from workflow_lock import ResourceLock


DEFAULT_BATCH_ROWS = 5000
PRESERVED_STATUSES = {"translated", "qa_passed", "blocked"}


def _batch_policy(root: Path, mod_name: str, game_id: str) -> tuple[int, str]:
    report = root / "qa" / f"{mod_name}.scale_execution.json"
    if not report.is_file():
        return DEFAULT_BATCH_ROWS, "built-in-default"
    payload = json.loads(report.read_text(encoding="utf-8-sig"))
    effective = payload.get("effective") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("report_type") != "mod-scale-execution"
        or payload.get("status") != "ready"
        or payload.get("mod_name") != mod_name
        or payload.get("game_id") != game_id
        or not isinstance(effective, dict)
    ):
        raise ValueError(f"Scale execution report is not valid for candidate sharding: {report}")
    batch_rows = effective.get("translation_batch_rows")
    if isinstance(batch_rows, bool) or not isinstance(batch_rows, int) or batch_rows <= 0:
        raise ValueError("Scale execution translation_batch_rows must be a positive integer")
    config = load_scale_config(default_scale_config_path())
    cap = int(config["absolute_limits"]["max_translation_batch_rows"])
    if batch_rows > cap:
        raise ValueError(
            f"Scale execution translation_batch_rows={batch_rows} exceeds absolute safety cap {cap}"
        )
    return batch_rows, relative_path(root, report).replace("\\", "/")


def _read_previous(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Translation candidate shard index is invalid: {path}") from exc
    rows = payload.get("shards") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != "translation-candidate-shards"
        or not isinstance(rows, list)
    ):
        raise ValueError(f"Translation candidate shard index schema is invalid: {path}")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        shard_id = str(row.get("shard_id") or "") if isinstance(row, dict) else ""
        if not shard_id or shard_id in indexed:
            raise ValueError(f"Translation candidate shard index contains invalid or duplicate rows: {path}")
        indexed[shard_id] = row
    return indexed


def _outputs_still_valid(root: Path, mod_name: str, row: Mapping[str, Any]) -> bool:
    output_files = row.get("output_files")
    output_hashes = row.get("output_sha256")
    if not isinstance(output_files, list) or not output_files or not isinstance(output_hashes, dict):
        return False
    allowed_roots = (root / "translated", root / "out" / mod_name)
    for value in output_files:
        try:
            path = resolve_project_path(root, str(value), must_exist=True)
            allowed = next((candidate for candidate in allowed_roots if is_under(path, candidate)), None)
            if allowed is None:
                return False
            validate_regular_path_under(path, allowed, kind="file", label="Translation shard output")
        except (OSError, ValueError):
            return False
        expected = str(output_hashes.get(str(value)) or "")
        if len(expected) != 64 or sha256_file(path).casefold() != expected.casefold():
            return False
    return True


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _write_translation_candidate_shards_unlocked(
    *,
    root: Path,
    mod_name: str,
    game_id: str,
    source_jsonl: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    source_jsonl = source_jsonl.resolve(strict=True)
    if not is_under(source_jsonl, root / "out" / mod_name):
        raise ValueError("Candidate source JSONL must stay under out/<ModName>/")
    batch_rows, policy_source = _batch_policy(root, mod_name, game_id)
    shard_root = root / "work" / "shards" / mod_name / "translation_candidates"
    shard_root.mkdir(parents=True, exist_ok=True)
    index_path = shard_root / "index.json"
    previous = _read_previous(index_path)
    shard_rows: list[dict[str, Any]] = []
    active_files: set[str] = set()

    for offset in range(0, len(rows), batch_rows):
        number = offset // batch_rows + 1
        shard_id = f"translation-candidates-{number:05d}"
        shard_path = shard_root / f"{shard_id}.jsonl"
        _write_jsonl(shard_path, rows[offset : offset + batch_rows])
        shard_relative = relative_path(root, shard_path).replace("\\", "/")
        active_files.add(shard_path.name.casefold())
        source_hash = sha256_file(shard_path)
        prior = previous.get(shard_id, {})
        status = "pending"
        output_files: list[str] = []
        output_hashes: dict[str, str] = {}
        qa_status = "pending"
        prior_status = str(prior.get("status") or "")
        if (
            prior.get("source_sha256") == source_hash
            and prior_status in PRESERVED_STATUSES
            and (prior_status == "blocked" or _outputs_still_valid(root, mod_name, prior))
        ):
            status = prior_status
            output_files = [str(value) for value in prior.get("output_files", [])]
            output_hashes = {str(key): str(value) for key, value in prior.get("output_sha256", {}).items()}
            qa_status = str(prior.get("qa_status") or "pending")
        shard_rows.append(
            {
                "shard_id": shard_id,
                "source_files": [relative_path(root, source_jsonl).replace("\\", "/")],
                "source_shard": shard_relative,
                "source_sha256": source_hash,
                "candidate_count": min(batch_rows, len(rows) - offset),
                "status": status,
                "output_files": output_files,
                "output_sha256": output_hashes,
                "qa_status": qa_status,
            }
        )

    for stale in shard_root.glob("translation-candidates-*.jsonl"):
        if stale.name.casefold() not in active_files:
            stale.unlink()
    payload = {
        "schema_version": 1,
        "kind": "translation-candidate-shards",
        "updated_at": utc_now(),
        "mod_name": mod_name,
        "game_id": game_id,
        "source_jsonl": relative_path(root, source_jsonl).replace("\\", "/"),
        "source_sha256": sha256_file(source_jsonl),
        "policy_source": policy_source,
        "translation_batch_rows": batch_rows,
        "candidate_count": len(rows),
        "shard_count": len(shard_rows),
        "shards": shard_rows,
    }
    temporary_index = index_path.with_suffix(".json.tmp")
    temporary_index.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_index, index_path)
    return payload


def write_translation_candidate_shards(
    *,
    root: Path,
    mod_name: str,
    game_id: str,
    source_jsonl: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    lock = ResourceLock(root, f"translation-candidate-index:{mod_name}", "translation_candidate_shards.py").acquire(
        timeout_seconds=30.0
    )
    try:
        return _write_translation_candidate_shards_unlocked(
            root=root,
            mod_name=mod_name,
            game_id=game_id,
            source_jsonl=source_jsonl,
            rows=rows,
        )
    finally:
        lock.release()


def _record_translation_shard_result_unlocked(
    *,
    root: Path,
    mod_name: str,
    shard_id: str,
    status: str,
    output_values: list[str],
) -> dict[str, Any]:
    if status not in PRESERVED_STATUSES:
        raise ValueError(f"Unsupported translation shard status: {status}")
    index_path = root / "work" / "shards" / mod_name / "translation_candidates" / "index.json"
    validate_regular_path_under(index_path, index_path.parent, kind="file", label="Translation shard index")
    payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
    rows = payload.get("shards") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("mod_name") != mod_name or not isinstance(rows, list):
        raise ValueError("Translation candidate shard index is invalid")
    row = next(
        (value for value in rows if isinstance(value, dict) and value.get("shard_id") == shard_id),
        None,
    )
    if row is None:
        raise ValueError(f"Unknown translation candidate shard: {shard_id}")
    source_shard = resolve_project_path(root, str(row.get("source_shard") or ""), must_exist=True)
    validate_regular_path_under(source_shard, index_path.parent, kind="file", label="Translation candidate source shard")
    if sha256_file(source_shard) != str(row.get("source_sha256") or ""):
        raise ValueError(f"Translation candidate shard source changed: {shard_id}")

    output_files: list[str] = []
    output_hashes: dict[str, str] = {}
    allowed_roots = (root / "translated", root / "out" / mod_name)
    seen_outputs: set[str] = set()
    for value in output_values:
        output = resolve_project_path(root, value, must_exist=True)
        allowed = next((candidate for candidate in allowed_roots if is_under(output, candidate)), None)
        if allowed is None:
            raise ValueError(f"Translation shard output must be a file under translated/ or out/{mod_name}/: {value}")
        validate_regular_path_under(output, allowed, kind="file", label="Translation shard output")
        relative = relative_path(root, output).replace("\\", "/")
        if relative.casefold() in seen_outputs:
            raise ValueError(f"Duplicate translation shard output: {relative}")
        seen_outputs.add(relative.casefold())
        output_files.append(relative)
        output_hashes[relative] = sha256_file(output)
    if status in {"translated", "qa_passed"} and not output_files:
        raise ValueError(f"Translation shard status {status} requires at least one output file")
    row["status"] = status
    row["output_files"] = output_files
    row["output_sha256"] = output_hashes
    row["qa_status"] = {"qa_passed": "passed", "translated": "pending", "blocked": "blocked"}[status]
    payload["updated_at"] = utc_now()
    temporary = index_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, index_path)
    return row


def record_translation_shard_result(
    *,
    root: Path,
    mod_name: str,
    shard_id: str,
    status: str,
    output_values: list[str],
) -> dict[str, Any]:
    lock = ResourceLock(root, f"translation-candidate-index:{mod_name}", "translation_candidate_shards.py").acquire(
        timeout_seconds=30.0
    )
    try:
        return _record_translation_shard_result_unlocked(
            root=root,
            mod_name=mod_name,
            shard_id=shard_id,
            status=status,
            output_values=output_values,
        )
    finally:
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a verified result for one model translation candidate shard.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--shard-id", required=True)
    parser.add_argument("--status", required=True, choices=sorted(PRESERVED_STATUSES))
    parser.add_argument("--output-path", action="append", default=[])
    args = parser.parse_args()

    root = project_root()
    mod_name = safe_file_name(args.mod_name)
    if not mod_name:
        raise ValueError("ModName cannot be empty")
    row = record_translation_shard_result(
        root=root,
        mod_name=mod_name,
        shard_id=args.shard_id,
        status=args.status,
        output_values=args.output_path,
    )
    print(f"Translation candidate shard recorded: {row['shard_id']} -> {row['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

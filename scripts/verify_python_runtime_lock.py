"""Verify the committed hash-pinned Python runtime export.

The check deliberately needs no ``uv`` installation: CI binds the committed
export to the authoritative ``uv.lock`` by digest and independently verifies
that every exported requirement is exact and hash protected.  Maintainers
regenerate both files with the command recorded in
``config/python-runtime-lock.json`` whenever ``uv.lock`` changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = PROJECT_ROOT / "config" / "python-runtime-lock.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REQUIREMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[^\\s;\\\\]+")


class RuntimeLockVerificationError(RuntimeError):
    """The committed Python runtime lock export is not reproducible."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_object(
    payload: object,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise RuntimeLockVerificationError(f"{label} must be a JSON object")
    actual = set(payload)
    if actual != keys:
        raise RuntimeLockVerificationError(
            f"{label} keys differ: missing={sorted(keys - actual)}, "
            f"extra={sorted(actual - keys)}"
        )
    return dict(payload)


def _project_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeLockVerificationError(f"{label} must be a relative path")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise RuntimeLockVerificationError(f"{label} escapes the project")
    path = (PROJECT_ROOT / raw).resolve(strict=True)
    try:
        path.relative_to(PROJECT_ROOT.resolve(strict=True))
    except ValueError as exc:
        raise RuntimeLockVerificationError(f"{label} escapes the project") from exc
    return path


def _required_digest(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RuntimeLockVerificationError(f"{key} must be a lowercase SHA-256")
    return value


def parse_locked_requirements(text: str) -> tuple[str, ...]:
    """Return normalized logical requirement records and reject loose input."""

    logical: list[str] = []
    current = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("--hash="):
            if not current:
                raise RuntimeLockVerificationError("orphaned requirement hash")
            current += " " + stripped.rstrip("\\").strip()
        elif current:
            logical.append(current)
            current = stripped.rstrip("\\").strip()
        else:
            current = stripped.rstrip("\\").strip()
        if raw_line.rstrip().endswith("\\"):
            continue
        if current:
            logical.append(current)
            current = ""
    if current:
        logical.append(current)

    records = tuple(value for value in logical if not value.startswith("--"))
    if not records:
        raise RuntimeLockVerificationError("runtime requirements export is empty")
    for record in records:
        if not _REQUIREMENT_RE.match(record):
            raise RuntimeLockVerificationError(
                f"runtime requirement is not exact: {record.split()[0]}"
            )
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\\s|$)", record)
        if not hashes:
            raise RuntimeLockVerificationError(
                f"runtime requirement has no SHA-256 hash: {record.split()[0]}"
            )
    return records


def verify_runtime_lock(metadata_path: Path = METADATA_PATH) -> dict[str, Any]:
    metadata = _strict_object(
        json.loads(metadata_path.read_text(encoding="utf-8")),
        {
            "schema_version",
            "authoritative_lock",
            "authoritative_lock_sha256",
            "export_command",
            "requirements_export",
            "requirements_export_sha256",
            "requirements_count",
        },
        "runtime lock metadata",
    )
    if metadata["schema_version"] != 1:
        raise RuntimeLockVerificationError("unsupported runtime lock metadata schema")
    export_command = metadata["export_command"]
    if not isinstance(export_command, list) or not all(
        isinstance(value, str) and value for value in export_command
    ):
        raise RuntimeLockVerificationError("export_command must be non-empty strings")
    if "--frozen" not in export_command or "--no-dev" not in export_command:
        raise RuntimeLockVerificationError(
            "runtime export command must be frozen and exclude development groups"
        )
    authoritative_lock = _project_path(
        metadata["authoritative_lock"],
        label="authoritative_lock",
    )
    requirements_export = _project_path(
        metadata["requirements_export"],
        label="requirements_export",
    )
    expected_lock_digest = _required_digest(metadata, "authoritative_lock_sha256")
    expected_export_digest = _required_digest(metadata, "requirements_export_sha256")
    if _sha256(authoritative_lock) != expected_lock_digest:
        raise RuntimeLockVerificationError(
            "uv.lock changed without regenerating the runtime requirements export"
        )
    if _sha256(requirements_export) != expected_export_digest:
        raise RuntimeLockVerificationError(
            "runtime requirements export changed without refreshing its metadata"
        )
    records = parse_locked_requirements(
        requirements_export.read_text(encoding="utf-8")
    )
    expected_count = metadata["requirements_count"]
    if not isinstance(expected_count, int) or expected_count < 1:
        raise RuntimeLockVerificationError("requirements_count must be positive")
    if len(records) != expected_count:
        raise RuntimeLockVerificationError(
            f"runtime requirements count differs: {len(records)} != {expected_count}"
        )
    return {
        "schema_version": 1,
        "status": "passed",
        "authoritative_lock_sha256": expected_lock_digest,
        "requirements_export_sha256": expected_export_digest,
        "requirements_count": len(records),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the committed Python runtime lock export."
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON result.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_runtime_lock()
    except (OSError, json.JSONDecodeError, RuntimeLockVerificationError) as exc:
        if args.json:
            print(
                json.dumps(
                    {"schema_version": 1, "status": "failed", "error": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "Python runtime lock verified: "
            f"{result['requirements_count']} exact hash-pinned requirements."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

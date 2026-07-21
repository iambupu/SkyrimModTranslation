"""Prepare hash-bound master-style evidence before plugin translation starts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from file_utils import sha256_file
from plugin_master_style_policy import known_full_masters
from plugin_resource_evidence import (
    create_evidence_directory_under,
    plugin_artifact_key,
    validate_regular_evidence_path_under,
)
from project_paths import is_under


TES4_HEADER_SIZE = 24
TES4_LIGHT_FLAG = 0x00000200
MAX_TES4_DATA_BYTES = 16 * 1024 * 1024
GENERATOR_ID = "plugin-master-style-preflight-v1"


@dataclass(frozen=True)
class PluginHeader:
    small_flag: bool
    masters: tuple[str, ...]


def _error(code: str, message: str) -> ValueError:
    return ValueError(f"{code}: {message}")


def _file_state(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def create_cached_sha256_resolver(
    resolver: Callable[[Path], str] = sha256_file,
) -> Callable[[Path], str]:
    """Cache unchanged master hashes for the lifetime of one workflow stage."""
    cache: dict[str, tuple[tuple[int, int, int, int, int], str]] = {}

    def resolve(path: Path) -> str:
        resolved = path.resolve(strict=True)
        before = _file_state(resolved)
        key = os.path.normcase(str(resolved))
        cached = cache.get(key)
        if cached is not None and cached[0] == before:
            return cached[1]
        digest = resolver(resolved)
        after = _file_state(resolved)
        if after != before:
            raise _error(
                "master_style_evidence_stale",
                f"master-style evidence changed while it was being hashed: {resolved}",
            )
        cache[key] = (after, digest)
        return digest

    return resolve


def read_plugin_header(path: Path) -> PluginHeader:
    try:
        with path.open("rb") as handle:
            header = handle.read(TES4_HEADER_SIZE)
            if len(header) != TES4_HEADER_SIZE or header[:4] != b"TES4":
                raise _error(
                    "master_style_conflict",
                    f"plugin does not contain a complete TES4 header: {path}",
                )
            data_size = int.from_bytes(header[4:8], "little")
            if data_size > MAX_TES4_DATA_BYTES:
                raise _error(
                    "master_style_conflict",
                    f"TES4 header exceeds {MAX_TES4_DATA_BYTES} bytes: {path}",
                )
            data = handle.read(data_size)
    except OSError as exc:
        raise _error(
            "master_style_evidence_stale",
            f"could not read plugin header {path}: {exc}",
        ) from exc
    if len(data) != data_size:
        raise _error(
            "master_style_conflict",
            f"TES4 header data exceeds the plugin boundary: {path}",
        )

    masters: list[str] = []
    offset = 0
    extended_size: int | None = None
    while offset < len(data):
        if offset + 6 > len(data):
            raise _error(
                "master_style_conflict",
                f"TES4 contains a truncated subrecord header: {path}",
            )
        signature = data[offset : offset + 4]
        short_size = int.from_bytes(data[offset + 4 : offset + 6], "little")
        offset += 6
        if signature == b"XXXX":
            if short_size != 4 or extended_size is not None or offset + 4 > len(data):
                raise _error(
                    "master_style_conflict",
                    f"TES4 contains an invalid XXXX subrecord: {path}",
                )
            extended_size = int.from_bytes(data[offset : offset + 4], "little")
            offset += 4
            continue
        payload_size = extended_size if extended_size is not None else short_size
        extended_size = None
        payload_end = offset + payload_size
        if payload_end > len(data):
            raise _error(
                "master_style_conflict",
                f"TES4 subrecord payload exceeds the record boundary: {path}",
            )
        if signature == b"MAST":
            raw_name = data[offset:payload_end].split(b"\0", 1)[0]
            try:
                name = raw_name.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise _error(
                    "master_style_conflict",
                    f"TES4 MAST is not valid UTF-8: {path}",
                ) from exc
            if not name or Path(name).name != name or Path(name).suffix.casefold() not in {
                ".esp",
                ".esm",
                ".esl",
            }:
                raise _error(
                    "master_style_conflict",
                    f"TES4 contains an invalid MAST name {name!r}: {path}",
                )
            if name.casefold() in {item.casefold() for item in masters}:
                raise _error(
                    "master_style_conflict",
                    f"TES4 contains duplicate MAST {name}: {path}",
                )
            masters.append(name)
        offset = payload_end
    if extended_size is not None:
        raise _error(
            "master_style_conflict",
            f"TES4 contains an orphan XXXX subrecord: {path}",
        )
    flags = int.from_bytes(header[8:12], "little")
    return PluginHeader(bool(flags & TES4_LIGHT_FLAG), tuple(masters))


def _style(path: Path, header: PluginHeader) -> str:
    return "light" if path.suffix.casefold() == ".esl" or header.small_flag else "full"


def _relative(root: Path, path: Path) -> str:
    return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()


def _write_json_atomic(path: Path, payload: dict[str, object], *, allowed_root: Path) -> None:
    if not os.path.lexists(allowed_root):
        create_evidence_directory_under(
            allowed_root,
            allowed_root.parent,
            label="Master-style manifest root",
        )
    parent = create_evidence_directory_under(
        path.parent,
        allowed_root,
        label="Master-style manifest directory",
    )
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _existing_master_candidates(
    root: Path,
    plugin: Path,
    mod_name: str,
    game_id: str,
    master_name: str,
) -> list[Path]:
    candidates = (
        plugin.parent / master_name,
        root / "work" / "master_context" / game_id / master_name,
        root / "work" / "master_context" / mod_name / master_name,
        root / "work" / "master_context" / master_name,
    )
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not os.path.lexists(candidate):
            continue
        validated = validate_regular_evidence_path_under(
            candidate,
            root,
            kind="file",
            label=f"Master-style evidence for {master_name}",
        )
        identity = os.path.normcase(str(validated))
        if identity not in seen:
            seen.add(identity)
            result.append(validated)
    return result


def _read_existing_manifest(
    path: Path,
    *,
    game_id: str,
    plugin: Path,
    expected_masters: tuple[str, ...],
) -> tuple[dict[str, object], list[dict[str, object]], set[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error("master_style_conflict", f"master-style manifest is invalid: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise _error("master_style_conflict", "master-style manifest schema_version must be 2")
    if payload.get("game_id") != game_id or str(payload.get("plugin", "")).casefold() != plugin.name.casefold():
        raise _error("master_style_conflict", "master-style manifest identity does not match the input plugin")
    rows = payload.get("masters")
    if not isinstance(rows, list) or not rows:
        raise _error("master_style_conflict", "master-style manifest masters must not be empty")
    expected = {name.casefold() for name in expected_masters}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _error("master_style_conflict", "master-style manifest contains a non-object master")
        mod_key = str(row.get("mod_key", "")).strip()
        key = mod_key.casefold()
        if key not in expected or key in seen:
            raise _error("master_style_conflict", f"master-style manifest contains unexpected or duplicate master {mod_key!r}")
        seen.add(key)
    return payload, rows, seen


def _validate_existing_manifest(
    path: Path,
    *,
    root: Path,
    game_id: str,
    plugin: Path,
    expected_masters: tuple[str, ...],
    sha256_resolver: Callable[[Path], str],
    required_masters: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    _, rows, _ = _read_existing_manifest(
        path,
        game_id=game_id,
        plugin=plugin,
        expected_masters=expected_masters,
    )
    expected = {name.casefold() for name in expected_masters}
    required = (
        None
        if required_masters is None
        else {name.casefold() for name in required_masters}
    )
    requested = required or set()
    unexpected_required = requested - expected
    if unexpected_required:
        raise _error(
            "master_style_conflict",
            "master-style evidence was requested for a non-master: "
            + ", ".join(sorted(unexpected_required)),
        )
    verified: list[dict[str, object]] = []
    covered: set[str] = set()
    for row in rows:
        mod_key = str(row.get("mod_key", "")).strip()
        key = mod_key.casefold()
        if required is not None and key not in required:
            continue
        inspected_raw = str(row.get("inspected_path", "")).strip()
        inspected = validate_regular_evidence_path_under(
            root.joinpath(*Path(inspected_raw.replace("\\", "/")).parts),
            root,
            kind="file",
            label=f"Master-style manifest evidence for {mod_key}",
        )
        if inspected.name.casefold() != key:
            raise _error("master_style_conflict", f"master-style evidence identity mismatch for {mod_key}")
        actual_hash = sha256_resolver(inspected)
        if actual_hash != str(row.get("inspected_sha256", "")).casefold():
            raise _error("master_style_evidence_stale", f"master-style evidence hash is stale for {mod_key}")
        header = read_plugin_header(inspected)
        if row.get("small_flag") is not header.small_flag or row.get("master_style") != _style(inspected, header):
            raise _error("master_style_conflict", f"master-style evidence conflicts with the header for {mod_key}")
        covered.add(key)
        verified.append(row)
    missing = sorted(requested - covered)
    if missing:
        raise _error("master_style_unknown", f"master-style manifest is missing: {', '.join(missing)}")
    return verified


def validate_master_style_manifest(
    path: Path | None,
    *,
    root: Path,
    game_id: str,
    plugin: Path,
    required_masters: tuple[str, ...] | None = None,
    sha256_resolver: Callable[[Path], str] = sha256_file,
) -> Path | None:
    """Validate an optional target-scoped apply manifest."""
    root = root.resolve(strict=True)
    plugin = validate_regular_evidence_path_under(
        plugin,
        root,
        kind="file",
        label="Plugin master-style manifest input",
    )
    if path is None:
        if required_masters:
            raise _error(
                "master_style_unknown",
                "master-style manifest is missing for translated target owner(s): "
                + ", ".join(sorted(required_masters, key=str.casefold)),
            )
        return None
    header = read_plugin_header(plugin)
    manifest = validate_regular_evidence_path_under(
        path,
        root / "work" / "plugin_context",
        kind="file",
        label="Input master-style manifest",
    )
    _validate_existing_manifest(
        manifest,
        root=root,
        game_id=game_id,
        plugin=plugin,
        expected_masters=header.masters,
        required_masters=required_masters,
        sha256_resolver=sha256_resolver,
    )
    return manifest


def prepare_master_style_manifest(
    *,
    root: Path,
    game_id: str,
    mod_name: str,
    plugin: Path,
    plugin_root: Path | None = None,
    relative_plugin: Path,
    required_masters: tuple[str, ...] = (),
    sha256_resolver: Callable[[Path], str] = sha256_file,
) -> Path | None:
    """Return schema-2 evidence only for master owners used by translation targets."""
    root = root.resolve(strict=True)
    default_lane_root = root / "work" / "extracted_mods" / mod_name
    allowed_plugin_root = default_lane_root if plugin_root is None else plugin_root
    allowed_plugin_root = allowed_plugin_root.resolve(strict=False)
    lane_roots = (
        root / "work" / "extracted_mods" / mod_name,
        root / "work" / "archive_extracts" / mod_name,
    )
    matching_lane = next(
        (lane for lane in lane_roots if is_under(allowed_plugin_root, lane)),
        None,
    )
    if matching_lane is None:
        raise _error(
            "master_style_conflict",
            "plugin_root must stay inside the selected extracted_mods or "
            "archive_extracts Mod lane",
        )
    allowed_plugin_root = validate_regular_evidence_path_under(
        allowed_plugin_root,
        matching_lane,
        kind="directory",
        label="Plugin master-style preflight lane",
    )
    plugin = validate_regular_evidence_path_under(
        plugin,
        allowed_plugin_root,
        kind="file",
        label="Plugin master-style preflight input",
    )
    header = read_plugin_header(plugin)
    known_full = known_full_masters(game_id)
    requested = {name.casefold() for name in required_masters}
    declared = {name.casefold(): name for name in header.masters}
    unexpected = requested - set(declared)
    if unexpected:
        raise _error(
            "master_style_conflict",
            "master-style evidence was requested for a non-master: "
            + ", ".join(sorted(unexpected)),
        )
    non_esl_masters = tuple(
        name
        for name in header.masters
        if name.casefold() in requested
        if Path(name).suffix.casefold() != ".esl"
        and name.casefold() not in known_full
    )
    if not non_esl_masters:
        return None

    artifact_key = plugin_artifact_key(mod_name, relative_plugin)
    destination = (
        root
        / "work"
        / "plugin_context"
        / mod_name
        / f"{artifact_key}.master-styles.json"
    )
    if os.path.lexists(destination):
        existing = validate_regular_evidence_path_under(
            destination,
            root / "work" / "plugin_context",
            kind="file",
            label="Input master-style manifest",
        )
        _, _, existing_keys = _read_existing_manifest(
            existing,
            game_id=game_id,
            plugin=plugin,
            expected_masters=header.masters,
        )
        required_keys = {name.casefold() for name in non_esl_masters}
        if required_keys.issubset(existing_keys):
            verified_rows = _validate_existing_manifest(
                existing,
                root=root,
                game_id=game_id,
                plugin=plugin,
                expected_masters=header.masters,
                required_masters=non_esl_masters,
                sha256_resolver=sha256_resolver,
            )
            if existing_keys != required_keys:
                _write_json_atomic(
                    destination,
                    {
                        "schema_version": 2,
                        "game_id": game_id,
                        "plugin": plugin.name,
                        "generated_by": GENERATOR_ID,
                        "masters": verified_rows,
                    },
                    allowed_root=root / "work" / "plugin_context",
                )
            return existing

    rows: list[dict[str, object]] = []
    for master_name in non_esl_masters:
        candidates = _existing_master_candidates(
            root,
            plugin,
            mod_name,
            game_id,
            master_name,
        )
        if not candidates:
            expected = root / "work" / "master_context" / game_id / master_name
            raise _error(
                "master_style_unknown",
                f"cannot confirm {master_name}; copy it to {expected} or beside the input plugin",
            )
        evidence = [(candidate, sha256_resolver(candidate)) for candidate in candidates]
        if len({digest for _, digest in evidence}) != 1:
            raise _error(
                "master_style_conflict",
                f"multiple different workspace copies were found for {master_name}",
            )
        inspected, digest = evidence[0]
        inspected_header = read_plugin_header(inspected)
        rows.append(
            {
                "mod_key": master_name,
                "master_style": _style(inspected, inspected_header),
                "inspected_path": _relative(root, inspected),
                "inspected_sha256": digest,
                "small_flag": inspected_header.small_flag,
            }
        )

    _write_json_atomic(
        destination,
        {
            "schema_version": 2,
            "game_id": game_id,
            "plugin": plugin.name,
            "generated_by": GENERATOR_ID,
            "masters": rows,
        },
        allowed_root=root / "work" / "plugin_context",
    )
    return destination

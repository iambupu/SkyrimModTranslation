"""Immutable, content-based identities for the public SMT input boundary."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import unicodedata
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from file_utils import (
    discover_regular_tree,
    is_reparse_point,
    validate_regular_path_under,
)
from game_context import GameContext
from project_paths import assert_no_risky_marker, risky_marker, safe_file_name


DIRECTORY_MAGIC = b"SMT-INPUT-DIR\x00"
DIRECTORY_VERSION = 1
HASH_CHUNK_SIZE = 1024 * 1024
FINGERPRINT_ALGORITHM = "smt-input-v1"
MAX_WORKSPACE_NAME_UNITS = 80

SourceKind = Literal["directory", "zip", "7z"]
EntryType = Literal["file", "directory"]


class UnsupportedInputError(ValueError):
    """Raised when the top-level input type is outside the public contract."""


class InputChangedError(ValueError):
    """Raised when an input no longer matches its immutable manifest."""


class InputSafetyError(ValueError):
    """Raised when a path violates the existing project safety policy."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class InputEntry:
    relative_path: str
    entry_type: EntryType
    size: int
    sha256: str | None
    identity: FileIdentity | None


@dataclass(frozen=True)
class InputManifest:
    source_kind: SourceKind
    entries: Sequence[InputEntry]
    digest: str
    source_identity: FileIdentity | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _assert_safe_location(path: Path, context: GameContext | None) -> None:
    profile_marker = risky_marker(path, context=context)
    if profile_marker:
        raise InputSafetyError(
            f"path contains forbidden game/mod-manager marker '{profile_marker}': {path}"
        )
    try:
        assert_no_risky_marker(path)
    except ValueError as exc:
        raise InputSafetyError(str(exc)) from exc


def _validate_regular_input(
    path: Path,
    *,
    kind: Literal["file", "directory"],
    context: GameContext | None,
    label: str,
) -> Path:
    lexical_path = _lexical_absolute(path)
    _assert_safe_location(lexical_path, context)
    if not os.path.lexists(lexical_path):
        raise InputSafetyError(f"{label} does not exist: {path}")
    try:
        entry_stat = lexical_path.lstat()
        if lexical_path.is_symlink() or is_reparse_point(entry_stat):
            raise ValueError(f"{label} is a symlink, junction, or reparse point: {path}")
        anchor = Path(lexical_path.anchor)
        return validate_regular_path_under(
            lexical_path,
            anchor,
            kind=kind,
            label=label,
        )
    except (OSError, ValueError) as exc:
        raise InputSafetyError(str(exc)) from exc


def _file_identity(path: Path) -> FileIdentity:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise InputSafetyError(f"cannot stat regular input file {path}: {exc}") from exc
    if path.is_symlink() or is_reparse_point(file_stat):
        raise InputSafetyError(f"input file is a symlink, junction, or reparse point: {path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise InputSafetyError(f"input file is not a regular file: {path}")
    if file_stat.st_nlink != 1:
        raise InputSafetyError(f"input file has multiple hardlinks: {path}")
    return FileIdentity(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
    )


def _read_file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                return
            yield chunk


def _hash_regular_file(path: Path, root: Path) -> tuple[str, FileIdentity]:
    try:
        validate_regular_path_under(path, root, kind="file", label="SMT input file")
    except (OSError, ValueError) as exc:
        raise InputSafetyError(str(exc)) from exc
    before = _file_identity(path)
    digest = hashlib.sha256()
    try:
        for chunk in _read_file_chunks(path):
            digest.update(chunk)
    except OSError as exc:
        raise InputSafetyError(f"cannot read input file {path}: {exc}") from exc
    after = _file_identity(path)
    if after != before:
        raise InputChangedError(f"input file changed while hashing: {path}")
    return digest.hexdigest(), before


def _normalized_relative(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return unicodedata.normalize("NFC", relative)


def _directory_entries(root: Path) -> tuple[InputEntry, ...]:
    try:
        files, directories = discover_regular_tree(root, label="SMT input directory")
    except (OSError, ValueError) as exc:
        raise InputSafetyError(str(exc)) from exc

    candidates: list[tuple[bytes, str, EntryType, Path]] = []
    casefold_paths: dict[str, str] = {}
    for entry_type, paths in (("directory", directories), ("file", files)):
        for path in paths:
            relative_path = _normalized_relative(root, path)
            collision_key = relative_path.casefold()
            previous = casefold_paths.get(collision_key)
            if previous is not None:
                raise InputSafetyError(
                    "case-insensitive path collision after NFC normalization: "
                    f"{previous!r} and {relative_path!r}"
                )
            casefold_paths[collision_key] = relative_path
            candidates.append(
                (relative_path.encode("utf-8"), relative_path, entry_type, path)
            )

    entries: list[InputEntry] = []
    for _sort_key, relative_path, entry_type, path in sorted(
        candidates,
        key=lambda candidate: candidate[0],
    ):
        if entry_type == "directory":
            entries.append(
                InputEntry(
                    relative_path=relative_path,
                    entry_type="directory",
                    size=0,
                    sha256=None,
                    identity=None,
                )
            )
            continue
        file_sha256, identity = _hash_regular_file(path, root)
        entries.append(
            InputEntry(
                relative_path=relative_path,
                entry_type="file",
                size=identity.size,
                sha256=file_sha256,
                identity=identity,
            )
        )
    return tuple(entries)


def _directory_digest(entries: tuple[InputEntry, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(DIRECTORY_MAGIC)
    digest.update(struct.pack(">H", DIRECTORY_VERSION))
    digest.update(struct.pack(">Q", len(entries)))
    for entry in entries:
        relative_bytes = entry.relative_path.encode("utf-8")
        digest.update(b"\x01" if entry.entry_type == "directory" else b"\x02")
        digest.update(struct.pack(">I", len(relative_bytes)))
        digest.update(relative_bytes)
        if entry.entry_type == "file":
            if entry.sha256 is None:
                raise ValueError(f"file entry has no SHA-256: {entry.relative_path}")
            digest.update(struct.pack(">Q", entry.size))
            digest.update(bytes.fromhex(entry.sha256))
    return digest.hexdigest()


def _build_directory_manifest(
    path: Path,
    context: GameContext | None,
) -> InputManifest:
    root = _validate_regular_input(
        path,
        kind="directory",
        context=context,
        label="SMT input directory",
    )
    entries = _directory_entries(root)
    return InputManifest(
        source_kind="directory",
        entries=entries,
        digest=_directory_digest(entries),
        source_identity=None,
    )


def _build_archive_manifest(
    path: Path,
    source_kind: Literal["zip", "7z"],
    context: GameContext | None,
) -> InputManifest:
    archive = _validate_regular_input(
        path,
        kind="file",
        context=context,
        label=f"SMT {source_kind} input",
    )
    anchor = Path(archive.anchor)
    digest, identity = _hash_regular_file(archive, anchor)
    return InputManifest(
        source_kind=source_kind,
        entries=(),
        digest=digest,
        source_identity=identity,
    )


def build_input_manifest(
    path: Path,
    context: GameContext | None = None,
) -> InputManifest:
    """Build one immutable manifest for a safe directory, ZIP, or 7Z input."""
    lexical_path = _lexical_absolute(Path(path))
    _assert_safe_location(lexical_path, context)
    if not os.path.lexists(lexical_path):
        raise InputSafetyError(f"SMT input does not exist: {path}")
    try:
        input_stat = lexical_path.lstat()
    except OSError as exc:
        raise InputSafetyError(f"cannot stat SMT input {path}: {exc}") from exc
    if lexical_path.is_symlink() or is_reparse_point(input_stat):
        raise InputSafetyError(f"SMT input is a symlink, junction, or reparse point: {path}")
    if stat.S_ISDIR(input_stat.st_mode):
        return _build_directory_manifest(lexical_path, context)
    if not stat.S_ISREG(input_stat.st_mode):
        raise InputSafetyError(f"SMT input is not a regular file or directory: {path}")

    suffix = lexical_path.suffix.casefold()
    if suffix == ".zip":
        return _build_archive_manifest(lexical_path, "zip", context)
    if suffix == ".7z":
        return _build_archive_manifest(lexical_path, "7z", context)
    raise UnsupportedInputError(
        "unsupported top-level input; expected a regular directory, .zip, or .7z file: "
        f"{path}"
    )


def composite_input_identity(game_id: str, manifest: InputManifest) -> str:
    return (
        f"{FINGERPRINT_ALGORITHM}:{game_id}:{manifest.source_kind}:{manifest.digest}"
    )


def _rebuild_expected_kind(
    path: Path,
    source_kind: SourceKind,
    context: GameContext | None,
) -> InputManifest:
    if source_kind == "directory":
        return _build_directory_manifest(path, context)
    return _build_archive_manifest(path, source_kind, context)


def verify_source_unchanged(path: Path, manifest: InputManifest) -> None:
    """Re-hash the complete source and require its original identities as well."""
    try:
        current = _rebuild_expected_kind(Path(path), manifest.source_kind, None)
    except (InputChangedError, InputSafetyError, UnsupportedInputError) as exc:
        raise InputChangedError(f"source input changed: {path}: {exc}") from exc
    if current != manifest:
        raise InputChangedError(f"source input changed: {path}")


def _content_entries(manifest: InputManifest) -> tuple[tuple[str, str, int, str | None], ...]:
    return tuple(
        (entry.relative_path, entry.entry_type, entry.size, entry.sha256)
        for entry in manifest.entries
    )


def verify_imported_copy(target: Path, manifest: InputManifest) -> None:
    """Verify a copied target by the expected kind, including suffixless staging files."""
    try:
        current = _rebuild_expected_kind(Path(target), manifest.source_kind, None)
    except (InputChangedError, InputSafetyError, UnsupportedInputError) as exc:
        raise InputChangedError(f"imported copy changed: {target}: {exc}") from exc
    if (
        current.source_kind != manifest.source_kind
        or current.digest != manifest.digest
        or _content_entries(current) != _content_entries(manifest)
    ):
        raise InputChangedError(f"imported copy changed: {target}")


def _utf16_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _truncate_utf16(value: str, maximum_units: int) -> str:
    if maximum_units < 1:
        raise ValueError("maximum UTF-16 length must be positive")
    units = 0
    result: list[str] = []
    for character in value:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if units + character_units > maximum_units:
            break
        result.append(character)
        units += character_units
    return "".join(result)


def _bounded_safe_name(value: str, maximum_units: int = MAX_WORKSPACE_NAME_UNITS) -> str:
    sanitized = safe_file_name(value)
    bounded = _truncate_utf16(sanitized, maximum_units)
    return safe_file_name(bounded)


def derive_mod_name(path: Path) -> str:
    """Derive a safe Mod name from a directory name or supported archive stem."""
    source = Path(path)
    if source.is_dir():
        raw_name = source.name
    elif source.suffix.casefold() in {".zip", ".7z"}:
        raw_name = source.stem
    else:
        raw_name = source.name
    return _bounded_safe_name(raw_name)


def _windows_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _name_with_suffix(base: str, suffix: str) -> str:
    available = MAX_WORKSPACE_NAME_UNITS - _utf16_units(suffix)
    prefix = _truncate_utf16(base, available).rstrip(" .")
    if not prefix:
        prefix = "_"
    return _bounded_safe_name(prefix + suffix)


def choose_workspace_name(
    mod_name: str,
    digest: str,
    occupied: Collection[str],
) -> str:
    """Choose a case-insensitively unique, UTF-16-bounded workspace name."""
    if len(digest) < 8:
        raise ValueError("digest must contain at least eight characters")
    safe_display_name = safe_file_name(mod_name)
    was_truncated = _utf16_units(safe_display_name) > MAX_WORKSPACE_NAME_UNITS
    base = _bounded_safe_name(safe_display_name)
    occupied_keys = {_windows_name_key(str(name)) for name in occupied}
    if not was_truncated and _windows_name_key(base) not in occupied_keys:
        return base

    digest_suffix = f"-{digest[:8]}"
    candidate = _name_with_suffix(base, digest_suffix)
    if _windows_name_key(candidate) not in occupied_keys:
        return candidate

    counter = 2
    while True:
        candidate = _name_with_suffix(base, f"{digest_suffix}-{counter}")
        if _windows_name_key(candidate) not in occupied_keys:
            return candidate
        counter += 1

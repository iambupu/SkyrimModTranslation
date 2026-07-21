"""Shared deterministic file operations used by workflow scripts."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any


FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---(?:\s*\r?\n|$)", re.DOTALL)


def py7zr_available() -> bool:
    try:
        importlib.import_module("py7zr")
    except Exception:
        return False
    return True


def is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _same_platform_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def lexical_path_chain_under(
    path: Path,
    root: Path,
    *,
    label: str,
) -> tuple[Path, list[Path]]:
    """Return the lexical path chain while accepting aliases of the same root."""
    lexical_path = Path(os.path.abspath(path))
    lexical_root = Path(os.path.abspath(root))
    if not os.path.lexists(lexical_root):
        raise ValueError(f"{label} root does not exist: {root}")

    current = lexical_path
    suffix: list[str] = []
    while True:
        if os.path.lexists(current):
            try:
                if os.path.samefile(current, lexical_root):
                    anchor = current
                    break
            except OSError:
                pass
        parent = current.parent
        if parent == current:
            raise ValueError(f"{label} is outside its allowed root: {path}")
        suffix.append(current.name)
        current = parent

    chain = [lexical_root]
    if not _same_platform_path(anchor, lexical_root):
        chain.append(anchor)
    current = anchor
    for part in reversed(suffix):
        current = current / part
        chain.append(current)
    return lexical_path, chain


def validate_regular_path_under(
    path: Path,
    root: Path,
    *,
    kind: str,
    label: str,
) -> Path:
    """Validate one existing file-system entry without following links first."""
    if kind not in {"file", "directory"}:
        raise ValueError(f"Unsupported path kind: {kind}")
    lexical_root = Path(os.path.abspath(root))
    lexical_path, chain = lexical_path_chain_under(path, root, label=label)
    for candidate in chain:
        candidate_stat = candidate.lstat()
        if candidate.is_symlink() or is_reparse_point(candidate_stat):
            raise ValueError(
                f"{label} path contains a symlink, junction, or reparse point: {candidate}"
            )
        if candidate != lexical_path and not stat.S_ISDIR(candidate_stat.st_mode):
            raise ValueError(f"{label} parent is not a regular directory: {candidate}")
    entry_stat = lexical_path.lstat()
    if lexical_path.is_symlink() or is_reparse_point(entry_stat):
        raise ValueError(f"{label} is a symlink, junction, or reparse point: {path}")
    expected = stat.S_ISREG(entry_stat.st_mode) if kind == "file" else stat.S_ISDIR(entry_stat.st_mode)
    if not expected:
        raise ValueError(f"{label} is not a regular {kind}: {path}")
    if kind == "file" and entry_stat.st_nlink != 1:
        raise ValueError(f"{label} has multiple hardlinks: {path}")
    resolved_root = lexical_root.resolve(strict=True)
    resolved_path = lexical_path.resolve(strict=True)
    try:
        if not _same_platform_path(
            os.path.commonpath((resolved_path, resolved_root)),
            resolved_root,
        ):
            raise ValueError(f"{label} resolves outside its allowed root: {path}")
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside its allowed root: {path}") from exc
    return resolved_path


def discover_regular_tree(
    root: Path,
    *,
    label: str,
    max_files: int | None = None,
) -> tuple[list[Path], list[Path]]:
    """Enumerate regular files and directories without accepting links."""
    if max_files is not None and max_files < 1:
        raise ValueError("max_files must be positive when provided")
    root = validate_regular_path_under(root, root, kind="directory", label=label)
    files: list[Path] = []
    directories: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda entry: entry.name.casefold())
        for entry in children:
            path = Path(entry.path)
            entry_stat = path.lstat()
            if path.is_symlink() or is_reparse_point(entry_stat):
                raise ValueError(f"{label} contains a symlink, junction, or reparse point: {path}")
            if stat.S_ISDIR(entry_stat.st_mode):
                directories.append(path)
                stack.append(path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError(f"{label} contains a non-regular file: {path}")
            if entry_stat.st_nlink != 1:
                raise ValueError(f"{label} contains a file with multiple hardlinks: {path}")
            files.append(path)
            if max_files is not None and len(files) >= max_files:
                key = lambda item: item.relative_to(root).as_posix().casefold()
                return sorted(files, key=key), sorted(directories, key=key)
    key = lambda path: path.relative_to(root).as_posix().casefold()
    return sorted(files, key=key), sorted(directories, key=key)


def discover_regular_files(root: Path, *, label: str, max_files: int | None = None) -> list[Path]:
    """Enumerate a tree without following or accepting link-like entries."""
    files, _directories = discover_regular_tree(
        root,
        label=label,
        max_files=max_files,
    )
    return files


def create_regular_directory_under(path: Path, root: Path, *, label: str) -> Path:
    """Create a directory tree without traversing link-like parents."""
    lexical_root = Path(os.path.abspath(root))
    validate_regular_path_under(
        lexical_root,
        lexical_root,
        kind="directory",
        label=f"{label} root",
    )
    lexical_path, chain = lexical_path_chain_under(path, root, label=label)
    for current in chain:
        if _same_platform_path(current, lexical_root):
            continue
        if not os.path.lexists(current):
            current.mkdir()
        validate_regular_path_under(
            current,
            lexical_root,
            kind="directory",
            label=label,
        )
    return lexical_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_upper(path: Path) -> str:
    return sha256_file(path).upper()


def read_text_utf8_sig(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_text_utf8_sig_strict(path: Path) -> str:
    return read_text_utf8_sig(path)


def has_utf16_le_bom(path: Path) -> bool:
    return path.read_bytes().startswith(b"\xff\xfe")


def read_text_auto(path: Path, encodings: tuple[str, ...]) -> str:
    last_error: UnicodeError | None = None
    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise UnicodeError(f"No text encodings were configured for {path}")


def read_text_auto_cp1252(path: Path) -> str:
    return read_text_auto(path, ("utf-8-sig", "utf-16", "cp1252"))


def read_text_auto_cp936(path: Path) -> str:
    return read_text_auto(path, ("utf-8-sig", "utf-16", "cp936"))


def read_lines_auto_cp936(path: Path) -> list[str]:
    return read_text_auto_cp936(path).splitlines()


def read_json_object_required(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload


def read_json_unchecked(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_json_object_if_exists_strict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must contain an object: {path}")
    return payload


def read_json_object_or_empty(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(read_text_utf8_sig(path))
    except (UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_json_object_or_empty_with_parse_errors(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_json_object_or_empty_any(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_json_object_or_invalid(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(read_text_utf8_sig(path))
    except (UnicodeError, json.JSONDecodeError):
        return {"_invalid_json": True}
    return payload if isinstance(payload, dict) else {"_invalid_json": True}


def read_json_object_or_invalid_any(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"_invalid_json": True}
    return payload if isinstance(payload, dict) else {"_invalid_json": True}


def parse_simple_frontmatter(path: Path) -> dict[str, str] | None:
    match = FRONTMATTER_RE.match(read_text_utf8_sig(path))
    if not match:
        return None
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip("'\"")
    return metadata


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_json_sorted(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_json_stream(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_jsonl_sorted(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_valid_jsonl_lines(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"line {index} is empty; JSONL requires one JSON object per line")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {index} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"line {index} is not a JSON object")
    return lines


def write_text_lines_if_changed(path: Path, lines: list[str], *, newline_if_empty: bool = True) -> bool:
    text = "\n".join(lines) + ("\n" if lines or newline_if_empty else "")
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def bytes_contains(data: bytes, pattern: bytes) -> bool:
    return bool(pattern) and data.find(pattern) >= 0


def encoded_text_present(data: bytes, text: str, *, variants: Iterable[str] | None = None) -> bool:
    candidates = list(variants) if variants is not None else [text]
    return any(
        bytes_contains(data, candidate.encode(encoding))
        for candidate in candidates
        if candidate
        for encoding in ("utf-8", "utf-16-le")
    )


def is_backup_artifact(path: Path, *, binary_extensions: set[str], backup_extensions: set[str]) -> bool:
    if path.suffix.lower() in backup_extensions:
        return True
    lowered = path.name.lower()
    return any(f".{extension[1:]}." in lowered for extension in binary_extensions)

"""Build project-local .NET adapters with source-hash cache invalidation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from file_utils import sha256_file as file_sha256
from project_paths import is_under
from workflow_lock import ResourceLock


MANIFEST_NAME = ".skyrim-chs-adapter.json"
SOURCE_SUFFIXES = {".cs", ".csproj", ".json", ".props", ".targets"}
IGNORED_DIRS = {"bin", "obj"}


def configured_dotnet_path(
    root: Path,
    config: dict[str, Any],
    *,
    source_root: Path | None = None,
) -> Path:
    decoder_tools = config.get("DecoderTools")
    configured = ""
    if isinstance(decoder_tools, dict):
        configured = str(decoder_tools.get("DotNetSdkPath") or "")
    candidate = Path(configured) if configured else Path("tools") / "dotnet-sdk" / "dotnet.exe"
    if not candidate.is_absolute():
        workspace_candidate = root / candidate
        source_candidate = source_root / candidate if source_root is not None else None
        candidate = (
            workspace_candidate
            if workspace_candidate.exists()
            or source_candidate is None
            or not source_candidate.exists()
            else source_candidate
        )
    resolved = candidate.resolve(strict=True)
    allowed_roots = [root]
    if source_root is not None:
        allowed_roots.append(source_root.resolve(strict=True))
    if not any(is_under(resolved, allowed_root) for allowed_root in allowed_roots):
        raise ValueError(f"DotNetSdkPath must be under the workspace or plugin source: {resolved}")
    return resolved



def adapter_source_hash(
    adapter_project: Path,
    *,
    source_root: Path | None = None,
) -> str:
    adapter_project = adapter_project.resolve(strict=True)
    adapter_root = adapter_project.parent
    allowed_root = (
        source_root.resolve(strict=True)
        if source_root is not None
        else adapter_root.resolve(strict=True)
    )
    sources: set[Path] = set()
    for source in adapter_root.rglob("*"):
        if not source.is_file():
            continue
        if any(part.lower() in IGNORED_DIRS for part in source.relative_to(adapter_root).parts[:-1]):
            continue
        if source.suffix.lower() in SOURCE_SUFFIXES:
            sources.add(source.resolve(strict=True))

    try:
        project_xml = ElementTree.parse(adapter_project)
    except (OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"Invalid .NET adapter project: {adapter_project}: {exc}") from exc
    for element in project_xml.iter():
        include = element.attrib.get("Include", "")
        for raw_path in include.split(";"):
            raw_path = raw_path.strip()
            if not raw_path or "$(" in raw_path or any(marker in raw_path for marker in "*?"):
                continue
            candidate = adapter_root / raw_path.replace("\\", os.sep).replace("/", os.sep)
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if not is_under(resolved, allowed_root):
                raise ValueError(
                    f".NET adapter project input must stay under plugin source: {resolved}"
                )
            sources.add(resolved)

    digest = hashlib.sha256()
    for source in sorted(
        sources,
        key=lambda item: os.path.relpath(item, adapter_root).replace("\\", "/").lower(),
    ):
        relative = os.path.relpath(source, adapter_root).replace("\\", "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def manifest_matches(manifest_path: Path, adapter_dll: Path, adapter_name: str, source_hash: str) -> bool:
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        dll_hash = file_sha256(adapter_dll)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        manifest.get("schema_version") == "1"
        and manifest.get("adapter_name") == adapter_name
        and manifest.get("source_hash") == source_hash
        and manifest.get("adapter_dll_sha256") == dll_hash
    )


def write_manifest(manifest_path: Path, adapter_name: str, adapter_project: Path, adapter_dll: Path, source_hash: str) -> None:
    payload = {
        "schema_version": "1",
        "adapter_name": adapter_name,
        "source_hash": source_hash,
        "adapter_dll_sha256": file_sha256(adapter_dll),
        "project_path": str(adapter_project),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=manifest_path.parent,
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, manifest_path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def ensure_adapter_dll(root: Path, source_root: Path, dotnet: Path, adapter_name: str) -> Path:
    adapter_project = source_root / "adapters" / adapter_name / f"{adapter_name}.csproj"
    if not adapter_project.is_file():
        raise FileNotFoundError(f"missing adapters/{adapter_name}/{adapter_name}.csproj")

    adapter_dir = root / "tools" / "dotnet-adapters" / adapter_name
    adapter_dll = adapter_dir / f"{adapter_name}.dll"
    manifest_path = adapter_dir / MANIFEST_NAME
    lock = ResourceLock(
        root,
        f"dotnet-adapter:{adapter_name}",
        owner=f"dotnet_adapter_cache:{os.getpid()}",
    ).acquire(timeout_seconds=300.0)
    try:
        source_hash = adapter_source_hash(adapter_project, source_root=source_root)
        if adapter_dll.is_file() and manifest_matches(
            manifest_path,
            adapter_dll,
            adapter_name,
            source_hash,
        ):
            return adapter_dll

        intermediate_dir = root / "work" / "dotnet-adapter-build" / adapter_name
        adapter_dir.mkdir(parents=True, exist_ok=True)
        intermediate_dir.mkdir(parents=True, exist_ok=True)
        build_result = subprocess.run(
            [
                str(dotnet),
                "build",
                str(adapter_project),
                "--framework",
                "net8.0",
                "-p:TargetFrameworks=net8.0",
                f"-p:OutputPath={str(adapter_dir) + os.sep}",
                f"-p:BaseIntermediateOutputPath={str(intermediate_dir) + os.sep}",
                f"-p:MSBuildProjectExtensionsPath={str(intermediate_dir) + os.sep}",
            ],
            cwd=str(root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if build_result.returncode != 0:
            output = (build_result.stdout or "").strip()
            detail = f": {output[-2000:]}" if output else ""
            raise RuntimeError(f"failed to build {adapter_project}{detail}")
        if not adapter_dll.is_file():
            raise FileNotFoundError(f"adapter DLL was not produced: {adapter_dll}")
        write_manifest(manifest_path, adapter_name, adapter_project, adapter_dll, source_hash)
        return adapter_dll
    finally:
        lock.release()

"""Provision immutable shared SMT tools and bind them to one workspace."""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.request import urlopen
from zipfile import ZipFile, ZipInfo

from dotnet_adapter_cache import adapter_source_hash
from file_utils import (
    create_regular_directory_under,
    sha256_file,
    validate_regular_path_under,
)
from managed_tool_store import (
    INCOMPLETE_MARKER_NAME,
    ManagedStoreRoots,
    ManagedToolStoreError,
    ToolKey,
    WorkspaceBindingEntry,
    atomic_create_json_no_replace,
    atomic_write_json,
    bind_workspace,
    build_adapter_key,
    build_decoder_key,
    build_dotnet_sdk_key,
    build_python_key,
    commit_entry,
    create_staging_directory,
    entry_directory,
    entry_lock,
    ensure_store_layout,
    make_entry_manifest,
    managed_path,
    new_binding,
    normalize_relative_path,
    publish_movable_entry,
    reserve_catalog_reference,
    resolve_managed_store_roots,
    store_lifecycle_lock,
    validate_entry,
    write_manifest,
)
from managed_tool_migration import (
    LegacyCandidate,
    copy_legacy_tree_safely,
    discover_legacy_candidates,
    import_movable_legacy_entry,
    legacy_payload_proves_key,
)
from managed_tool_resolver import (
    FIELD_RULES,
    ToolPathResolution,
    ToolPathProvenance,
    classify_configured_tool_path,
    load_workspace_tool_config,
    read_workspace_identity_evidence,
)
from smt_windows import (
    SmtProcessFileLock,
    copy_file_exclusive,
    publish_path_no_replace,
    remove_regular_tree,
    validate_regular_single_link_file,
)
from verify_python_runtime_lock import METADATA_PATH, verify_runtime_lock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCER_VERSION = "smt-managed-tools/1"
INSTALLER_SCHEMA = 1
PYTHON_IMPORTS = ("bethesda_structs", "py7zr")
PYTHON_RUNTIME_METADATA = METADATA_PATH
DOTNET_SDK_VERSION = "8.0.422"
DOTNET_ARCHITECTURE = "x64"
DOTNET_SDK_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/Sdk/8.0.422/"
    "dotnet-sdk-8.0.422-win-x64.zip"
)
DOTNET_SDK_SHA256 = (
    "0dff8ab15ddac965944f7311453f79023a550c23062f5a7fa9b5e137b5b3639d"
)
GITHUB_ARCHIVES: Mapping[str, Mapping[str, str]] = {
    "BSAFileExtractor": {
        "ref": "cce03dfc294f1f31fa0af0fe1d2ef3b5dde67c27",
        "url": (
            "https://codeload.github.com/Sw4T/BSAFileExtractor/zip/"
            "cce03dfc294f1f31fa0af0fe1d2ef3b5dde67c27"
        ),
        "sha256": (
            "9c7138fbb6672f032c4c7a86526104ec4cbd7db9eca1672d49d73f2cfc9ea86a"
        ),
        "entry_point": "BSAFileExtractor.py",
    },
    "Champollion": {
        "ref": "bc961a0bdfb4831f8240e6dacee0818b4bf81e00",
        "url": (
            "https://codeload.github.com/Orvid/Champollion/zip/"
            "bc961a0bdfb4831f8240e6dacee0818b4bf81e00"
        ),
        "sha256": (
            "f83f626d40a88cd8e11189a908f503f8b8bcd4072e1294187687857528739b46"
        ),
        "entry_point": "Champollion.sln",
    },
}
ADAPTER_NAMES = (
    "SkyrimPluginTextTool",
    "SkyrimPexStringTool",
    "BethesdaStringTableTool",
)


Runner = Callable[..., subprocess.CompletedProcess[str]]
Downloader = Callable[[str, Path, Path], None]


@dataclass(frozen=True)
class ProvisionedTool:
    logical_name: str
    key: ToolKey
    entry_path: Path
    entry_point: str
    reused: bool

    @property
    def executable_path(self) -> Path:
        return self.entry_path.joinpath(*self.entry_point.split("/"))

    def binding_entry(self) -> WorkspaceBindingEntry:
        return WorkspaceBindingEntry(
            logical_name=self.logical_name,
            tool_kind=self.key.tool_kind,
            key_digest=self.key.key_digest,
            entry_point=self.entry_point,
        )


@dataclass(frozen=True)
class WorkspaceProvisioningResult:
    binding_path: Path
    tools: tuple[ProvisionedTool, ...]
    steps: tuple[str, ...]

    @property
    def reused_count(self) -> int:
        return sum(tool.reused for tool in self.tools)


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    runner: Runner,
    label: str,
) -> str:
    result = runner(
        list(command),
        cwd=cwd,
        env=dict(env),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = f": {output[-2000:]}" if output else ""
        raise ManagedToolStoreError(f"{label} failed with code {result.returncode}{detail}")
    return output


def _default_downloader(url: str, target: Path, allowed_root: Path) -> None:
    def copy_response(_source: Path, output: object) -> None:
        with urlopen(url, timeout=120) as response:
            shutil.copyfileobj(response, output)  # type: ignore[arg-type]

    copy_file_exclusive(Path("network-download"), target, allowed_root, copy_response)


def _zip_entry_mode(member: ZipInfo) -> int:
    return (member.external_attr >> 16) & 0xFFFF


def extract_zip_safely(archive_path: Path, target: Path, *, allowed_root: Path) -> None:
    """Extract a ZIP without trusting member paths or link metadata."""

    create_regular_directory_under(target, allowed_root, label="managed archive extraction")
    with ZipFile(archive_path) as archive:
        members = archive.infolist()
        names: list[str] = []
        normalized: list[tuple[ZipInfo, str]] = []
        for member in members:
            raw = member.filename.replace("\\", "/").rstrip("/")
            if not raw:
                continue
            relative = normalize_relative_path(raw)
            names.append(relative.casefold())
            mode = _zip_entry_mode(member)
            if stat.S_ISLNK(mode):
                raise ManagedToolStoreError(
                    f"managed archive contains a symbolic link: {relative}"
                )
            normalized.append((member, relative))
        if len(names) != len(set(names)):
            raise ManagedToolStoreError(
                "managed archive contains a case-fold path collision"
            )
        for member, relative in sorted(
            normalized,
            key=lambda item: (item[1].count("/"), item[1].casefold()),
        ):
            destination = target.joinpath(*relative.split("/"))
            if member.is_dir():
                create_regular_directory_under(
                    destination,
                    target,
                    label="managed archive directory",
                )
                continue
            create_regular_directory_under(
                destination.parent,
                target,
                label="managed archive parent",
            )

            def copy_member(
                _source: Path,
                output: object,
                *,
                _member: ZipInfo = member,
            ) -> None:
                with archive.open(_member, "r") as source:
                    shutil.copyfileobj(source, output)  # type: ignore[arg-type]

            copy_file_exclusive(
                archive_path,
                destination,
                target,
                copy_member,
            )


def _single_extracted_root(extract_root: Path) -> Path:
    children = sorted(extract_root.iterdir(), key=lambda path: path.name.casefold())
    if len(children) != 1 or not children[0].is_dir():
        raise ManagedToolStoreError(
            f"managed archive must contain one root directory, found {len(children)}"
        )
    return validate_regular_path_under(
        children[0],
        extract_root,
        kind="directory",
        label="managed archive root",
    )


def _move_incomplete_to_trash(
    roots: ManagedStoreRoots,
    target: Path,
    *,
    label: str,
) -> Path:
    target = validate_regular_path_under(
        target,
        roots.entries,
        kind="directory",
        label=f"{label} managed entry",
    )
    create_regular_directory_under(
        roots.trash,
        roots.payload,
        label="managed trash root",
    )
    trash_target = roots.trash / f"{label}-{uuid.uuid4()}"
    publish_path_no_replace(target, trash_target)
    return trash_target


def _quarantine_damaged_entry(
    roots: ManagedStoreRoots,
    key: ToolKey,
    *,
    deep: bool = False,
    entry_lock_held: bool = False,
) -> Path | None:
    """Move only a safe, non-healthy managed directory out of the final key."""

    target = entry_directory(roots, key.tool_kind, key.key_digest)

    def quarantine_under_lock() -> Path | None:
        current = validate_entry(
            roots,
            key.tool_kind,
            key.key_digest,
            deep=deep,
        )
        if current.healthy:
            return current.entry_path
        if not os.path.lexists(target):
            return None
        validate_regular_path_under(
            target,
            roots.entries,
            kind="directory",
            label="damaged managed entry",
        )
        _move_incomplete_to_trash(
            roots,
            target,
            label=f"damaged-{key.tool_kind}",
        )
        return None
    if entry_lock_held:
        return quarantine_under_lock()
    with entry_lock(
        roots,
        key.tool_kind,
        key.key_digest,
        mode="exclusive",
        timeout_seconds=300.0,
        command=f"quarantine damaged managed entry {key.entry_id}",
    ):
        return quarantine_under_lock()


def _runtime_lock() -> tuple[Path, str]:
    result = verify_runtime_lock(PYTHON_RUNTIME_METADATA)
    metadata = json.loads(PYTHON_RUNTIME_METADATA.read_text(encoding="utf-8"))
    requirements = PROJECT_ROOT / metadata["requirements_export"]
    return requirements, str(result["requirements_export_sha256"])


def _base_interpreter_path() -> Path:
    return Path(
        getattr(sys, "_base_executable", sys.executable)
    ).resolve(strict=True)


def _base_interpreter_identity() -> dict[str, str]:
    base_executable = _base_interpreter_path()
    return {
        "base_executable": str(base_executable),
        "base_executable_sha256": sha256_file(base_executable),
        "base_prefix": str(Path(sys.base_prefix).resolve(strict=True)),
    }


def _backend_identity(
    *,
    runner: Runner,
    env: Mapping[str, str],
    force_backend: str | None = None,
) -> tuple[str, str, str | None]:
    uv = shutil.which("uv")
    if force_backend == "pip":
        uv = None
    if force_backend == "uv" and uv is None:
        raise ManagedToolStoreError("uv backend was requested but uv is unavailable")
    if uv:
        try:
            output = _run(
                [uv, "--version"],
                cwd=PROJECT_ROOT,
                env=env,
                runner=runner,
                label="uv version check",
            )
        except ManagedToolStoreError:
            if force_backend == "uv":
                raise
        else:
            return "uv", output, uv
    base_interpreter = _base_interpreter_path()
    output = _run(
        [
            str(base_interpreter),
            "-I",
            "-c",
            "import ensurepip; print(ensurepip.version())",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        runner=runner,
        label="bundled pip version check",
    )
    version = output.strip()
    if not version:
        raise ManagedToolStoreError("bundled pip version check returned no version")
    return "pip", version, None


def python_runtime_key(
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    force_backend: str | None = None,
) -> tuple[ToolKey, str, str | None, Path]:
    environment = dict(os.environ if env is None else env)
    requirements, lock_sha256 = _runtime_lock()
    backend, backend_version, uv = _backend_identity(
        runner=runner,
        env=environment,
        force_backend=force_backend,
    )
    key = build_python_key(
        implementation=platform.python_implementation(),
        full_version=platform.python_version(),
        architecture=platform.machine().casefold(),
        base_interpreter_identity=_base_interpreter_identity(),
        runtime_lock_sha256=lock_sha256,
        installer_backend=backend,
        installer_backend_version=backend_version,
        installer_schema=INSTALLER_SCHEMA,
    )
    return key, backend, uv, requirements


def dotnet_sdk_key() -> ToolKey:
    return build_dotnet_sdk_key(
        version=DOTNET_SDK_VERSION,
        architecture=DOTNET_ARCHITECTURE,
        source=DOTNET_SDK_URL,
        package_sha256=DOTNET_SDK_SHA256,
        installer_schema=INSTALLER_SCHEMA,
    )


def decoder_archive_key(name: str) -> ToolKey:
    try:
        spec = GITHUB_ARCHIVES[name]
    except KeyError as exc:
        raise ManagedToolStoreError(f"unsupported managed decoder: {name}") from exc
    return build_decoder_key(
        tool_name=name,
        pinned_ref=spec["ref"],
        source=spec["url"],
        archive_sha256=spec["sha256"],
        installer_schema=INSTALLER_SCHEMA,
    )


def dotnet_adapter_key(adapter_name: str, dotnet_key: ToolKey) -> ToolKey:
    if adapter_name not in ADAPTER_NAMES:
        raise ManagedToolStoreError(f"unsupported managed adapter: {adapter_name}")
    project = PROJECT_ROOT / "adapters" / adapter_name / f"{adapter_name}.csproj"
    return build_adapter_key(
        adapter_name=adapter_name,
        source_digest=adapter_source_hash(project, source_root=PROJECT_ROOT),
        project_digest=_file_digest(project),
        sdk_entry_id=dotnet_key.entry_id,
        configuration="Release",
        target_framework="net8.0",
        rid="portable",
        architecture=platform.machine().casefold(),
        installer_schema=INSTALLER_SCHEMA,
    )


def _venv_python(entry_root: Path) -> Path:
    return entry_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_launcher(entry_root: Path) -> Path:
    return entry_root / ("Scripts/py7zr.exe" if os.name == "nt" else "bin/py7zr")


def _validate_python_runtime(
    entry_root: Path,
    *,
    expected_version: str,
    runner: Runner,
    env: Mapping[str, str],
) -> tuple[str, str]:
    python = validate_regular_single_link_file(
        _venv_python(entry_root),
        entry_root,
        label="managed Python executable",
    )
    launcher = validate_regular_single_link_file(
        _venv_launcher(entry_root),
        entry_root,
        label="managed py7zr launcher",
    )
    probe = (
        "import sys, bethesda_structs, py7zr; "
        "print(sys.version.split()[0]); "
        "print(sys.executable)"
    )
    output = _run(
        [str(python), "-I", "-c", probe],
        cwd=PROJECT_ROOT,
        env=env,
        runner=runner,
        label="managed Python runtime validation",
    )
    lines = output.splitlines()
    if not lines or lines[0].strip() != expected_version:
        raise ManagedToolStoreError(
            f"managed Python version differs: {lines[0] if lines else 'missing'}"
        )
    _run(
        [str(launcher), "--help"],
        cwd=PROJECT_ROOT,
        env=env,
        runner=runner,
        label="managed py7zr launcher validation",
    )
    entry_text = str(entry_root.resolve(strict=True)).casefold()
    if entry_text not in "\n".join(lines[1:]).casefold():
        raise ManagedToolStoreError(
            "managed Python resolved outside its final key directory"
        )
    return (
        python.relative_to(entry_root).as_posix(),
        launcher.relative_to(entry_root).as_posix(),
    )


def provision_python_runtime(
    roots: ManagedStoreRoots,
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    force_backend: str | None = None,
    offline: bool = False,
    runtime_identity: tuple[ToolKey, str, str | None, Path] | None = None,
    legacy_candidate: LegacyCandidate | None = None,
    migration_steps: list[str] | None = None,
) -> ProvisionedTool:
    environment = dict(os.environ if env is None else env)
    key, backend, uv, requirements = runtime_identity or python_runtime_key(
        runner=runner,
        env=environment,
        force_backend=force_backend,
    )
    existing = validate_entry(roots, key.tool_kind, key.key_digest)
    if existing.healthy:
        return ProvisionedTool(
            "python-runtime",
            key,
            existing.entry_path,
            _venv_python(existing.entry_path).relative_to(existing.entry_path).as_posix(),
            True,
        )
    target = entry_directory(roots, key.tool_kind, key.key_digest)
    with entry_lock(
        roots,
        key.tool_kind,
        key.key_digest,
        mode="exclusive",
        timeout_seconds=600.0,
        command="provision managed Python runtime",
    ):
        existing = validate_entry(roots, key.tool_kind, key.key_digest)
        if existing.healthy:
            return ProvisionedTool(
                "python-runtime",
                key,
                existing.entry_path,
                _venv_python(existing.entry_path)
                .relative_to(existing.entry_path)
                .as_posix(),
                True,
            )
        create_regular_directory_under(
            target.parent,
            roots.entries,
            label="managed Python kind directory",
        )
        if os.path.lexists(target):
            _move_incomplete_to_trash(roots, target, label="incomplete-python")
        if legacy_candidate is not None:
            entry_point = _venv_python(target).relative_to(target).as_posix()
            proven, diagnostics = legacy_payload_proves_key(
                legacy_candidate,
                key,
                entry_point=entry_point,
            )
            if migration_steps is not None:
                migration_steps.extend(diagnostics)
            if proven:
                try:
                    copy_legacy_tree_safely(
                        legacy_candidate.payload_root,
                        target,
                    )
                    atomic_create_json_no_replace(
                        target / INCOMPLETE_MARKER_NAME,
                        {
                            "schema_version": 1,
                            "entry_id": key.entry_id,
                            "created_by": PRODUCER_VERSION,
                            "migration": "copy-only",
                        },
                        allowed_root=roots.entries,
                    )
                    python_relative, launcher_relative = _validate_python_runtime(
                        target,
                        expected_version=platform.python_version(),
                        runner=runner,
                        env=environment,
                    )
                    (target / INCOMPLETE_MARKER_NAME).unlink()
                    manifest = make_entry_manifest(
                        key=key,
                        entry_root=target,
                        source={
                            "type": "hash-pinned-runtime-export",
                            "path": str(requirements.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                            "backend": backend,
                            "offline": offline,
                            "migration": "copy-only",
                        },
                        critical_entries=(python_relative, launcher_relative),
                        producer_version=PRODUCER_VERSION,
                    )
                    write_manifest(target, manifest, entries_root=roots.entries)
                    commit_entry(target, manifest, entries_root=roots.entries)
                    validation = validate_entry(
                        roots,
                        key.tool_kind,
                        key.key_digest,
                        deep=True,
                    )
                    if not validation.healthy:
                        raise ManagedToolStoreError(
                            "migrated Python entry failed deep validation"
                        )
                    if migration_steps is not None:
                        migration_steps.append(
                            "python-runtime: imported by copy; legacy duplicate retained "
                            f"({legacy_candidate.logical_bytes} logical bytes)"
                        )
                    return ProvisionedTool(
                        "python-runtime",
                        key,
                        validation.entry_path,
                        python_relative,
                        False,
                    )
                except (OSError, ValueError, ManagedToolStoreError, RuntimeError) as exc:
                    if os.path.lexists(target):
                        _move_incomplete_to_trash(
                            roots,
                            target,
                            label="failed-legacy-python",
                        )
                    if migration_steps is not None:
                        migration_steps.append(
                            f"python-runtime: copy migration validation failed; "
                            f"normal provisioning will be used: {exc}"
                        )
        create_regular_directory_under(
            target,
            roots.entries,
            label="managed Python final key directory",
        )
        atomic_create_json_no_replace(
            target / INCOMPLETE_MARKER_NAME,
            {
                "schema_version": 1,
                "entry_id": key.entry_id,
                "created_by": PRODUCER_VERSION,
            },
            allowed_root=roots.entries,
        )
        try:
            if backend == "uv":
                assert uv is not None
                _run(
                    [
                        uv,
                        "venv",
                        "--allow-existing",
                        "--python",
                        str(_base_interpreter_path()),
                        str(target),
                    ],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    runner=runner,
                    label="managed uv venv creation",
                )
                install_python = validate_regular_single_link_file(
                    _venv_python(target),
                    target,
                    label="managed Python install executable",
                )
                install_command = [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(install_python),
                    "--require-hashes",
                    "--strict",
                    "--link-mode",
                    "copy",
                    "-r",
                    str(requirements),
                ]
                if offline:
                    install_command.append("--offline")
            else:
                _run(
                    [str(_base_interpreter_path()), "-m", "venv", str(target)],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    runner=runner,
                    label="managed stdlib venv creation",
                )
                install_python = validate_regular_single_link_file(
                    _venv_python(target),
                    target,
                    label="managed Python install executable",
                )
                install_command = [
                    str(install_python),
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    str(requirements),
                ]
                if offline:
                    install_command.append("--no-index")
            _run(
                install_command,
                cwd=PROJECT_ROOT,
                env=environment,
                runner=runner,
                label="managed Python locked dependency installation",
            )
            python_relative, launcher_relative = _validate_python_runtime(
                target,
                expected_version=platform.python_version(),
                runner=runner,
                env=environment,
            )
        except (OSError, ValueError, ManagedToolStoreError, RuntimeError) as exc:
            if backend != "uv" or force_backend is not None:
                raise
            if os.path.lexists(target):
                _move_incomplete_to_trash(
                    roots,
                    target,
                    label="failed-uv-python",
                )
            if migration_steps is not None:
                migration_steps.append(
                    "python-runtime: uv provisioning failed; "
                    f"standard venv/pip fallback will be used: {exc}"
                )
            return provision_python_runtime(
                roots,
                runner=runner,
                env=environment,
                force_backend="pip",
                offline=offline,
                legacy_candidate=legacy_candidate,
                migration_steps=migration_steps,
            )
        (target / INCOMPLETE_MARKER_NAME).unlink()
        manifest = make_entry_manifest(
            key=key,
            entry_root=target,
            source={
                "type": "hash-pinned-runtime-export",
                "path": str(requirements.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "backend": backend,
                "offline": offline,
            },
            critical_entries=(python_relative, launcher_relative),
            producer_version=PRODUCER_VERSION,
        )
        write_manifest(target, manifest, entries_root=roots.entries)
        commit_entry(target, manifest, entries_root=roots.entries)
        validation = validate_entry(roots, key.tool_kind, key.key_digest, deep=True)
        if not validation.healthy:
            raise ManagedToolStoreError(
                "managed Python failed post-commit validation: "
                + " | ".join(validation.diagnostics)
            )
        return ProvisionedTool(
            "python-runtime",
            key,
            validation.entry_path,
            python_relative,
            False,
        )


def _provision_zip_payload(
    roots: ManagedStoreRoots,
    *,
    key: ToolKey,
    logical_name: str,
    url: str,
    expected_sha256: str,
    entry_point: str,
    source: Mapping[str, str],
    downloader: Downloader,
    offline: bool,
    legacy_candidate: LegacyCandidate | None = None,
    migration_steps: list[str] | None = None,
) -> ProvisionedTool:
    with entry_lock(
        roots,
        key.tool_kind,
        key.key_digest,
        mode="exclusive",
        timeout_seconds=600.0,
        command=f"provision managed tool {key.entry_id}",
    ):
        return _provision_zip_payload_under_lock(
            roots,
            key=key,
            logical_name=logical_name,
            url=url,
            expected_sha256=expected_sha256,
            entry_point=entry_point,
            source=source,
            downloader=downloader,
            offline=offline,
            legacy_candidate=legacy_candidate,
            migration_steps=migration_steps,
        )


def _provision_zip_payload_under_lock(
    roots: ManagedStoreRoots,
    *,
    key: ToolKey,
    logical_name: str,
    url: str,
    expected_sha256: str,
    entry_point: str,
    source: Mapping[str, str],
    downloader: Downloader,
    offline: bool,
    legacy_candidate: LegacyCandidate | None = None,
    migration_steps: list[str] | None = None,
) -> ProvisionedTool:
    existing = validate_entry(roots, key.tool_kind, key.key_digest)
    if existing.healthy:
        return ProvisionedTool(
            logical_name,
            key,
            existing.entry_path,
            entry_point,
            True,
        )
    winner = _quarantine_damaged_entry(
        roots,
        key,
        entry_lock_held=True,
    )
    if winner is not None:
        return ProvisionedTool(
            logical_name,
            key,
            winner,
            entry_point,
            True,
        )
    if legacy_candidate is not None:
        imported, diagnostics = import_movable_legacy_entry(
            roots,
            legacy_candidate,
            key,
            entry_point=entry_point,
            source=source,
            producer_version=PRODUCER_VERSION,
            entry_lock_held=True,
        )
        if migration_steps is not None:
            migration_steps.extend(diagnostics)
        if imported is not None:
            return ProvisionedTool(
                logical_name,
                key,
                imported,
                entry_point,
                False,
            )
    if offline:
        raise ManagedToolStoreError(
            f"offline setup cannot obtain uncached managed tool: {logical_name}"
        )
    staging = create_staging_directory(roots, prefix=key.tool_kind)
    archive = staging / "payload.zip"
    extracted = staging / "extracted"
    payload = staging / "payload"
    downloader(url, archive, staging)
    actual_sha256 = sha256_file(archive)
    if actual_sha256.casefold() != expected_sha256.casefold():
        raise ManagedToolStoreError(
            f"{logical_name} archive SHA-256 differs: "
            f"{actual_sha256} != {expected_sha256}"
        )
    extract_zip_safely(archive, extracted, allowed_root=staging)
    source_root = _single_extracted_root(extracted)
    publish_path_no_replace(source_root, payload)
    archive.unlink()
    extracted.rmdir()
    required = payload.joinpath(*entry_point.split("/"))
    if not required.is_file():
        raise ManagedToolStoreError(
            f"{logical_name} archive entry point is missing: {entry_point}"
        )
    manifest = make_entry_manifest(
        key=key,
        entry_root=payload,
        source={**source, "archive_sha256": expected_sha256, "url": url},
        critical_entries=(entry_point,),
        producer_version=PRODUCER_VERSION,
    )
    published = publish_movable_entry(
        roots,
        payload,
        manifest,
        entry_lock_held=True,
    )
    try:
        staging.rmdir()
    except OSError:
        pass
    return ProvisionedTool(logical_name, key, published, entry_point, False)


def provision_dotnet_sdk(
    roots: ManagedStoreRoots,
    *,
    downloader: Downloader = _default_downloader,
    offline: bool = False,
    planned_key: ToolKey | None = None,
    legacy_candidate: LegacyCandidate | None = None,
    migration_steps: list[str] | None = None,
) -> ProvisionedTool:
    current_key = dotnet_sdk_key()
    if planned_key is not None and planned_key != current_key:
        raise ManagedToolStoreError(
            "managed .NET SDK identity changed after workspace reservation"
        )
    key = planned_key or current_key
    return _provision_zip_payload(
        roots,
        key=key,
        logical_name="dotnet-sdk",
        url=DOTNET_SDK_URL,
        expected_sha256=DOTNET_SDK_SHA256,
        entry_point="dotnet.exe",
        source={
            "type": "microsoft-dotnet-sdk-archive",
            "version": DOTNET_SDK_VERSION,
            "architecture": DOTNET_ARCHITECTURE,
        },
        downloader=downloader,
        offline=offline,
        legacy_candidate=legacy_candidate,
        migration_steps=migration_steps,
    )


def provision_decoder_archive(
    roots: ManagedStoreRoots,
    name: str,
    *,
    downloader: Downloader = _default_downloader,
    offline: bool = False,
    planned_key: ToolKey | None = None,
    legacy_candidate: LegacyCandidate | None = None,
    migration_steps: list[str] | None = None,
) -> ProvisionedTool:
    try:
        spec = GITHUB_ARCHIVES[name]
    except KeyError as exc:
        raise ManagedToolStoreError(f"unsupported managed decoder: {name}") from exc
    current_key = decoder_archive_key(name)
    if planned_key is not None and planned_key != current_key:
        raise ManagedToolStoreError(
            f"managed decoder identity changed after workspace reservation: {name}"
        )
    key = planned_key or current_key
    return _provision_zip_payload(
        roots,
        key=key,
        logical_name=f"decoder-{name.casefold()}",
        url=spec["url"],
        expected_sha256=spec["sha256"],
        entry_point=spec["entry_point"],
        source={
            "type": "github-archive",
            "name": name,
            "ref": spec["ref"],
        },
        downloader=downloader,
        offline=offline,
        legacy_candidate=legacy_candidate,
        migration_steps=migration_steps,
    )


def _file_digest(path: Path) -> str:
    return sha256_file(path.resolve(strict=True))


def provision_dotnet_adapter(
    roots: ManagedStoreRoots,
    *,
    adapter_name: str,
    dotnet: ProvisionedTool,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    offline: bool = False,
    planned_key: ToolKey | None = None,
    legacy_candidate: LegacyCandidate | None = None,
    migration_steps: list[str] | None = None,
) -> ProvisionedTool:
    current_key = dotnet_adapter_key(adapter_name, dotnet.key)
    if planned_key is not None and planned_key != current_key:
        raise ManagedToolStoreError(
            f"managed adapter identity changed after workspace reservation: "
            f"{adapter_name}"
        )
    key = planned_key or current_key
    with entry_lock(
        roots,
        key.tool_kind,
        key.key_digest,
        mode="exclusive",
        timeout_seconds=600.0,
        command=f"provision managed adapter {adapter_name}",
    ):
        return _provision_dotnet_adapter_under_lock(
            roots,
            adapter_name=adapter_name,
            dotnet=dotnet,
            runner=runner,
            env=env,
            offline=offline,
            planned_key=key,
            legacy_candidate=legacy_candidate,
            migration_steps=migration_steps,
        )


def _provision_dotnet_adapter_under_lock(
    roots: ManagedStoreRoots,
    *,
    adapter_name: str,
    dotnet: ProvisionedTool,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    offline: bool = False,
    planned_key: ToolKey | None = None,
    legacy_candidate: LegacyCandidate | None = None,
    migration_steps: list[str] | None = None,
) -> ProvisionedTool:
    if adapter_name not in ADAPTER_NAMES:
        raise ManagedToolStoreError(f"unsupported managed adapter: {adapter_name}")
    environment = dict(os.environ if env is None else env)
    project = PROJECT_ROOT / "adapters" / adapter_name / f"{adapter_name}.csproj"
    source_digest = adapter_source_hash(project, source_root=PROJECT_ROOT)
    project_digest = _file_digest(project)
    current_key = build_adapter_key(
        adapter_name=adapter_name,
        source_digest=source_digest,
        project_digest=project_digest,
        sdk_entry_id=dotnet.key.entry_id,
        configuration="Release",
        target_framework="net8.0",
        rid="portable",
        architecture=platform.machine().casefold(),
        installer_schema=INSTALLER_SCHEMA,
    )
    if planned_key is not None and planned_key != current_key:
        raise ManagedToolStoreError(
            f"managed adapter identity changed after workspace reservation: "
            f"{adapter_name}"
        )
    key = planned_key or current_key
    existing = validate_entry(roots, key.tool_kind, key.key_digest)
    entry_point = f"{adapter_name}.dll"
    if existing.healthy:
        return ProvisionedTool(
            f"adapter-{adapter_name.casefold()}",
            key,
            existing.entry_path,
            entry_point,
            True,
        )
    winner = _quarantine_damaged_entry(
        roots,
        key,
        entry_lock_held=True,
    )
    if winner is not None:
        return ProvisionedTool(
            f"adapter-{adapter_name.casefold()}",
            key,
            winner,
            entry_point,
            True,
        )
    if legacy_candidate is not None:
        imported, diagnostics = import_movable_legacy_entry(
            roots,
            legacy_candidate,
            key,
            entry_point=entry_point,
            source={
                "type": "dotnet-adapter-build",
                "adapter_name": adapter_name,
                "project": project.relative_to(PROJECT_ROOT).as_posix(),
                "source_digest": source_digest,
                "project_digest": project_digest,
                "sdk_entry_id": dotnet.key.entry_id,
            },
            producer_version=PRODUCER_VERSION,
            entry_lock_held=True,
        )
        if migration_steps is not None:
            migration_steps.extend(diagnostics)
        if imported is not None:
            return ProvisionedTool(
                f"adapter-{adapter_name.casefold()}",
                key,
                imported,
                entry_point,
                False,
            )
    staging = create_staging_directory(roots, prefix=key.tool_kind)
    output = staging / "payload"
    intermediate = staging / "obj"
    create_regular_directory_under(output, staging, label="managed adapter output")
    create_regular_directory_under(
        intermediate,
        staging,
        label="managed adapter intermediate",
    )
    with entry_lock(
        roots,
        dotnet.key.tool_kind,
        dotnet.key.key_digest,
        mode="shared",
        timeout_seconds=600.0,
        command=f"build managed adapter {adapter_name}",
    ):
        sdk_validation = validate_entry(
            roots,
            dotnet.key.tool_kind,
            dotnet.key.key_digest,
        )
        if not sdk_validation.healthy:
            raise ManagedToolStoreError(
                "managed .NET SDK changed before adapter build: "
                + " | ".join(sdk_validation.diagnostics)
            )
        dotnet_executable = managed_path(
            sdk_validation.entry_path,
            dotnet.entry_point,
            must_exist=True,
            kind="file",
            label="managed .NET SDK executable",
        )
        command = [
            str(dotnet_executable),
            "build",
            str(project),
            "--configuration",
            "Release",
            "--framework",
            "net8.0",
            "-p:TargetFrameworks=net8.0",
            f"-p:OutputPath={str(output) + os.sep}",
            f"-p:BaseIntermediateOutputPath={str(intermediate) + os.sep}",
            f"-p:MSBuildProjectExtensionsPath={str(intermediate) + os.sep}",
        ]
        if offline:
            command.append("--no-restore")
        _run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            runner=runner,
            label=f"managed adapter build {adapter_name}",
        )
    dll = output / entry_point
    if not dll.is_file():
        raise ManagedToolStoreError(f"managed adapter DLL was not produced: {dll}")
    manifest = make_entry_manifest(
        key=key,
        entry_root=output,
        source={
            "type": "dotnet-adapter-build",
            "adapter_name": adapter_name,
            "project": project.relative_to(PROJECT_ROOT).as_posix(),
            "source_digest": source_digest,
            "project_digest": project_digest,
            "sdk_entry_id": dotnet.key.entry_id,
        },
        critical_entries=(entry_point,),
        producer_version=PRODUCER_VERSION,
    )
    published = publish_movable_entry(
        roots,
        output,
        manifest,
        entry_lock_held=True,
    )
    try:
        remove_regular_tree(
            intermediate,
            staging,
            label="managed adapter intermediate output",
        )
        staging.rmdir()
    except OSError:
        pass
    return ProvisionedTool(
        f"adapter-{adapter_name.casefold()}",
        key,
        published,
        entry_point,
        False,
    )


def _workspace_identity(workspace: Path) -> tuple[str, str]:
    workspace = workspace.resolve(strict=True)
    evidence = read_workspace_identity_evidence(workspace)
    if evidence.marker_workspace_id is not None:
        return evidence.marker_workspace_id, evidence.game_id

    with SmtProcessFileLock(
        workspace / ".workflow" / "managed-tool-identity.lock",
        "exclusive",
        30.0,
        command="managed-tool-identity-upgrade",
        allowed_root=workspace,
    ):
        evidence = read_workspace_identity_evidence(workspace)
        if evidence.marker_workspace_id is not None:
            return evidence.marker_workspace_id, evidence.game_id
        workspace_id = evidence.session_workspace_id or str(uuid.uuid4())
        marker_payload = dict(evidence.marker_payload)
        marker_payload["workspace_id"] = workspace_id
        atomic_write_json(
            evidence.marker_path,
            marker_payload,
            allowed_root=workspace,
        )
        verified = read_workspace_identity_evidence(workspace)
        if verified.marker_workspace_id != workspace_id:
            raise ManagedToolStoreError(
                "workspace marker identity upgrade did not persist the selected UUID"
            )
        return workspace_id, verified.game_id


def _validate_workspace_tool_overrides(
    workspace: Path,
) -> Mapping[str, ToolPathResolution]:
    payload = load_workspace_tool_config(workspace)
    resolutions: dict[str, ToolPathResolution] = {}
    for field in FIELD_RULES:
        resolution = classify_configured_tool_path(workspace, payload, field)
        resolutions[field] = resolution
        if resolution.provenance is ToolPathProvenance.LEGACY_UNKNOWN:
            raise ManagedToolStoreError(
                f"{field} points at unknown workspace-local tool content; "
                "automatic setup will not overwrite, migrate, delete, or replace it"
            )
    return resolutions


def provision_workspace_tools(
    workspace: Path,
    *,
    roots: ManagedStoreRoots | None = None,
    runner: Runner = subprocess.run,
    downloader: Downloader = _default_downloader,
    env: Mapping[str, str] | None = None,
    force_python_backend: str | None = None,
    offline: bool = False,
) -> WorkspaceProvisioningResult:
    """Provision the required managed set and atomically bind one workspace."""

    workspace = workspace.resolve(strict=True)
    roots = roots or resolve_managed_store_roots()
    ensure_store_layout(roots)
    workspace_id, game_id = _workspace_identity(workspace)
    resolutions = _validate_workspace_tool_overrides(workspace)
    managed_fields = {
        field
        for field, resolution in resolutions.items()
        if resolution.provenance is not ToolPathProvenance.USER_EXTERNAL
    }
    managed_logical_names = {
        rule.logical_name
        for field, rule in FIELD_RULES.items()
        if field in managed_fields and rule.logical_name is not None
    }
    managed_adapter_names = tuple(
        name
        for name in ADAPTER_NAMES
        if f"adapter-{name.casefold()}" in managed_logical_names
    )
    managed_decoder_names = tuple(
        name
        for name in GITHUB_ARCHIVES
        if f"decoder-{name.casefold()}" in managed_logical_names
    )
    needs_python = "python-runtime" in managed_logical_names
    needs_sdk = (
        "dotnet-sdk" in managed_logical_names
        or bool(managed_adapter_names)
    )
    legacy = discover_legacy_candidates(workspace, fields=managed_fields)
    if legacy.blockers:
        raise ManagedToolStoreError(
            "legacy managed-tool locations require user input: "
            + " | ".join(legacy.blockers)
        )
    environment = dict(os.environ if env is None else env)
    runtime_identity = (
        python_runtime_key(
            runner=runner,
            env=environment,
            force_backend=force_python_backend,
        )
        if needs_python
        else None
    )
    python_key = runtime_identity[0] if runtime_identity is not None else None
    sdk_key = dotnet_sdk_key() if needs_sdk else None
    decoder_keys = {
        name: decoder_archive_key(name) for name in managed_decoder_names
    }
    adapter_keys = {
        name: dotnet_adapter_key(name, sdk_key)
        for name in managed_adapter_names
        if sdk_key is not None
    }
    planned_entries: list[WorkspaceBindingEntry] = []
    if python_key is not None:
        planned_entries.append(
            WorkspaceBindingEntry(
                "python-runtime",
                python_key.tool_kind,
                python_key.key_digest,
                (
                    "Scripts/python.exe"
                    if os.name == "nt"
                    else "bin/python"
                ),
            )
        )
    if sdk_key is not None:
        planned_entries.append(
            WorkspaceBindingEntry(
                "dotnet-sdk",
                sdk_key.tool_kind,
                sdk_key.key_digest,
                "dotnet.exe",
            )
        )
    planned_entries.extend(
        WorkspaceBindingEntry(
            f"decoder-{name.casefold()}",
            decoder_keys[name].tool_kind,
            decoder_keys[name].key_digest,
            GITHUB_ARCHIVES[name]["entry_point"],
        )
        for name in managed_decoder_names
    )
    planned_entries.extend(
        WorkspaceBindingEntry(
            f"adapter-{name.casefold()}",
            adapter_keys[name].tool_kind,
            adapter_keys[name].key_digest,
            f"{name}.dll",
        )
        for name in managed_adapter_names
    )
    binding = new_binding(
        workspace_id=workspace_id,
        game_id=game_id,
        entries=planned_entries,
    )
    migration_steps: list[str] = list(legacy.diagnostics)

    def provision_all() -> list[ProvisionedTool]:
        selected: list[ProvisionedTool] = []
        if runtime_identity is not None:
            selected.append(
                provision_python_runtime(
                    roots,
                    runner=runner,
                    env=environment,
                    force_backend=force_python_backend,
                    offline=offline,
                    runtime_identity=runtime_identity,
                    legacy_candidate=legacy.candidates.get("python-runtime"),
                    migration_steps=migration_steps,
                )
            )
        dotnet: ProvisionedTool | None = None
        if sdk_key is not None:
            dotnet = provision_dotnet_sdk(
                roots,
                downloader=downloader,
                offline=offline,
                planned_key=sdk_key,
                legacy_candidate=legacy.candidates.get("dotnet-sdk"),
                migration_steps=migration_steps,
            )
            selected.append(dotnet)
        for name in managed_decoder_names:
            selected.append(
                provision_decoder_archive(
                    roots,
                    name,
                    downloader=downloader,
                    offline=offline,
                    planned_key=decoder_keys[name],
                    legacy_candidate=legacy.candidates.get(
                        f"decoder-{name.casefold()}"
                    ),
                    migration_steps=migration_steps,
                )
            )
        for adapter_name in managed_adapter_names:
            if dotnet is None:
                raise ManagedToolStoreError(
                    "managed adapter provisioning requires the pinned .NET SDK"
                )
            selected.append(
                provision_dotnet_adapter(
                    roots,
                    adapter_name=adapter_name,
                    dotnet=dotnet,
                    runner=runner,
                    env=environment,
                    offline=offline,
                    planned_key=adapter_keys[adapter_name],
                    legacy_candidate=legacy.candidates.get(
                        f"adapter-{adapter_name.casefold()}"
                    ),
                    migration_steps=migration_steps,
                )
            )
        return selected

    def validate_reserved_entries(
        selected: Sequence[ProvisionedTool],
    ) -> None:
        actual_entries = tuple(tool.binding_entry() for tool in selected)
        if set(actual_entries) != set(binding.entries):
            raise ManagedToolStoreError(
                "provisioned managed-tool identities differ from the reserved binding"
            )

    # Reserve the exact deterministic generation before publishing any payload.
    # A separate shared lifecycle lock prevents full uninstall from clearing
    # that reservation or detaching staging while this workspace is prepared;
    # different workspace preparations can still proceed in parallel.
    with store_lifecycle_lock(
        roots,
        mode="shared",
        timeout_seconds=600.0,
        command="provision workspace managed tools",
    ):
        reserve_catalog_reference(
            roots,
            workspace_id=binding.workspace_id,
            workspace_path=workspace,
            game_id=binding.game_id,
            generation=binding.generation,
            entry_ids=[entry.entry_id for entry in binding.entries],
            timeout_seconds=30.0,
        )
        tools = provision_all()
        validate_reserved_entries(tools)
        try:
            binding_path = bind_workspace(roots, workspace, binding)
        except ManagedToolStoreError:
            damaged_tools = [
                tool
                for tool in tools
                if not validate_entry(
                    roots,
                    tool.key.tool_kind,
                    tool.key.key_digest,
                    deep=True,
                ).healthy
            ]
            if not damaged_tools:
                raise
            for tool in damaged_tools:
                winner = _quarantine_damaged_entry(
                    roots,
                    tool.key,
                    deep=True,
                )
                migration_steps.append(
                    (
                        "Reused concurrently repaired"
                        if winner is not None
                        else "Quarantined damaged"
                    )
                    + f" {tool.logical_name}: {tool.key.entry_id}"
                )
            tools = provision_all()
            validate_reserved_entries(tools)
            binding_path = bind_workspace(roots, workspace, binding)
    steps = tuple(migration_steps) + tuple(
        (
            f"{'Reused' if tool.reused else 'Published'} "
            f"{tool.logical_name}: {tool.key.entry_id}"
        )
        for tool in tools
    )
    return WorkspaceProvisioningResult(binding_path, tuple(tools), steps)


def assert_command_does_not_mutate_runtime(
    command: Sequence[str],
    python_entry: Path,
) -> None:
    """Reject controlled attempts to install packages into a published entry."""

    normalized = [str(value).casefold() for value in command]
    if not normalized:
        return
    executable = Path(command[0]).resolve(strict=False)
    try:
        executable.relative_to(python_entry.resolve(strict=True))
    except ValueError:
        return
    joined = " ".join(normalized)
    if " pip " in f" {joined} " and any(
        token in normalized for token in ("install", "uninstall", "wheel")
    ):
        raise ManagedToolStoreError(
            "published managed Python entries are immutable; "
            "change the locked runtime export to provision a new key"
        )

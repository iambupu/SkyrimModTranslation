from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import managed_tool_provisioning as provisioning  # noqa: E402
import managed_tool_store as managed_store  # noqa: E402
from managed_tool_maintenance import create_plan  # noqa: E402
from managed_tool_store import (  # noqa: E402
    ManagedToolStoreError,
    ToolKey,
    create_staging_directory,
    ensure_store_layout,
    entry_directory,
    load_catalog,
    make_entry_manifest,
    make_tool_key,
    publish_movable_entry,
    read_workspace_binding,
    resolve_bound_entry,
    resolve_managed_store_roots,
    validate_entry,
)
from smt_windows import ManagedProcessEnvironmentError  # noqa: E402


def _completed(command: list[str], output: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=output)


class FakePythonRunner:
    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in command]
        self.commands.append(command)
        if command[-1:] == ["--version"]:
            if Path(command[0]).name.casefold().startswith("uv"):
                return _completed(command, "uv 0.9.26\n")
            return _completed(command, "pip 25.1 from fake\n")
        if len(command) >= 2 and command[1] == "venv":
            target = Path(command[-1])
            scripts = target / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            shutil.copy2(sys.executable, scripts / ("python.exe" if os.name == "nt" else "python"))
            shutil.copy2(sys.executable, scripts / ("py7zr.exe" if os.name == "nt" else "py7zr"))
            return _completed(command)
        if "-m" in command and "venv" in command:
            target = Path(command[-1])
            scripts = target / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            shutil.copy2(sys.executable, scripts / ("python.exe" if os.name == "nt" else "python"))
            shutil.copy2(sys.executable, scripts / ("py7zr.exe" if os.name == "nt" else "py7zr"))
            return _completed(command)
        if "ensurepip.version()" in command:
            return _completed(command, "24.0\n")
        if "-I" in command:
            return _completed(
                command,
                f"{platform.python_version()}\n{Path(command[0]).resolve()}\n",
            )
        return _completed(command, "usage\n")


@pytest.mark.parametrize("backend", ("uv", "pip"))
def test_python_runtime_uses_exact_locked_install_and_final_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    runner = FakePythonRunner(backend)
    monkeypatch.setattr(
        provisioning.shutil,
        "which",
        lambda name: "uv.exe" if name == "uv" and backend == "uv" else None,
    )
    roots = resolve_managed_store_roots(tmp_path)

    tool = provisioning.provision_python_runtime(
        roots,
        runner=runner,
        force_backend=backend,
    )

    assert validate_entry(
        roots,
        tool.key.tool_kind,
        tool.key.key_digest,
        deep=True,
    ).healthy
    install = next(
        command
        for command in runner.commands
        if "install" in command
    )
    assert "--require-hashes" in install
    assert str(provisioning._runtime_lock()[0]) in install
    if backend == "uv":
        assert "--link-mode" in install
        assert install[install.index("--link-mode") + 1] == "copy"
        assert "--strict" in install
    assert str(roots.staging).casefold() not in str(tool.executable_path).casefold()
    launcher = tool.entry_path / (
        "Scripts/py7zr.exe" if os.name == "nt" else "bin/py7zr"
    )
    assert launcher.is_file()


def test_pip_backend_identity_uses_the_base_interpreter_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    base = Path(sys.executable).resolve()
    monkeypatch.setattr(provisioning, "_base_interpreter_path", lambda: base)

    def runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed(command, "24.0\n")

    backend, version, executable = provisioning._backend_identity(
        runner=runner,
        env={},
        force_backend="pip",
    )

    assert (backend, version, executable) == ("pip", "24.0", None)
    assert calls == [
        [
            str(base),
            "-I",
            "-c",
            "import ensurepip; print(ensurepip.version())",
        ]
    ]
    assert set(provisioning._base_interpreter_identity()) == {
        "base_executable",
        "base_executable_sha256",
        "base_prefix",
    }


@pytest.mark.parametrize("failed_step", ("venv", "pip"))
def test_auto_python_runtime_falls_back_to_pip_when_uv_provisioning_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_step: str,
) -> None:
    runner = FakePythonRunner("uv")
    original_call = runner.__call__

    def failing_uv_runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        normalized = [str(item) for item in command]
        uv_step = normalized[1] if len(normalized) >= 2 else ""
        if Path(normalized[0]).name.casefold().startswith("uv") and (
            (failed_step == "venv" and uv_step == "venv")
            or (
                failed_step == "pip"
                and normalized[1:3] == ["pip", "install"]
            )
        ):
            runner.commands.append(normalized)
            return subprocess.CompletedProcess(
                normalized,
                17,
                stdout="simulated uv failure",
            )
        return original_call(normalized, **kwargs)

    monkeypatch.setattr(
        provisioning.shutil,
        "which",
        lambda name: "uv.exe" if name == "uv" else None,
    )
    roots = resolve_managed_store_roots(tmp_path)
    steps: list[str] = []

    tool = provisioning.provision_python_runtime(
        roots,
        runner=failing_uv_runner,
        migration_steps=steps,
    )

    assert tool.key.inputs["installer_backend"] == "pip"
    assert validate_entry(
        roots,
        tool.key.tool_kind,
        tool.key.key_digest,
        deep=True,
    ).healthy
    assert any("uv provisioning failed" in step for step in steps)
    assert any(
        "-m" in command and "venv" in command
        for command in runner.commands
    )


def test_auto_backend_falls_back_when_uv_version_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        normalized = [str(item) for item in command]
        calls.append(normalized)
        if normalized[-1:] == ["--version"]:
            return subprocess.CompletedProcess(
                normalized,
                9,
                stdout="broken uv",
            )
        return _completed(normalized, "24.0\n")

    monkeypatch.setattr(
        provisioning.shutil,
        "which",
        lambda name: "uv.exe" if name == "uv" else None,
    )

    backend, version, executable = provisioning._backend_identity(
        runner=runner,
        env={},
    )

    assert (backend, version, executable) == ("pip", "24.0", None)
    assert calls[0] == ["uv.exe", "--version"]
    assert any("ensurepip.version()" in argument for argument in calls[1])


def test_preplanned_uv_identity_without_executable_requests_pip_fallback(
    tmp_path: Path,
) -> None:
    roots = resolve_managed_store_roots(tmp_path)
    key = make_tool_key(
        "python-runtime",
        {"installer_backend": "uv"},
    )
    steps: list[str] = []

    with pytest.raises(provisioning._PythonBackendFallbackRequired):
        provisioning.provision_python_runtime(
            roots,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "missing uv executable must fail before command execution"
            ),
            runtime_identity=(
                key,
                "uv",
                None,
                provisioning._runtime_lock()[0],
            ),
            migration_steps=steps,
        )

    assert any("uv provisioning failed" in step for step in steps)


def test_python_runtime_validation_rejects_a_multi_link_executable(
    tmp_path: Path,
) -> None:
    entry = tmp_path / "runtime"
    scripts = entry / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    launcher = scripts / ("py7zr.exe" if os.name == "nt" else "py7zr")
    shutil.copy2(sys.executable, python)
    shutil.copy2(sys.executable, launcher)
    try:
        os.link(python, scripts / "python-alias.exe")
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(ManagedProcessEnvironmentError, match="hardlink"):
        provisioning._validate_python_runtime(
            entry,
            expected_version=platform.python_version(),
            runner=lambda *_args, **_kwargs: pytest.fail(
                "unsafe runtime must not be executed"
            ),
            env={},
        )


def test_python_legacy_copy_preserves_links_for_post_copy_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy-python"
    scripts = legacy / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    launcher = scripts / ("py7zr.exe" if os.name == "nt" else "py7zr")
    shutil.copy2(sys.executable, python)
    shutil.copy2(sys.executable, launcher)

    runner = FakePythonRunner("pip")
    runtime_identity = provisioning.python_runtime_key(
        runner=runner,
        force_backend="pip",
    )
    candidate = provisioning.LegacyCandidate(
        "PythonRuntimePath",
        "python-runtime",
        legacy,
        python,
        sum(path.stat().st_size for path in (python, launcher)),
    )
    monkeypatch.setattr(
        provisioning,
        "legacy_payload_proves_key",
        lambda *_args, **_kwargs: (True, ("legacy proof accepted",)),
    )
    real_copytree = shutil.copytree
    preserved_links: list[tuple[Path, bool]] = []

    def checked_copytree(*args: object, **kwargs: object) -> Path:
        preserved_links.append((Path(str(args[0])), kwargs.get("symlinks") is True))
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(provisioning.shutil, "copytree", checked_copytree)
    store = tmp_path / "store"
    store.mkdir()
    roots = resolve_managed_store_roots(store)

    tool = provisioning.provision_python_runtime(
        roots,
        runner=runner,
        force_backend="pip",
        runtime_identity=runtime_identity,
        legacy_candidate=candidate,
    )

    assert [
        preserves_links
        for source, preserves_links in preserved_links
        if source == legacy
    ] == [True]
    assert tool.reused is False
    assert validate_entry(
        roots,
        tool.key.tool_kind,
        tool.key.key_digest,
        deep=True,
    ).healthy


def _test_archive(path: Path, root_name: str, entry_point: str) -> str:
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{root_name}/{entry_point}", "payload")
        archive.writestr(f"{root_name}/README.txt", "readme")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_decoder_offline_reuse_and_damaged_generation_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "tool.zip"
    digest = _test_archive(archive, "Tool-ref", "tool.py")
    monkeypatch.setitem(
        provisioning.GITHUB_ARCHIVES,
        "TestTool",
        {
            "ref": "abc123",
            "url": "https://example.invalid/tool.zip",
            "sha256": digest,
            "entry_point": "tool.py",
        },
    )
    downloads = 0

    def downloader(_url: str, target: Path, _allowed_root: Path) -> None:
        nonlocal downloads
        downloads += 1
        shutil.copy2(archive, target)

    roots = resolve_managed_store_roots(tmp_path)
    first = provisioning.provision_decoder_archive(
        roots,
        "TestTool",
        downloader=downloader,
    )
    reused = provisioning.provision_decoder_archive(
        roots,
        "TestTool",
        downloader=downloader,
        offline=True,
    )
    assert downloads == 1
    assert reused.reused is True
    assert reused.key == first.key
    assert not any(roots.staging.iterdir())

    first.executable_path.write_text("damaged", encoding="utf-8")
    with pytest.raises(ManagedToolStoreError, match="offline"):
        provisioning.provision_decoder_archive(
            roots,
            "TestTool",
            downloader=downloader,
            offline=True,
        )
    repaired = provisioning.provision_decoder_archive(
        roots,
        "TestTool",
        downloader=downloader,
    )
    assert downloads == 2
    assert validate_entry(
        roots,
        repaired.key.tool_kind,
        repaired.key.key_digest,
        deep=True,
    ).healthy
    assert any(roots.trash.iterdir())


def test_incomplete_entry_quarantine_rejects_reparse_root(
    tmp_path: Path,
) -> None:
    roots = resolve_managed_store_roots(tmp_path)
    ensure_store_layout(roots)
    key = make_tool_key("python-runtime", {"version": "unsafe-test"})
    target = entry_directory(roots, key.tool_kind, key.key_digest)
    target.parent.mkdir(exist_ok=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    try:
        target.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink|junction|reparse"):
        provisioning._move_incomplete_to_trash(
            roots,
            target,
            label="unsafe-python",
        )

    assert target.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not any(roots.trash.iterdir())


def test_zip_extraction_rejects_traversal_and_casefold_collision(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    traversal = allowed / "traversal.zip"
    with ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    with pytest.raises(ManagedToolStoreError):
        provisioning.extract_zip_safely(
            traversal,
            allowed / "out-a",
            allowed_root=allowed,
        )
    collision = allowed / "collision.zip"
    with ZipFile(collision, "w") as archive:
        archive.writestr("Root/Tool.txt", "one")
        archive.writestr("root/tool.TXT", "two")
    with pytest.raises(ManagedToolStoreError, match="collision"):
        provisioning.extract_zip_safely(
            collision,
            allowed / "out-b",
            allowed_root=allowed,
        )


def _workspace(path: Path) -> Path:
    (path / ".workflow").mkdir(parents=True)
    (path / "config").mkdir()
    (path / "config" / "tools.local.json").write_text("{}\n", encoding="utf-8")
    (path / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "workspace_id": str(uuid.uuid4()),
                "game_id": "skyrim-se",
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_session(
    workspace: Path,
    *,
    workspace_id: str,
    game_id: str = "skyrim-se",
) -> None:
    digest = "a" * 64
    (workspace / ".workflow" / "smt-session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": workspace_id,
                "mod_name": "ExampleMod",
                "game_id": game_id,
                "fingerprint_algorithm": "smt-input-v1",
                "input_identity": f"smt-input-v1:{game_id}:zip:{digest}",
                "source_kind": "zip",
                "source_display_name": "ExampleMod.zip",
                "source_sha256": digest,
                "import_relative_path": "mod/ExampleMod.zip",
                "imported_sha256": digest,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _remove_workspace_id(workspace: Path) -> Path:
    marker_path = workspace / ".skyrim-chs-workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.pop("workspace_id")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    return marker_path


def test_auto_identity_upgrade_reuses_matching_session_uuid(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    marker_path = _remove_workspace_id(workspace)
    session_workspace_id = "0bf9244e-7adf-43a3-a8d2-4b2af43cd335"
    _write_session(workspace, workspace_id=session_workspace_id)

    workspace_id, game_id = provisioning._workspace_identity(workspace)

    assert workspace_id == session_workspace_id
    assert game_id == "skyrim-se"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["workspace_id"] == session_workspace_id


def test_auto_identity_upgrade_assigns_uuid_without_session(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    marker_path = _remove_workspace_id(workspace)

    workspace_id, game_id = provisioning._workspace_identity(workspace)

    assert str(uuid.UUID(workspace_id)) == workspace_id
    assert game_id == "skyrim-se"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["workspace_id"] == workspace_id


@pytest.mark.parametrize("conflict", ("invalid-marker-uuid", "session-game"))
def test_auto_identity_upgrade_preserves_conflicting_evidence(
    tmp_path: Path,
    conflict: str,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    marker_path = workspace / ".skyrim-chs-workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if conflict == "invalid-marker-uuid":
        marker["workspace_id"] = "not-a-uuid"
    else:
        marker.pop("workspace_id")
        _write_session(
            workspace,
            workspace_id="5e3cac29-372f-43dd-b468-c31508552228",
            game_id="fallout4",
        )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    before = marker_path.read_bytes()

    with pytest.raises(ManagedToolStoreError):
        provisioning._workspace_identity(workspace)

    assert marker_path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "relative"),
    (
        (
            "PythonRuntimePath",
            (
                "tools/python-venv/Scripts/python.exe"
                if os.name == "nt"
                else "tools/python-venv/bin/python"
            ),
        ),
        (
            "BethesdaStringTableToolPath",
            (
                "tools/dotnet-adapters/BethesdaStringTableTool/"
                "BethesdaStringTableTool.dll"
            ),
        ),
    ),
)
def test_auto_setup_blocks_unknown_explicit_workspace_tool_overrides(
    tmp_path: Path,
    field: str,
    relative: str,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    configured = workspace.joinpath(*relative.split("/"))
    configured.parent.mkdir(parents=True, exist_ok=True)
    configured.write_bytes(b"unproven")
    (workspace / "config" / "tools.local.json").write_text(
        json.dumps({"DecoderTools": {field: relative}}),
        encoding="utf-8",
    )

    with pytest.raises(
        ManagedToolStoreError,
        match=rf"{field} points at unknown workspace-local tool content",
    ):
        provisioning._validate_workspace_tool_overrides(workspace)


def _published_fake_tool(
    roots: object,
    logical_name: str,
    key: ToolKey | None = None,
    entry_point: str = "tool.exe",
) -> provisioning.ProvisionedTool:
    key = key or make_tool_key(f"fake-{logical_name}", {"version": "1"})
    validation = validate_entry(roots, key.tool_kind, key.key_digest)  # type: ignore[arg-type]
    if validation.healthy:
        return provisioning.ProvisionedTool(
            logical_name,
            key,
            validation.entry_path,
            entry_point,
            True,
        )
    staging = create_staging_directory(roots, prefix="fake")  # type: ignore[arg-type]
    executable = staging.joinpath(*entry_point.split("/"))
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(logical_name.encode("utf-8"))
    (staging / "support.dat").write_bytes(b"managed-support")
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "test"},
        critical_entries=(entry_point,),
        producer_version="test",
    )
    path = publish_movable_entry(roots, staging, manifest)  # type: ignore[arg-type]
    return provisioning.ProvisionedTool(
        logical_name,
        key,
        path,
        entry_point,
        False,
    )


def _patch_fake_workspace_provisioners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provisioning,
        "provision_python_runtime",
        lambda selected, runtime_identity, **_kwargs: _published_fake_tool(
            selected,
            "python-runtime",
            runtime_identity[0],
            (
                "Scripts/python.exe"
                if os.name == "nt"
                else "bin/python"
            ),
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "provision_dotnet_sdk",
        lambda selected, planned_key, **_kwargs: _published_fake_tool(
            selected,
            "dotnet-sdk",
            planned_key,
            "dotnet.exe",
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "provision_decoder_archive",
        lambda selected, name, planned_key, **_kwargs: _published_fake_tool(
            selected,
            f"decoder-{name.casefold()}",
            planned_key,
            provisioning.GITHUB_ARCHIVES[name]["entry_point"],
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "provision_dotnet_adapter",
        lambda selected, adapter_name, planned_key, **_kwargs: _published_fake_tool(
            selected,
            f"adapter-{adapter_name.casefold()}",
            planned_key,
            f"{adapter_name}.dll",
        ),
    )


def test_adapter_build_revalidates_sdk_under_its_runtime_lease(
    tmp_path: Path,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    sdk_key = make_tool_key("dotnet-sdk", {"version": "test"})
    dotnet = _published_fake_tool(
        roots,
        "dotnet-sdk",
        sdk_key,
        "dotnet.exe",
    )
    dotnet.executable_path.write_bytes(b"changed-after-publication")

    with pytest.raises(
        ManagedToolStoreError,
        match="changed before adapter build",
    ):
        provisioning.provision_dotnet_adapter(
            roots,
            adapter_name="SkyrimPluginTextTool",
            dotnet=dotnet,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "damaged managed SDK must not be executed"
            ),
        )


def test_two_workspaces_bind_the_same_complete_tool_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    first_workspace = _workspace(tmp_path / "one")
    second_workspace = _workspace(tmp_path / "two")
    _patch_fake_workspace_provisioners(monkeypatch)

    provisioning.provision_workspace_tools(first_workspace, roots=roots)
    provisioning.provision_workspace_tools(second_workspace, roots=roots)
    first = read_workspace_binding(first_workspace)
    second = read_workspace_binding(second_workspace)

    assert len(first.entries) == 7
    assert {
        (entry.logical_name, entry.entry_id, entry.entry_point)
        for entry in first.entries
    } == {
        (entry.logical_name, entry.entry_id, entry.entry_point)
        for entry in second.entries
    }
    shutil.rmtree(first_workspace)
    path, _entry = resolve_bound_entry(
        roots,
        second_workspace,
        "python-runtime",
    )
    assert path.is_file()


def test_workspace_auto_repairs_noncritical_payload_damage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    first_workspace = _workspace(tmp_path / "one")
    second_workspace = _workspace(tmp_path / "two")
    _patch_fake_workspace_provisioners(monkeypatch)

    first_result = provisioning.provision_workspace_tools(
        first_workspace,
        roots=roots,
    )
    damaged_tool = next(
        tool
        for tool in first_result.tools
        if tool.logical_name == "decoder-bsafileextractor"
    )
    support = damaged_tool.entry_path / "support.dat"
    support.write_bytes(b"changed-support")

    assert validate_entry(
        roots,
        damaged_tool.key.tool_kind,
        damaged_tool.key.key_digest,
    ).healthy
    assert not validate_entry(
        roots,
        damaged_tool.key.tool_kind,
        damaged_tool.key.key_digest,
        deep=True,
    ).healthy

    repaired = provisioning.provision_workspace_tools(
        second_workspace,
        roots=roots,
    )

    repaired_tool = next(
        tool
        for tool in repaired.tools
        if tool.logical_name == damaged_tool.logical_name
    )
    assert validate_entry(
        roots,
        repaired_tool.key.tool_kind,
        repaired_tool.key.key_digest,
        deep=True,
    ).healthy
    assert any(
        step.startswith("Quarantined damaged decoder-bsafileextractor:")
        for step in repaired.steps
    )
    assert any(roots.trash.iterdir())
    resolved, _entry = resolve_bound_entry(
        roots,
        second_workspace,
        "decoder-bsafileextractor",
        deep=True,
    )
    assert resolved.is_file()


def test_deep_quarantine_reuses_concurrently_repaired_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    tool = _published_fake_tool(roots, "decoder-test")
    support = tool.entry_path / "support.dat"
    original = support.read_bytes()
    support.write_bytes(b"changed-support")

    @contextmanager
    def repairing_lock(*_args: object, **_kwargs: object):
        support.write_bytes(original)
        yield

    monkeypatch.setattr(provisioning, "entry_lock", repairing_lock)

    winner = provisioning._quarantine_damaged_entry(
        roots,
        tool.key,
        deep=True,
    )

    assert winner == tool.entry_path
    assert validate_entry(
        roots,
        tool.key.tool_kind,
        tool.key.key_digest,
        deep=True,
    ).healthy
    assert not any(roots.trash.iterdir())


def test_healthy_workspace_binding_performs_one_full_scan_per_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    seed_workspace = _workspace(tmp_path / "seed")
    workspace = _workspace(tmp_path / "workspace")
    _patch_fake_workspace_provisioners(monkeypatch)
    provisioning.provision_workspace_tools(seed_workspace, roots=roots)
    original_validate = managed_store.validate_entry
    deep_calls: list[tuple[str, str]] = []

    def counted_validate(
        selected_roots: object,
        tool_kind: str,
        key_digest: str,
        **kwargs: object,
    ):
        if kwargs.get("deep") is True:
            deep_calls.append((tool_kind, key_digest))
        return original_validate(
            selected_roots,  # type: ignore[arg-type]
            tool_kind,
            key_digest,
            **kwargs,
        )

    monkeypatch.setattr(managed_store, "validate_entry", counted_validate)

    result = provisioning.provision_workspace_tools(workspace, roots=roots)

    assert len(result.tools) == 7
    assert len(deep_calls) == len(result.tools)
    assert len(set(deep_calls)) == len(result.tools)


def test_workspace_reserves_exact_generation_before_first_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    workspace = _workspace(tmp_path / "workspace")
    observed: list[bool] = []

    def fake_python(
        selected: object,
        *,
        runtime_identity: tuple[ToolKey, str, str | None, Path],
        **_kwargs: object,
    ) -> provisioning.ProvisionedTool:
        tool = _published_fake_tool(
            selected,
            "python-runtime",
            runtime_identity[0],
            (
                "Scripts/python.exe"
                if os.name == "nt"
                else "bin/python"
            ),
        )
        catalog = load_catalog(roots)
        references = tuple(catalog["references"].values())
        plan = create_plan("clean-unused", roots=roots)
        python_candidate = next(
            row
            for row in plan.candidates
            if row.get("entry_id") == tool.key.entry_id
        )
        observed.append(
            len(references) == 1
            and references[0]["status"] == "pending"
            and tool.key.entry_id in references[0]["entry_ids"]
            and python_candidate["included"] is False
        )
        return tool

    monkeypatch.setattr(provisioning, "provision_python_runtime", fake_python)
    monkeypatch.setattr(
        provisioning,
        "provision_dotnet_sdk",
        lambda selected, planned_key, **_kwargs: _published_fake_tool(
            selected,
            "dotnet-sdk",
            planned_key,
            "dotnet.exe",
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "provision_decoder_archive",
        lambda selected, name, planned_key, **_kwargs: _published_fake_tool(
            selected,
            f"decoder-{name.casefold()}",
            planned_key,
            provisioning.GITHUB_ARCHIVES[name]["entry_point"],
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "provision_dotnet_adapter",
        lambda selected, adapter_name, planned_key, **_kwargs: _published_fake_tool(
            selected,
            f"adapter-{adapter_name.casefold()}",
            planned_key,
            f"{adapter_name}.dll",
        ),
    )

    provisioning.provision_workspace_tools(workspace, roots=roots)

    assert observed == [True]


def test_workspace_replaces_uv_reservation_before_pip_fallback_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    workspace = _workspace(tmp_path / "workspace")
    uv_key = make_tool_key(
        "python-runtime",
        {"installer_backend": "uv"},
    )
    pip_key = make_tool_key(
        "python-runtime",
        {"installer_backend": "pip"},
    )
    observed: list[str] = []

    def fake_runtime_key(
        **kwargs: object,
    ) -> tuple[ToolKey, str, str | None, Path]:
        if kwargs.get("force_backend") == "pip":
            return pip_key, "pip", None, provisioning._runtime_lock()[0]
        return uv_key, "uv", "uv.exe", provisioning._runtime_lock()[0]

    def fake_python(
        selected: object,
        *,
        runtime_identity: tuple[ToolKey, str, str | None, Path],
        **_kwargs: object,
    ) -> provisioning.ProvisionedTool:
        catalog = load_catalog(roots)
        references = tuple(catalog["references"].values())
        assert len(references) == 1
        reserved_ids = set(references[0]["entry_ids"])
        backend = runtime_identity[1]
        observed.append(backend)
        if backend == "uv":
            assert uv_key.entry_id in reserved_ids
            raise provisioning._PythonBackendFallbackRequired(
                "simulated uv failure"
            )
        assert pip_key.entry_id in reserved_ids
        assert uv_key.entry_id not in reserved_ids
        return _published_fake_tool(
            selected,
            "python-runtime",
            pip_key,
            (
                "Scripts/python.exe"
                if os.name == "nt"
                else "bin/python"
            ),
        )

    _patch_fake_workspace_provisioners(monkeypatch)
    monkeypatch.setattr(provisioning, "python_runtime_key", fake_runtime_key)
    monkeypatch.setattr(provisioning, "provision_python_runtime", fake_python)

    result = provisioning.provision_workspace_tools(workspace, roots=roots)

    binding = read_workspace_binding(workspace)
    python_entry = next(
        entry
        for entry in binding.entries
        if entry.logical_name == "python-runtime"
    )
    references = tuple(load_catalog(roots)["references"].values())
    assert observed == ["uv", "pip"]
    assert python_entry.entry_id == pip_key.entry_id
    assert result.tools[0].key == pip_key
    assert len(references) == 1
    assert references[0]["status"] == "active"
    assert pip_key.entry_id in references[0]["entry_ids"]
    assert uv_key.entry_id not in references[0]["entry_ids"]


def test_valid_external_override_is_not_published_or_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "store").mkdir()
    roots = resolve_managed_store_roots(tmp_path / "store")
    workspace = _workspace(tmp_path / "workspace")
    external = tmp_path / "external" / "BSAFileExtractor.py"
    external.parent.mkdir()
    external.write_text("print('external')\n", encoding="utf-8")
    (workspace / "config" / "tools.local.json").write_text(
        json.dumps(
            {
                "DecoderTools": {
                    "BsaFileExtractorPath": str(external),
                }
            }
        ),
        encoding="utf-8",
    )
    decoder_calls: list[str] = []

    monkeypatch.setattr(
        provisioning,
        "provision_python_runtime",
        lambda selected, runtime_identity, **_kwargs: _published_fake_tool(
            selected,
            "python-runtime",
            runtime_identity[0],
            (
                "Scripts/python.exe"
                if os.name == "nt"
                else "bin/python"
            ),
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "provision_dotnet_sdk",
        lambda selected, planned_key, **_kwargs: _published_fake_tool(
            selected,
            "dotnet-sdk",
            planned_key,
            "dotnet.exe",
        ),
    )

    def fake_decoder(
        selected: object,
        name: str,
        *,
        planned_key: ToolKey,
        **_kwargs: object,
    ) -> provisioning.ProvisionedTool:
        decoder_calls.append(name)
        return _published_fake_tool(
            selected,
            f"decoder-{name.casefold()}",
            planned_key,
            provisioning.GITHUB_ARCHIVES[name]["entry_point"],
        )

    monkeypatch.setattr(provisioning, "provision_decoder_archive", fake_decoder)
    monkeypatch.setattr(
        provisioning,
        "provision_dotnet_adapter",
        lambda selected, adapter_name, planned_key, **_kwargs: _published_fake_tool(
            selected,
            f"adapter-{adapter_name.casefold()}",
            planned_key,
            f"{adapter_name}.dll",
        ),
    )

    provisioning.provision_workspace_tools(workspace, roots=roots)

    binding = read_workspace_binding(workspace)
    assert decoder_calls == ["Champollion"]
    assert all(
        entry.logical_name != "decoder-bsafileextractor"
        for entry in binding.entries
    )

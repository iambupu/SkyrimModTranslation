#!/usr/bin/env python3
"""Prepare or document local tool setup for a Skyrim CHS workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlretrieve
from zipfile import ZipFile

from dotnet_adapter_cache import ensure_adapter_dll

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
DOTNET_INSTALL_URL = "vendored:scripts/vendor/dotnet-install.ps1"
LEGACY_DOTNET_INSTALL_URLS = {"https://dot.net/v1/dotnet-install.ps1"}
DOTNET_INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "vendor" / "dotnet-install.ps1"
DOTNET_INSTALL_SHA256 = "6585899aed55ff6ae13dbe1e8c3b878f2d00433520e7efbe250b75db948b7da9"
DOTNET_SDK_VERSION = "8.0.422"
PYTHON_VENV_DIR = Path("tools") / "python-venv"
TOOL_MANIFEST_NAME = ".skyrim-chs-tool.json"
GITHUB_ARCHIVES = {
    "BSAFileExtractor": {
        "ref": "cce03dfc294f1f31fa0af0fe1d2ef3b5dde67c27",
        "url": "https://codeload.github.com/Sw4T/BSAFileExtractor/zip/cce03dfc294f1f31fa0af0fe1d2ef3b5dde67c27",
        "sha256": "9c7138fbb6672f032c4c7a86526104ec4cbd7db9eca1672d49d73f2cfc9ea86a",
        "target": Path("tools") / "BSAFileExtractor",
    },
    "Champollion": {
        "ref": "bc961a0bdfb4831f8240e6dacee0818b4bf81e00",
        "url": "https://codeload.github.com/Orvid/Champollion/zip/bc961a0bdfb4831f8240e6dacee0818b4bf81e00",
        "sha256": "f83f626d40a88cd8e11189a908f503f8b8bcd4072e1294187687857528739b46",
        "target": Path("tools") / "Champollion",
    },
}


def workspace_root() -> Path:
    configured = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path.cwd().resolve(strict=False)


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def validate_workspace_root(root: Path) -> None:
    if is_under(root, PROJECT_ROOT):
        raise RuntimeError(
            "Refusing to run tool setup inside the plugin source repository. "
            "Initialize or open a separate workspace first."
        )
    if not (root / WORKSPACE_MARKER).is_file():
        raise RuntimeError(
            f"Tool setup requires an initialized workspace marker: {root / WORKSPACE_MARKER}"
        )


def run_command(command: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return result.returncode, result.stdout.strip()


def download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    urlretrieve(url, target)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def replace_dir_preserving_old(source: Path, target: Path, backup: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f"Replacement source is missing: {source}")
    remove_path(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    moved_old = False
    if target.exists():
        shutil.move(str(target), str(backup))
        moved_old = True
    try:
        shutil.move(str(source), str(target))
    except Exception:
        if moved_old and backup.exists() and not target.exists():
            shutil.move(str(backup), str(target))
        raise
    remove_path(backup)


def write_tool_manifest(target: Path, payload: dict[str, str]) -> None:
    manifest = {
        "schema_version": "1",
        "installed_at": datetime.now().isoformat(timespec="seconds"),
        **payload,
    }
    with (target / TOOL_MANIFEST_NAME).open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def installed_github_archive_matches(target: Path, name: str) -> bool:
    manifest_path = target / TOOL_MANIFEST_NAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    spec = GITHUB_ARCHIVES[name]
    return (
        manifest.get("name") == name
        and manifest.get("source_type") == "github-archive"
        and manifest.get("ref") == spec["ref"]
        and str(manifest.get("archive_sha256", "")).lower() == str(spec["sha256"]).lower()
        and manifest.get("url") == spec["url"]
    )


def installed_dotnet_matches(target: Path) -> bool:
    manifest_path = target / TOOL_MANIFEST_NAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        manifest.get("name") == "dotnet-sdk"
        and manifest.get("source_type") == "dotnet-install"
        and manifest.get("install_script_source") == DOTNET_INSTALL_URL
        and str(manifest.get("install_script_sha256", "")).lower() == DOTNET_INSTALL_SHA256
        and manifest.get("sdk_version") == DOTNET_SDK_VERSION
    )


def dotnet_manifest_can_migrate(target: Path) -> bool:
    manifest_path = target / TOOL_MANIFEST_NAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    script_source = manifest.get("install_script_source") or manifest.get("url")
    return (
        manifest.get("name") == "dotnet-sdk"
        and manifest.get("source_type") == "dotnet-install"
        and script_source in {DOTNET_INSTALL_URL, *LEGACY_DOTNET_INSTALL_URLS}
        and str(manifest.get("install_script_sha256", "")).lower() == DOTNET_INSTALL_SHA256
        and manifest.get("sdk_version") == DOTNET_SDK_VERSION
    )


def safe_extract_zip(archive_path: Path, extract_root: Path) -> None:
    extract_root.mkdir(parents=True, exist_ok=True)
    root_resolved = extract_root.resolve(strict=False)
    with ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (extract_root / member.filename).resolve(strict=False)
            if os.path.commonpath([str(root_resolved), str(target)]) != str(root_resolved):
                raise RuntimeError(f"Unsafe archive member path: {member.filename}")
        archive.extractall(extract_root)


def install_github_archive(root: Path, name: str, steps: list[str], errors: list[str]) -> None:
    spec = GITHUB_ARCHIVES[name]
    target = root / spec["target"]
    if target.exists():
        if installed_github_archive_matches(target, name):
            steps.append(f"{name} pinned install already verified: {target}")
            return
        steps.append(f"{name} existing install is missing or mismatches pinned manifest; will replace after verifying pinned download.")

    temp_root = root / "work" / "tool_setup_downloads"
    archive_path = temp_root / f"{name}.zip"
    extract_root = temp_root / f"{name}_extract"
    staged_target = temp_root / f"{name}_staged"
    backup_target = temp_root / f"{name}_previous"
    try:
        remove_path(extract_root)
        remove_path(staged_target)
        download_file(str(spec["url"]), archive_path)
        actual_sha256 = file_sha256(archive_path)
        expected_sha256 = str(spec["sha256"]).lower()
        if actual_sha256.lower() != expected_sha256:
            raise RuntimeError(
                f"SHA256 mismatch for {name}: expected {expected_sha256}, got {actual_sha256.lower()}."
            )
        safe_extract_zip(archive_path, extract_root)
        children = [child for child in extract_root.iterdir() if child.is_dir()]
        if len(children) != 1:
            raise RuntimeError(f"Expected one extracted root for {name}, got {len(children)}.")
        staged_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(children[0]), str(staged_target))
        write_tool_manifest(
            staged_target,
            {
                "name": name,
                "source_type": "github-archive",
                "url": str(spec["url"]),
                "ref": str(spec["ref"]),
                "archive_sha256": expected_sha256,
            },
        )
        replace_dir_preserving_old(staged_target, target, backup_target)
        steps.append(f"{name} downloaded from pinned GitHub ref {spec['ref']} into {target}.")
    except (OSError, RuntimeError, URLError) as exc:
        remove_path(staged_target)
        errors.append(f"{name} GitHub download failed: {exc}")


def powershell_executable() -> str:
    for name in ("pwsh", "powershell"):
        path = shutil.which(name)
        if path:
            return path
    return ""


def install_dotnet_sdk(root: Path, env: dict[str, str], steps: list[str], errors: list[str]) -> None:
    dotnet = root / "tools" / "dotnet-sdk" / "dotnet.exe"
    if dotnet.is_file():
        code, output = run_command([str(dotnet), "--version"], cwd=root, env=env)
        installed_version = output.strip().splitlines()[-1] if output.strip() else ""
        if code == 0 and installed_version == DOTNET_SDK_VERSION and installed_dotnet_matches(dotnet.parent):
            steps.append(f".NET SDK pinned install already verified: {dotnet} ({installed_version})")
            return
        if code == 0 and installed_version == DOTNET_SDK_VERSION and dotnet_manifest_can_migrate(dotnet.parent):
            try:
                write_tool_manifest(
                    dotnet.parent,
                    {
                        "name": "dotnet-sdk",
                        "source_type": "dotnet-install",
                        "install_script_source": DOTNET_INSTALL_URL,
                        "install_script_sha256": DOTNET_INSTALL_SHA256,
                        "sdk_version": DOTNET_SDK_VERSION,
                    },
                )
                steps.append(f".NET SDK pinned manifest migrated and verified: {dotnet} ({installed_version})")
                return
            except OSError as exc:
                errors.append(f".NET SDK manifest refresh failed: {exc}")
                return
        steps.append(
            f".NET SDK existing install is {installed_version or 'unknown'} or lacks pinned manifest; will replace after verifying pinned {DOTNET_SDK_VERSION}."
        )

    shell = powershell_executable()
    if not shell:
        errors.append("PowerShell was not found; cannot install project-local .NET SDK automatically.")
        return

    temp_root = root / "work" / "tool_setup_downloads"
    install_script = temp_root / "dotnet-install.ps1"
    staged_dotnet = temp_root / "dotnet-sdk_staged"
    backup_dotnet = temp_root / "dotnet-sdk_previous"
    try:
        remove_path(staged_dotnet)
        if not DOTNET_INSTALL_SCRIPT.is_file():
            raise RuntimeError(f"vendored dotnet installer is missing: {DOTNET_INSTALL_SCRIPT}")
        install_script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DOTNET_INSTALL_SCRIPT, install_script)
        actual_sha256 = file_sha256(install_script)
        if actual_sha256.lower() != DOTNET_INSTALL_SHA256:
            raise RuntimeError(
                f"dotnet-install.ps1 SHA256 mismatch: expected {DOTNET_INSTALL_SHA256}, got {actual_sha256.lower()}."
            )
    except (OSError, URLError) as exc:
        errors.append(f".NET install script preparation failed: {exc}")
        return
    except RuntimeError as exc:
        errors.append(str(exc))
        return

    command = [
        shell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(install_script),
        "-Version",
        DOTNET_SDK_VERSION,
        "-InstallDir",
        str(staged_dotnet),
        "-NoPath",
    ]
    code, output = run_command(command, cwd=root, env=env)
    steps.append(f".NET SDK install exited with code {code}.")
    if output:
        steps.append(f".NET SDK install output: {output}")
    staged_dotnet_exe = staged_dotnet / "dotnet.exe"
    version_code, version_output = run_command([str(staged_dotnet_exe), "--version"], cwd=root, env=env) if staged_dotnet_exe.is_file() else (1, "")
    installed_version = version_output.strip().splitlines()[-1] if version_output.strip() else ""
    if code != 0 or version_code != 0 or installed_version != DOTNET_SDK_VERSION:
        errors.append("Project-local .NET SDK installation failed.")
        remove_path(staged_dotnet)
    else:
        try:
            write_tool_manifest(
                staged_dotnet,
                {
                    "name": "dotnet-sdk",
                    "source_type": "dotnet-install",
                    "install_script_source": DOTNET_INSTALL_URL,
                    "install_script_sha256": DOTNET_INSTALL_SHA256,
                    "sdk_version": DOTNET_SDK_VERSION,
                },
            )
            replace_dir_preserving_old(staged_dotnet, dotnet.parent, backup_dotnet)
        except (OSError, RuntimeError) as exc:
            errors.append(f"Project-local .NET SDK replacement failed: {exc}")


def workspace_python(root: Path) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return root / PYTHON_VENV_DIR / scripts_dir / executable


def uv_executable() -> str:
    return shutil.which("uv") or ""


def install_python_requirements_with_uv(root: Path, env: dict[str, str], steps: list[str]) -> tuple[Path, bool]:
    venv_python = workspace_python(root)
    uv = uv_executable()
    if not uv:
        steps.append("uv executable was not found; falling back to stdlib venv and pip.")
        return venv_python, False

    venv_dir = root / PYTHON_VENV_DIR
    if not venv_python.is_file():
        command = [uv, "venv", "--python", sys.executable, str(venv_dir)]
        code, output = run_command(command, cwd=root, env=env)
        steps.append(f"Workspace Python uv venv creation exited with code {code}.")
        if output:
            steps.append(f"uv venv output: {output}")
        if code != 0 or not venv_python.is_file():
            steps.append("uv venv creation failed; falling back to stdlib venv and pip.")
            return venv_python, False
    else:
        steps.append(f"Workspace Python venv already exists: {venv_python}")

    requirements = PROJECT_ROOT / "requirements.txt"
    command = [uv, "pip", "install", "--python", str(venv_python), "-r", str(requirements)]
    code, output = run_command(command, cwd=PROJECT_ROOT, env=env)
    steps.append(f"Workspace Python requirements install via uv exited with code {code}.")
    if output:
        steps.append(f"uv pip output: {output}")
    if code != 0:
        steps.append("uv pip install failed; falling back to pip inside the workspace venv.")
        return venv_python, False
    return venv_python, True


def install_python_requirements_with_pip(root: Path, env: dict[str, str], steps: list[str], errors: list[str]) -> Path:
    venv_python = workspace_python(root)
    if not venv_python.is_file():
        command = [sys.executable, "-m", "venv", str(root / PYTHON_VENV_DIR)]
        code, output = run_command(command, cwd=root, env=env)
        steps.append(f"Workspace Python venv creation exited with code {code}.")
        if output:
            steps.append(f"venv output: {output}")
        if code != 0 or not venv_python.is_file():
            errors.append("Workspace-local Python venv creation failed.")
            return venv_python
    else:
        steps.append(f"Workspace Python venv already exists: {venv_python}")

    requirements = PROJECT_ROOT / "requirements.txt"
    command = [str(venv_python), "-m", "pip", "install", "-r", str(requirements)]
    code, output = run_command(command, cwd=PROJECT_ROOT, env=env)
    steps.append(f"Workspace Python requirements install exited with code {code}.")
    if output:
        steps.append(f"pip output: {output}")
    if code != 0:
        errors.append("Workspace Python requirements installation failed.")
    return venv_python


def install_python_requirements(root: Path, env: dict[str, str], steps: list[str], errors: list[str]) -> Path:
    venv_python, uv_ok = install_python_requirements_with_uv(root, env, steps)
    if uv_ok:
        return venv_python
    return install_python_requirements_with_pip(root, env, steps, errors)


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def update_tools_config(root: Path, steps: list[str], errors: list[str]) -> None:
    config_path = root / "config" / "tools.local.json"
    try:
        config = load_config(config_path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Cannot update config/tools.local.json: {exc}")
        return

    decoder_tools = config.setdefault("DecoderTools", {})
    if not isinstance(decoder_tools, dict):
        errors.append("config/tools.local.json DecoderTools must be an object.")
        return

    paths = {
        "DotNetSdkPath": root / "tools" / "dotnet-sdk" / "dotnet.exe",
        "ChampollionSourceDir": root / "tools" / "Champollion",
    }
    for key, path in paths.items():
        if path.exists():
            decoder_tools[key] = str(path.relative_to(root)).replace("\\", "/")
    bsa_tool = root / "tools" / "BSAFileExtractor" / "BSAFileExtractor.py"
    bsa_wrapper = PROJECT_ROOT / "scripts" / "invoke_bsa_file_extractor_safe.py"
    if bsa_tool.exists() and bsa_wrapper.is_file():
        decoder_tools["BsaFileExtractorPath"] = "scripts/invoke_bsa_file_extractor_safe.py"

    try:
        write_config(config_path, config)
        steps.append("Updated config/tools.local.json with installed non-GUI tool paths.")
    except OSError as exc:
        errors.append(f"Failed to write config/tools.local.json: {exc}")


def build_dotnet_adapters(root: Path, env: dict[str, str], steps: list[str], errors: list[str]) -> None:
    dotnet = root / "tools" / "dotnet-sdk" / "dotnet.exe"
    if not dotnet.is_file():
        steps.append("Skipped Mutagen adapter build because project-local dotnet.exe is missing.")
        return
    for adapter_name in ("SkyrimPluginTextTool", "SkyrimPexStringTool"):
        try:
            adapter_dll = ensure_adapter_dll(root, PROJECT_ROOT, dotnet, adapter_name)
            steps.append(f"Adapter ready with source-hash manifest: {adapter_dll}")
        except (OSError, RuntimeError) as exc:
            errors.append(f"Adapter build failed for {adapter_name}: {exc}")


def write_setup_report(
    root: Path,
    *,
    mode: str,
    steps: list[str],
    errors: list[str],
) -> Path:
    report_path = root / "qa" / "tool_setup.md"
    lines = [
        "# Tool Setup",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Mode: {mode}",
        f"- Blocking errors: {len(errors)}",
        "",
        "## What This Step Does",
        "",
        "- `auto` installs Python packages into workspace `tools/python-venv/`, installs or verifies pinned project-local .NET 8 SDK, installs known pinned GitHub non-GUI tools, then writes tool detection reports.",
        "- When `uv` is available, auto mode prefers `uv venv` and `uv pip install` for the workspace Python environment; otherwise it falls back to stdlib `venv` and `pip`.",
        "- `manual` does not install external programs. It writes the local config template and detection reports so the user can fill paths.",
        "- LexTranslator and xTranslator are GUI tools and are not silently downloaded or installed.",
        f"- .NET SDK auto mode reuses an existing project-local SDK only when `dotnet --version` reports {DOTNET_SDK_VERSION} and a pinned manifest is present or safely migratable; otherwise it installs from the vendored dotnet-install.ps1 after verifying SHA256.",
        "- GitHub non-GUI tools currently covered by auto mode: pinned BSAFileExtractor and Champollion source archives with SHA256 verification and local tool manifests.",
        "- When `tools/python-venv/` exists, run plugin Python scripts with that workspace Python so auto-installed packages are visible.",
        "- External executable paths must be configured in `config/tools.local.json` before GUI/tool fallback is available.",
        "",
        "## Steps",
        "",
    ]
    lines.extend([f"- {step}" for step in steps] or ["No steps recorded."])
    lines.extend(["", "## Errors", ""])
    lines.extend([f"- {error}" for error in errors] or ["No blocking errors."])
    lines.extend(
        [
            "",
            "## Manual Tool Paths",
            "",
            "Edit `config/tools.local.json` when you want to enable optional local tools:",
            "",
            "- `LexTranslatorPath`: local LexTranslator executable.",
            "- `XTranslatorPath`: local xTranslator executable.",
            f"- `DecoderTools.DotNetSdkPath`: project-local or trusted .NET SDK executable. Auto mode installs pinned `{DOTNET_SDK_VERSION}` at `tools/dotnet-sdk/dotnet.exe`.",
            "- `DecoderTools.Archive7zPath`: optional 7-Zip executable.",
            "- `DecoderTools.BsaFileExtractorPath`: project BSA extractor wrapper target; auto mode writes `scripts/invoke_bsa_file_extractor_safe.py` when BSAFileExtractor source exists.",
            "",
            "After editing paths, run the tool detection scripts again from the workspace.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare local tool setup for a workspace.")
    parser.add_argument(
        "--mode",
        choices=("auto", "manual"),
        default="manual",
        help="auto installs safe non-GUI tools; manual writes reports only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = workspace_root()
    try:
        validate_workspace_root(root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2
    env = {
        **os.environ,
        WORKSPACE_ROOT_ENV: str(root),
        PLUGIN_ROOT_ENV: str(PROJECT_ROOT),
    }
    steps: list[str] = []
    errors: list[str] = []
    check_python = Path(sys.executable)

    if args.mode == "auto":
        venv_python = install_python_requirements(root, env, steps, errors)
        if venv_python.is_file():
            check_python = venv_python
        install_dotnet_sdk(root, env, steps, errors)
        install_github_archive(root, "BSAFileExtractor", steps, errors)
        install_github_archive(root, "Champollion", steps, errors)
        update_tools_config(root, steps, errors)
        build_dotnet_adapters(root, env, steps, errors)
    else:
        steps.append("Manual mode selected; skipped Python requirements installation.")

    checks = [
        (
            "decoder tool detection",
            [str(check_python), str(PROJECT_ROOT / "scripts" / "detect_decoder_tools.py")],
        ),
        (
            "GUI tool config validation",
            [str(check_python), str(PROJECT_ROOT / "scripts" / "validate_tools_config.py")],
        ),
    ]
    for label, command in checks:
        code, output = run_command(command, cwd=root, env=env)
        steps.append(f"{label} exited with code {code}.")
        if output:
            steps.append(f"{label} output: {output}")
        if code != 0:
            errors.append(f"{label} reported blocking errors.")

    report_path = write_setup_report(root, mode=args.mode, steps=steps, errors=errors)
    print(f"Tool setup report written to: {report_path}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

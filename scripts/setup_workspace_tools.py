"""Prepare or document shared managed tools for one SMT workspace."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from managed_tool_provisioning import DOTNET_SDK_VERSION, provision_workspace_tools
from managed_tool_resolver import leased_payload_path, load_workspace_tool_config
from managed_tool_store import ManagedToolStoreError
from project_paths import is_under
from smt_windows import (
    ManagedProcessEnvironmentError,
    SmtLockTimeoutError,
    validate_regular_single_link_file,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"


def workspace_root() -> Path:
    configured = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path.cwd().resolve(strict=False)


def validate_workspace_root(root: Path) -> None:
    if is_under(root, PROJECT_ROOT):
        raise RuntimeError(
            "Refusing to run tool setup inside the plugin source repository. "
            "Initialize or open a separate workspace first."
        )
    marker = root / WORKSPACE_MARKER
    try:
        validate_regular_single_link_file(
            marker,
            root,
            label="workspace marker",
        )
    except (OSError, ValueError, ManagedProcessEnvironmentError) as exc:
        raise RuntimeError(
            f"Tool setup requires a safe initialized workspace marker: {marker}"
        ) from exc


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
        "- `auto` preserves valid user-managed external overrides, publishes or reuses only the remaining required immutable shared Python, pinned .NET 8 SDK, pinned GitHub decoders, and source-keyed adapters, then atomically binds this workspace.",
        "- When `uv` is available, auto mode uses an exact hash-pinned runtime export and copy link mode; otherwise stdlib venv and pip consume the same hash-pinned export.",
        "- `manual` does not install external programs. It writes detection reports so the user can fill explicit paths.",
        "- LexTranslator, xTranslator, and ESP-ESM Translator are GUI tools and are not silently downloaded or installed.",
        f"- .NET SDK auto mode reuses or downloads the immutable `{DOTNET_SDK_VERSION}` shared archive after exact SHA-256 verification.",
        "- GitHub non-GUI tools covered by auto mode: pinned BSAFileExtractor and Champollion source archives with SHA-256 verification and immutable manifests.",
        "- Post-binding workflow Python children use the leased shared interpreter; setup, doctor, maintenance, and the public controller continue to use bootstrap Python.",
        "- `config/tools.local.json` remains reserved for explicit user-managed external tools and never stores Local AppData managed-cache paths.",
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
            "- `EspEsmTranslatorPath`: optional local EET4 executable; EET RAG decoding does not require it.",
            f"- `DecoderTools.DotNetSdkPath`: optional user-managed .NET SDK executable. Auto mode otherwise binds pinned shared `{DOTNET_SDK_VERSION}`.",
            "- `DecoderTools.Archive7zPath`: optional 7-Zip executable.",
            "- `DecoderTools.BsaFileExtractorPath`: optional controlled wrapper override; auto mode resolves its payload through the managed binding.",
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
        help="auto publishes/reuses shared non-GUI tools; manual writes reports only.",
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
    with ExitStack() as leases:
        check_python = Path(sys.executable)
        if args.mode == "auto":
            try:
                shared = provision_workspace_tools(root, env=env)
                steps.extend(shared.steps)
                steps.append(f"Managed binding published: {shared.binding_path}")
                config = load_workspace_tool_config(root)
                runtime = leases.enter_context(
                    leased_payload_path(
                        root,
                        config,
                        "PythonRuntimePath",
                        timeout_seconds=30.0,
                        command="run post-setup managed-tool checks",
                    )
                )
                if runtime.path is None:
                    raise ManagedToolStoreError(
                        "post-setup Python runtime has no executable"
                    )
                check_python = runtime.path
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                ManagedToolStoreError,
                ManagedProcessEnvironmentError,
                SmtLockTimeoutError,
            ) as exc:
                errors.append(f"Shared managed-tool setup failed: {exc}")
        else:
            steps.append(
                "Manual mode selected; skipped managed publication and binding."
            )

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

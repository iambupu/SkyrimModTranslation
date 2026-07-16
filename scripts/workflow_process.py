"""Run plugin-owned Python scripts in an initialized workspace."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from project_paths import plugin_root, plugin_script_path


def run_plugin_python(
    workspace_root: Path,
    script_name: str,
    args: list[str],
    *,
    trace_child: bool = False,
) -> subprocess.CompletedProcess[str]:
    source_root = plugin_root()
    script = plugin_script_path(script_name)
    if not script.is_file():
        raise FileNotFoundError(f"missing plugin script: scripts/{script_name}")
    environment = {
        **os.environ,
        "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace_root),
        "SKYRIM_CHS_PLUGIN_ROOT": str(source_root),
    }
    if trace_child:
        environment["SKYRIM_CHS_TRACE_CHILD"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(workspace_root),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
